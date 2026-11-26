"""All GPIO reads are fakes. No real pinctrl command is executed."""
import json
import subprocess
from threading import Event, Thread, TIMEOUT_MAX
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from production_stop import Contact, ResetConditions, StopController, Zone
from production_stop.front_gpio import (
    FrontGpioAdapter, PassivePinctrlSource, PinReading, SimulatedFrontSource,
)


class FrontAdapterTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.clock = lambda: self.now
        self.core = StopController(clock=self.clock)
        self.source = Mock(kind='FAKE')
        self.source.read.side_effect = lambda: PinReading('17: ip pu | hi', self.now)
        self.adapter = FrontGpioAdapter(self.core, self.source, clock=self.clock)

    def ready(self):
        self.adapter.poll()
        accepted, state = self.core.reset(ResetConditions(self.now, True, True, True, True))
        self.assertTrue(accepted)
        self.assertTrue(state.motion_permitted)
        self.now += .01

    def assert_unknown(self, state):
        self.assertEqual(dict(state.contacts)[Zone.FRONT], Contact.UNKNOWN)
        self.assertTrue(state.stop_latch)
        self.assertFalse(state.motion_permitted)

    def test_construction_has_no_io(self):
        with patch('subprocess.run') as run:
            PassivePinctrlSource()
            SimulatedFrontSource()
            self.source.read.assert_not_called()
            run.assert_not_called()

    def test_high_low_and_manual_reset(self):
        self.ready()
        self.source.read.side_effect = lambda: PinReading('17: ip pu | lo // GPIO17 = input', self.now)
        state = self.adapter.poll()
        self.assertEqual(dict(state.contacts)[Zone.FRONT], Contact.PRESSED)
        self.assertFalse(state.motion_permitted)
        self.now += .01
        self.source.read.side_effect = lambda: PinReading('17: ip pu | hi', self.now)
        self.assertTrue(self.adapter.poll().stop_latch)
        self.assertEqual(self.adapter.payload()['blocking_reasons'], ['STOP_LATCH'])
        self.assertTrue(self.core.reset(ResetConditions(self.now, True, True, True, True))[0])

    def test_wrong_mode_pin_pull_level_and_multiline(self):
        for output in ('17: op pu | hi', '17: ip pd | hi', '17: ip pn | lo',
                       '18: ip pu | hi', '17: a1 pu | hi', '17: ip pu | ??',
                       '17: ip pu | high', '', '17: ip pu | hi\n18: ip pu | hi',
                       '17:\nip pu | hi', '17: ip\rpu | hi',
                       '17: ip pu | hi junk', None, 1):
            with self.subTest(output=output):
                self.setUp()
                self.ready()
                self.source.read.side_effect = lambda: PinReading(output, self.now)
                self.assert_unknown(self.adapter.poll())
                self.assertEqual(self.adapter.payload()['front_signal']['reason'],
                                 'INVALID_GPIO_MODE_OR_DATA')

    def test_read_errors_revoke_permit(self):
        for error in (OSError('read'), PermissionError('denied'), ValueError('bad'),
                      subprocess.CalledProcessError(1, ['fake'])):
            with self.subTest(error=type(error).__name__):
                self.setUp()
                self.ready()
                self.source.read.side_effect = error
                self.assert_unknown(self.adapter.poll())
                self.assertEqual(self.adapter.payload()['front_signal']['reason'], 'READ_ERROR')

    def test_source_timeouts(self):
        for error in (TimeoutError(), subprocess.TimeoutExpired(['fake'], .01)):
            self.setUp()
            self.ready()
            self.source.read.side_effect = error
            self.assert_unknown(self.adapter.poll())
            self.assertEqual(self.adapter.payload()['front_signal']['reason'], 'READ_TIMEOUT')

    def test_hung_read_is_bounded_and_late_result_discarded(self):
        self.ready()
        entered, release, finished = Event(), Event(), Event()

        def hang():
            entered.set()
            try:
                release.wait()
                return PinReading('17: ip pu | hi', self.now)
            finally:
                finished.set()

        self.source.read.side_effect = hang
        self.adapter = FrontGpioAdapter(self.core, self.source, clock=self.clock, timeout_s=.02)
        try:
            before = time.monotonic()
            self.assert_unknown(self.adapter.poll())
            self.assertLess(time.monotonic() - before, 1.0)
            self.assertTrue(entered.is_set())
            self.assertEqual(self.adapter.payload()['front_signal']['reason'], 'READ_TIMEOUT')
            calls = self.source.read.call_count
            for _ in range(3):
                self.assert_unknown(self.adapter.poll())
            self.assertEqual(self.source.read.call_count, calls)
            self.assertEqual(self.adapter.payload()['front_signal']['reason'], 'READ_BUSY')
        finally:
            release.set()
            self.assertTrue(finished.wait(1))
        self.assert_unknown(self.core.snapshot())
        self.assertFalse(self.core.reset(ResetConditions(self.now, True, True, True, True))[0])

    def test_invalid_or_cached_timestamp(self):
        for stamp in (None, True, '10', [], {}, object(), 10j, float('nan'),
                      float('inf'), float('-inf'), 10**1000, -(10**1000),
                      -1e308, 1e308, 9.99, 10.02):
            with self.subTest(stamp_type=type(stamp).__name__):
                self.setUp()
                self.ready()
                self.source.read.side_effect = lambda: PinReading('17: ip pu | hi', stamp)
                self.assert_unknown(self.adapter.poll())

    def test_slow_completed_read_is_rejected(self):
        def slow():
            stamp = self.now
            self.now += .125
            return PinReading('17: ip pu | hi', stamp)

        self.ready()
        self.adapter = FrontGpioAdapter(self.core, self.source, clock=self.clock, timeout_s=.125)
        self.source.read.side_effect = slow
        self.assert_unknown(self.adapter.poll())

    def test_freshness_expiry_without_new_read(self):
        self.ready()
        self.now = 10.5
        payload = self.adapter.payload()
        self.assertEqual(payload['front_signal']['contact'], 'UNKNOWN')
        self.assertFalse(payload['front_signal']['fresh'])
        self.assertEqual(payload['front_signal']['age_s'], .5)
        self.assertEqual(payload['front_signal']['reason'], 'STALE')
        self.assertTrue(payload['stop_latch'])
        self.assertFalse(payload['motion_permitted'])
        self.assertEqual(self.source.read.call_count, 1)
        self.assertFalse(self.core.reset(ResetConditions(self.now, True, True, True, True))[0])

    def test_json_shape_and_uninstalled_zones(self):
        self.ready()
        payload = self.adapter.payload()
        self.assertEqual(json.loads(json.dumps(payload, allow_nan=False)), payload)
        self.assertEqual(payload['front_signal']['source'], 'FAKE')
        self.assertEqual(payload['front_signal']['bcm_gpio'], 17)
        self.assertTrue(payload['front_signal']['fresh'])
        for zone in ('REAR', 'LEFT', 'RIGHT'):
            self.assertEqual(payload['contacts'][zone], 'NOT_INSTALLED')
        self.assertFalse(payload['stop_latch'])

    def test_missing_sample(self):
        payload = self.adapter.payload()
        self.assertEqual(payload['front_signal']['reason'], 'NO_SAMPLE')
        self.assertFalse(payload['front_signal']['fresh'])
        self.assertTrue(payload['stop_latch'])
        self.source.read.assert_not_called()

    def test_core_shorter_expiry_is_respected(self):
        self.core = StopController(clock=self.clock, freshness_s=.1)
        self.adapter = FrontGpioAdapter(self.core, self.source, clock=self.clock)
        self.ready()
        self.now = 10.1 + .001
        payload = self.adapter.payload()
        self.assertEqual(payload['front_signal']['contact'], 'UNKNOWN')
        self.assertFalse(payload['front_signal']['fresh'])
        self.assertFalse(payload['motion_permitted'])

    def test_adapter_shorter_expiry_revokes_longer_core_permit(self):
        self.core = StopController(clock=self.clock, freshness_s=1.0)
        self.adapter = FrontGpioAdapter(self.core, self.source, clock=self.clock,
                                       freshness_s=.25)
        self.ready()
        self.now = 10.25
        payload = self.adapter.payload()
        self.assertFalse(payload['motion_permitted'])
        self.assertEqual(payload['front_signal']['reason'], 'STALE')
        self.assert_unknown(self.core.snapshot())

    def test_invalid_adapter_clock_never_raises(self):
        for value in (None, True, float('nan'), float('inf'), 10**1000):
            with self.subTest(clock_type=type(value).__name__):
                self.setUp()
                self.ready()
                self.adapter._clock = lambda: value
                self.assert_unknown(self.adapter.poll())
                json.dumps(self.adapter.payload(), allow_nan=False)

    def test_stale_acquisition_rejected_before_core(self):
        self.ready()

        def delayed():
            stamp = self.now
            self.now += .25
            return PinReading('17: ip pu | hi', stamp)

        self.adapter = FrontGpioAdapter(self.core, self.source, clock=self.clock,
                                       timeout_s=.5, freshness_s=.25)
        self.source.read.side_effect = delayed
        self.assert_unknown(self.adapter.poll())

    def test_duplicate_observation_is_not_a_fresh_permit(self):
        self.adapter.poll()
        self.core.reset(ResetConditions(self.now, True, True, True, True))
        self.assert_unknown(self.adapter.poll())
        self.assertFalse(self.adapter.payload()['front_signal']['fresh'])

    def test_adapter_clock_fault_is_permanent(self):
        self.ready()
        self.now = 9.0
        self.assert_unknown(self.adapter.poll())
        self.now = 11.0
        self.assert_unknown(self.adapter.poll())
        self.assertEqual(self.adapter.payload()['front_signal']['reason'], 'CLOCK_FAULT')

    def test_invalid_result_and_configuration(self):
        self.ready()
        self.source.read.side_effect = None
        self.source.read.return_value = 'hi'
        self.assert_unknown(self.adapter.poll())
        for value in (True, None, '1', 0, -1, float('nan'), float('inf'), 10**1000):
            for option in ('timeout_s', 'freshness_s'):
                with self.assertRaises(ValueError):
                    FrontGpioAdapter(self.core, self.source, **{option: value})

    def test_timeout_exceeding_platform_limit_is_rejected(self):
        for value in (1e308, TIMEOUT_MAX * 2):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    FrontGpioAdapter(self.core, self.source, timeout_s=value)
                with self.assertRaises(ValueError):
                    PassivePinctrlSource(timeout_s=value, runner=Mock())
        self.source.read.assert_not_called()

    def test_worker_creation_failure_revokes_permit(self):
        for target in ('Thread', 'Thread.start'):
            self.setUp()
            self.ready()
            with patch('production_stop.front_gpio.' + target, side_effect=RuntimeError('resource')):
                self.assert_unknown(self.adapter.poll())
            self.assertEqual(self.adapter.payload()['front_signal']['reason'], 'READ_ERROR')

    def test_wait_exception_keeps_pending_and_discards_result(self):
        self.ready()
        release, done = Event(), Event()
        self.addCleanup(release.set)
        self.source.read.side_effect = lambda: (release.wait(), PinReading('17: ip pu | hi', self.now))[1]
        wrapped = Mock(wraps=done)
        wrapped.wait.side_effect = OverflowError('platform wait failure')
        with patch('production_stop.front_gpio.Event', return_value=wrapped):
            self.assert_unknown(self.adapter.poll())
        self.assertEqual(self.adapter.payload()['front_signal']['reason'], 'READ_WAIT_ERROR')
        calls = self.source.read.call_count
        self.assert_unknown(self.adapter.poll())
        self.assertEqual(self.source.read.call_count, calls)
        release.set()
        self.assertTrue(done.wait(1))
        self.assert_unknown(self.core.snapshot())

    def test_repeated_hung_polls_do_not_create_more_workers(self):
        self.ready()
        release = Event()
        self.addCleanup(release.set)
        self.source.read.side_effect = lambda: (release.wait(), PinReading('17: ip pu | hi', self.now))[1]
        self.adapter = FrontGpioAdapter(self.core, self.source, clock=self.clock, timeout_s=.01)
        with patch('production_stop.front_gpio.Thread', wraps=Thread) as worker:
            self.assert_unknown(self.adapter.poll())
            for _ in range(100):
                self.assert_unknown(self.adapter.poll())
            self.assertEqual(worker.call_count, 1)
        release.set()
        self.assertTrue(self.adapter._pending.wait(1))

    def test_old_released_cannot_override_newer_pressed_or_unknown(self):
        for newer in (Contact.PRESSED, Contact.UNKNOWN):
            with self.subTest(newer=newer):
                self.setUp()
                self.ready()

                def reordered():
                    old_stamp = self.now
                    self.now += .01
                    self.core.update(Zone.FRONT, newer, observed_at=self.now)
                    return PinReading('17: ip pu | hi', old_stamp)

                self.source.read.side_effect = reordered
                self.assert_unknown(self.adapter.poll())
                payload = self.adapter.payload()
                self.assertFalse(payload['front_signal']['fresh'])
                self.assertEqual(payload['front_signal']['contact'], 'UNKNOWN')

    def test_timed_out_released_never_reappears_after_new_pressed(self):
        self.ready()
        release = Event()
        self.addCleanup(release.set)
        old_stamp = self.now
        self.source.read.side_effect = lambda: (release.wait(), PinReading('17: ip pu | hi', old_stamp))[1]
        self.adapter = FrontGpioAdapter(self.core, self.source, clock=self.clock, timeout_s=.01)
        self.assert_unknown(self.adapter.poll())
        self.now += .01
        self.core.update(Zone.FRONT, Contact.PRESSED)
        release.set()
        self.assertTrue(self.adapter._pending.wait(1))
        self.assertEqual(dict(self.core.snapshot().contacts)[Zone.FRONT], Contact.PRESSED)
        self.now += .01
        self.source.read.side_effect = lambda: PinReading('17: ip pu | lo', self.now)
        state = self.adapter.poll()
        self.assertEqual(dict(state.contacts)[Zone.FRONT], Contact.PRESSED)
        self.assertTrue(state.stop_latch)
        self.assertFalse(state.motion_permitted)

    def test_json_extreme_clock_difference_is_finite(self):
        self.now = -1e308
        self.adapter.poll()
        self.now = 1e308
        payload = self.adapter.payload()
        json.dumps(payload, allow_nan=False)
        self.assertIsNone(payload['front_signal']['age_s'])
        self.assertFalse(payload['front_signal']['fresh'])
        self.assertFalse(payload['motion_permitted'])

    def test_adapter_clock_exception_does_not_read_again(self):
        self.ready()
        calls = self.source.read.call_count

        def broken_clock():
            raise OSError('clock failure')

        self.adapter._clock = broken_clock
        self.assert_unknown(self.adapter.poll())
        self.assertEqual(self.source.read.call_count, calls)
        self.assertEqual(self.adapter.payload()['front_signal']['reason'], 'CLOCK_FAULT')

    def test_clock_regression_during_read_revokes_permit(self):
        self.ready()

        def regress():
            self.now -= 1
            return PinReading('17: ip pu | hi', self.now)

        self.source.read.side_effect = regress
        self.assert_unknown(self.adapter.poll())
        self.assertEqual(self.adapter.payload()['front_signal']['reason'], 'CLOCK_FAULT')

    def test_clock_fault_payload_masks_previously_released_signal(self):
        self.ready()
        self.adapter._clock = lambda: float('nan')
        payload = self.adapter.payload()
        json.dumps(payload, allow_nan=False)
        self.assertFalse(payload['front_signal']['fresh'])
        self.assertEqual(payload['front_signal']['contact'], 'UNKNOWN')
        self.assertTrue(payload['stop_latch'])


class SourceTests(unittest.TestCase):
    def test_passive_command_is_get_only(self):
        runner = Mock(return_value=SimpleNamespace(stdout='17: ip pu | hi'))
        source = PassivePinctrlSource(runner=runner, clock=lambda: 10.0)
        self.assertEqual(source.read(), PinReading('17: ip pu | hi', 10.0))
        runner.assert_called_once_with(['/usr/bin/pinctrl', 'get', '17'],
                                       capture_output=True, text=True, check=True, timeout=.1)

    def test_simulator_is_explicit_and_has_no_real_fallback(self):
        with patch('subprocess.run', side_effect=AssertionError('Real I/O forbidden')):
            source = SimulatedFrontSource(clock=lambda: 10.0)
            self.assertEqual(source.kind, 'SIMULATOR')
            self.assertEqual(source.read().output, '17: ip pu | hi')
            source.high = False
            self.assertEqual(source.read().output, '17: ip pu | lo')
            source.high = 'bad'
            with self.assertRaises(ValueError):
                source.read()


if __name__ == '__main__':
    unittest.main()
