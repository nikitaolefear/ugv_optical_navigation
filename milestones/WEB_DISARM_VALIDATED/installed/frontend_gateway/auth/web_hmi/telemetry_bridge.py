"""Read-only HTTP adapter; never opens a transport to Pixhawk."""
import copy
import json
import math
import time
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler
try:
    from .pixhawk_telemetry import PixhawkTelemetry
except ImportError:
    from pixhawk_telemetry import PixhawkTelemetry

URL = 'http://127.0.0.1:8082/api/telemetry'
TIMEOUT = 0.7

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

def fresh(age):
    return number(age) and 0 <= age < 3

def select_voltage(data):
    """Keep upstream battery selection; otherwise select matching ESC slot 3."""
    data = copy.deepcopy(data)
    field = data.get('voltage_v', {})
    connected = data.get('connection') == 'CONNECTED'
    diagnostics = data.get('voltage_diagnostics', {})
    source = diagnostics.get('selected_source')
    record = diagnostics.get(source, {}) if isinstance(source, str) else {}
    age = record.get('age_s')
    if (connected and source in ('BATTERY_STATUS', 'SYS_STATUS') and field.get('state') == 'FRESH'
            and number(field.get('value')) and field['value'] > 0 and fresh(age)):
        data['voltage_detail'] = dict(source=source, age_s=age)
        return data
    stale = None
    for group in data.get('esc_diagnostics', {}).get('groups', []):
        if group.get('matches_current_autopilot') is not True:
            continue
        for esc in group.get('controllers', []):
            value = esc.get('voltage_v')
            if esc.get('esc_number') != 3 or not number(value) or value <= 0:
                continue
            age = group.get('age_s')
            candidate = dict(source='Вхід ESC №3', age_s=age)
            if connected and group.get('state') == 'FRESH' and fresh(age):
                data['voltage_v'] = dict(state='FRESH', value=value)
                data['voltage_detail'] = candidate
                return data
            if number(age) and (age >= 3 or not connected):
                stale = candidate
    data['voltage_v'] = dict(state='STALE' if stale else 'UNKNOWN', value=None)
    data['voltage_detail'] = stale or dict(source=None, age_s=None)
    return data

class TelemetryBridge:
    def __init__(self, fetch=None):
        self.fetch = fetch or self._fetch

    @staticmethod
    def _fetch():
        # Ignore proxy environment and prohibit redirects from the fixed loopback URL.
        with build_opener(ProxyHandler({}), NoRedirect()).open(URL, timeout=TIMEOUT) as response:
            raw = response.read(262145)
        if len(raw) > 262144:
            raise ValueError('Oversized telemetry')
        return json.loads(raw)

    def snapshot(self):
        started = time.monotonic()
        try:
            data = self.fetch()
            # Validate the fields consumed by the existing UI before publishing them.
            for key in ('armed', 'flight_mode', 'voltage_v', 'yaw_deg', 'ekf', 'messages'):
                if not isinstance(data[key], dict):
                    raise ValueError('Invalid telemetry schema')
            if not isinstance(data['connection'], str):
                raise ValueError('Invalid connection')
            elapsed = time.monotonic() - started
            data = copy.deepcopy(data)
            for section in (data['messages'].values(), data.get('voltage_diagnostics', {}).values(),
                            data.get('esc_diagnostics', {}).get('groups', [])):
                for record in section:
                    if isinstance(record, dict) and number(record.get('age_s')):
                        record['age_s'] += elapsed
            data = select_voltage(data)
            data['telemetry_bridge'] = dict(state='OK')
            return data
        except Exception:
            # No previous physical value survives a failed upstream read.
            data = PixhawkTelemetry().snapshot()
            data['connection'] = 'DISCONNECTED'
            data['voltage_v'] = dict(state='UNKNOWN', value=None)
            data['voltage_detail'] = dict(source=None, age_s=None)
            data['telemetry_bridge'] = dict(state='UNAVAILABLE')
            return data
