"""One isolated brake cycle. Install only in the temporary bench owner tree."""

from disarm_control.engine import Rejected, finite
from .brake_link import BrakeLink


class BenchBrakeLink(BrakeLink):
    wire_name = b'UGVBENCH01'
    setup_s = 30.0
    release_s = 3.0

    def __init__(self, reader, disarm, stop, encoder, rc_snapshot, *, inhibited, **kwargs):
        super().__init__(reader, disarm, stop, encoder, rc_snapshot,
                         hold_only=True, **kwargs)
        self.inhibited = inhibited
        self.setup_deadline = reader.clock() + self.setup_s
        self.release_deadline = None
        self.front_at = None
        self.phase = 'BENCH_WAIT_APPLY'

    def _finish(self, reason):
        if self.phase != 'BENCH_FINISHED':
            self.phase, self.fault_reason = 'BENCH_FINISHED', reason

    def observe_front(self, contact, observed_at):
        # The owner calls this under the reader lock before any journal I/O.
        if contact == 'RELEASED':
            self.front_at = observed_at
        else:
            if contact == 'PRESSED':
                expired = self.release_deadline is not None and observed_at >= self.release_deadline
                self._finish('BENCH_WINDOW_EXPIRED' if expired else 'BENCH_GPIO_STOP')
            elif self.front_at is not None:
                self._finish('BENCH_GPIO_UNKNOWN')
            self.front_at = None

    def _heartbeat(self, now):
        try:
            return super()._heartbeat(now)
        except Rejected:
            if self.phase != 'BENCH_WAIT_APPLY':
                self._finish('BENCH_HEARTBEAT_LOST')
            raise

    def _fail(self, reason):
        self._finish(reason)
        self.stop.latch_for_brake()

    def _rc_position(self, now):
        row = self.rc_snapshot()
        if not row or not finite(row.get('at')) or not 0 <= now - row['at'] < 1.5:
            return None
        pwm = row['channels'].get('8')
        if type(pwm) is not int or not 800 <= pwm <= 2200:
            return None
        if abs(pwm - 999) <= 75:
            return 'APPLY'
        if abs(pwm - 2000) <= 75:
            return 'RELEASE'
        return 'BETWEEN'

    def _state(self, now, hb):
        if self.phase == 'BENCH_FINISHED':
            return self.HOLD
        if (hb.base_mode & 128 or not self.inhibited() or self.stop.storage_fault or
                not self.stop.committed_latch or not self.stop.core.snapshot().stop_latch or
                self.stop.bench.disarm_pending()):
            self._finish('BENCH_PRECONDITION_LOST')
            return self.HOLD
        deadline = self.release_deadline if self.release_deadline is not None else self.setup_deadline
        if now >= deadline:
            self._finish('BENCH_WINDOW_EXPIRED')
            return self.HOLD
        position = self._rc_position(now)
        if self.front_at is None or not 0 <= now - self.front_at < .5 or position is None:
            if self.phase != 'BENCH_WAIT_APPLY':
                self._finish('BENCH_INPUT_STALE')
            return self.HOLD
        if self.phase == 'BENCH_WAIT_APPLY':
            if position == 'APPLY':
                self.phase = 'BENCH_READY'
            return self.HOLD
        if self.phase == 'BENCH_READY':
            if position == 'RELEASE' and self.last_state == self.HOLD and self.last_sent is not None:
                self.phase = 'BENCH_RELEASE'
                self.release_deadline = now + self.release_s
                return self.RELEASE
            return self.HOLD
        if position != 'RELEASE':
            self._finish('BENCH_CH8_LEFT_RELEASE')
            return self.HOLD
        return self.KEEP

    def _release_sent(self):
        # A serial write is not Lua receipt or brake position feedback.
        self.stop.record_brake_result('BENCH_RELEASE_SENT', None, 'PHYSICAL_POSITION_UNCONFIRMED')
