"""Functional brake requests. A serial write does not confirm brake position."""
import logging
import os

from disarm_control.engine import Rejected, finite


class BrakeLink:
    HOLD, KEEP, RELEASE = range(3)

    def __init__(self, reader, disarm, stop, encoder, rc_snapshot, write=os.write, *, hold_only=False):
        self.reader, self.disarm, self.stop = reader, disarm, stop
        self.encoder, self.write, self.rc_snapshot = encoder, write, rc_snapshot
        self.hold_only = hold_only
        self.last_sent, self.last_state = None, self.HOLD
        self.request_id = None
        self.release_sent_at = None
        self.release_safety_serial = None
        self.phase, self.fault_reason = 'HOLD', None

    def _heartbeat(self, now):
        _, hb, target, _, age = self.disarm._heartbeat(now)
        if target != (1, 1) or age >= 1.5:
            raise Rejected('Brake heartbeat unavailable')
        return hb

    def _rc_available(self, now):
        rc = self.rc_snapshot()
        if rc is None or not finite(rc.get('at')) or not 0 <= now - rc['at'] < .5:
            return False
        pwm = rc['channels'].get('8')
        return type(pwm) is int and 800 <= pwm <= 2200

    def reset_link_ready(self, now):
        if self.hold_only:
            return False
        try:
            return not (self._heartbeat(now).base_mode & 128) and self._rc_available(now)
        except Rejected:
            return False

    def traction_ready(self):
        # Brake-only profile. Dispatch is not brake feedback and cannot enable traction.
        return False

    def begin_release(self, request_id):
        self.phase, self.fault_reason = 'PENDING', None
        self.request_id = request_id
        self.release_sent_at = None
        self.release_safety_serial = None

    def _fail(self, reason):
        self.stop.latch_for_brake()
        if self.phase != 'FAULT':
            self.phase, self.fault_reason = 'FAULT', reason
            self.stop.record_brake_result('BRAKE_RELEASE_FAULT', self.request_id, reason)

    def on_disarm(self, request_id, previous_active):
        if self.disarm.active == request_id and previous_active != request_id:
            self.stop.latch_for_brake(request_id)

    def _state(self, now, hb):
        if self.hold_only:
            return self.HOLD
        active = self.phase in ('PENDING', 'SENT')
        native = self.stop.native
        if active and (native is None or native.enforces_elrs_stop_latch() is not True):
            self._fail('TRACTION_INTERLOCK_FEEDBACK_LOST')
            return self.HOLD
        if self.stop.storage_fault or self.stop.committed_latch or self.stop.core.snapshot().stop_latch:
            if active:
                self.phase = 'HOLD'
                self.stop.record_brake_result('BRAKE_RELEASE_CANCELLED', self.request_id, 'STOP_LATCH')
            return self.HOLD
        if not active:
            return self.HOLD
        if not self._rc_available(now):
            self._fail('BRAKE_RC_LINK_LOST')
            return self.HOLD
        if self.phase == 'SENT':
            return self.KEEP
        conditions = native.reset_conditions()
        if (conditions is None or conditions.stationary is not True or
                conditions.disarmed is not True or conditions.hold_confirmed is not True or
                conditions.stop_transaction_idle is not True or hb.base_mode & 128 or
                self.stop.bench.disarm_pending()):
            self._fail('BRAKE_RELEASE_UNSAFE')
            return self.HOLD
        return self.RELEASE if self.last_sent is not None and self.last_state == self.HOLD else self.HOLD

    def tick(self, fd):
        from pymavlink.dialects.v20 import ardupilotmega as mav

        with self.reader.lock:
            try:
                now = self.reader.clock()
                if not finite(now) or now < 0 or (self.last_sent is not None and now < self.last_sent):
                    raise ValueError('Invalid brake-link clock')
                state = self._state(now, self._heartbeat(now))
                if self.last_sent is not None and state == self.last_state and now - self.last_sent < .1:
                    return
                packet = mav.MAVLink_named_value_int_message(
                    int(now * 1000) & 0xffffffff, b'UGVBRAKE03', state).pack(self.encoder)
                if self.write(fd, packet) != len(packet):
                    raise OSError('Partial brake gate write')
                self.encoder.seq = (self.encoder.seq + 1) % 256
                self.last_sent, self.last_state = now, state
                if state == self.RELEASE:
                    self.stop.record_brake_result('BRAKE_RELEASE_REQUEST_SENT', self.request_id,
                                                  'PHYSICAL_POSITION_UNCONFIRMED')
                    if not self.stop.storage_fault and not self.stop.core.snapshot().stop_latch:
                        self.release_sent_at = now
                        self.release_safety_serial = self.reader.heartbeat_safety_serial
                        self.phase = 'SENT'
            except Rejected:
                if self.phase in ('PENDING', 'SENT'):
                    self._fail('BRAKE_LINK_LOST')
                self.last_sent, self.last_state = None, self.HOLD
            except Exception:
                if self.phase != 'FAULT':
                    logging.getLogger(__name__).exception('Functional brake link fault')
                self._fail('BRAKE_LINK_FAULT')
