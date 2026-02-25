from flask import Flask, jsonify
from flask_cors import CORS
import garth, os, time, hashlib, requests as req_lib
from datetime import date, timedelta

app = Flask(__name__)
CORS(app)

GARMIN_EMAIL = os.environ.get('GARMIN_EMAIL')
GARMIN_PASSWORD = os.environ.get('GARMIN_PASSWORD')
_client = None

VESYNC_EMAIL = os.environ.get('VESYNC_EMAIL')
VESYNC_PASSWORD = os.environ.get('VESYNC_PASSWORD')
VESYNC_BASE = 'https://smartapi.vesync.com'

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
            x in d.get('deviceType', '') for x in ['ESF', 'Scale', 'scale']
        )), None
    )
    if not scale:
        types = [d.get('deviceType') for d in devices]
        raise Exception(f'No scale found in device list: {types}')

    config_module = scale.get('configModule', '')
    cid = scale.get('cid', '')
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
    v2_body = _vsync_base_body(token, account_id, 'getWeighingDataV2')
    v2_body.update({'configModule': config_module, 'pageSize': 1, 'page': 1, 'allData': True})
    v2 = req_lib.post(
        f'{VESYNC_BASE}/cloud/v2/deviceManaged/getWeighingDataV2',
        headers=hdrs, json=v2_body, timeout=10
    ).json()

    records = v2.get('result', {}).get('weightDatas', [])
    if records:
        r = records[0]
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
    try:
        data = get_vesync_weight()
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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
                x in d.get('deviceType', '') for x in ['ESF', 'Scale', 'scale']
            )), None
        )
        info['scale_found'] = scale.get('deviceType') if scale else None

        if scale:
            config_module = scale.get('configModule', '')
            cid = scale.get('cid', '')
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

            v2_body = _vsync_base_body(token, account_id, 'getWeighingDataV2')
            v2_body.update({'configModule': config_module, 'pageSize': 1, 'page': 1, 'allData': True})
            info['v2_raw'] = req_lib.post(
                f'{VESYNC_BASE}/cloud/v2/deviceManaged/getWeighingDataV2',
                headers=hdrs, json=v2_body, timeout=10
            ).json()

    except Exception as e:
        info['error'] = str(e)
        return jsonify(info), 500

    return jsonify(info)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
