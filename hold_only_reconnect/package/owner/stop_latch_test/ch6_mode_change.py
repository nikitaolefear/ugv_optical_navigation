"""One-shot CH6 mode migration on the owner thread, with durable recovery evidence."""
import json
import os
from pathlib import Path

from pymavlink.dialects.v20 import ardupilotmega as mav

MODES = ('MODE1', 'MODE2', 'MODE3', 'MODE4', 'MODE5', 'MODE6')
ORIGINAL = dict.fromkeys(MODES, 0) | {'MODE_CH': 8}
TARGET = dict(zip(MODES, (0, 4, 4, 4, 4, 10))) | {'MODE_CH': 6}
UNCHANGED = dict(RC5_OPTION=153, RC6_OPTION=0, RC8_OPTION=0, RCMAP_ROLL=1,
                 RCMAP_PITCH=2, RCMAP_THROTTLE=3, RCMAP_YAW=4)
AUTOPILOT = (1, 1)
RC_RECEIVER = 1 << 16


class ModeChange:
    def __init__(self, reader, diagnostics, encoder, safety_check, action, journal, write=os.write):
        self.reader, self.diagnostics, self.encoder = reader, diagnostics, encoder
        self.safety_check, self.action, self.write = safety_check, action, write
        self.journal = Path(journal)
        self.path = self.journal if action == 'apply' else self.journal.with_suffix('.rollback.jsonl')
        self.queued = list(TARGET) if action == 'apply' else ['MODE_CH', *MODES]
        self.goal = TARGET if action == 'apply' else ORIGINAL
        self.confirmed = {}
        self.verified_modes = []
        self.phase, self.reason = 'PREFLIGHT', None
        self.deadline = reader.clock() + 50
        self.log = None
        self.current = self.epoch = self.sent_at = self.read_at = None
        try:
            marker = self.path.with_suffix('.attempt')
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        except FileExistsError:
            self.phase, self.reason = 'BLOCKED', 'ONE_SHOT_ALREADY_CONSUMED'

    @property
    def writing(self):
        return self.phase in ('SET', 'REQUEST_READBACK', 'WAIT_READBACK')

    def _record(self, **event):
        self.log.write(json.dumps(event, sort_keys=True, allow_nan=False) + '\n')
        self.log.flush()
        os.fsync(self.log.fileno())

    def _stop(self, reason):
        self.phase, self.reason = 'BLOCKED', reason
        if self.log is not None:
            try:
                self._record(event='BLOCKED', reason=reason, parameter=self.current)
            except OSError:
                self.reason += '_JOURNAL_FAILED'

    def _safe(self, now, ch6_low):
        reader, diag = self.reader, self.diagnostics
        hb = reader.messages.get('HEARTBEAT')
        rc, health = diag.rc, diag.sys
        if (reader.state != 'READING' or reader.source != AUTOPILOT or not self.safety_check() or
                hb is None or not 0 <= now - hb[0] < 1.5 or hb[1].base_mode & 128 or
                hb[1].autopilot != 3 or hb[1].type != 10 or not hb[1].base_mode & 1 or
                (hb[1].get_srcSystem(), hb[1].get_srcComponent()) != AUTOPILOT or
                rc is None or not 0 <= now - rc['at'] < 1.5 or
                health is None or not 0 <= now - health['at'] < 1.5 or
                not all(health[key] & RC_RECEIVER for key in ('present', 'enabled', 'health'))):
            return False
        channels = rc['channels']
        return (channels.get('5') == 999 and channels.get('8') == 999 and
                (channels.get('6') == 999 if ch6_low else channels.get('6') in (999, 1500, 2000)))

    def _begin(self):
        observed = {name: self.diagnostics.params[name]['value'] for name in (*ORIGINAL, *UNCHANGED)}
        if any(observed[name] != value for name, value in UNCHANGED.items()):
            self._stop('PROTECTED_PARAMETER_MISMATCH')
            return
        initial = {name: observed[name] for name in ORIGINAL}
        if self.action == 'apply':
            valid = initial == ORIGINAL
        else:
            with self.journal.open() as stream:
                backup = json.loads(stream.readline())
            valid = (backup == {'event':'ORIGINAL', 'values':ORIGINAL} and
                     all(initial[name] in (ORIGINAL[name], TARGET[name]) for name in ORIGINAL))
        if not valid:
            self._stop('BASELINE_MISMATCH')
            return
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.log = os.fdopen(fd, 'w')
        self._record(event='ORIGINAL', values=initial)
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        self.epoch = self.reader.heartbeat_safety_serial
        self.phase = 'SET'

    def _send(self, fd, message):
        data = message.pack(self.encoder)
        if self.write(fd, data) != len(data):
            raise OSError('Partial parameter packet')
        self.encoder.seq = (self.encoder.seq + 1) % 256

    def _advance(self, fd, now):
        if self.phase == 'SET':
            self.current = self.queued.pop(0)
            if self.current == 'MODE_CH' and self.action == 'apply' and (
                    any(self.confirmed.get(name) != TARGET[name] or
                        self.diagnostics.params.get(name, {}).get('value') != TARGET[name] for name in MODES)):
                self._stop('SIX_MODES_NOT_CONFIRMED')
                return
            self._record(event='WRITE_INTENT', parameter=self.current, value=self.goal[self.current])
            self._send(fd, mav.MAVLink_param_set_message(*AUTOPILOT, self.current.encode(),
                       self.goal[self.current], mav.MAV_PARAM_TYPE_INT8))
            self.sent_at, self.phase = now, 'REQUEST_READBACK'
            return
        if self.phase == 'REQUEST_READBACK':
            if now - self.sent_at >= .15:
                self._send(fd, mav.MAVLink_param_request_read_message(*AUTOPILOT, self.current.encode(), -1))
                self.read_at, self.phase = now, 'WAIT_READBACK'
            return
        sample = self.diagnostics.params.get(self.current, {})
        if now - self.read_at >= 3:
            self._stop('PARAMETER_CONFIRMATION_TIMEOUT')
        elif sample.get('at', -1) > self.read_at:
            if sample['value'] != self.goal[self.current]:
                self._stop('PARAMETER_VALUE_MISMATCH')
                return
            self.confirmed[self.current] = sample['value']
            self._record(event='READBACK', parameter=self.current, value=sample['value'])
            if self.queued:
                self.phase = 'SET'
            else:
                self.phase = 'VERIFY_SWITCH' if self.action == 'apply' else 'ROLLED_BACK'
                self.deadline = now + 120

    def tick(self, fd, *, command_pending):
        if self.phase in ('BLOCKED', 'VERIFIED', 'ROLLED_BACK'):
            return
        now = self.reader.clock()
        try:
            if self.phase == 'PREFLIGHT':
                if now >= self.deadline:
                    self._stop('PREFLIGHT_TIMEOUT')
                elif (not command_pending and self._safe(now, True) and
                      all(name in self.diagnostics.params for name in (*ORIGINAL, *UNCHANGED))):
                    self._begin()
                return
            if (self.reader.heartbeat_safety_serial != self.epoch or command_pending or
                    not self._safe(now, self.phase != 'VERIFY_SWITCH')):
                self._stop('LIVE_PRECONDITION_LOST')
                return
            if self.writing:
                self._advance(fd, now)
                return
            if now >= self.deadline:
                self._stop('SWITCH_VERIFICATION_TIMEOUT')
                return
            expected = ((999, 0, 'MANUAL'), (1500, 4, 'HOLD'), (2000, 10, 'AUTO'), (1500, 4, 'HOLD'))
            pwm, mode, label = expected[len(self.verified_modes)]
            hb_at, hb = self.reader.messages['HEARTBEAT']
            if (hb_at > self.read_at and self.diagnostics.rc['channels']['6'] == pwm and
                    hb.custom_mode == mode):
                self._record(event='SWITCH_MODE_OBSERVED', pwm=pwm, mode=label)
                self.verified_modes.append(label)
                self.read_at = hb_at
                if len(self.verified_modes) == len(expected):
                    self.phase = 'VERIFIED'
        except (OSError, ValueError):
            self._stop('WRITE_OR_JOURNAL_FAILED')

    def snapshot(self):
        with self.reader.lock:
            return dict(phase=self.phase, reason=self.reason, confirmed=dict(self.confirmed),
                        verified_modes=list(self.verified_modes), parameter=self.current)
