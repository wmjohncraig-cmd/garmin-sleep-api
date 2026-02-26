from flask import Flask, jsonify, request, redirect, make_response
from flask_cors import CORS
import garth, os, time, hashlib, requests as req_lib, json
from datetime import date, timedelta
from urllib.parse import urlencode

app = Flask(__name__)
CORS(app)

GARMIN_EMAIL = os.environ.get('GARMIN_EMAIL')
GARMIN_PASSWORD = os.environ.get('GARMIN_PASSWORD')
_client = None

VESYNC_EMAIL = os.environ.get('VESYNC_EMAIL')
VESYNC_PASSWORD = os.environ.get('VESYNC_PASSWORD')
VESYNC_BASE = 'https://smartapi.vesync.com'

MANUAL_WEIGHT_LBS = os.environ.get('MANUAL_WEIGHT_LBS')
WEIGHT_LOG = os.path.join(os.path.dirname(__file__), 'weight_log.json')
NUTRITION_LOG = os.path.join(os.path.dirname(__file__), 'nutrition_log.json')

ATHLETE_HEIGHT_INCHES = 77  # John Craig, 6'5"

WITHINGS_CLIENT_ID = os.environ.get('WITHINGS_CLIENT_ID')
WITHINGS_CLIENT_SECRET = os.environ.get('WITHINGS_CLIENT_SECRET')
WITHINGS_REDIRECT_URI = 'https://garmin-sleep-api.onrender.com/withings/callback'
WITHINGS_TOKEN_FILE = os.path.join(os.path.dirname(__file__), 'withings_token.json')

# In-memory token cache (survives across requests within same process)
_withings_token_cache = None

def _load_weight_log():
    try:
        with open(WEIGHT_LOG) as f:
            return json.load(f)
    except Exception:
        return []

def _save_weight_log(entries):
    with open(WEIGHT_LOG, 'w') as f:
        json.dump(entries, f, indent=2)

def _vsync_hdrs(token, account_id):
    return {
        'accept-language': 'en',
        'accountId': account_id,
        'appVersion': '2.8.6',
        'content-type': 'application/json',
        'tk': token,
        'tz': 'America/Chicago',
    }

def _vsync_base_body(token, account_id, method):
    ts = str(int(time.time() * 1000))
    return {
        'timeZone': 'America/Chicago',
        'acceptLanguage': 'en',
        'accountID': account_id,
        'token': token,
        'appVersion': '2.8.6',
        'phoneBrand': 'SM N9005',
        'phoneOS': 'Android',
        'traceId': ts,
        'method': method,
    }

def _calc_body_composition(weight_kg, height_cm, impedance, age, gender_str):
    """Estimate body composition from BIA data (impedance-based).
    Uses adapted Tanita/Omron leg-to-leg formula for fat-free mass.
    Returns dict of all body composition metrics."""
    is_male = 1 if str(gender_str) == '2' else 0
    out = {
        'bmi': None, 'body_fat_pct': None, 'fat_free_weight_lbs': None,
        'muscle_mass_lbs': None, 'bmr_kcal': None,
        'visceral_fat': None, 'metabolic_age': None,
    }
    if not weight_kg or not height_cm:
        return out
    height_m = height_cm / 100
    out['bmi'] = round(weight_kg / (height_m ** 2), 1)
    if age:
        out['bmr_kcal'] = round(
            10 * weight_kg + 6.25 * height_cm - 5 * int(age) + (5 if is_male else -161)
        )
    if not impedance or not age:
        return out
    # Fat-Free Mass — adapted Tanita/Omron BIA (leg-to-leg, H²/Z index)
    bia_idx = (height_cm ** 2) / impedance
    if is_male:
        ffm_kg = 0.6062 * bia_idx + 0.00536 * height_cm - 0.04804 * int(age) + 12.96
    else:
        ffm_kg = 0.4848 * bia_idx + 0.00513 * height_cm - 0.01733 * int(age) + 12.44
    fat_kg = max(0.0, weight_kg - ffm_kg)
    bfp = round(fat_kg / weight_kg * 100, 1)
    out['body_fat_pct']        = bfp
    out['fat_free_weight_lbs'] = round(ffm_kg * 2.20462, 1)
    out['muscle_mass_lbs']     = round(ffm_kg * 0.75 * 2.20462, 1)
    # Visceral fat level 1–30 (empirical estimate)
    out['visceral_fat'] = max(1, min(30, round(bfp * 0.3 + out['bmi'] * 0.3 - 5)))
    # Metabolic age: actual age adjusted for body composition vs. healthy reference
    ideal_bfp = 15 if is_male else 25
    met_age = int(age) + round((bfp - ideal_bfp) * 0.5 + max(0, out['bmi'] - 22) * 0.4)
    out['metabolic_age'] = max(18, min(80, met_age))
    return out

def _vesync_login():
    """Authenticate with VeSync API, return (token, account_id)."""
    if not VESYNC_EMAIL or not VESYNC_PASSWORD:
        raise Exception(
            f"VeSync credentials missing from environment: "
            f"VESYNC_EMAIL={'set' if VESYNC_EMAIL else 'NOT SET'}, "
            f"VESYNC_PASSWORD={'set' if VESYNC_PASSWORD else 'NOT SET'}"
        )
    body = {
        'timeZone': 'America/Chicago',
        'acceptLanguage': 'en',
        'appVersion': '2.8.6',
        'phoneBrand': 'SM N9005',
        'phoneOS': 'Android',
        'traceId': str(int(time.time())),
        'email': VESYNC_EMAIL,
        'password': hashlib.md5(VESYNC_PASSWORD.encode('utf-8')).hexdigest(),
        'devToken': '',
        'userType': '1',
        'method': 'login',
    }
    resp = req_lib.post(
        f'{VESYNC_BASE}/cloud/v1/user/login',
        json=body,
        headers={'Content-Type': 'application/json; charset=UTF-8',
                 'User-Agent': 'okhttp/3.12.1'},
        timeout=10
    ).json()
    if resp.get('code') != 0:
        raise Exception(f"VeSync login failed: {resp.get('msg', resp)}")
    token = resp['result']['token']
    account_id = resp['result']['accountID']
    return token, account_id

def get_vesync_weight():
    token, account_id = _vesync_login()
    hdrs = _vsync_hdrs(token, account_id)

    # Get device list to find the scale's configModule + cid
    dev_body = _vsync_base_body(token, account_id, 'devices')
    dev_body.update({'pageNo': '1', 'pageSize': '100'})
    dev_resp = req_lib.post(
        f'{VESYNC_BASE}/cloud/v2/deviceManaged/devices',
        headers=hdrs, json=dev_body, timeout=10
    ).json()

    devices = dev_resp.get('result', {}).get('list', [])
    scale = next(
        (d for d in devices if any(
            x in d.get('deviceType', '') for x in ['EFS', 'ESF', 'Scale', 'scale']
        )), None
    )
    if not scale:
        types = [d.get('deviceType') for d in devices]
        raise Exception(f'No scale found in device list: {types}')

    config_module = scale.get('configModule', '')
    cid = scale.get('cid') or scale.get('uuid') or scale.get('deviceId') or ''
    now_ts = int(time.time())
    ts = str(int(time.time() * 1000))

    # Try V1 endpoint (WiFi+BT scales, returns weigh_lb directly)
    v1_body = _vsync_base_body(token, account_id, 'getWeighData')
    v1_body.update({
        'startTime': 0, 'endTime': now_ts,
        'configModule': config_module, 'cid': cid,
        'pageSize': 1, 'order': 'desc', 'index': 0, 'flag': 1,
    })
    v1 = req_lib.post(
        f'{VESYNC_BASE}/cloud/v1/deviceManaged/fatScale/getWeighData',
        headers=hdrs, json=v1_body, timeout=10
    ).json()

    records = v1.get('result', [])
    if isinstance(records, list) and records:
        r = records[0]
        weight_lb = r.get('weigh_lb')
        weight_kg = r.get('weigh_kg') or (weight_lb / 2.20462 if weight_lb else None)
        comp = _calc_body_composition(weight_kg, r.get('heightCm'), r.get('impedance'), r.get('age'), r.get('gender', '1'))
        ts_val = r.get('timestamp')
        date_str = date.fromtimestamp(ts_val).isoformat() if ts_val else date.today().isoformat()
        return {
            'weight_lbs': round(float(weight_lb), 1) if weight_lb else (round(weight_kg * 2.20462, 1) if weight_kg else None),
            'date': date_str, 'unit': 'lbs',
            **comp,
        }

    # Fallback: V2 endpoint (BT-only scales, returns weightG in grams)
    # Collect all pages, pick the record with the highest timestamp
    all_records = []
    for page in range(1, 21):  # cap at 20 pages (~2000 records)
        v2_body = _vsync_base_body(token, account_id, 'getWeighingDataV2')
        v2_body.update({'configModule': config_module, 'pageSize': 100, 'page': page, 'allData': True})
        v2 = req_lib.post(
            f'{VESYNC_BASE}/cloud/v2/deviceManaged/getWeighingDataV2',
            headers=hdrs, json=v2_body, timeout=10
        ).json()
        page_records = v2.get('result', {}).get('weightDatas', []) if v2.get('code') == 0 else []
        all_records.extend(page_records)
        if not page_records or len(page_records) < 100:
            break  # final page

    if all_records:
        r = max(all_records, key=lambda x: x.get('timestamp', 0))
        weight_g = r.get('weightG')
        weight_kg = weight_g / 1000 if weight_g else None
        weight_lbs = round(weight_g / 453.592, 1) if weight_g else None
        comp = _calc_body_composition(weight_kg, r.get('heightCm'), r.get('impedance'), r.get('age'), r.get('gender', '1'))
        ts_val = r.get('timestamp')
        date_str = date.fromtimestamp(ts_val).isoformat() if ts_val else date.today().isoformat()
        return {
            'weight_lbs': weight_lbs,
            'date': date_str, 'unit': 'lbs',
            **comp,
        }

    raise Exception(f'No weight records in V1 or V2 response. V1={v1}, V2={v2}')

def get_client():
    global _client
    if _client is None:
        _client = garth.Client()
        _client.login(GARMIN_EMAIL, GARMIN_PASSWORD)
    return _client


# ── WITHINGS ──────────────────────────────────────────────────

def _load_withings_token():
    global _withings_token_cache
    # 1. In-memory cache (fastest, survives across requests)
    if _withings_token_cache:
        return _withings_token_cache
    # 2. File on disk (survives process restart if disk persists)
    try:
        with open(WITHINGS_TOKEN_FILE) as f:
            _withings_token_cache = json.load(f)
            return _withings_token_cache
    except Exception:
        pass
    # 3. Env var WITHINGS_TOKEN (JSON string — survives deploys)
    env_token = os.environ.get('WITHINGS_TOKEN')
    if env_token:
        try:
            _withings_token_cache = json.loads(env_token)
            return _withings_token_cache
        except Exception:
            pass
    return None

def _save_withings_token(data):
    global _withings_token_cache
    _withings_token_cache = data
    try:
        with open(WITHINGS_TOKEN_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def _refresh_withings_token(token_data):
    """Refresh an expired Withings access token."""
    resp = req_lib.post('https://wbsapi.withings.net/v2/oauth2', data={
        'action': 'requesttoken',
        'grant_type': 'refresh_token',
        'client_id': WITHINGS_CLIENT_ID,
        'client_secret': WITHINGS_CLIENT_SECRET,
        'refresh_token': token_data['refresh_token'],
    }, timeout=10).json()
    body = resp.get('body', {})
    if resp.get('status') != 0 or not body.get('access_token'):
        raise Exception(f"Withings token refresh failed: {resp}")
    new_data = {
        'access_token': body['access_token'],
        'refresh_token': body['refresh_token'],
        'expires_at': int(time.time()) + body.get('expires_in', 10800),
    }
    _save_withings_token(new_data)
    return new_data

def _get_withings_access_token():
    """Return a valid access token, refreshing if expired."""
    token_data = _load_withings_token()
    if not token_data:
        raise Exception('Withings not authorized. Visit /withings/auth to connect.')
    if time.time() >= token_data.get('expires_at', 0) - 60:
        token_data = _refresh_withings_token(token_data)
    return token_data['access_token']

# Withings measure type IDs → field names
_WITHINGS_TYPES = {
    1:  'weight_kg',
    6:  'body_fat_pct',
    8:  'fat_mass_kg',
    5:  'fat_free_mass_kg',
    76: 'muscle_mass_kg',
    88: 'bone_mass_kg',
    77: 'body_water_pct',
    73: 'visceral_fat',
}

def get_withings_weight():
    """Fetch latest Withings measurement with full body composition."""
    access_token = _get_withings_access_token()
    # Don't filter by meastype — get all available measures
    resp = req_lib.post('https://wbsapi.withings.net/measure', data={
        'action': 'getmeas',
        'category': 1,  # real measurements only
    }, headers={
        'Authorization': f'Bearer {access_token}',
    }, timeout=10).json()

    if resp.get('status') != 0:
        raise Exception(f"Withings API error: {resp}")

    groups = resp.get('body', {}).get('measuregrps', [])
    if not groups:
        raise Exception('No Withings measurements found')

    # Collect metrics across recent groups (different metrics may be in different groups)
    metrics = {}
    latest_ts = 0
    for grp in groups[:10]:  # check up to 10 most recent groups
        for m in grp.get('measures', []):
            mtype = m.get('type')
            if mtype in _WITHINGS_TYPES and _WITHINGS_TYPES[mtype] not in metrics:
                val = m['value'] * (10 ** m['unit'])
                metrics[_WITHINGS_TYPES[mtype]] = val
        if grp.get('date', 0) > latest_ts:
            latest_ts = grp['date']

    date_str = date.fromtimestamp(latest_ts).isoformat() if latest_ts else date.today().isoformat()

    weight_kg = metrics.get('weight_kg')
    weight_lbs = round(weight_kg * 2.20462, 1) if weight_kg else None

    result = {
        'weight_lbs': weight_lbs,
        'date': date_str,
        'unit': 'lbs',
        'source': 'withings',
        'body_fat_pct': round(metrics['body_fat_pct'], 1) if 'body_fat_pct' in metrics else None,
        'muscle_mass_lbs': round(metrics['muscle_mass_kg'] * 2.20462, 1) if 'muscle_mass_kg' in metrics else None,
        'bone_mass_lbs': round(metrics['bone_mass_kg'] * 2.20462, 1) if 'bone_mass_kg' in metrics else None,
        'fat_free_weight_lbs': round(metrics['fat_free_mass_kg'] * 2.20462, 1) if 'fat_free_mass_kg' in metrics else None,
        'body_water_pct': round(metrics['body_water_pct'], 1) if 'body_water_pct' in metrics else None,
        'visceral_fat': round(metrics['visceral_fat']) if 'visceral_fat' in metrics else None,
        'bmi': round((weight_lbs / (77 ** 2)) * 703, 1) if weight_lbs else None,
    }
    return result


@app.route('/withings/debug')
def withings_debug():
    """Show raw Withings API response for debugging."""
    try:
        access_token = _get_withings_access_token()
        resp = req_lib.post('https://wbsapi.withings.net/measure', data={
            'action': 'getmeas',
            'category': 1,
        }, headers={
            'Authorization': f'Bearer {access_token}',
        }, timeout=10).json()
        # Trim to first 5 groups for readability
        groups = resp.get('body', {}).get('measuregrps', [])[:5]
        return jsonify({'status': resp.get('status'), 'num_groups': len(resp.get('body', {}).get('measuregrps', [])), 'first_5_groups': groups})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/withings/auth')
def withings_auth():
    if not WITHINGS_CLIENT_ID:
        return jsonify({'error': 'WITHINGS_CLIENT_ID not configured'}), 500
    params = urlencode({
        'response_type': 'code',
        'client_id': WITHINGS_CLIENT_ID,
        'redirect_uri': WITHINGS_REDIRECT_URI,
        'scope': 'user.metrics',
        'state': 'imtx',
    })
    return redirect(f'https://account.withings.com/oauth2_user/authorize2?{params}')


@app.route('/withings/callback')
def withings_callback():
    code = request.args.get('code')
    if not code:
        return jsonify({'error': 'No authorization code received',
                        'args': dict(request.args)}), 400
    resp = req_lib.post('https://wbsapi.withings.net/v2/oauth2', data={
        'action': 'requesttoken',
        'grant_type': 'authorization_code',
        'client_id': WITHINGS_CLIENT_ID,
        'client_secret': WITHINGS_CLIENT_SECRET,
        'code': code,
        'redirect_uri': WITHINGS_REDIRECT_URI,
    }, timeout=10).json()

    body = resp.get('body', {})
    if resp.get('status') != 0 or not body.get('access_token'):
        return jsonify({'error': 'Token exchange failed', 'response': resp}), 500

    token_data = {
        'access_token': body['access_token'],
        'refresh_token': body['refresh_token'],
        'expires_at': int(time.time()) + body.get('expires_in', 10800),
        'userid': body.get('userid'),
    }
    _save_withings_token(token_data)
    token_json = json.dumps(token_data)
    html = f'''<!DOCTYPE html><html><head><title>Withings Connected</title></head>
    <body style="background:#0a0a0f;color:#e8e8f0;font-family:monospace;padding:40px;max-width:800px;margin:0 auto">
    <h2 style="color:#2ecc71">Withings Connected Successfully</h2>
    <p>Copy the token below and paste it as the <code>WITHINGS_TOKEN</code> env var in your Render dashboard.</p>
    <textarea id="tok" style="width:100%;height:120px;background:#111;color:#4A7FD4;border:1px solid #333;padding:12px;font-size:12px" readonly>{token_json}</textarea>
    <br><br>
    <button onclick="navigator.clipboard.writeText(document.getElementById('tok').value).then(()=>this.textContent='COPIED!')" style="background:#E85D26;color:white;border:none;padding:12px 32px;font-size:14px;cursor:pointer;font-family:monospace">COPY TOKEN</button>
    <p style="color:#6b6b8a;margin-top:20px;font-size:11px">After pasting in Render, the token will persist across deploys. No need to re-authorize.</p>
    </body></html>'''
    resp = make_response(html)
    resp.headers['Content-Type'] = 'text/html'
    return resp


@app.route('/withings/weight')
def withings_weight():
    try:
        return jsonify(get_withings_weight())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _load_nutrition_log():
    try:
        with open(NUTRITION_LOG) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_nutrition_log(data):
    with open(NUTRITION_LOG, 'w') as f:
        json.dump(data, f, indent=2)

def _nutrition_totals(entries):
    return {
        'calories': sum(e.get('calories', 0) for e in entries),
        'protein': sum(e.get('protein', 0) for e in entries),
        'carbs': sum(e.get('carbs', 0) for e in entries),
        'fat': sum(e.get('fat', 0) for e in entries),
    }


@app.route('/nutrition/log', methods=['POST'])
def nutrition_log():
    body = request.get_json(force=True) or {}
    date_str = body.get('date')
    entries = body.get('entries', [])
    if not date_str or not entries:
        return jsonify({'error': 'date and entries[] required'}), 400
    log = _load_nutrition_log()
    if date_str not in log:
        log[date_str] = []
    log[date_str].extend(entries)
    _save_nutrition_log(log)
    return jsonify({
        'date': date_str,
        'entries': log[date_str],
        'totals': _nutrition_totals(log[date_str]),
    })


@app.route('/nutrition/today')
def nutrition_today():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ct = datetime.now(ZoneInfo('America/Chicago'))
    today_key = ct.strftime('%Y-%m-%d')
    log = _load_nutrition_log()
    entries = log.get(today_key, [])
    return jsonify({
        'date': today_key,
        'entries': entries,
        'totals': _nutrition_totals(entries),
    })


@app.route('/withings/weight-history')
def withings_weight_history():
    try:
        access_token = _get_withings_access_token()
        start_ts = int(time.time()) - 14 * 86400
        resp = req_lib.post('https://wbsapi.withings.net/measure', data={
            'action': 'getmeas',
            'category': 1,
            'meastype': 1,  # weight only
            'startdate': start_ts,
        }, headers={
            'Authorization': f'Bearer {access_token}',
        }, timeout=10).json()
        if resp.get('status') != 0:
            raise Exception(f"Withings API error: {resp}")
        groups = resp.get('body', {}).get('measuregrps', [])
        by_date = {}
        for grp in groups:
            for m in grp.get('measures', []):
                if m.get('type') == 1:
                    weight_kg = m['value'] * (10 ** m['unit'])
                    d = date.fromtimestamp(grp['date']).isoformat()
                    by_date[d] = round(weight_kg * 2.20462, 1)
        result = [{'date': d, 'weight_lbs': w} for d, w in sorted(by_date.items())]
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

@app.route('/garmin-sleep')
def garmin_sleep():
    try:
        client = get_client()
        today = date.today().isoformat()
        sleep = client.connectapi('/wellness-service/wellness/dailySleepData/' + GARMIN_EMAIL + '?date=' + today + '&nonSleepBufferMinutes=60')
        sleep_data = sleep.get('dailySleepDTO', {})
        sleep_score = sleep_data.get('sleepScores', {}).get('overall', {}).get('value', None)
        sleep_seconds = sleep_data.get('sleepTimeSeconds', 0)
        sleep_hours = round(sleep_seconds / 3600, 1) if sleep_seconds else None
        deep_secs  = sleep_data.get('deepSleepSeconds',  0) or 0
        light_secs = sleep_data.get('lightSleepSeconds', 0) or 0
        rem_secs   = sleep_data.get('remSleepSeconds',   0) or 0
        deep_sleep_hours  = round(deep_secs  / 3600, 1) if deep_secs  else None
        light_sleep_hours = round(light_secs / 3600, 1) if light_secs else None
        rem_sleep_hours   = round(rem_secs   / 3600, 1) if rem_secs   else None
        deep_plus_rem     = round((deep_secs + rem_secs) / 3600, 1) if (deep_secs or rem_secs) else None
        bb_data = sleep.get('sleepBodyBattery', [])
        body_battery = max([r.get('value', 0) for r in bb_data if r.get('value')]) if bb_data else None
        hrv_value = sleep.get('avgOvernightHrv', None)
        readiness = None
        try:
            tr = client.connectapi('/metrics-service/metrics/trainingReadiness/' + today)
            readiness = tr[0].get('score') if isinstance(tr, list) and tr else None
        except:
            pass
        return jsonify({
            'date': today,
            'sleep_score': sleep_score,
            'sleep_hours': sleep_hours,
            'deep_sleep_hours': deep_sleep_hours,
            'light_sleep_hours': light_sleep_hours,
            'rem_sleep_hours': rem_sleep_hours,
            'deep_plus_rem_hours': deep_plus_rem,
            'hrv': hrv_value,
            'body_battery': body_battery,
            'readiness': readiness,
        })
    except Exception as e:
        global _client
        _client = None
        return jsonify({'error': str(e)}), 500

@app.route('/weight')
def weight():
    best_data = None
    best_date = ''
    last_err  = None

    # 1. Try Withings (highest priority — real body comp data)
    try:
        withings = get_withings_weight()
        if withings.get('weight_lbs') and withings.get('date', '') >= best_date:
            best_data = withings
            best_date = withings['date']
    except Exception as e:
        last_err = e

    # 2. Try VeSync API
    try:
        vesync = get_vesync_weight()
        if vesync.get('weight_lbs') and vesync.get('date', '') >= best_date:
            best_data = vesync
            best_date = vesync['date']
    except Exception as e:
        if not last_err:
            last_err = e

    # 3. Check manual log — use if more recent
    try:
        entries = _load_weight_log()
        if entries:
            latest = sorted(entries, key=lambda e: e.get('date', ''))[-1]
            if latest.get('date', '') >= best_date:
                best_data = {**latest, 'unit': 'lbs'}
                best_date = latest['date']
    except Exception:
        pass

    # 4. Return best result
    if best_data:
        return jsonify(best_data)

    # 5. Fall back to MANUAL_WEIGHT_LBS env var
    if MANUAL_WEIGHT_LBS:
        try:
            return jsonify({
                'weight_lbs': round(float(MANUAL_WEIGHT_LBS), 1),
                'date': date.today().isoformat(),
                'unit': 'lbs',
                'source': 'manual_env',
            })
        except Exception:
            pass

    return jsonify({'error': str(last_err)}), 500


@app.route('/weight/manual', methods=['POST'])
def weight_manual():
    body = request.get_json(force=True) or {}
    weight_lbs = body.get('weight_lbs')
    date_str   = body.get('date', date.today().isoformat())
    if weight_lbs is None:
        return jsonify({'error': 'weight_lbs required'}), 400
    entry = {
        'date': date_str,
        'weight_lbs': round(float(weight_lbs), 1),
        'source': 'manual',
    }
    entries = _load_weight_log()
    # Replace existing entry for same date, or append
    entries = [e for e in entries if e.get('date') != date_str]
    entries.append(entry)
    entries.sort(key=lambda e: e.get('date', ''))
    _save_weight_log(entries)
    return jsonify(entry)

@app.route('/weight/debug')
def weight_debug():
    info = {
        'env': {
            'VESYNC_EMAIL': VESYNC_EMAIL if VESYNC_EMAIL else 'NOT SET',
            'VESYNC_PASSWORD': 'set' if VESYNC_PASSWORD else 'NOT SET',
        },
        'login': None,
        'devices': None,
        'v1_raw': None,
        'v2_raw': None,
        'error': None,
    }
    try:
        if not VESYNC_EMAIL or not VESYNC_PASSWORD:
            info['error'] = 'Missing credentials (see env)'
            return jsonify(info), 500

        # Attempt login
        token, account_id = _vesync_login()
        info['login'] = {'status': 'ok', 'account_id': account_id}
        hdrs = _vsync_hdrs(token, account_id)

        # List devices
        dev_body = _vsync_base_body(token, account_id, 'devices')
        dev_body.update({'pageNo': '1', 'pageSize': '100'})
        dev_resp = req_lib.post(
            f'{VESYNC_BASE}/cloud/v2/deviceManaged/devices',
            headers=hdrs, json=dev_body, timeout=10
        ).json()
        devices = dev_resp.get('result', {}).get('list', [])
        info['devices'] = [
            {'deviceName': d.get('deviceName'), 'deviceType': d.get('deviceType'), 'cid': d.get('cid')}
            for d in devices
        ]

        # Find scale
        scale = next(
            (d for d in devices if any(
                x in d.get('deviceType', '') for x in ['EFS', 'ESF', 'Scale', 'scale']
            )), None
        )
        info['scale_found'] = scale.get('deviceType') if scale else None

        if scale:
            config_module = scale.get('configModule', '')
            cid = scale.get('cid') or scale.get('uuid') or scale.get('deviceId') or ''
            info['scale_cid'] = cid
            info['scale_config_module'] = config_module
            now_ts = int(time.time())

            v1_body = _vsync_base_body(token, account_id, 'getWeighData')
            v1_body.update({
                'startTime': 0, 'endTime': now_ts,
                'configModule': config_module, 'cid': cid,
                'pageSize': 1, 'order': 'desc', 'index': 0, 'flag': 1,
            })
            info['v1_raw'] = req_lib.post(
                f'{VESYNC_BASE}/cloud/v1/deviceManaged/fatScale/getWeighData',
                headers=hdrs, json=v1_body, timeout=10
            ).json()

            last_page_resp = None
            for page in range(1, 21):
                v2_body = _vsync_base_body(token, account_id, 'getWeighingDataV2')
                v2_body.update({'configModule': config_module, 'pageSize': 100, 'page': page, 'allData': True})
                resp = req_lib.post(
                    f'{VESYNC_BASE}/cloud/v2/deviceManaged/getWeighingDataV2',
                    headers=hdrs, json=v2_body, timeout=10
                ).json()
                page_records = resp.get('result', {}).get('weightDatas', []) if resp.get('code') == 0 else []
                last_page_resp = resp
                if not page_records or len(page_records) < 100:
                    info['v2_pages_fetched'] = page
                    break
            info['v2_raw'] = last_page_resp

    except Exception as e:
        info['error'] = str(e)
        return jsonify(info), 500

    return jsonify(info)


ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')

AUDIT_SYSTEM_PROMPT = """You are a world-class Ironman triathlon coaching auditor.
Your job is to review the following Coach Brief and identify any gaps, risks, or suboptimal programming decisions.
Be direct and critical. Flag anything that could prevent a sub-10 Ironman finish. Check:

1. CTL ramp rate — should be +1 to +3 TSS/week in build phase
2. Run volume — minimum 25 miles/week by race minus 8 weeks
3. Long run presence — must appear weekly
4. Swim pace progression toward 1:48/100yd target
5. Nutrition flags — any bonking or underfueling patterns
6. Sleep — Deep+REM under 3hrs should modify next day load
7. Bike intensity — NP should progress toward 238W race target
8. Weekly TSS distribution — run:bike:swim ratio 35:45:20
9. Recovery adequacy — TSB should not go below -30
10. Race day readiness trajectory
11. VO2 Max work — athlete should have at least 1 session per week where HR exceeds 163 bpm. Flag if no activity in last 7 days shows max HR above 163.
12. Threshold work — at least 2 sessions per week should show sustained HR 150-162 for 20+ minutes. Flag if missing.
13. HR ceiling — if max HR across all activities this week is under 155, flag as HIGH severity: "No high-intensity work detected. VO2 and threshold underdeveloped. Sub-10 requires all three energy systems."

Return ONLY a JSON object (no markdown, no code fences):
{
  "overall_risk": "LOW/MEDIUM/HIGH",
  "sub10_trajectory": "ON TRACK/NEEDS WORK/AT RISK",
  "flags": [
    {
      "category": "CATEGORY NAME",
      "severity": "HIGH/MEDIUM/LOW",
      "issue": "description",
      "recommendation": "specific fix"
    }
  ],
  "green_lights": ["things going well"],
  "tomorrow_modification": "any changes to prescribed workout or NONE"
}"""

@app.route('/coaching-audit', methods=['POST'])
def coaching_audit():
    if not ANTHROPIC_API_KEY:
        return jsonify({'error': 'ANTHROPIC_API_KEY not configured'}), 500
    body = request.get_json(force=True) or {}
    brief = body.get('brief', '')
    if not brief:
        return jsonify({'error': 'brief text required'}), 400
    try:
        resp = req_lib.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
            json={
                'model': 'claude-sonnet-4-20250514',
                'max_tokens': 2048,
                'system': AUDIT_SYSTEM_PROMPT,
                'messages': [{'role': 'user', 'content': brief}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data['content'][0]['text']
        audit = json.loads(text)
        return jsonify(audit)
    except json.JSONDecodeError:
        return jsonify({'error': 'Failed to parse audit response', 'raw': text}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
