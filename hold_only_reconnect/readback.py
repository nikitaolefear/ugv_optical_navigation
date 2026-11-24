"""Read the existing owner's loopback diagnostics without opening serial."""
import json
import re
from urllib.request import ProxyHandler, build_opener

opener = build_opener(ProxyHandler({}))


def get(port, endpoint):
    try:
        with opener.open(f'http://127.0.0.1:{port}/api/{endpoint}', timeout=2) as reply:
            return json.load(reply)
    except (OSError, ValueError):
        return {}


def fresh(field):
    return field.get('value') if field.get('state') == 'FRESH' else None


telemetry = get(8082, 'telemetry')
diagnostics = get(8084, 'owner-diagnostics')
heartbeat = telemetry.get('messages', {}).get('HEARTBEAT', {'state': 'UNKNOWN'})
connected = telemetry.get('connection') == 'CONNECTED' and heartbeat.get('state') == 'FRESH'
mode = fresh(telemetry.get('flight_mode', {})) if connected else None
rc = diagnostics.get('rc') or {}
rc_age = rc.get('age_s')
channels = rc.get('channels', {}) if connected and isinstance(rc_age, (int, float)) and 0 <= rc_age < 3 else {}
pwm = diagnostics.get('brake_pwm') or {}
servo = pwm if connected and pwm.get('servo_state') == 'FRESH' else {}
parameters = diagnostics.get('parameters', {}) if connected else {}
banner = next((entry for entry in reversed(diagnostics.get('statustext', []))
               if re.search(r'ArduRover\s+V?\d+\.\d+', entry.get('text', ''))), None)
print(json.dumps({
    'connection': telemetry.get('connection', 'UNKNOWN'),
    'HEARTBEAT': heartbeat,
    'armed': (fresh(telemetry.get('armed', {})) if connected else None) or 'UNKNOWN',
    'mode': ('MANUAL' if mode.get('autopilot') == 3 and mode.get('vehicle_type') == 10
             and mode.get('base_mode', 0) & 1 and mode.get('custom_mode') == 0
             else mode.get('custom_mode')) if mode else 'UNKNOWN',
    'firmware': (diagnostics.get('firmware') if connected else None) or 'UNKNOWN',
    'firmware_cached_banner': {'text': banner['text'], 'age_s': banner.get('age_s')}
        if connected and banner else 'UNKNOWN',
    'RC': {k: channels.get(k, 'UNKNOWN') for k in ('1', '3', '5', '8')},
    'servo_pwm_us': {k: servo.get(f'servo{k}_raw_us') or 'UNKNOWN' for k in (5, 6)},
    'parameter_cache': {k: parameters.get(k, 'UNKNOWN') for k in ('MODE_CH', 'INITIAL_MODE', 'FS_ACTION')},
}, ensure_ascii=False))
