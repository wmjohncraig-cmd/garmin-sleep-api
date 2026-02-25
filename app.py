from flask import Flask, jsonify, request
from flask_cors import CORS
import garth, os, time, hashlib, requests as req_lib, json
from datetime import date, timedelta

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

def _calc_bmi_and_bf(weight_kg, height_cm, age, gender_str):
    bmi = None
    body_fat = None
    if weight_kg and height_cm:
        h_m = height_cm / 100
        bmi = round(weight_kg / (h_m * h_m), 1)
    if bmi and age:
        # Deurenberg (1991) BIA formula; gender '2' = male in VeSync
        is_male = 1 if str(gender_str) == '2' else 0
        body_fat = round(1.20 * bmi + 0.23 * int(age) - 10.8 * is_male - 5.4, 1)
    return bmi, body_fat

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
        bmi, body_fat = _calc_bmi_and_bf(weight_kg, r.get('heightCm'), r.get('age'), r.get('gender', '1'))
        ts_val = r.get('timestamp')
        date_str = date.fromtimestamp(ts_val).isoformat() if ts_val else date.today().isoformat()
        return {
            'weight_lbs': round(float(weight_lb), 1) if weight_lb else (round(weight_kg * 2.20462, 1) if weight_kg else None),
            'body_fat_pct': body_fat,
            'bmi': bmi,
            'date': date_str,
            'unit': 'lbs',
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
        bmi, body_fat = _calc_bmi_and_bf(weight_kg, r.get('heightCm'), r.get('age'), r.get('gender', '1'))
        ts_val = r.get('timestamp')
        date_str = date.fromtimestamp(ts_val).isoformat() if ts_val else date.today().isoformat()
        return {
            'weight_lbs': weight_lbs,
            'body_fat_pct': body_fat,
            'bmi': bmi,
            'date': date_str,
            'unit': 'lbs',
        }

    raise Exception(f'No weight records in V1 or V2 response. V1={v1}, V2={v2}')

def get_client():
    global _client
    if _client is None:
        _client = garth.Client()
        _client.login(GARMIN_EMAIL, GARMIN_PASSWORD)
    return _client

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
        bb_data = sleep.get('sleepBodyBattery', [])
        body_battery = max([r.get('value', 0) for r in bb_data if r.get('value')]) if bb_data else None
        hrv_value = sleep.get('avgOvernightHrv', None)
        readiness = None
        try:
            tr = client.connectapi('/metrics-service/metrics/trainingReadiness/' + today)
            readiness = tr[0].get('score') if isinstance(tr, list) and tr else None
        except:
            pass
        return jsonify({'date': today, 'sleep_score': sleep_score, 'sleep_hours': sleep_hours, 'hrv': hrv_value, 'body_battery': body_battery, 'readiness': readiness})
    except Exception as e:
        global _client
        _client = None
        return jsonify({'error': str(e)}), 500

@app.route('/weight')
def weight():
    vesync_data = None
    vesync_err  = None

    # 1. Try VeSync API
    try:
        vesync_data = get_vesync_weight()
    except Exception as e:
        vesync_err = e

    # 2. Check manual log — use if more recent than VeSync result
    try:
        entries = _load_weight_log()
        if entries:
            latest = sorted(entries, key=lambda e: e.get('date', ''))[-1]
            vesync_date = vesync_data.get('date', '') if vesync_data else ''
            if latest.get('date', '') >= vesync_date:
                return jsonify({**latest, 'unit': 'lbs'})
    except Exception:
        pass

    # 3. Return VeSync data if we got it
    if vesync_data:
        return jsonify(vesync_data)

    # 4. Fall back to MANUAL_WEIGHT_LBS env var
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

    return jsonify({'error': str(vesync_err)}), 500


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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
