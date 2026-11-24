"""Run against a temporary owner copy with owner.patch and web-owner.patch."""
from contextlib import redirect_stderr
from io import StringIO
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock
import unittest
from uuid import uuid4

from pymavlink.dialects.v20 import ardupilotmega as mav

from disarm_control.engine import Rejected
from production_stop.web_bench_brake_link import WebBenchBrakeLink
from stop_latch_test.hold_runner import parse_args


class WebBenchCycleTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.
        self.pwm = 999
        self.inhibit = True
        self.packets = []
        self.reader = SimpleNamespace(lock=RLock(), clock=lambda: self.now)
        self.disarm = Mock()
        self.disarm._heartbeat.return_value = (10., SimpleNamespace(base_mode=0), (1, 1), 0, 0)
        self.stop = Mock(storage_fault=False, committed_latch=True)
        self.stop.core.snapshot.return_value = SimpleNamespace(stop_latch=True)
        self.stop.bench.disarm_pending.return_value = False
        self.link = WebBenchBrakeLink(
            self.reader, self.disarm, self.stop, mav.MAVLink(None, 255, 190),
            lambda: {'at': self.now, 'channels': {'8': self.pwm}},
            inhibited=lambda: self.inhibit, operator_id=1, write=self.write)

    def write(self, fd, raw):
        self.assertIsNone(fd)
        packet, = mav.MAVLink(None).parse_buffer(raw)
        self.assertEqual(packet.name, 'UGVBENCH01')
        self.packets.append(packet.value)
        return len(raw)

    def tick(self, elapsed=.11):
        self.now += elapsed
        self.link.observe_front('RELEASED', self.now)
        self.link.tick(None)

    def request(self, actor_id=1, request_id=None):
        return self.link.request_web_release(actor_id, 'operator', request_id or str(uuid4()))

    def ready(self):
        self.tick()
        self.pwm = 2000
        self.tick()

    def test_ch8_alone_never_releases_and_request_is_one_shot(self):
        self.ready()
        self.assertEqual(self.packets, [0, 0])
        self.assertFalse(self.link.release_available(2))
        self.assertFalse(self.request(2)['bench_release_accepted'])
        self.assertTrue(self.link.release_available(1))
        req = str(uuid4())
        result = self.request(request_id=req)
        self.assertEqual(result, {'request_id': req, 'bench_release_accepted': True,
                                  'stop_latch': True, 'reset_available': False})
        self.assertFalse(self.request(request_id=req)['bench_release_accepted'])
        self.assertEqual(self.packets, [0, 0])  # Web request only queues.
        self.tick()
        self.tick()
        self.assertEqual(self.packets, [0, 0, 2, 1])
        self.assertFalse(self.request()['bench_release_accepted'])
        self.assertFalse(self.link.traction_ready())
        self.assertFalse(self.link.reset_link_ready(self.now))

    def test_gpio_stop_after_release_prioritizes_hold(self):
        self.ready()
        self.assertTrue(self.request()['bench_release_accepted'])
        self.tick()
        self.link.observe_front('PRESSED', self.now + .01)
        self.tick()  # Bumper released before the next serial tick.
        self.assertEqual(self.packets[-2:], [2, 0])
        self.assertEqual(self.link.fault_reason, 'BENCH_GPIO_STOP')
        self.assertFalse(self.request()['bench_release_accepted'])

    def test_stop_after_http_request_before_serial_dispatch(self):
        self.ready()
        self.assertTrue(self.request()['bench_release_accepted'])
        self.link.observe_front('PRESSED', self.now + .01)
        self.tick()
        self.assertNotIn(2, self.packets)

    def test_expired_queued_request_and_changed_ch8(self):
        self.ready()
        self.assertTrue(self.request()['bench_release_accepted'])
        self.tick(.51)
        self.assertNotIn(2, self.packets)
        self.setUp()
        self.ready()
        self.assertTrue(self.request()['bench_release_accepted'])
        self.pwm = 999
        self.tick()
        self.pwm = 2000
        self.tick()
        self.assertNotIn(2, self.packets)

    def test_absolute_timeout_and_heartbeat_loss(self):
        self.ready()
        self.assertTrue(self.request()['bench_release_accepted'])
        self.tick()
        self.tick(2.9)
        self.tick(.11)
        self.assertEqual(self.packets[-3:], [2, 1, 0])
        self.setUp()
        self.ready()
        self.assertTrue(self.request()['bench_release_accepted'])
        self.tick()
        self.disarm._heartbeat.side_effect = Rejected('link gone')
        self.tick()
        self.assertEqual(self.packets[-1], 2)  # No new lease renewals on loss.
        self.disarm._heartbeat.side_effect = None
        self.tick()
        self.assertEqual(self.packets[-1], 0)
        self.assertFalse(self.request()['bench_release_accepted'])

    def test_requires_boot_binding_and_explicit_actor(self):
        args = ['--journal','unused','--web-disarm-socket','unused','--web-disarm-db','unused',
                '--web-disarm-client-uid','999','--web-disarm-client-gid','983',
                '--functional-brake-boot-id','boot','--brake-hold-only',
                '--externalnav-observe-only','--observe-brake-pwm',
                '--isolated-brake-cycle','VESC_ISOLATED_OPERATOR_PRESENT',
                '--bench-web-release-boot-id','boot']
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parse_args(args)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parse_args(args[:-2])
        self.assertEqual(parse_args(args + ['--bench-web-operator-id','1']).bench_web_operator_id, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
