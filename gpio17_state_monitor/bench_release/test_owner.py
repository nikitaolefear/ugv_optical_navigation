"""Offline tests, imported against an owner copy with owner.patch applied."""
import unittest
from contextlib import redirect_stderr
from io import StringIO
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

from pymavlink.dialects.v20 import ardupilotmega as mav

from disarm_control.engine import Rejected
from production_stop.bench_brake_link import BenchBrakeLink
from stop_latch_test.hold_runner import parse_args


class BenchCycleTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.pwm = 999
        self.inhibit = True
        self.armed = False
        self.packets = []
        self.reader = SimpleNamespace(lock=RLock(), clock=lambda: self.now)
        self.disarm = Mock()
        self.disarm._heartbeat.side_effect = lambda now: (
            now, SimpleNamespace(base_mode=128 if self.armed else 0), (1, 1), 1, 0)
        self.stop = Mock(storage_fault=False, committed_latch=True)
        self.stop.core.snapshot.return_value = SimpleNamespace(stop_latch=True)
        self.stop.bench.disarm_pending.return_value = False
        self.link = BenchBrakeLink(
            self.reader, self.disarm, self.stop, mav.MAVLink(None, 255, 190),
            lambda: {'at': self.now, 'channels': {'8': self.pwm}},
            inhibited=lambda: self.inhibit, write=self.write)

    def write(self, fd, raw):
        self.assertIsNone(fd)
        packet, = mav.MAVLink(None).parse_buffer(raw)
        self.assertEqual(packet.name, 'UGVBENCH01')
        self.packets.append(packet.value)
        return len(raw)

    def tick(self, elapsed=.11, *, refresh_front=True):
        self.now += elapsed
        if refresh_front:
            self.link.observe_front('RELEASED', self.now)
        self.link.tick(None)

    def release(self):
        self.tick()
        self.assertEqual(self.packets[-1], 0)
        self.pwm = 2000
        self.tick()
        self.assertEqual(self.packets[-1], 2)

    def test_requires_manual_apply_before_release(self):
        self.pwm = 2000
        self.tick()
        self.tick()
        self.assertEqual(self.packets, [0, 0])
        self.pwm = 999
        self.release()
        self.tick()
        self.assertEqual(self.packets[-1], 1)
        self.assertFalse(self.link.traction_ready())
        self.assertFalse(self.link.reset_link_ready(self.now))

    def test_stop_edge_between_ticks_is_sticky(self):
        self.release()
        self.link.observe_front('PRESSED', self.now + .02)
        self.link.observe_front('RELEASED', self.now + .03)
        self.tick()
        self.assertEqual(self.packets[-1], 0)
        self.assertEqual(self.link.fault_reason, 'BENCH_GPIO_STOP')
        self.pwm = 999
        self.tick()
        self.pwm = 2000
        self.tick()
        self.assertEqual(self.packets.count(2), 1)

    def test_deadline_is_not_extended_by_keep(self):
        self.release()
        self.tick(2.9)
        self.assertEqual(self.packets[-1], 1)
        self.link.observe_front('PRESSED', self.link.release_deadline + .001)
        self.tick(.11)
        self.assertEqual(self.packets[-1], 0)
        self.assertEqual(self.link.fault_reason, 'BENCH_WINDOW_EXPIRED')

    def test_heartbeat_loss_stops_keep_and_recovery_stays_hold(self):
        self.release()
        self.disarm._heartbeat.side_effect = Rejected('simulated disconnect')
        self.tick()
        self.assertEqual(self.packets, [0, 2])
        self.disarm._heartbeat.side_effect = None
        self.disarm._heartbeat.return_value = (0, SimpleNamespace(base_mode=0), (1, 1), 1, 0)
        self.tick()
        self.assertEqual(self.packets, [0, 2, 0])

    def test_stale_gpio_and_inhibit_cancel(self):
        self.release()
        self.tick(.51, refresh_front=False)
        self.assertEqual(self.packets[-1], 0)
        self.setUp()
        self.release()
        self.inhibit = False
        self.tick()
        self.assertEqual(self.packets[-1], 0)

    def test_cli_requires_hold_only_and_observe_only(self):
        args = ['--journal', 'unused', '--web-disarm-socket', 'unused', '--web-disarm-db', 'unused',
                '--web-disarm-client-uid', '999', '--web-disarm-client-gid', '983',
                '--functional-brake-boot-id', 'boot', '--brake-hold-only',
                '--externalnav-observe-only', '--observe-brake-pwm',
                '--isolated-brake-cycle', 'VESC_ISOLATED_OPERATOR_PRESENT']
        self.assertTrue(parse_args(args).isolated_brake_cycle)
        for invalid in ([s for s in args if s != '--brake-hold-only'],
                        args + ['--web-arm-boot-id', 'boot'],
                        [s.replace('--externalnav-observe-only', '--externalnav-tx') for s in args]):
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                parse_args(invalid)


if __name__ == '__main__':
    unittest.main(verbosity=2)
