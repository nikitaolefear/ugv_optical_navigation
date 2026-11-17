"""Isolated GPIO17 STOP_LATCH experiment. No vehicle interfaces."""
import datetime
import json
from pathlib import Path
import re
import subprocess
import threading
import time

class Rejected(ValueError):
    pass

class StopLatch:
    def __init__(self, journal, clock=None):
        self.lock = threading.RLock()
        self.journal = Path(journal)
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds'))
        self.contact = 'UNKNOWN'
        self.latched = True
        self.run = 'STOPPED'
        self.probe = False
        self.events = []
        self.sequence = 0
        self.last_sample = None
        if self.journal.exists():
            rows = [json.loads(line) for line in self.journal.read_text().splitlines() if line]
            self.events = rows[-500:]
            self.sequence = max((row['sequence'] for row in rows), default=0)
        self._record('BOOT', 'Startup requires a released input and explicit RESET')

    def _record(self, event, detail=''):
        self.sequence += 1
        row = dict(sequence=self.sequence, time=self.clock(), event=event, detail=detail,
                   contact=self.contact, stop_latch=self.latched, test_state=self.run,
                   reset_probe_armed=self.probe, test_only=True, movement_allowed=False)
        try:
            with self.journal.open('a', encoding='utf8') as f:
                f.write(json.dumps(row, ensure_ascii=False)+'\n')
                f.flush()
        except OSError:
            self.contact, self.latched, self.run = 'UNKNOWN', True, 'STOPPED'
            raise
        self.events.append(row)
        self.events = self.events[-500:]

    def _check_input(self):
        if self.contact != 'UNKNOWN' and (self.last_sample is None or time.monotonic() - self.last_sample > 1.5):
            self.contact, self.latched, self.run = 'UNKNOWN', True, 'STOPPED'
            self._record('INPUT_STALE', 'No GPIO sample within 1.5 seconds')

    def snapshot(self):
        with self.lock:
            self._check_input()
            return dict(contact=self.contact, stop_latch=self.latched, test_state=self.run,
                        reset_allowed=self.contact == 'RELEASED', reset_probe_armed=self.probe,
                        test_only=True, movement_allowed=False, events=list(self.events))

    def input(self, contact):
        if contact not in ('PRESSED','RELEASED','UNKNOWN'):
            raise ValueError('Invalid contact')
        with self.lock:
            self.last_sample = time.monotonic()
            if contact == self.contact:
                return
            self.contact = contact
            if contact != 'RELEASED':
                self.latched, self.run = True, 'STOPPED'
            self._record('CONTACT_'+contact)
            if contact == 'PRESSED' and self.probe:
                self.probe = False
                try:
                    self.command('reset', actor='AUTO_RESET_PROBE')
                except Rejected:
                    pass

    def command(self, action, actor='TEST_UI'):
        with self.lock:
            self._check_input()
            reason = None
            if action in ('start','resume') and (self.latched or self.contact != 'RELEASED'):
                reason = 'STOP_LATCH активний або контакт невідомий'
            elif action == 'reset' and self.contact != 'RELEASED':
                reason = 'RESET дозволений лише при відпущеному бампері'
            elif action == 'start' and self.run != 'IDLE':
                reason = 'START потребує IDLE та окремої дії після RESET'
            elif action == 'resume' and self.run != 'PAUSED':
                reason = 'RESUME потребує PAUSED'
            elif action == 'pause' and self.run != 'TEST_RUNNING':
                reason = 'PAUSE потребує TEST_RUNNING'
            elif action not in ('start','resume','reset','pause','stop','arm_reset_probe'):
                reason = 'Невідома тестова дія'
            if reason:
                self._record(action.upper()+'_REJECTED', actor+': '+reason)
                raise Rejected(reason)
            if action == 'reset':
                self.latched, self.run = False, 'IDLE'
            elif action in ('start','resume'):
                self.run = 'TEST_RUNNING'
            elif action == 'pause':
                self.run = 'PAUSED'
            elif action == 'stop':
                self.run = 'STOPPED'
            elif action == 'arm_reset_probe':
                self.probe = True
            self._record(action.upper()+'_ACCEPTED', actor)
            return self.snapshot()

LEVEL = re.compile(r'^17:\s+ip\s+pu\s*\|\s*(hi|lo)\b')
def read_contact():
    # Read-only command: does not request a line, configure bias or write a value.
    result = subprocess.run(['/usr/bin/pinctrl','get','17'], capture_output=True,
                            text=True, timeout=0.5, check=True)
    match = LEVEL.match(result.stdout.strip())
    if not match:
        raise ValueError('GPIO17 is not input with pull-up')
    return 'PRESSED' if match[1] == 'lo' else 'RELEASED'

def monitor(latch, stop, reader=read_contact):
    while not stop.is_set():
        try:
            contact = reader()
        except (OSError, ValueError, subprocess.SubprocessError):
            contact = 'UNKNOWN'
        latch.input(contact)
        stop.wait(0.1)
