"""One-shot GPIO17 -> latch -> HOLD. No transport is opened here."""
import os
from stop_latch_test.stop_latch import StopLatch, Rejected


class HoldBench(StopLatch):
    def __init__(self, journal, reader, send_hold, timeout=5.0):
        super().__init__(journal)
        self.reader, self.send_hold = reader, send_hold
        # One lock protects GPIO, cached heartbeat, test actions and serial write.
        self.lock = reader.lock
        self.phase = 'IDLE'
        self.target = None
        self.safety_serial = None
        self.sent_at = None
        self.sent_serial = None
        self.timeout = timeout

    def _record(self, event, detail=''):
        super()._record(event, detail)
        try:
            with self.journal.open('a') as journal:
                os.fsync(journal.fileno())
        except OSError:
            self.contact, self.latched, self.run = 'UNKNOWN', True, 'STOPPED'
            raise

    def _heartbeat(self):
        item = self.reader.messages.get('HEARTBEAT')
        if self.reader.state != 'READING' or item is None:
            return None, 'HEARTBEAT_UNAVAILABLE'
        stamp, hb = item
        if not 0 <= self.reader.clock() - stamp <= self.reader.timeout:
            return None, 'HEARTBEAT_STALE'
        if hb.autopilot != 3 or hb.type != 10:
            return None, 'NOT_ARDUPILOT_ROVER'
        source = (hb.get_srcSystem(), hb.get_srcComponent())
        if source != self.reader.source or (self.target and source != self.target):
            return None, 'AUTOPILOT_CHANGED'
        if hb.base_mode & 128:
            return None, 'ARMED'
        return (stamp, hb), None

    def command(self, action, actor='TEST_UI'):
        with self.lock:
            if action == 'arm_hold_once':
                self._check_input()
                hb, reason = self._heartbeat()
                if self.phase in ('ENABLED', 'PENDING', 'WAITING'):
                    reason = 'TEST_ALREADY_ENABLED'
                elif self.contact != 'RELEASED' or self.latched:
                    reason = 'RELEASE_AND_RESET_REQUIRED'
                elif hb and hb[1].custom_mode != 0:
                    reason = 'MANUAL_REQUIRED'
                if reason:
                    self._record('HOLD_ENABLE_REJECTED', reason)
                    raise Rejected(reason)
                self.target = self.reader.source
                self.safety_serial = self.reader.heartbeat_safety_serial
                self.phase = 'ENABLED'
                self._record('HOLD_ENABLED', f'One press; autopilot={self.target}')
                return self.snapshot()
            if action == 'reset' and self.phase in ('PENDING', 'WAITING'):
                self._record('RESET_REJECTED', 'HOLD transaction pending')
                raise Rejected('Wait for HOLD confirmation or timeout')
            if action not in ('reset',):
                self._record('ACTION_REJECTED', action)
                raise Rejected('Only RESET and arm_hold_once are supported')
            # RESET never writes serial, changes Pixhawk mode or enables another test.
            if self.phase == 'ENABLED':
                self.phase = 'IDLE'
                self._record('HOLD_CANCELLED', 'RESET before press')
            return super().command(action, actor)

    def input(self, contact):
        with self.lock:
            previous = self.contact
            super().input(contact)  # latch is set before any request or journal result
            if previous != contact and contact == 'PRESSED':
                if self.phase == 'ENABLED':
                    self.phase = 'PENDING'  # consumed even if safety gate subsequently fails
                    self._record('HOLD_PRESS', f'autopilot={self.target}')
                else:
                    self._record('HOLD_PRESS_IGNORED', 'Explicit enable required; no repeat request')
            if contact == 'UNKNOWN' and self.phase in ('ENABLED', 'PENDING'):
                self.phase = 'SKIPPED'
                self._record('HOLD_SKIPPED', 'GPIO_UNKNOWN')

    def tick(self, fd):
        """Called only by the serial owner's loop, including when no packets arrive."""
        with self.lock:
            self._check_input()
            if self.contact == 'UNKNOWN' and self.phase == 'ENABLED':
                self.phase = 'SKIPPED'
                self._record('HOLD_SKIPPED', 'GPIO_UNKNOWN_OR_STALE')
            if self.phase == 'PENDING':
                hb, reason = self._heartbeat()
                if reason is None and self.reader.heartbeat_safety_serial != self.safety_serial:
                    reason = 'DISARMED_OR_FRESHNESS_LOST_SINCE_ENABLE'
                if self.contact == 'UNKNOWN':
                    reason = 'GPIO_UNKNOWN'
                if reason:
                    self.phase = 'SKIPPED'
                    self._record('HOLD_SKIPPED', reason)
                    return
                self.phase = 'FAILED'  # exceptions can never retry this press
                self._record('HOLD_SEND_ATTEMPT', f'target={self.target}; custom_mode=4')
                try:
                    self.send_hold(fd, self.target)
                except (OSError, ValueError) as exc:
                    self._record('HOLD_SEND_FAILED', str(exc))
                    return
                self.sent_at = self.reader.clock()
                self.sent_serial = self.reader.heartbeat_serial
                self.phase = 'WAITING'
                self._record('HOLD_SENT', f'target={self.target}; heartbeat_serial={self.sent_serial}')
            elif self.phase == 'WAITING':
                now = self.reader.clock()
                hb, reason = self._heartbeat()
                if now - self.sent_at >= self.timeout:
                    self.phase = 'TIMEOUT'
                    self._record('HOLD_TIMEOUT', f'No qualifying HEARTBEAT within {self.timeout}s')
                elif (hb and self.reader.heartbeat_serial > self.sent_serial and
                      hb[0] > self.sent_at and hb[1].custom_mode == 4):
                    self.phase = 'CONFIRMED'
                    self._record('HOLD_CONFIRMED', f'target={self.target}; DISARMED; heartbeat_serial={self.reader.heartbeat_serial}')
                elif reason == 'ARMED':
                    self.phase = 'FAILED'
                    self._record('HOLD_CONFIRM_FAILED', 'ARMED; latch remains active')

    def snapshot(self):
        with self.lock:
            result = super().snapshot()
            hb, reason = self._heartbeat()
            result.update(hold_phase=self.phase, autopilot=self.target,
                          heartbeat_reason=reason, heartbeat_age_s=(self.reader.clock()-hb[0] if hb else None),
                          pixhawk_mode=(hb[1].custom_mode if hb else None),
                          pixhawk_disarmed=bool(hb), sent_monotonic=self.sent_at)
            return result


def hold_sender():
    # Construct packets only; this MAVLink object never owns/opens a transport.
    from pymavlink.dialects.v20 import ardupilotmega as mav
    encoder = mav.MAVLink(None, srcSystem=255, srcComponent=190)

    def send(fd, target):
        packet = mav.MAVLink_set_mode_message(target[0], mav.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, 4).pack(encoder)
        # Never retry a partial write: record failure and retain latch.
        if os.write(fd, packet) != len(packet):
            raise OSError('Partial HOLD packet write; no retry')
        encoder.seq = (encoder.seq + 1) % 256
    return send
