"""Owner-local ARM transactions. The serial owner supplies the only writer."""
import fcntl
import os
import sqlite3
import stat
from pathlib import Path

from disarm_control.engine import DisarmEngine, Rejected, finite


class WebArm:
    def __init__(self, reader, disarm, stop, path, sender):
        self.reader, self.disarm, self.stop, self.sender = reader, disarm, stop, sender
        self.active = None
        self.fault = False
        self.last_clock = None
        path = Path(path)
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise PermissionError('Private ARM state directory required')
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        self.db = None
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PermissionError('Private ARM database required')
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db = sqlite3.connect(path, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA synchronous=FULL')
            with self.db:
                self.db.execute('''CREATE TABLE IF NOT EXISTS arm_requests (
                    id TEXT PRIMARY KEY, actor_id INTEGER NOT NULL, actor TEXT NOT NULL,
                    state TEXT NOT NULL, reason TEXT NOT NULL, created REAL NOT NULL)''')
                self.db.execute("""UPDATE arm_requests SET
                    state=CASE state WHEN 'QUEUED' THEN 'REJECTED' ELSE 'UNKNOWN' END,
                    reason='OWNER_RESTARTED' WHERE state IN ('QUEUED','ATTEMPT','SENT')""")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
        os.close(self.fd)

    def _now(self):
        now = self.reader.clock()
        if not finite(now) or (self.last_clock is not None and now < self.last_clock):
            self.fault = True
            raise Rejected('ARM_CLOCK_FAULT')
        self.last_clock = now
        return now

    def _eligible(self, now):
        from pymavlink.dialects.v20 import ardupilotmega as mav

        if (self.fault or not self.disarm.enabled or self.disarm.active is not None or
                self.disarm.storage_fault or self.disarm.clock_fault):
            raise Rejected('ARM_BLOCKED_BY_OWNER')
        if self.stop.storage_fault or self.stop.core.snapshot().stop_latch:
            raise Rejected('STOP_LATCH_ACTIVE_OR_FAULTED')
        brake = self.stop.brake
        if brake is not None and not brake.traction_ready():
            raise Rejected('BRAKE_RELEASE_REQUEST_UNAVAILABLE')
        native = self.stop.native
        if native is None or native.enforces_elrs_stop_latch() is not True:
            raise Rejected('INTERLOCK_UNVERIFIED')
        # Provider supplies a fresh receipt time for a healthy gate with drive
        # inhibited. ARM itself never grants traction permission.
        observed_at = native.arm_gate_verified_at()
        if not finite(observed_at) or not 0 <= now - observed_at < .5:
            raise Rejected('INTERLOCK_EVIDENCE_STALE')
        _, hb, target, _, age = self.disarm._heartbeat(now)
        if hb.base_mode & mav.MAV_MODE_FLAG_SAFETY_ARMED or hb.system_status != mav.MAV_STATE_STANDBY or hb.custom_mode != 4 or age >= 1.5:
            raise Rejected('FRESH_DISARMED_STANDBY_REQUIRED')
        sample = self.reader.messages.get('SYS_STATUS')
        if not sample or not finite(sample[0]) or not 0 <= now - sample[0] < 1.5:
            raise Rejected('SYS_STATUS_STALE')
        sys = sample[1]
        required = mav.MAV_SYS_STATUS_PREARM_CHECK | mav.MAV_SYS_STATUS_SENSOR_RC_RECEIVER
        if ((sys.get_srcSystem(), sys.get_srcComponent()) != target or
                any(mask & required != required for mask in (
                    sys.onboard_control_sensors_present, sys.onboard_control_sensors_enabled,
                    sys.onboard_control_sensors_health)) or
                sys.onboard_control_sensors_enabled & ~sys.onboard_control_sensors_health):
            raise Rejected('PREARM_OR_FAILSAFE')
        return target

    def ready(self, *_):
        with self.reader.lock:
            try:
                if self.active is not None:
                    raise Rejected('ARM_PENDING')
                self._eligible(self._now())
            except Exception as exc:
                return {'ready': False, 'reason': str(exc) if isinstance(exc, Rejected) else 'ARM_UNAVAILABLE'}
            return {'ready': True}

    def status(self, actor_id, actor, request_id):
        with self.reader.lock:
            row = self.db.execute('SELECT * FROM arm_requests WHERE id=? AND actor_id=?',
                                  (request_id, actor_id)).fetchone()
            if row is None:
                raise Rejected('ARM_REQUEST_NOT_FOUND')
            return dict(request_id=request_id, state=row['state'], reason=row['reason'])

    def request(self, actor_id, actor, request_id):
        DisarmEngine._validate_actor(actor_id, actor, request_id)
        with self.reader.lock:
            row = self.db.execute('SELECT actor_id FROM arm_requests WHERE id=?', (request_id,)).fetchone()
            if row is not None:
                return self.status(actor_id, actor, request_id)
            now = self._now()
            state, reason = 'QUEUED', 'WAITING_FOR_OWNER'
            try:
                if self.active is not None:
                    raise Rejected('ARM_PENDING')
                target = self._eligible(now)
            except Rejected as exc:
                state, reason = 'REJECTED', str(exc)
            with self.db:
                self.db.execute('INSERT INTO arm_requests VALUES(?,?,?,?,?,?)',
                                (request_id, actor_id, actor, state, reason, now))
            if state == 'QUEUED':
                self.active = (request_id, actor_id, now, target, None, None)
            return self.status(actor_id, actor, request_id)

    def _record(self, request_id, state, reason):
        with self.db:
            self.db.execute('UPDATE arm_requests SET state=?, reason=? WHERE id=?',
                            (state, reason, request_id))

    def _finish(self, state, reason):
        request_id = self.active[0]
        self.active = None
        self._record(request_id, state, reason)

    def preempt(self):
        if self.active is not None:
            try:
                self._finish('REJECTED' if self.active[4] is None else 'UNKNOWN', 'DISARM_PRIORITY')
            except Exception:
                self.fault = True

    def tick(self, fd):
        with self.reader.lock:
            if self.active is None:
                return
            request_id, actor_id, created, target, sent_at, sent_serial = self.active
            attempted = sent_at is not None
            try:
                now = self._now()
                if now - created >= 5:
                    self._finish('UNKNOWN' if attempted else 'REJECTED', 'ARM_TIMEOUT')
                    return
                if attempted:
                    stamp, hb, source, serial, _ = self.disarm._heartbeat(now)
                    if source != target:
                        raise Rejected('AUTOPILOT_CHANGED')
                    if stamp > sent_at and serial > sent_serial and hb.base_mode & 128:
                        self._finish('ARMED_OBSERVED', 'FRESH_POST_WRITE_HEARTBEAT')
                    return
                if self._eligible(now) != target:
                    raise Rejected('AUTOPILOT_CHANGED')
                self._record(request_id, 'ATTEMPT', 'DELIVERY_IN_PROGRESS')
                attempted = True
                self.sender(fd, target)
                self.active = (request_id, actor_id, created, target, self._now(), self.reader.heartbeat_serial)
                self._record(request_id, 'SENT', 'AWAITING_HEARTBEAT')
            except Exception as exc:
                self.active = None
                if not isinstance(exc, Rejected):
                    self.fault = True
                try:
                    self._record(request_id, 'UNKNOWN' if attempted else 'REJECTED',
                                 str(exc) if isinstance(exc, Rejected) else 'ARM_IO_OR_STORAGE_FAULT')
                except Exception:
                    self.fault = True


def arm_sender(encoder, write=os.write):
    from pymavlink.dialects.v20 import ardupilotmega as mav

    def send(fd, target):
        packet = mav.MAVLink_command_long_message(
            *target, mav.MAV_CMD_COMPONENT_ARM_DISARM, 0,
            1, 0, 0, 0, 0, 0, 0).pack(encoder)
        if write(fd, packet) != len(packet):
            raise OSError('Partial ARM write')
        encoder.seq = (encoder.seq + 1) % 256

    return send
