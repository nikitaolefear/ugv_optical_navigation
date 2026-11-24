"""Exercise existing MAVLink decoders and HMI with synthetic input, offline."""
from contextlib import closing
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import Mock
from uuid import uuid4

from pymavlink.dialects.v20 import ardupilotmega as mav

from test_candidate import BASES, ROOT, StopTests, paths


def owner_reader_type():
    # The frontend and the owner have distinct packages named web_hmi.
    source = BASES['owner_externalnav'] / 'owner/web_hmi/pixhawk_telemetry.py'
    spec = spec_from_file_location('owner_pixhawk_telemetry', source)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PixhawkTelemetry


class StateMonitorTests(unittest.TestCase):
    def setUp(self):
        from production_stop.state_monitor import StateMonitor
        from stop_latch_test.diagnostics import Diagnostics

        self.now = 10.0
        self.opener = Mock(side_effect=AssertionError('Opening serial would violate the test'))
        self.write = Mock(side_effect=AssertionError('Writing serial would violate the test'))
        self.reader = owner_reader_type()(clock=lambda: self.now, opener=self.opener)
        self.reader.state = 'READING'
        self.diagnostics = Diagnostics(self.reader, mav.MAVLink(None), write=self.write, pwm_observe=True)
        self.reader.command_observer = self.diagnostics.observe
        self.monitor = StateMonitor(read_brake_pwm=self.diagnostics.brake_pwm_snapshot)
        self.stop_status = {'contacts': {'FRONT': 'PRESSED'}, 'stop_latch': True}
        self.heartbeat()

    def tearDown(self):
        self.opener.assert_not_called()
        self.write.assert_not_called()
        self.assertIsNone(self.reader.thread)

    def ingest(self, message, system=1, component=1):
        encoder = mav.MAVLink(None, srcSystem=system, srcComponent=component)
        self.reader.ingest(mav.MAVLink(None).parse_char(message.pack(encoder)))

    def heartbeat(self):
        self.ingest(mav.MAVLink_heartbeat_message(
            mav.MAV_TYPE_GROUND_ROVER, mav.MAV_AUTOPILOT_ARDUPILOTMEGA,
            mav.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, 4, mav.MAV_STATE_STANDBY, 3))

    def esc(self, voltage, system=1, component=1):
        message = mav.MAVLink_esc_telemetry_1_to_4_message(
            [20] * 4, voltage, [0] * 4, [0] * 4, [0] * 4, [1] * 4)
        self.ingest(message, system, component)

    def snapshot(self):
        return self.monitor.snapshot(self.reader.snapshot(), self.stop_status)

    def test_missing_esc_does_not_interrupt_other_state_or_substitute_battery(self):
        self.ingest(mav.MAVLink_sys_status_message(0, 0, 0, 0, 58000, 0, 0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(self.reader.snapshot()['voltage_v']['value'], 58.0)
        state = self.snapshot()
        self.assertEqual(state['gpio17_front'], 'PRESSED')
        self.assertIs(state['stop_latch'], True)
        self.assertEqual(state['pixhawk']['armed']['value'], 'DISARMED')
        self.assertEqual(state['pixhawk']['flight_mode']['value']['custom_mode'], 4)
        self.assertEqual(state['vesc']['esc_slots_voltage_v'], {})
        for side in ('left', 'right'):
            self.assertEqual(state['vesc'][side],
                             {'state': 'UNKNOWN', 'esc_slot': None, 'voltage_v': None})

    def test_real_mavlink_groups_keep_esc_slots_distinct_and_wheels_unassigned(self):
        self.esc([0, 0, 5793, 5780])
        vesc = self.snapshot()['vesc']
        self.assertEqual(vesc['esc_slots_voltage_v'], {'1': None, '2': None, '3': 57.93, '4': 57.8})
        self.assertEqual({vesc[side]['state'] for side in ('left', 'right')}, {'UNKNOWN'})

    def test_explicit_slot_mapping_reads_each_wheel_independently(self):
        from production_stop.state_monitor import StateMonitor

        self.monitor = StateMonitor(left_esc_slot=3, right_esc_slot=4)
        self.esc([0, 0, 5793, 5780])
        vesc = self.snapshot()['vesc']
        self.assertEqual(vesc['left'], {'state': 'FRESH', 'esc_slot': 3, 'voltage_v': 57.93})
        self.assertEqual(vesc['right'], {'state': 'FRESH', 'esc_slot': 4, 'voltage_v': 57.8})
        self.esc([0, 0, 5793, 0])
        vesc = self.snapshot()['vesc']
        self.assertEqual(vesc['left']['state'], 'FRESH')
        self.assertEqual(vesc['right'], {'state': 'UNKNOWN', 'esc_slot': 4, 'voltage_v': None})

    def test_stale_esc_with_fresh_heartbeat_clears_voltage(self):
        self.esc([0, 0, 5793, 0])
        self.now += self.reader.timeout + .01
        self.heartbeat()
        state = self.snapshot()
        self.assertEqual(state['pixhawk']['connection'], 'CONNECTED')
        self.assertEqual(state['vesc']['esc_slots_voltage_v'], {})

    def test_disconnected_and_foreign_esc_samples_are_not_used(self):
        self.esc([0, 0, 5793, 0], system=42)
        self.esc([0, 0, 5793, 0], component=190)
        self.assertEqual(self.snapshot()['vesc']['esc_slots_voltage_v'], {})
        self.esc([0, 0, 5793, 0])
        self.assertEqual(self.snapshot()['vesc']['esc_slots_voltage_v']['3'], 57.93)
        self.reader.state = 'DISCONNECTED'
        self.assertEqual(self.snapshot()['vesc']['esc_slots_voltage_v'], {})

    def test_passive_pwm_observation_leaves_physical_brake_state_unknown(self):
        self.stop_status['brake_release'] = 'SENT'
        self.ingest(mav.MAVLink_servo_output_raw_message(100, 0, 0, 0, 0, 0, 2000, 2000, 0, 0))
        self.diagnostics.tick(123)
        self.assertEqual(self.snapshot()['brakes'], {'physical_state': 'UNKNOWN', 'command': 'SENT',
                                                  'servo5_pwm_us': 2000, 'servo6_pwm_us': 2000})
        self.now += 3.1
        self.heartbeat()
        self.assertIsNone(self.snapshot()['brakes']['servo5_pwm_us'])

    def test_owner_status_is_available_with_or_without_esc_and_keeps_commands_blocked(self):
        from production_stop.owner_bridge import OwnerProductionStop

        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bench = Mock(disarm_pending=lambda: False)
        mission = Mock()
        stop = self.enterContext(closing(OwnerProductionStop(
            self.reader, bench, folder / 'stop.db', mission_supervisor=mission, state_monitor=self.monitor)))
        stop.brake = Mock(hold_only=False, phase='HOLD', fault_reason=None,
                          reset_link_ready=lambda now: True)
        stop.gpio_observed('RELEASED', self.now)
        self.assertEqual(set(stop.rpc_operations()), {'STOP_STATUS'})
        for esc_voltage in (0, 5793):
            self.esc([0, 0, esc_voltage, 0])
            status = stop.status(1, 'operator', str(uuid4()))
            self.assertFalse(status['reset_available'])
            self.assertFalse(status['movement_allowed'])
            self.assertEqual(status['state_monitor']['gpio17_front'], 'RELEASED')
            self.assertEqual(status['state_monitor']['vesc']['esc_slots_voltage_v']['3'],
                             57.93 if esc_voltage else None)
        reset = stop.reset(1, 'operator', str(uuid4()))
        self.assertFalse(reset['reset_accepted'])
        self.assertNotIn('state_monitor', reset)
        resume = stop.mission_resume(1, 'operator', str(uuid4()))
        self.assertFalse(resume['resume_accepted'])
        mission.resume.assert_not_called()
        stop.brake.begin_release.assert_not_called()

    def test_all_esc_slots_fit_existing_ipc_limit(self):
        from disarm_control.ipc import LIMIT

        self.stop_status['state_monitor'] = self.monitor.snapshot({
            'connection': 'CONNECTED',
            'esc_diagnostics': {'groups': [
                {'state': 'FRESH', 'matches_current_autopilot': True,
                 'source_filter_at_receive': 'MATCH',
                 'controllers': [{'esc_number': i, 'voltage_v': 655.35} for i in range(1, 65)]}
            ]},
        }, self.stop_status)
        self.assertLess(len(json.dumps({'ok': True, 'command': self.stop_status}).encode()) + 1, LIMIT)

    def test_hmi_serves_diagnostics_only_to_telemetry_viewers(self):
        from web_hmi.app import create_app
        from web_hmi.test_support import CONFIG, PASSWORD, TestEnvironment

        env = TestEnvironment()
        self.addCleanup(env.close)
        response = dict(self.stop_status, reset_available=False, state_monitor=self.snapshot())
        owner = Mock()
        owner.call.return_value = {'ok': True, 'command': response}
        app = create_app(config=CONFIG, auth_store=env.store, disarm_client=owner)

        def login():
            client = app.test_client()
            token = re.search(r'name="csrf_token" value="([^"]+)"', client.get('/login').text)[1]
            reply = client.post('/login', data={
                'username': 'testadmin', 'password': PASSWORD, 'csrf_token': token,
            })
            self.assertEqual(reply.status_code, 303)
            return client

        client = login()
        result = client.get('/api/production-stop')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['state_monitor']['vesc']['left']['state'], 'UNKNOWN')
        self.assertIn('id="state-monitor">VESC лівий: UNKNOWN', client.get('/').text)
        with env.store.transaction() as db:
            db.execute("DELETE FROM role_permissions WHERE role='admin' AND permission='telemetry.view'")
        client = login()
        result = client.get('/api/production-stop')
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json['stop_latch'])
        self.assertNotIn('state_monitor', result.json)
        self.assertNotIn('id="state-monitor"', client.get('/').text)


if __name__ == '__main__':
    regressions = (
        'test_gpio_latch_and_disabled_reset',
        'test_brake_diagnostics_are_opt_in_and_emit_only_parameter_reads',
        'test_mission_reset_authorization_and_audit',
        'test_mission_resume_requires_independent_physical_evidence',
        'test_new_gpio_stop_revokes_mission_resume',
        'test_mission_resume_gateway_requires_opt_in',
    )
    with closing(paths()) as imports:
        sys.path[:0] = [*next(imports), str(ROOT / 'owner_externalnav/owner')]
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(StateMonitorTests)
        suite.addTests(StopTests(name) for name in regressions)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
