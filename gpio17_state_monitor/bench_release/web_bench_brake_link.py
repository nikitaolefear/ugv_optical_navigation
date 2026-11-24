"""One Web bench release. Production RESET and InterlockIO are not involved."""

from disarm_control.engine import Rejected, finite
from disarm_control.gateway import validate_actor
from .bench_brake_link import BenchBrakeLink


class WebBenchBrakeLink(BenchBrakeLink):
    dispatch_s = .5

    def __init__(self, *args, operator_id, **kwargs):
        super().__init__(*args, **kwargs)
        if type(operator_id) is not int or operator_id <= 0:
            raise ValueError('Web bench operator must be an explicit user ID')
        self.operator_id = operator_id
        self.request_deadline = None

    def _available(self, now):
        if self.phase != 'BENCH_READY' or self.request_id is not None:
            return False
        if not finite(now):
            self._finish('BENCH_CLOCK_INVALID')
            return False
        try:
            heartbeat = self._heartbeat(now)
        except Rejected:
            return False
        return (self._inputs_available(now, heartbeat) == 'RELEASE' and
                self.last_state == self.HOLD and self.last_sent is not None and
                0 <= now - self.last_sent < .35)

    def release_available(self, actor_id):
        with self.reader.lock:
            return actor_id == self.operator_id and self._available(self.reader.clock())

    def request_web_release(self, actor_id, actor, request_id):
        validate_actor(actor_id, actor, request_id)
        with self.reader.lock:
            now = self.reader.clock()
            accepted = actor_id == self.operator_id and self._available(now)
            if accepted:
                # Consume before delivery. Losing the HTTP/RPC reply grants no retry.
                self.request_id = request_id
                self.request_deadline = now + self.dispatch_s
                self.stop.record_brake_result('BENCH_WEB_RELEASE_QUEUED', request_id,
                                              'PHYSICAL_POSITION_UNCONFIRMED')
                if self.stop.storage_fault:
                    self._finish('BENCH_STOP_JOURNAL_FAILED')
                    accepted = False
            return {'bench_release_accepted': accepted, 'request_id': request_id,
                    'stop_latch': self.stop.core.snapshot().stop_latch, 'reset_available': False}

    def _release_requested(self, now, position):
        if self.request_deadline is None:
            return False
        if (now >= self.request_deadline or position != 'RELEASE' or
                self.last_sent is None or not 0 <= now - self.last_sent < .35):
            self._finish('BENCH_WEB_REQUEST_EXPIRED_OR_RC8_CHANGED')
            return False
        return True

    def _release_sent(self):
        self.stop.record_brake_result('BENCH_WEB_RELEASE_SENT', self.request_id,
                                      'PHYSICAL_POSITION_UNCONFIRMED')
