"""Functional brake requests. A serial write does not confirm brake position."""
import logging
import os

from disarm_control.engine import Rejected, finite
from .state import ResetConditions


class BrakeLink:
    HOLD, KEEP, RELEASE = range(3)
    LEASE = .350

    def __init__(self, reader, disarm, stop, encoder, rc_snapshot, write=os.write, *,
                 hold_only=False, rc_health=lambda: None, parameters=lambda: {},
                 release_inhibited=lambda: False):
        self.reader, self.disarm, self.stop = reader, disarm, stop
        self.encoder, self.write, self.rc_snapshot = encoder, write, rc_snapshot
        self.hold_only = hold_only
        self.rc_health, self.parameters = rc_health, parameters
        self.release_inhibited = release_inhibited
        self.last_sent, self.last_state = None, self.HOLD
        self.reset_epoch = None
        self.request_id = None
        self.web_requested = False
        self.phase, self.fault_reason = 'HOLD', None

    def _heartbeat(self, now):
        _, hb, target, _, age = self.disarm._heartbeat(now)
        if target != (1, 1) or age >= 1.5:
            raise Rejected('Brake heartbeat unavailable')
        return hb

    def _operator_request(self, now):
        rc = self.rc_snapshot()
        health = self.rc_health()
        receiver = 1 << 16  # MAV_SYS_STATUS_SENSOR_RC_RECEIVER
        if (not rc or not finite(rc.get('at')) or not 0 <= now - rc['at'] < .5 or
                not health or not finite(health.get('at')) or not 0 <= now - health['at'] < 1.5 or
                not all(health[field] & receiver for field in ('present', 'enabled', 'health'))):
            return None, 'BRAKE_RC_LINK_UNAVAILABLE'
        params = self.parameters()

        def value(name):
            return params.get(name, {}).get('value')

        apply, release = value('UGVB_CH_APPLY'), value('UGVB_CH_RELEASE')
        trim, deadzone, options = value('RC3_TRIM'), value('RC3_DZ'), value('RC_OPTIONS')
        mode_channel = value('MODE_CH')
        stick_channels = tuple(value(name) for name in
                               ('RCMAP_ROLL', 'RCMAP_PITCH', 'RCMAP_THROTTLE', 'RCMAP_YAW'))
        mode_channel_valid = mode_channel == 0 or (
            mode_channel == 6 and value('RC6_OPTION') == 0 and
            all(finite(channel) and channel == int(channel) and 1 <= channel <= 16
                and channel not in (5, 6, 8) for channel in stick_channels)
        )
        if (not mode_channel_valid or value('RC8_OPTION') != 0 or
                value('RCMAP_THROTTLE') != 3 or value('UGVB_APPLY_ONLY') != 0 or
                not all(finite(v) and v == int(v)
                        for v in (apply, release, trim, deadzone, options)) or
                not all(800 <= v <= 2200 for v in (apply, release, trim)) or
                abs(apply - release) < 200 or not 0 <= deadzone <= 200 or
                options < 0 or not int(options) & 2):
            return None, 'BRAKE_RC_PROFILE_UNVERIFIED'
        required_channels = ('3', '8', '6') if mode_channel == 6 else ('3', '8')
        pulses = [rc['channels'].get(channel) for channel in required_channels]
        if not all(type(pwm) is int and 800 <= pwm <= 2200 for pwm in pulses):
            return None, 'BRAKE_RC_CHANNEL_INVALID'
        throttle, ch8 = pulses[:2]
        if abs(throttle - trim) > min(deadzone, 50):
            return None, 'BRAKE_THROTTLE_NOT_NEUTRAL'
        if abs(ch8 - apply) <= 75:
            return self.HOLD, None
        if abs(ch8 - release) <= 75:
            return self.RELEASE, None
        return None, 'BRAKE_CH8_POSITION_INVALID'

    def reset_blocker(self, now):
        if self.hold_only:
            return 'BRAKE_HOLD_ONLY'
        if self.release_inhibited():
            return 'BRAKE_RELEASE_INHIBITED'
        try:
            if self._heartbeat(now).base_mode & 128:
                return 'BRAKE_DISARMED_REQUIRED'
        except Rejected:
            return 'BRAKE_OWNER_LINK_UNAVAILABLE'
        request, reason = self._operator_request(now)
        if reason:
            return reason
        if request != self.HOLD:
            return 'BRAKE_CH8_APPLY_REQUIRED_FOR_RESET'
        if (self.last_state != self.HOLD or self.last_sent is None or
                not 0 <= now - self.last_sent < self.LEASE):
            return 'BRAKE_HOLD_LEASE_UNAVAILABLE'
        return None

    def traction_ready(self):
        # Brake-only profile. Dispatch is not brake feedback and cannot enable traction.
        return False

    def begin_release(self, request_id):
        self.phase, self.fault_reason = 'PENDING', None
        self.request_id = request_id
        self.reset_epoch = self.reader.heartbeat_safety_serial
        self.web_requested = False

    def owner_lost(self):
        if self.phase in ('PENDING', 'SENT'):
            self._fail('BRAKE_OWNER_LINK_LOST')
        self.last_sent, self.last_state = None, self.HOLD

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
        if self.stop.storage_fault or self.stop.committed_latch or self.stop.core.snapshot().stop_latch:
            if active:
                self.phase = 'HOLD'
                self.stop.record_brake_result('BRAKE_RELEASE_CANCELLED', self.request_id, 'STOP_LATCH')
            return self.HOLD
        if not active:
            return self.HOLD
        request, reason = self._release_gate(now, hb)
        if reason:
            self._fail(reason)
            return self.HOLD
        if request == self.HOLD:
            self.phase = 'PENDING'
            return self.HOLD
        return self.KEEP if self.phase == 'SENT' else self.RELEASE

    def _release_gate(self, now, hb):
        if self.release_inhibited():
            return None, 'BRAKE_RELEASE_INHIBITED'
        native = self.stop.native
        if native is None or native.enforces_elrs_stop_latch() is not True:
            return None, 'TRACTION_INTERLOCK_FEEDBACK_LOST'
        if (self.reset_epoch != self.reader.heartbeat_safety_serial or self.last_sent is None or
                not 0 <= now - self.last_sent < self.LEASE):
            return None, 'BRAKE_OWNER_LEASE_LOST'
        conditions = native.reset_conditions()
        blocked_at = native.blocked_at()
        if (not finite(blocked_at) or not 0 <= now - blocked_at < .5 or
                type(conditions) is not ResetConditions or not finite(conditions.observed_at) or
                not 0 <= now - conditions.observed_at < .5 or conditions.stationary is not True or
                conditions.disarmed is not True or conditions.hold_confirmed is not True or
                conditions.stop_transaction_idle is not True or hb.base_mode & 128 or
                self.stop.bench.disarm_pending()):
            return None, 'BRAKE_RELEASE_UNSAFE'
        return self._operator_request(now)

    def web_release_blocker(self, now):
        if self.hold_only:
            return 'BRAKE_HOLD_ONLY'
        if self.stop.storage_fault:
            return 'BRAKE_STORAGE_FAULT'
        if self.stop.committed_latch or self.stop.core.snapshot().stop_latch:
            return 'BRAKE_STOP_RESET_REQUIRED'
        if self.web_requested:
            return 'BRAKE_RELEASE_ALREADY_REQUESTED'
        if self.phase != 'PENDING' or self.last_state != self.HOLD:
            return 'BRAKE_HOLD_LEASE_UNAVAILABLE'
        try:
            request, reason = self._release_gate(now, self._heartbeat(now))
        except Rejected:
            return 'BRAKE_OWNER_LINK_UNAVAILABLE'
        return reason or ('BRAKE_CH8_APPLY_REQUIRED' if request != self.HOLD else None)

    def accept_web_release(self, request_id):
        self.request_id = request_id
        self.web_requested = True

    def tick(self, fd):
        from pymavlink.dialects.v20 import ardupilotmega as mav

        with self.reader.lock:
            try:
                now = self.reader.clock()
                if not finite(now) or now < 0 or (self.last_sent is not None and now < self.last_sent):
                    raise ValueError('Invalid brake-link clock')
                hb = self._heartbeat(now)
                state = self._state(now, hb)
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
                        self.phase = 'SENT'
            except Rejected:
                self.owner_lost()
            except Exception:
                if self.phase != 'FAULT':
                    logging.getLogger(__name__).exception('Functional brake link fault')
                self._fail('BRAKE_LINK_FAULT')
