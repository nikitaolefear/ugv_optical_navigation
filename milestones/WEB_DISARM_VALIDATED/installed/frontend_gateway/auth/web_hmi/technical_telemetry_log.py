"""Bounded background diagnostics from the existing HTTP bridge; no serial or audit writes."""
import json
import logging
from logging.handlers import RotatingFileHandler
import threading
import time
from datetime import datetime, timezone

from .telemetry_bridge import TelemetryBridge
from .auth import private_file

MAX_BYTES = 1024 * 1024
BACKUPS = 3
SAMPLE_SECONDS = 30


def records(snapshot):
    upstream = snapshot.get('telemetry_bridge', {}).get('state')
    source = snapshot.get('voltage_diagnostics', {}).get('autopilot_source')
    unavailable = upstream == 'UNAVAILABLE'
    yield dict(indicator='ALT', source=None, unit=None, state='UNKNOWN', value=None,
               reason='UPSTREAM_UNAVAILABLE' if unavailable else 'NOT_RECEIVED',
               detail='ALT_NOT_COLLECTED_BY_CURRENT_API', transport='8082/api/telemetry')
    battery = snapshot.get('voltage_diagnostics', {}).get('BATTERY_STATUS', {})
    state = snapshot.get('messages', {}).get('BATTERY_STATUS', {}).get('state', 'UNKNOWN')
    if unavailable:
        state = 'UNKNOWN'
    reason = battery.get('reason', 'NOT_RECEIVED')
    yield dict(indicator='BATTERY_STATUS', source='Pixhawk/BATTERY_STATUS', autopilot_source=source,
               state=state, value={k: battery.get(k) for k in ('id', 'voltages_mv', 'voltages_ext_mv')}
               if battery.get('received') else None,
               reason='UPSTREAM_UNAVAILABLE' if unavailable else reason,
               received_unix_s=battery.get('received_unix_s'), age_s=battery.get('age_s'))
    estimator = snapshot.get('ekf', {}).get('ESTIMATOR_STATUS', {})
    state = 'UNKNOWN' if unavailable else estimator.get('state', 'UNKNOWN')
    yield dict(indicator='ESTIMATOR_STATUS', source='Pixhawk/ESTIMATOR_STATUS', autopilot_source=source,
               state=state, value=estimator.get('value'),
               reason='UPSTREAM_UNAVAILABLE' if unavailable else
               ('NOT_RECEIVED' if state == 'UNKNOWN' else 'STALE' if state != 'FRESH' else None),
               age_s=snapshot.get('messages', {}).get('ESTIMATOR_STATUS', {}).get('age_s'))

    report = snapshot.get('ekf', {}).get('EKF_STATUS_REPORT', {})
    state = 'UNKNOWN' if unavailable else report.get('state', 'UNKNOWN')
    yield dict(indicator='EKF_STATUS_REPORT', source='Pixhawk/EKF_STATUS_REPORT',
               autopilot_source=source, state=state, value=report.get('value'),
               reason='UPSTREAM_UNAVAILABLE' if unavailable else
               ('NOT_RECEIVED' if state == 'UNKNOWN' else 'STALE' if state != 'FRESH' else None),
               age_s=snapshot.get('messages', {}).get('EKF_STATUS_REPORT', {}).get('age_s'))


class PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        private_file(self.baseFilename)
        return super()._open()


class TechnicalLog:
    def __init__(self, path, bridge=None, clock=time.monotonic, max_bytes=MAX_BYTES):
        private_file(path)
        self.handler = PrivateRotatingHandler(path, maxBytes=max_bytes, backupCount=BACKUPS,
                                           encoding='utf-8')
        self.handler.setFormatter(logging.Formatter('%(message)s'))
        self.logger = logging.Logger('ugv-technical', level=logging.INFO)
        self.logger.propagate = False
        self.logger.addHandler(self.handler)
        self.bridge = bridge or TelemetryBridge()
        self.clock = clock
        self.previous = {}
        self.last_poll = None
        self.stop = threading.Event()
        self.thread = None

    def tick(self):
        now = self.clock()
        # Hard cap even if invoked repeatedly. One batch at most per second.
        if self.last_poll is not None and now - self.last_poll < 1:
            return
        self.last_poll = now
        timestamp = datetime.now(timezone.utc).isoformat()
        for item in records(self.bridge.snapshot()):
            key = item['indicator']
            signature = (item['state'], item['reason'], json.dumps(item.get('autopilot_source')))
            old = self.previous.get(key)
            if old and old[0] == signature and (item['state'] != 'FRESH' or now - old[1] < SAMPLE_SECONDS):
                continue
            self.logger.info(json.dumps(dict(time=timestamp, **item), ensure_ascii=False, allow_nan=False))
            self.previous[key] = (signature, now)

    def run(self):
        last_error = None
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                # Rate-limited failure message, never payloads, credentials or operator audit.
                now = self.clock()
                if last_error is None or now - last_error >= 60:
                    logging.getLogger(__name__).error('Technical telemetry log unavailable')
                    last_error = now
            self.stop.wait(1)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=3)
        if not self.thread or not self.thread.is_alive():
            self.handler.close()


def start_technical_log(app):
    if 'technical_telemetry_log' in app.extensions:
        return
    log = TechnicalLog(app.extensions['auth_store'].directory / 'telemetry-technical.jsonl')
    app.extensions['technical_telemetry_log'] = log
    log.thread = threading.Thread(target=log.run, name='technical-telemetry-log', daemon=True)
    log.thread.start()


def stop_technical_log(app):
    log = app.extensions.pop('technical_telemetry_log', None)
    if log:
        log.close()
