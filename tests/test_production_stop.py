import unittest
import json
from production_stop import BumperInput, Contact, ResetConditions, StopController, Zone, snapshot_payload


# Synthetic pins for tests only; no wiring is assigned to planned zones.
FOUR_INPUTS = tuple(BumperInput(zone, pin) for zone, pin in
                    zip(Zone, (17, 18, 22, 23)))


class StopTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.stop = StopController(clock=lambda: self.now, inputs=FOUR_INPUTS)

    def conditions(self, **changes):
        values = dict(observed_at=self.now, disarmed=True, stationary=True,
                      hold_confirmed=True, stop_transaction_idle=True)
        values.update(changes)
        return ResetConditions(**values)

    def release(self):
        for zone in Zone:
            self.stop.update(zone, Contact.RELEASED)

    def ready(self):
        self.release()
        self.assertTrue(self.stop.reset(self.conditions())[0])
        self.assertTrue(self.stop.snapshot().motion_permitted)

    def test_startup_and_explicit_reset(self):
        self.assertTrue(self.stop.snapshot().stop_latch)
        self.assertFalse(self.stop.reset(self.conditions())[0])
        self.release()
        self.assertFalse(self.stop.snapshot().motion_permitted)
        self.assertTrue(self.stop.reset(self.conditions())[0])

    def test_hmi_payload_is_json_compatible_and_detached(self):
        state = self.stop.snapshot()
        payload = snapshot_payload(state)
        self.assertEqual(json.loads(json.dumps(payload)), payload)
        self.assertEqual(set(payload['contacts']), {zone.value for zone in Zone})
        payload['contacts']['FRONT'] = 'RELEASED'
        payload['reasons'].clear()
        self.assertEqual(dict(state.contacts)[Zone.FRONT], Contact.UNKNOWN)
        self.assertTrue(state.reasons)

    def test_reset_only_clears_latch_and_does_not_refresh_inputs(self):
        self.release()
        before = self.stop.snapshot().contacts
        accepted, state = self.stop.reset(self.conditions())
        self.assertTrue(accepted)
        self.assertEqual(state.contacts, before)
        self.now += .5
        self.assertFalse(self.stop.tick().motion_permitted)

    def test_each_zone_press_unknown_and_recovery(self):
        for zone in Zone:
            for value in (Contact.PRESSED, Contact.UNKNOWN, 'invalid'):
                with self.subTest(zone=zone, value=value):
                    self.now += .01
                    self.ready()
                    self.now += .01
                    self.assertFalse(self.stop.update(zone, value).motion_permitted)
                    self.assertFalse(self.stop.reset(self.conditions())[0])
                    self.now += .01
                    self.release()
                    self.assertTrue(self.stop.snapshot().stop_latch)

    def test_single_stale_input_and_recovery(self):
        self.ready()
        self.now += .4
        for zone in (Zone.FRONT, Zone.REAR, Zone.LEFT):
            self.stop.update(zone, Contact.RELEASED)
        self.now += .1
        state = self.stop.tick()
        self.assertIn('RIGHT:STALE', state.reasons)
        self.assertFalse(state.motion_permitted)
        self.stop.update(Zone.RIGHT, Contact.RELEASED)
        self.assertTrue(self.stop.snapshot().stop_latch)

    def test_gap_detected_on_update_without_tick(self):
        self.ready()
        self.now += .6
        self.release()
        self.assertTrue(self.stop.snapshot().stop_latch)

    def test_reset_conditions(self):
        self.release()
        for field in ('disarmed', 'stationary', 'hold_confirmed', 'stop_transaction_idle'):
            self.assertFalse(self.stop.reset(self.conditions(**{field: False}))[0])
        for stamp in (self.now - .5, self.now + 1, float('nan')):
            self.assertFalse(self.stop.reset(self.conditions(observed_at=stamp))[0])
        self.assertFalse(self.stop.reset(None)[0])

    def test_invalid_sample_times(self):
        for stamp in (9, 11, float('nan'), float('inf'), 'bad', True, 10):
            with self.subTest(stamp=stamp):
                self.stop = StopController(clock=lambda: self.now, inputs=FOUR_INPUTS)
                self.ready()
                state = self.stop.update(Zone.FRONT, Contact.RELEASED, observed_at=stamp)
                self.assertFalse(state.motion_permitted)

    def test_clock_regression_is_permanent_fault(self):
        self.ready()
        self.now -= 1
        self.assertIn('CLOCK_FAULT', self.stop.snapshot().reasons)
        self.now += 2
        self.release()
        self.assertFalse(self.stop.reset(self.conditions())[0])

    def test_freshness_boundary_for_each_zone(self):
        for expired_zone in Zone:
            with self.subTest(zone=expired_zone):
                self.now = 10.0
                self.stop = StopController(clock=lambda: self.now, inputs=FOUR_INPUTS)
                self.ready()
                self.now = 10.25
                for zone in Zone:
                    if zone != expired_zone:
                        self.stop.update(zone, Contact.RELEASED)
                self.now = 10.499
                self.assertTrue(self.stop.tick().motion_permitted)
                self.now = 10.5
                state = self.stop.tick()
                self.assertFalse(state.motion_permitted)
                self.assertEqual(dict(state.contacts)[expired_zone], Contact.UNKNOWN)
                self.assertIn(f'{expired_zone.value}:STALE', state.reasons)
                self.assertFalse(self.stop.reset(self.conditions())[0])

    def test_reset_requires_literal_true(self):
        self.release()
        for field in ('disarmed', 'stationary', 'hold_confirmed', 'stop_transaction_idle'):
            for value in (None, 1, 'true'):
                with self.subTest(field=field, value=value):
                    self.assertFalse(self.stop.reset(self.conditions(**{field: value}))[0])

    def test_nonfinite_clock_fault_survives_recovery(self):
        for value in (float('nan'), float('inf'), float('-inf')):
            with self.subTest(clock=value):
                self.now = 10.0
                self.stop = StopController(clock=lambda: self.now, inputs=FOUR_INPUTS)
                self.ready()
                self.now = value
                state = self.stop.tick()
                self.assertIn('CLOCK_FAULT', state.reasons)
                self.assertFalse(state.motion_permitted)
                self.now = 11.0
                self.release()
                self.assertFalse(self.stop.reset(self.conditions())[0])

    def test_acquisition_time_is_not_delivery_time(self):
        self.ready()
        self.now = 10.25
        self.stop.update(Zone.FRONT, Contact.RELEASED, observed_at=10.125)
        for zone in (Zone.REAR, Zone.LEFT, Zone.RIGHT):
            self.stop.update(zone, Contact.RELEASED)
        self.now = 10.625
        state = self.stop.tick()
        self.assertIn('FRONT:STALE', state.reasons)
        self.assertFalse(state.motion_permitted)

    def test_bad_configuration_and_zone(self):
        for timeout in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                StopController(timeout)
        self.ready()
        with self.assertRaises(ValueError):
            self.stop.update('NO_ZONE', Contact.RELEASED)
        self.assertTrue(self.stop.snapshot().stop_latch)


class FrontOnlyTests(unittest.TestCase):
    conditions = StopTests.conditions

    def setUp(self):
        self.now = 10.0
        self.stop = StopController(clock=lambda: self.now)

    def ready(self):
        self.stop.update(Zone.FRONT, Contact.RELEASED)
        self.assertTrue(self.stop.reset(self.conditions())[0])

    def test_default_wiring_and_missing_front(self):
        state = self.stop.snapshot()
        self.assertEqual(state.installed_inputs, (BumperInput(Zone.FRONT, 17),))
        self.assertEqual(state.reasons, ('FRONT:MISSING',))
        self.assertEqual(dict(state.contacts)[Zone.FRONT], Contact.UNKNOWN)
        self.assertFalse(state.motion_permitted)
        self.assertFalse(self.stop.reset(self.conditions())[0])

    def test_uninstalled_zones_are_never_released_or_required(self):
        self.ready()
        state = self.stop.snapshot()
        self.assertTrue(state.motion_permitted)
        self.assertFalse(state.reasons)
        for zone in (Zone.REAR, Zone.LEFT, Zone.RIGHT):
            self.assertEqual(dict(state.contacts)[zone], Contact.NOT_INSTALLED)
        payload = snapshot_payload(state)
        self.assertEqual(payload['installed_inputs'], {'FRONT': {'bcm_gpio': 17}})
        self.assertEqual(payload['contacts']['REAR'], 'NOT_INSTALLED')

    def test_press_unknown_and_release_require_manual_reset(self):
        for value in (Contact.PRESSED, Contact.UNKNOWN, Contact.NOT_INSTALLED, 'bad'):
            with self.subTest(value=value):
                self.now += .01
                self.ready()
                self.now += .01
                state = self.stop.update(Zone.FRONT, value)
                self.assertTrue(state.stop_latch)
                self.assertFalse(state.motion_permitted)
                self.assertFalse(self.stop.reset(self.conditions())[0])
                self.now += .01
                self.stop.update(Zone.FRONT, Contact.RELEASED)
                self.assertTrue(self.stop.snapshot().stop_latch)
                self.assertTrue(self.stop.reset(self.conditions())[0])

    def test_stale_front_blocks_reset(self):
        self.ready()
        self.now += .5
        state = self.stop.tick()
        self.assertEqual(state.reasons, ('FRONT:STALE',))
        self.assertFalse(state.motion_permitted)
        self.assertFalse(self.stop.reset(self.conditions())[0])

    def test_reset_requires_all_fresh_safety_confirmations(self):
        self.stop.update(Zone.FRONT, Contact.RELEASED)
        for field in ('disarmed', 'stationary', 'hold_confirmed', 'stop_transaction_idle'):
            self.assertFalse(self.stop.reset(self.conditions(**{field: False}))[0])
        self.assertFalse(self.stop.reset(self.conditions(observed_at=9.5))[0])
        self.assertTrue(self.stop.reset(self.conditions())[0])

    def test_unconfigured_updates_cannot_install_zones(self):
        self.ready()
        with self.assertRaises(ValueError):
            self.stop.update(Zone.REAR, Contact.RELEASED)
        state = self.stop.snapshot()
        self.assertTrue(state.stop_latch)
        self.assertEqual(dict(state.contacts)[Zone.REAR], Contact.NOT_INSTALLED)

    def test_reject_empty_duplicate_or_invalid_configuration(self):
        for inputs in ((), (BumperInput(Zone.FRONT, 17),) * 2,
                       (BumperInput(Zone.FRONT, 17), BumperInput(Zone.REAR, 17)),
                       ('FRONT',)):
            with self.subTest(inputs=inputs), self.assertRaises(ValueError):
                StopController(inputs=inputs)
        for zone, pin in (('BAD', 17), (Zone.FRONT, True), (Zone.FRONT, -1),
                          (Zone.FRONT, 28)):
            with self.subTest(zone=zone, pin=pin), self.assertRaises(ValueError):
                BumperInput(zone, pin)

    def test_expansion_requires_data_from_every_configured_zone(self):
        self.stop = StopController(clock=lambda: self.now, inputs=FOUR_INPUTS)
        self.stop.update(Zone.FRONT, Contact.RELEASED)
        self.assertFalse(self.stop.reset(self.conditions())[0])
        self.assertEqual(dict(self.stop.snapshot().contacts)[Zone.REAR], Contact.UNKNOWN)
        for zone in (Zone.REAR, Zone.LEFT, Zone.RIGHT):
            self.stop.update(zone, Contact.RELEASED)
        self.assertTrue(self.stop.reset(self.conditions())[0])
        self.assertNotIn(Contact.NOT_INSTALLED, dict(self.stop.snapshot().contacts).values())


class TimestampRegressionTests(unittest.TestCase):
    setUp = FrontOnlyTests.setUp
    ready = FrontOnlyTests.ready
    conditions = StopTests.conditions

    def assert_blocked(self, state):
        self.assertTrue(state.stop_latch)
        self.assertFalse(state.motion_permitted)
        self.assertTrue(self.stop.snapshot().stop_latch)
        self.assertFalse(self.stop.snapshot().motion_permitted)

    def test_pressed_oversized_timestamp_revokes_existing_permit(self):
        self.ready()
        self.assertTrue(self.stop.snapshot().motion_permitted)
        state = self.stop.update(Zone.FRONT, Contact.PRESSED, observed_at=10**1000)
        self.assert_blocked(state)
        self.assertEqual(dict(state.contacts)[Zone.FRONT], Contact.UNKNOWN)

    def test_unknown_oversized_timestamp_revokes_existing_permit(self):
        self.ready()
        state = self.stop.update(Zone.FRONT, Contact.UNKNOWN, observed_at=10**1000)
        self.assert_blocked(state)

    def test_invalid_sample_timestamp_matrix(self):
        stamps = (None, True, False, '10', b'10', [], {}, object(), 10j,
                  float('nan'), float('inf'), float('-inf'),
                  10**1000, -(10**1000), 1e308, -1e308, 10.01, 9.5, 10.0)
        for contact in (Contact.PRESSED, Contact.UNKNOWN, Contact.RELEASED):
            for index, stamp in enumerate(stamps):
                with self.subTest(contact=contact, stamp_index=index):
                    self.setUp()
                    self.ready()
                    self.assert_blocked(self.stop.update(Zone.FRONT, contact,
                                                        observed_at=stamp))
                    self.assertEqual(dict(self.stop.snapshot().contacts)[Zone.FRONT],
                                     Contact.UNKNOWN)

    def test_invalid_reset_timestamp_matrix(self):
        stamps = (None, True, False, '10', [], {}, object(), 10j,
                  float('nan'), float('inf'), float('-inf'),
                  10**1000, -(10**1000), 1e308, -1e308, 10.01, 9.5)
        for index, stamp in enumerate(stamps):
            with self.subTest(stamp_index=index):
                self.setUp()
                self.ready()
                before = self.stop.snapshot().contacts
                accepted, state = self.stop.reset(self.conditions(observed_at=stamp))
                self.assertFalse(accepted)
                self.assert_blocked(state)
                self.assertEqual(state.contacts, before)

    def test_invalid_reset_evidence_revokes_existing_permit(self):
        for field in ('disarmed', 'stationary', 'hold_confirmed', 'stop_transaction_idle'):
            for value in (False, None, 1, 'true', object()):
                with self.subTest(field=field, value=value):
                    self.setUp()
                    self.ready()
                    accepted, state = self.stop.reset(self.conditions(**{field: value}))
                    self.assertFalse(accepted)
                    self.assert_blocked(state)
        for value in (None, {}, True, object()):
            self.setUp()
            self.ready()
            accepted, state = self.stop.reset(value)
            self.assertFalse(accepted)
            self.assert_blocked(state)

    def test_valid_timestamp_boundaries(self):
        for stamp, expected in ((10.0, True), (9.500001, True),
                                (9.5, False), (10.000001, False)):
            with self.subTest(stamp=stamp):
                self.setUp()
                self.stop.update(Zone.FRONT, Contact.RELEASED, observed_at=stamp)
                accepted, state = self.stop.reset(self.conditions(observed_at=stamp))
                self.assertEqual(accepted, expected)
                self.assertEqual(state.motion_permitted, expected)

    def test_bad_sample_recovery_needs_fresh_release_and_manual_reset(self):
        self.ready()
        self.assert_blocked(self.stop.update(Zone.FRONT, Contact.PRESSED,
                                            observed_at=10**1000))
        self.assertFalse(self.stop.reset(self.conditions())[0])
        self.now += .01
        self.assert_blocked(self.stop.update(Zone.FRONT, Contact.RELEASED))
        self.assertTrue(self.stop.reset(self.conditions())[0])

    def test_clock_exception_and_invalid_values_fail_closed(self):
        for value in (10**1000, None, '10', True):
            with self.subTest(clock=value):
                self.setUp()
                self.ready()
                self.now = value
                state = self.stop.tick()
                self.assert_blocked(state)
                self.assertIn('CLOCK_FAULT', state.reasons)
                self.now = 11.0
                self.stop.update(Zone.FRONT, Contact.RELEASED)
                self.assertFalse(self.stop.reset(self.conditions())[0])

        self.setUp()
        self.ready()

        def broken_clock():
            raise OverflowError('clock conversion failed')

        self.stop._clock = broken_clock
        self.assert_blocked(self.stop.tick())

    def test_numeric_subclasses_are_rejected_without_running_conversion(self):
        class BadFloat(float):
            def __sub__(self, other):
                raise AssertionError('untrusted arithmetic executed')

            def __rsub__(self, other):
                raise AssertionError('untrusted arithmetic executed')

        self.ready()
        self.assert_blocked(self.stop.update(Zone.FRONT, Contact.RELEASED,
                                            observed_at=BadFloat(10.1)))
        accepted, state = self.stop.reset(self.conditions(observed_at=BadFloat(10)))
        self.assertFalse(accepted)
        self.assert_blocked(state)


if __name__ == '__main__':
    unittest.main()
