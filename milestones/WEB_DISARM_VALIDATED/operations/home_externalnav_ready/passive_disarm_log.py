#!/usr/bin/python3
"""Passive Web DISARM evidence logger. HTTP GET + query-only SQLite; no commands.

Records receipt times, not exact autopilot execution or physical wheel-stop time.
Loss of telemetry is logged and never interpreted as stopped motors. No control
RPC, POST, serial/camera open, parameter request or service operation exists here.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import urllib.request
from zoneinfo import ZoneInfo

ENDPOINTS = {
    'diagnostics': 'http://127.0.0.1:8084/api/owner-diagnostics',
    'externalnav': 'http://127.0.0.1:8084/api/externalnav',
    'state': 'http://127.0.0.1:8084/api/state',
    'telemetry': 'http://127.0.0.1:8082/api/telemetry',
}
LEDGER = Path('/var/lib/ugv-pixhawk/disarm.sqlite3')
DEFAULT_OUTPUT = Path('/home/ugvs2/ugv_optical_navigation/web_disarm_logs')
KYIV = ZoneInfo('Europe/Kyiv')
INTERVAL_S = .2


def duration_value(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError('duration must be a finite number') from None
    if not math.isfinite(number) or not 0 < number <= 600:
        raise argparse.ArgumentTypeError('duration must be >0 and <=600 seconds')
    return number


def stamps():
    now = datetime.now(timezone.utc)
    return dict(utc=now.isoformat(), kyiv=now.astimezone(KYIV).isoformat(),
                monotonic_s=time.monotonic())


def get_json(url):
    if url not in ENDPOINTS.values():
        raise ValueError('Only the fixed read-only local HTTP endpoints are allowed')
    request = urllib.request.Request(url, method='GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=.18) as response:
        return json.load(response)


def ledger_snapshot(path):
    """One consistent read transaction; pending states are evidence, not errors."""
    with closing(sqlite3.connect(Path(path).absolute().as_uri() + '?mode=ro',
                                 uri=True, timeout=.05)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        return {table: [dict(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY id')]
                for table in ('commands', 'delivery_evidence')}


def collect_http(getter=get_json):
    values, errors = {}, {}
    for name, url in ENDPOINTS.items():
        try:
            value = getter(url)
            if type(value) is not dict:
                raise ValueError('Expected JSON object')
            json.dumps(value, allow_nan=False)
            values[name] = value
        except Exception as error:
            errors[name] = type(error).__name__ + ': ' + str(error)[:250]
    return values, errors


class Events:
    def __init__(self):
        self.armed = None
        self.link = None
        self.texts = set()
        self.arm_transitions = []
        self.armed_to_disarmed = []
        self.lost_sources = set()
        self.previous_ledger = None
        self.rows = 0

    def observe(self, row):
        self.rows += 1
        events = []
        values, errors = row['http'], row['errors']
        lost = set(errors)
        def obj(value):
            return value if type(value) is dict else {}
        telemetry = obj(values.get('telemetry'))
        heartbeat = obj(obj(telemetry.get('messages')).get('HEARTBEAT'))
        if telemetry.get('connection') != 'CONNECTED' or heartbeat.get('state') != 'FRESH':
            lost.add('fresh_heartbeat')
        diagnostic = obj(values.get('diagnostics'))
        attitude = obj(diagnostic.get('attitude'))
        if attitude.get('fresh') is not True or attitude.get('valid') is not True:
            lost.add('fresh_attitude')
        if obj(values.get('externalnav')).get('pose_fresh') is not True:
            lost.add('fresh_optical_pose')
        for source in sorted(lost - self.lost_sources):
            events.append(dict(kind='OBSERVATION_SOURCE_LOST', source=source,
                               meaning='NO_CONTROL_ACTION_AND_NO_WHEEL_STOP_INFERENCE'))
        for source in sorted(self.lost_sources - lost):
            events.append(dict(kind='OBSERVATION_SOURCE_RECOVERED', source=source))
        self.lost_sources = lost
        link = telemetry.get('connection', 'UNAVAILABLE')
        if link != self.link:
            events.append(dict(kind='TELEMETRY_LINK', previous=self.link, current=link))
            self.link = link
        armed = obj(telemetry.get('armed'))
        current = (armed.get('value') if link == 'CONNECTED' and armed.get('state') == 'FRESH'
                   and heartbeat.get('state') == 'FRESH' else 'UNKNOWN')
        if current not in ('ARMED', 'DISARMED'):
            current = 'UNKNOWN'
        if current != self.armed:
            change = dict(kind='ARM_STATE_OBSERVED', previous=self.armed, current=current,
                          receipt_monotonic_s=row['monotonic_s'], utc=row['utc'])
            events.append(change)
            self.arm_transitions.append(change)
            if self.armed == 'ARMED' and current == 'DISARMED':
                self.armed_to_disarmed.append(change)
            self.armed = current
        for text in diagnostic.get('statustext') or []:
            if type(text) is not dict:
                continue
            key = json.dumps({k: text.get(k) for k in ('at', 'severity', 'text')}, sort_keys=True)
            if key not in self.texts:
                self.texts.add(key)
                events.append(dict(kind='STATUSTEXT_OBSERVED', message=text,
                                   prearm_or_arm=('prearm' in str(text.get('text')).lower()
                                                  or 'arm:' in str(text.get('text')).lower())))
        if row.get('ledger') is not None:
            encoded = json.dumps(row['ledger'], sort_keys=True, allow_nan=False)
            if encoded != self.previous_ledger:
                events.append(dict(kind='LEDGER_SNAPSHOT_CHANGED', snapshot=row['ledger']))
                self.previous_ledger = encoded
            else:
                row.pop('ledger')  # Full tables are logged only when changed.
        return events


def record(duration, output, ledger=LEDGER, getter=get_json):
    duration = duration_value(str(duration))
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    tracker = Events()
    start = time.monotonic()
    stopped = 'DURATION_EXPIRED'
    try:
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        boot_id = None
    header = dict(kind='HEADER', **stamps(), boot_id=boot_id, duration_limit_s=duration,
                  nominal_poll_hz=5, endpoints=ENDPOINTS, ledger=str(ledger),
                  control_actions=False, physical_wheel_stop_measured=False,
                  wheel_rpm_available=False, timing_scope='HTTP_AND_DB_OBSERVATION_RECEIPT_ONLY',
                  ack_scope='NOT_EXPOSED_BY_FIXED_ENDPOINTS_NO_CONTROL_STATUS_RPC_REQUESTED')
    print(json.dumps(dict(log_directory=str(output), duration_s=duration,
                          control_actions=False)), flush=True)
    with (output / 'raw.jsonl').open('w') as stream:
        stream.write(json.dumps(header, allow_nan=False) + '\n')
        try:
            while time.monotonic() - start < duration:
                cycle = time.monotonic()
                row = dict(kind='SAMPLE', **stamps())
                row['http'], row['errors'] = collect_http(getter)
                try:
                    row['ledger'] = ledger_snapshot(ledger)
                    json.dumps(row['ledger'], allow_nan=False)
                except Exception as error:
                    row.pop('ledger', None)
                    row['errors']['ledger'] = type(error).__name__ + ': ' + str(error)[:250]
                row['collection_started_monotonic_s'] = row['monotonic_s']
                row.update(stamps())
                events = tracker.observe(row)
                row['events'] = events
                stream.write(json.dumps(row, allow_nan=False) + '\n')
                stream.flush()
                for event in events:
                    if event['kind'] in ('ARM_STATE_OBSERVED', 'OBSERVATION_SOURCE_LOST', 'OBSERVATION_SOURCE_RECOVERED'):
                        print(json.dumps({**event, 'utc': row['utc'], 'kyiv': row['kyiv'],
                                          'monotonic_s': row['monotonic_s']}), flush=True)
                remaining = duration - (time.monotonic() - start)
                delay = min(INTERVAL_S - (time.monotonic() - cycle), remaining)
                if delay > 0:
                    time.sleep(delay)
        except KeyboardInterrupt:
            stopped = 'OPERATOR_CTRL_C'
        finally:
            stream.write(json.dumps(dict(kind='END', **stamps(), reason=stopped,
                                         control_actions=False), allow_nan=False) + '\n')
    summary = dict(reason=stopped, **stamps(), elapsed_s=time.monotonic()-start,
                   rows=tracker.rows, arm_observation_transitions=tracker.arm_transitions,
                   directly_observed_armed_to_disarmed=tracker.armed_to_disarmed,
                   unresolved_observation_sources=sorted(tracker.lost_sources),
                   control_actions=False, physical_wheel_stop_measured=False,
                   wheel_rpm_available=False,
                   command_execution_causality='NOT_PROVEN_BY_SENT_ACK_OR_HEARTBEAT_ALONE')
    (output / 'summary.json').write_text(json.dumps(summary, indent=2, allow_nan=False)+'\n')
    files = [output/'raw.jsonl', output/'summary.json']
    (output / 'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in files))
    print(json.dumps(dict(status='PASSIVE_LOG_SAVED', directory=str(output), rows=tracker.rows)), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', type=duration_value, default=180.)
    parser.add_argument('--out-dir', type=Path, help='New output directory (must not exist)')
    args = parser.parse_args()
    output = args.out_dir or DEFAULT_OUTPUT / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    record(args.duration, output)


if __name__ == '__main__':
    main()
