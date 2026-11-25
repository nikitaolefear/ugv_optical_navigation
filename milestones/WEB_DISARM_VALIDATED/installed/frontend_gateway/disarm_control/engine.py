"""Stock ArduPilot DISARM delivery and independently observed state.

No transport is opened here. SENT means a complete write to the existing
owner FD; DISARMED_OBSERVED means only a fresh post-write HEARTBEAT state.
Neither ACK nor HEARTBEAT proves execution of this particular request.
"""
import fcntl
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
DELIVERY_STATES = ('QUEUED', 'SENT', 'UNKNOWN', 'REJECTED')


class Rejected(ValueError):
    pass


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except (OverflowError, ValueError):
        return False


class DisarmEngine:
    def __init__(self, reader, bench, sender, database, *, clock=time.monotonic,
                 timeout=5.0, enabled=True):
        if not finite(timeout) or not 0 < timeout <= 30:
            raise ValueError('Invalid observation timeout')
        if not finite(reader.timeout) or not 0 < reader.timeout:
            raise ValueError('Invalid HEARTBEAT freshness')
        if type(enabled) is not bool:
            raise ValueError('enabled must be bool')
        self.reader, self.bench, self.sender = reader, bench, sender
        self.clock, self.timeout, self.lock = clock, timeout, reader.lock
        self.freshness = min(reader.timeout, 3.0)
        self.enabled = enabled
        self.epoch = str(uuid.uuid4())
        self.active = None
        self.last_clock = None
        self.clock_fault = False
        self.storage_fault = False
        self.ack_diagnostic = None
        path = Path(database)
        info = path.parent.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077 or path.parent.is_symlink():
            raise PermissionError('DISARM database directory must be private')
        self.db_fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.db = None
        try:
            info = os.fstat(self.db_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PermissionError('DISARM database must be a private regular file')
            fcntl.flock(self.db_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db = sqlite3.connect(database, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA synchronous=FULL')
            with self.db:
                self.db.execute('CREATE TABLE IF NOT EXISTS commands('
                    'id TEXT PRIMARY KEY, actor_id INTEGER, actor TEXT, state TEXT, '
                    'reason TEXT, created REAL, updated REAL)')
                # Additive schema only. Any pre-existing identities table is left
                # untouched: no allocation, deletion, reuse or reset of its rows.
                self.db.execute('CREATE TABLE IF NOT EXISTS delivery_evidence('
                    'id TEXT PRIMARY KEY, owner_epoch TEXT NOT NULL, '
                    'target_system INTEGER NOT NULL, target_component INTEGER NOT NULL, '
                    'sent_at REAL, sent_serial INTEGER, write_state TEXT NOT NULL)')
                self.db.execute("UPDATE commands SET state='UNKNOWN', reason='OWNER_RESTARTED' "
                    "WHERE state IN ('QUEUED','SEND_ATTEMPT','WAITING','SENT')")
        except BaseException:
            if self.db is not None:
                self.db.close()
            os.close(self.db_fd)
            raise

    def _now(self):
        try:
            now = self.clock()
        except Exception:
            now = None
        if not finite(now) or (self.last_clock is not None and now < self.last_clock):
            self.clock_fault = True
            raise Rejected('CLOCK_FAULT')
        self.last_clock = now
        if self.clock_fault:
            raise Rejected('CLOCK_FAULT')
        return now

    def _heartbeat(self, now):
        item = self.reader.messages.get('HEARTBEAT')
        if self.reader.state != 'READING' or not item:
            raise Rejected('LINK_UNAVAILABLE')
        try:
            stamp, hb = item
            age = now - stamp if finite(stamp) else None
            if not finite(age) or not 0 <= age < self.freshness:
                raise Rejected('HEARTBEAT_STALE')
            target = (hb.get_srcSystem(), hb.get_srcComponent())
            serial = self.reader.heartbeat_serial
            if (type(hb.base_mode) is not int or not 0 <= hb.base_mode <= 255 or
                    hb.autopilot != 3 or hb.type != 10 or target != self.reader.source or
                    type(target[0]) is not int or not 1 <= target[0] <= 255 or
                    type(target[1]) is not int or target[1] != 1 or
                    type(serial) is not int or serial < 0):
                raise Rejected('UNTRUSTED_HEARTBEAT')
            return stamp, hb, target, serial, age
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            if isinstance(exc, Rejected):
                raise
            raise Rejected('UNTRUSTED_HEARTBEAT') from None

    def _update(self, request_id, state, reason, now):
        with self.db:
            self.db.execute('UPDATE commands SET state=?,reason=?,updated=? WHERE id=?',
                            (state, reason, now, request_id))

    def _retire(self, row, reason, now):
        # A full write remains a known delivery fact even after link loss or
        # observation timeout. We never convert this into a command-success fact.
        state = ('SENT' if row['state'] == 'SENT' else
                 'REJECTED' if row['state'] == 'QUEUED' else 'UNKNOWN')
        self._update(row['id'], state, reason, now)
        self.active = None

    @staticmethod
    def _validate_actor(actor_id, actor, request_id):
        if (type(actor_id) is not int or actor_id <= 0 or type(actor) is not str or
                not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', actor) or
                type(request_id) is not str or not UUID.fullmatch(request_id)):
            raise Rejected('INVALID_REQUEST')

    def request(self, actor_id, actor, request_id):
        self._validate_actor(actor_id, actor, request_id)
        with self.lock:
            if self.storage_fault:
                raise Rejected('AUDIT_STORAGE_UNAVAILABLE')
            if not self.enabled:
                raise Rejected('FEATURE_DISABLED')
            existing = self.db.execute('SELECT * FROM commands WHERE id=?', (request_id,)).fetchone()
            if existing:
                if existing['actor_id'] != actor_id:
                    raise Rejected('REQUEST_NOT_OWNED')
                return self.status(actor_id, request_id)
            if self.active:
                # Refresh the old observation/timeout before accepting another
                # independent operator request. Never send from status/request.
                old = self.db.execute('SELECT actor_id FROM commands WHERE id=?', (self.active,)).fetchone()
                self.status(old['actor_id'], self.active)
                if self.active:
                    raise Rejected('DISARM_BUSY')
            now = self._now()
            _, _, target, _, _ = self._heartbeat(now)
            with self.db:
                self.db.execute('INSERT INTO commands VALUES(?,?,?,?,?,?,?)',
                                (request_id, actor_id, actor, 'QUEUED', 'ACCEPTED_FOR_DELIVERY', now, now))
                self.db.execute('INSERT INTO delivery_evidence VALUES(?,?,?,?,?,?,?)',
                                (request_id, self.epoch, *target, None, None, 'NOT_ATTEMPTED'))
            self.active = request_id
            if self.bench.phase in ('ENABLED', 'PENDING', 'WAITING'):
                self.bench.phase = 'CANCELLED_DISARM'
            self.bench.latched, self.bench.run = True, 'STOPPED'
            return self.status(actor_id, request_id)

    def _observation(self, row, evidence, now):
        empty = dict(observation_state='UNKNOWN', observation_reason='NO_POST_SEND_HEARTBEAT',
                     heartbeat_age_s=None, heartbeat_rx_serial=None,
                     heartbeat_observed_at=None, heartbeat_source=None,
                     heartbeat_freshness_s=self.freshness)
        if not self.enabled:
            return dict(empty, observation_reason='FEATURE_DISABLED')
        if (row['state'] != 'SENT' or evidence is None or
                evidence['write_state'] != 'FULL_WRITE' or evidence['owner_epoch'] != self.epoch):
            return dict(empty, observation_reason='NO_CURRENT_OWNER_SEND_EVIDENCE')
        if self.clock_fault or now is None:
            return dict(empty, observation_reason='CLOCK_FAULT')
        try:
            stamp, hb, target, serial, age = self._heartbeat(now)
        except Rejected as exc:
            return dict(empty, observation_reason=str(exc))
        if target != (evidence['target_system'], evidence['target_component']):
            return dict(empty, observation_reason='AUTOPILOT_CHANGED')
        if not (stamp > evidence['sent_at'] and serial > evidence['sent_serial']):
            return empty
        return dict(empty,
                    observation_state='ARMED_OBSERVED' if hb.base_mode & 128 else 'DISARMED_OBSERVED',
                    observation_reason='FRESH_POST_SEND_HEARTBEAT', heartbeat_age_s=age,
                    heartbeat_rx_serial=serial, heartbeat_observed_at=stamp,
                    heartbeat_source=list(target))

    def status(self, actor_id, request_id):
        with self.lock:
            if self.storage_fault:
                raise Rejected('AUDIT_STORAGE_UNAVAILABLE')
            row = self.db.execute('SELECT * FROM commands WHERE id=? AND actor_id=?',
                                  (request_id, actor_id)).fetchone()
            if not row:
                raise Rejected('REQUEST_NOT_FOUND')
            evidence = self.db.execute('SELECT * FROM delivery_evidence WHERE id=?', (request_id,)).fetchone()
            try:
                now = self._now()
            except Rejected:
                now = None
            observation = self._observation(row, evidence, now)
            if self.active == request_id:
                reason = None
                if not self.enabled:
                    reason = 'FEATURE_DISABLED'
                elif now is None:
                    reason = 'CLOCK_FAULT'
                elif now - row['created'] >= self.timeout:
                    reason = 'OBSERVATION_TIMEOUT'
                elif observation['observation_state'] == 'DISARMED_OBSERVED':
                    reason = 'DISARMED_STATE_OBSERVED_NOT_COMMAND_PROOF'
                else:
                    try:
                        _, _, target, _, _ = self._heartbeat(now)
                        if target != (evidence['target_system'], evidence['target_component']):
                            reason = 'AUTOPILOT_CHANGED'
                    except Rejected as exc:
                        reason = str(exc)
                if reason:
                    self._retire(row, reason, now if now is not None else self.last_clock or 0)
                    row = self.db.execute('SELECT * FROM commands WHERE id=?', (request_id,)).fetchone()
                    observation = self._observation(row, evidence, now)
            value = dict(row)
            if value['state'] not in DELIVERY_STATES:
                # Historical strict-ACK rows are retained unchanged but never
                # exposed as CONFIRMED under the new observational contract.
                value.update(state='UNKNOWN', reason='LEGACY_RESULT_NOT_COMMAND_PROOF')
            value.update(delivery_state=value['state'], command_outcome='UNKNOWN',
                         observation_pending=self.active == request_id,
                         ack_diagnostic=dict(self.ack_diagnostic) if self.ack_diagnostic else None,
                         **observation)
            return value

    def tick(self, fd):
        with self.lock:
            if not self.active:
                return
            try:
                row = self.db.execute('SELECT * FROM commands WHERE id=?', (self.active,)).fetchone()
                self.status(row['actor_id'], row['id'])
                if not self.active or row['state'] != 'QUEUED':
                    return
                evidence = self.db.execute('SELECT * FROM delivery_evidence WHERE id=?', (self.active,)).fetchone()
                now = self._now()
                _, _, target, serial, _ = self._heartbeat(now)
                if target != (evidence['target_system'], evidence['target_component']):
                    self._retire(row, 'AUTOPILOT_CHANGED', now)
                    return
                request_id = self.active
                # Persist uncertainty before invoking the writer. A crash after
                # this point can never cause automatic replay after restart.
                with self.db:
                    self.db.execute("UPDATE commands SET state='SEND_ATTEMPT', reason='DELIVERY_IN_PROGRESS', updated=? WHERE id=?",
                                    (now, request_id))
                    self.db.execute("UPDATE delivery_evidence SET write_state='ATTEMPTED' WHERE id=?", (request_id,))
                try:
                    self.sender(fd, target)
                except Exception:
                    self._update(request_id, 'UNKNOWN', 'SEND_UNCERTAIN_NO_RETRY', now)
                    self.active = None
                    return
                try:
                    completed = self._now()
                except Rejected:
                    completed = self.last_clock or now
                serial = self.reader.heartbeat_serial
                with self.db:
                    self.db.execute("UPDATE delivery_evidence SET sent_at=?, sent_serial=?, write_state='FULL_WRITE' WHERE id=?",
                                    (completed, serial, request_id))
                    self.db.execute("UPDATE commands SET state='SENT', reason='FULL_WRITE_NOT_COMMAND_PROOF', updated=? WHERE id=?",
                                    (completed, request_id))
                self.status(row['actor_id'], request_id)
            except Rejected as exc:
                if self.active:
                    row = self.db.execute('SELECT * FROM commands WHERE id=?', (self.active,)).fetchone()
                    self._retire(row, str(exc), self.last_clock or 0)
            except sqlite3.Error:
                self.storage_fault = True
                self.active = None

    def observe(self, msg):
        """Bounded uncorrelated ACK diagnostic; never modifies delivery/state."""
        with self.lock:
            try:
                if (msg.get_type() != 'COMMAND_ACK' or msg.command != 400 or
                        (msg.get_srcSystem(), msg.get_srcComponent()) != self.reader.source or
                        type(msg.result) is not int or not 0 <= msg.result <= 255):
                    return
                now = self._now()
                self.ack_diagnostic = dict(scope='UNCORRELATED', command=400, result=msg.result,
                                           source=list(self.reader.source), received_at=now)
            except (Rejected, AttributeError, TypeError, ValueError):
                return

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled must be bool')
        with self.lock:
            self.enabled = enabled
            if not enabled and self.active:
                try:
                    row = self.db.execute('SELECT * FROM commands WHERE id=?', (self.active,)).fetchone()
                    self._retire(row, 'FEATURE_DISABLED_NO_SEND' if row['state'] == 'QUEUED'
                                 else 'FEATURE_DISABLED', self.last_clock or 0)
                except sqlite3.Error:
                    self.storage_fault = True
                    self.active = None

    def close(self):
        with self.lock:
            self.db.close()
            os.close(self.db_fd)


def disarm_sender(*, write=os.write, encoder=None):
    """Only COMMAND_LONG(400, param1=0, param2=0), fixed GCS source 255/190."""
    from pymavlink.dialects.v20 import ardupilotmega as mav
    encoder = encoder or mav.MAVLink(None, srcSystem=255, srcComponent=190)

    def send(fd, target):
        original = encoder.srcSystem, encoder.srcComponent
        try:
            encoder.srcSystem, encoder.srcComponent = 255, 190
            packet = mav.MAVLink_command_long_message(
                target[0], target[1], mav.MAV_CMD_COMPONENT_ARM_DISARM, 0,
                0, 0, 0, 0, 0, 0, 0).pack(encoder)
        finally:
            encoder.srcSystem, encoder.srcComponent = original
        if write(fd, packet) != len(packet):
            raise OSError('Partial DISARM write; never retry')
        encoder.seq = (encoder.seq + 1) % 256
    return send
