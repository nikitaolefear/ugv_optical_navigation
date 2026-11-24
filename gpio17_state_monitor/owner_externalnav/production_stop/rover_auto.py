"""AUTO checkpoint and single-attempt restore on the existing owner stream."""
from struct import error as StructError
from uuid import uuid4

from pymavlink.dialects.v20 import ardupilotmega as mav

from .mission_checkpoint import ROUTE_MAX_AGE, MissionCheckpoint
from .mission_resume import ResumeAcceptance

AUTO_MODE = 10
TELEMETRY_MAX_AGE = .5
RESTORE_TIMEOUT = 2.0
SET_CURRENT = 224  # MAV_CMD_DO_SET_MISSION_CURRENT


class RoverAutoAdapter:
    """The host supplies a nonblocking, trusted route cache and existing sender.

    read_route returns a complete RouteSnapshot from Pixhawk 1/1, or None
    during downloads, uploads or link loss. send_current(fd, seq) sends one command
    with param1=seq and param2=0, without retries or waiting for a reply.
    """

    def __init__(self, reader, ipc, mission_opaque_id=None, *, interlock_ready=None,
                 read_route=None, send_current=None):
        self.reader, self.ipc = reader, ipc
        self.mission_opaque_id = mission_opaque_id
        self.interlock_ready = interlock_ready
        self.read_route, self.send_current = read_route, send_current
        self.current = None
        self.invalidate()

    def invalidate(self):
        self.phase = 'STOPPED'
        self.checkpoint = self.reset_id = self.pending = None
        self.sent_at = self.confirmed_at = self.accepted = None
        self.restore_ack = self.restore_current = False

    def _fresh(self, now, at):
        return 0 <= now - at < TELEMETRY_MAX_AGE

    def _route(self, now, seq, *, after=None):
        if self.read_route is None:
            return None
        try:
            route = self.read_route()
            if (route is None or not 0 <= now - route.observed_at < ROUTE_MAX_AGE or
                    not 0 < seq < len(route.items)):
                return None
            started_at = route.observed_at if route.started_at is None else route.started_at
            if after is not None and started_at <= after:
                return None
            return route.fingerprint()
        except (OSError, ValueError, TypeError, AttributeError, OverflowError, StructError):
            return None

    def _auto(self, now, *, after=None):
        heartbeat = self.reader.messages.get('HEARTBEAT')
        if heartbeat is None:
            return False
        at, hb = heartbeat
        return (self._fresh(now, at) and (after is None or at > after) and
                (hb.get_srcSystem(), hb.get_srcComponent()) == (1, 1) and
                hb.autopilot == mav.MAV_AUTOPILOT_ARDUPILOTMEGA and
                hb.type == mav.MAV_TYPE_GROUND_ROVER and hb.custom_mode == AUTO_MODE and
                hb.base_mode & mav.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED and
                hb.base_mode & mav.MAV_MODE_FLAG_SAFETY_ARMED)

    def front_pressed(self, observed_at=None):
        with self.reader.lock:
            # A second STOP invalidates the first even if the rover is already in HOLD.
            can_capture = self.phase not in ('RESTORE_QUEUED', 'RESTORING')
            current = self.current
            self.invalidate()
            self.current = None
            now = self.reader.clock()
            if (not can_capture or observed_at is None or not self._fresh(now, observed_at)
                    or current is None or not self._fresh(now, current[0])
                    or current[0] > observed_at or not self._auto(observed_at)
                    or current[2] != mav.MISSION_STATE_ACTIVE):
                return
            digest = self._route(observed_at, current[1])
            if digest is not None and self.reader.state == 'READING':
                self.checkpoint = MissionCheckpoint(str(uuid4()), observed_at, digest, current[1])

    def stop_latched(self):
        with self.reader.lock:
            if self.phase == 'RESTORING':
                self._finish('UNKNOWN')
            elif self.phase not in ('STOPPED', 'UNKNOWN', 'REFUSED'):
                self.invalidate()

    def link_lost(self):
        with self.reader.lock:
            if self.phase in ('RESTORING', 'UNKNOWN'):
                self._finish('UNKNOWN')
            else:
                self.invalidate()
            self.current = None

    def reset_accepted(self, reset_id):
        with self.reader.lock:
            if self.phase in ('UNKNOWN', 'REFUSED'):
                self.invalidate()
            if self.phase == 'STOPPED' and self.checkpoint is not None:
                self.reset_id = reset_id
                self.phase = 'RESET_ACCEPTED'

    def evidence(self):
        return self.ipc.evidence() if self.ipc is not None else None

    def release_confirmed(self, reset_id):
        with self.reader.lock:
            if self.reset_id == reset_id and self.phase == 'RESET_ACCEPTED':
                self.phase = 'RELEASE_CONFIRMED'

    def _ready(self, now):
        checkpoint = self.checkpoint
        return (checkpoint is not None and self.reader.state == 'READING' and
                self.interlock_ready is not None and self.interlock_ready() is True and
                self._route(now, checkpoint.seq, after=checkpoint.stopped_at)
                == checkpoint.route_sha256)

    def resume(self, request_id, reset_id):
        with self.reader.lock:
            if (self.reset_id != reset_id or self.phase != 'RELEASE_CONFIRMED' or
                    self.ipc is None or self.send_current is None or not self._ready(self.reader.clock())):
                return ResumeAcceptance.REFUSED
            self.pending = (request_id, reset_id, self.checkpoint.stop_id)
            self.phase = 'RESTORE_QUEUED'
            self.queued_at = self.reader.clock()
            return ResumeAcceptance.ACCEPTED

    def _finish(self, phase):
        self.invalidate()
        self.phase = phase

    def tick(self, fd):
        with self.reader.lock:
            if self.phase not in ('RESTORE_QUEUED', 'RESTORING'):
                return
            now = self.reader.clock()
            if (not self._ready(now) or now - self.queued_at >= RESTORE_TIMEOUT or
                    self.pending[2] != self.checkpoint.stop_id):
                self._finish('UNKNOWN' if self.sent_at is not None else 'REFUSED')
                return
            if self.phase == 'RESTORE_QUEUED':
                self.sent_at = now
                self.phase = 'RESTORING'
                try:
                    self.send_current(fd, self.checkpoint.seq)
                except OSError:
                    self._finish('UNKNOWN')
                return
            if not (self.restore_ack and self.restore_current and
                    self._fresh(now, self.confirmed_at)):
                return
            request_id, reset_id, _ = self.pending
            self.pending = None
            self._finish('UNKNOWN')
            try:
                decision = self.ipc.resume(request_id, reset_id)
            except OSError:
                return
            if decision == ResumeAcceptance.ACCEPTED:
                self.phase = 'RESUME_ACCEPTED'
                self.accepted = self.reader.clock()
                self.current = None
            elif decision == ResumeAcceptance.REFUSED:
                self._finish('REFUSED')

    def observe(self, message):
        if (message.get_srcSystem(), message.get_srcComponent()) != (1, 1):
            return
        with self.reader.lock:
            now = self.reader.clock()
            kind = message.get_type()
            restoring = self.phase == 'RESTORING' and now > self.sent_at
            if kind == 'MISSION_CURRENT':
                if (self.mission_opaque_id is not None and
                        getattr(message, 'mission_id', None) != self.mission_opaque_id):
                    self.current = None
                    if restoring:
                        self._finish('UNKNOWN')
                    return
                self.current = now, message.seq, message.mission_state
                if restoring:
                    self.restore_current = message.seq == self.checkpoint.seq
                    self.confirmed_at = now if self.restore_current else None
            elif restoring and kind == 'COMMAND_ACK' and message.command == SET_CURRENT:
                if message.result == mav.MAV_RESULT_ACCEPTED:
                    self.restore_ack = True
                else:
                    self._finish('REFUSED')

    def status(self):
        with self.reader.lock:
            now = self.reader.clock()
            if (self.phase == 'RESUME_ACCEPTED' and self.reader.state == 'READING' and
                    self.current is not None and self.current[0] > self.accepted and
                    self._fresh(now, self.current[0]) and self._auto(now, after=self.accepted) and
                    self.current[2] == mav.MISSION_STATE_ACTIVE):
                return 'RUNNING'
            return self.phase


def mission_current_sender(encoder, write):
    """Build a sender for the existing serial owner or isolated test transport."""
    def send(fd, seq):
        message = mav.MAVLink_command_long_message(1, 1, SET_CURRENT, 0,
                                                   seq, 0, 0, 0, 0, 0, 0)
        packet = message.pack(encoder)
        encoder.seq = (encoder.seq + 1) % 256
        if write(fd, packet) != len(packet):
            raise OSError('Partial waypoint restore write')
    return send
