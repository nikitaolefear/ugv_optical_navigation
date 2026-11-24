"""Offline Web → gateway → owner → brake/checkpoint integration."""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
from uuid import uuid4

from pymavlink.dialects.v20 import ardupilotmega as mav

from test_candidate import ROOT, paths
from test_state_monitor import owner_reader_type


class StopMissionIntegrationTests(unittest.TestCase):
    def setup_system(self, *, physical_interlock=True, mission_opaque_id=None):
        from disarm_control.engine import DisarmEngine, Rejected
        from disarm_control.gateway import CommandGateway
        from production_stop.brake_link import BrakeLink
        from production_stop.hardwired_interlock import HardwiredInterlock, InterlockFeedback
        from production_stop.mission_checkpoint import RouteSnapshot
        from production_stop.mission_resume import MissionSupervisorAdapter
        from production_stop.owner_bridge import OwnerProductionStop
        from production_stop.rover_auto import RoverAutoAdapter, mission_current_sender
        from production_stop.state_monitor import StateMonitor
        from stop_latch_test.diagnostics import Diagnostics
        from web_hmi.app import create_app
        from web_hmi.test_support import CONFIG, PASSWORD, TestEnvironment

        self.now = 10.0
        self.contact = None
        self.feedback = physical_interlock
        self.encoder = mav.MAVLink(None, srcSystem=255, srcComponent=190)
        self.pixhawk = mav.MAVLink(None, srcSystem=1, srcComponent=1)
        self.writes = []
        folder = Path(self.enterContext(TemporaryDirectory()))
        self.opener = Mock(side_effect=AssertionError('hardware access in offline test'))
        self.addCleanup(self.opener.assert_not_called)
        self.reader = owner_reader_type()(clock=lambda: self.now, opener=self.opener)
        self.reader.state = 'READING'
        self.diagnostics = Diagnostics(self.reader, self.encoder, pwm_observe=True, write=self.write)
        self.bench = Mock(disarm_pending=lambda: False)
        self.disarm = self.enterContext(closing(DisarmEngine(
            self.reader, self.bench, Mock(), folder / 'disarm.db', clock=self.reader.clock,
        )))

        def interlock_feedback(*, pulse_watchdog):
            self.assertFalse(pulse_watchdog)
            return InterlockFeedback(
                observed_at=self.now, controller_healthy=True,
                stop_inputs_released=self.contact == 'RELEASED', outputs_deenergized=True,
                k1_open=True, k2_open=True, traction_bus_safe=True, restart_required=True,
                wheels_stationary=True, traction_observed_at=self.now, wheels_observed_at=self.now,
                left_brake_hold_confirmed=True, left_brake_observed_at=self.now,
                right_brake_hold_confirmed=True, right_brake_observed_at=self.now,
            )

        native = HardwiredInterlock(Mock(exchange=interlock_feedback), self.reader) if physical_interlock else None
        self.route = RouteSnapshot(self.now, tuple(
            mav.MAVLink_mission_item_int_message(255, 190, seq, 6, 16, 0, 1, 0, 0, 0, 0,
                                                490000000 + seq, 310000000, 0)
            for seq in range(4)
        ))

        self.exchange = Mock(side_effect=lambda op, rid, reset, **kwargs: {
            'request_id': rid, 'reset_id': reset, 'acceptance': 'ACCEPTED',
        })
        supervisor = MissionSupervisorAdapter(self.exchange, self.resume_evidence, self.reader.clock)
        self.adapter = RoverAutoAdapter(
            self.reader, supervisor, mission_opaque_id,
            interlock_ready=lambda: native is not None and native.blocked_at() is not None,
            read_route=lambda: self.route, send_current=mission_current_sender(self.encoder, self.write),
        )

        def observe(message):
            self.diagnostics.observe(message)
            self.adapter.observe(message)

        self.reader.command_observer = observe
        self.owner = self.enterContext(closing(OwnerProductionStop(
            self.reader, self.bench, folder / 'stop.db', native=native,
            mission_supervisor=self.adapter, state_monitor=StateMonitor(self.diagnostics.brake_pwm_snapshot),
        )))
        self.brake = BrakeLink(self.reader, self.disarm, self.owner, self.encoder,
                               lambda: self.diagnostics.rc, write=self.write)
        self.owner.brake = self.brake

        def owner_call(op, actor, rid):
            command = self.owner.rpc_operations().get(op)
            if command is None:
                return {'ok': False, 'error': 'UNAVAILABLE'}
            return {'ok': True, 'command': command(actor['id'], actor['username'], rid)}

        gateway = CommandGateway(Mock(call=owner_call))
        gateway.stop_reset_enabled = gateway.mission_resume_enabled = True

        def gateway_call(op, actor, rid):
            try:
                return {'ok': True, 'command': gateway.stop_operation(op, actor['id'], actor['username'], rid)}
            except Rejected as error:
                return {'ok': False, 'error': str(error)}

        env = TestEnvironment()
        self.addCleanup(env.close)
        with env.store.transaction() as db:
            db.executemany("INSERT INTO role_permissions VALUES ('admin', ?)",
                           [('stop.reset',), ('mission.resume',)])
        app = create_app(config=CONFIG, auth_store=env.store, disarm_client=Mock(call=gateway_call))
        self.client = app.test_client()
        page = self.client.get('/login')
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
        login = self.client.post('/login', data={
            'username': 'testadmin', 'password': PASSWORD, 'csrf_token': token,
        })
        self.assertEqual(login.status_code, 303)
        page = self.client.get('/')
        self.headers = {
            'Origin': CONFIG['PUBLIC_ORIGIN'],
            'X-CSRF-Token': re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)[1],
        }

    def resume_evidence(self):
        from production_stop.mission_resume import PhysicalConfirmation, ResumeEvidence

        if not self.feedback:
            return None
        sample = PhysicalConfirmation(True, self.now)
        return ResumeEvidence(sample, sample, sample, sample)

    def write(self, fd, packet):
        self.assertEqual(fd, 123)  # Synthetic descriptor, with a memory-only writer.
        message = mav.MAVLink(None).parse_char(packet)
        if message.get_type() == 'COMMAND_LONG':
            from production_stop.rover_auto import SET_CURRENT

            self.assertEqual(message.command, SET_CURRENT)
        else:
            self.assertEqual(message.get_type(), 'NAMED_VALUE_INT')
        self.writes.append(message)
        return len(packet)

    @property
    def waypoint_writes(self):
        return [message for message in self.writes if message.get_type() == 'COMMAND_LONG']

    def frame(self, message, **extensions):
        parsed = mav.MAVLink(None).parse_char(message.pack(self.pixhawk))
        for name, value in extensions.items():
            setattr(parsed, name, value)
        self.reader.ingest(parsed)

    def heartbeat(self, *, prior_auto=False):
        flags = mav.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
        if prior_auto:
            flags |= mav.MAV_MODE_FLAG_SAFETY_ARMED
        self.frame(mav.MAVLink_heartbeat_message(
            mav.MAV_TYPE_GROUND_ROVER, mav.MAV_AUTOPILOT_ARDUPILOTMEGA, flags,
            10 if prior_auto else 4, mav.MAV_STATE_STANDBY, 3,
        ))

    def waypoint(self, seq, *, active=False, **extensions):
        state = mav.MISSION_STATE_ACTIVE if active else mav.MISSION_STATE_NOT_STARTED
        self.frame(mav.MAVLink_mission_current_message(seq, 3, state, int(active)), **extensions)

    def ack(self):
        from production_stop.rover_auto import SET_CURRENT

        self.frame(mav.MAVLink_command_ack_message(SET_CURRENT, mav.MAV_RESULT_ACCEPTED))

    def gpio(self, contact):
        self.now += .02
        self.contact = contact
        self.owner.gpio_observed(contact, self.now)

    def status(self):
        reply = self.client.get('/api/production-stop')
        self.assertEqual(reply.status_code, 200)
        state = reply.json
        monitor = state['state_monitor']
        self.assertEqual(monitor['gpio17_front'], state['contacts']['FRONT'])
        self.assertEqual(monitor['stop_latch'], state['stop_latch'])
        self.assertEqual(monitor['mission_state'], state['mission_state'])
        for side in ('left', 'right'):
            self.assertEqual(monitor['vesc'][side]['state'], 'UNKNOWN')
        self.assertEqual(monitor['vesc']['esc_slots_voltage_v'], {})
        self.assertEqual(monitor['brakes']['physical_state'], 'UNKNOWN')
        self.assertFalse(state['movement_allowed'])
        return state

    def post(self, action, request_id, **kwargs):
        return self.client.post(f'/api/production-stop/{action}',
                                json={'request_id': request_id, **kwargs}, headers=self.headers)

    def stopped_at_waypoint(self, **extensions):
        self.gpio('RELEASED')
        self.heartbeat(prior_auto=True)  # Supplied telemetry only.
        self.waypoint(2, active=True, **extensions)
        self.gpio('PRESSED')
        self.tick()
        status = self.status()
        self.assertTrue(status['stop_latch'])
        self.assertEqual(status['contacts']['FRONT'], 'PRESSED')
        self.assertEqual(self.writes[-1].value, self.brake.HOLD)
        checkpoint = self.adapter.checkpoint
        self.assertEqual(checkpoint.seq, 2)
        event = self.owner.journal.db.execute(
            "SELECT details FROM stop_events WHERE event='FRONT_PRESSED'"
        ).fetchone()
        self.assertEqual(json.loads(event[0])['stop_id'], checkpoint.stop_id)
        self.assertEqual(json.loads(event[0])['route_sha256'], checkpoint.route_sha256)
        self.gpio('RELEASED')
        self.heartbeat()
        self.diagnostics.rc = {'at': self.now, 'channels': {'8': 1500}}
        self.route = replace(self.route, observed_at=self.now)

    def tick(self):
        self.brake.tick(123)
        self.owner.tick(123)

    def reset(self):
        request = str(uuid4())
        response = self.post('reset', request)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.json['reset_accepted'])
        self.assertEqual(self.adapter.reset_id, request)
        self.assertEqual(self.brake.phase, 'PENDING')
        self.assertEqual(self.waypoint_writes, [])
        self.exchange.assert_not_called()
        self.tick()
        self.assertEqual(self.brake.phase, 'SENT')
        self.assertEqual(self.writes[-1].value, self.brake.RELEASE)
        self.now += .01
        return request

    def queue_restore(self):
        request = str(uuid4())
        response = self.post('resume', request, confirm=True)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['mission_state'], 'RESTORE_QUEUED')
        self.exchange.assert_not_called()
        return request

    def test_web_stop_reset_restore_and_confirmed_handoff(self):
        self.setup_system()
        self.stopped_at_waypoint()
        reset_id = self.reset()
        self.assertEqual(self.post('resume', str(uuid4())).status_code, 400)
        self.assertEqual(len(self.waypoint_writes), 0)
        request_id = self.queue_restore()
        self.waypoint(2)  # At/before the command, so cannot confirm the restore.
        self.tick()
        self.assertEqual(self.status()['mission_state'], 'RESTORING')
        self.assertEqual(len(self.waypoint_writes), 1)
        self.assertEqual(self.waypoint_writes[0].param1, 2)
        self.now += .01
        self.ack()
        self.tick()
        self.exchange.assert_not_called()
        self.waypoint(2)
        self.tick()
        self.assertEqual(self.status()['mission_state'], 'RESUME_ACCEPTED')
        self.exchange.assert_called_once_with('MISSION_RESUME', request_id, reset_id, timeout=.15)
        self.assertTrue(self.post('resume', request_id, confirm=True).json['replayed'])
        self.assertEqual(len(self.waypoint_writes), 1)

    def test_no_interlock_keeps_web_commands_blocked_with_monitor_online(self):
        self.setup_system(physical_interlock=False)
        self.stopped_at_waypoint()
        self.assertFalse(self.status()['reset_available'])
        self.assertEqual(set(self.owner.rpc_operations()), {'STOP_STATUS'})
        self.assertEqual(self.post('reset', str(uuid4())).status_code, 503)
        self.assertEqual(self.post('resume', str(uuid4()), confirm=True).status_code, 503)
        self.assertTrue(self.status()['stop_latch'])
        self.assertTrue(all(message.value == self.brake.HOLD for message in self.writes))
        self.exchange.assert_not_called()

    def test_changed_route_between_web_confirmation_and_serial_tick(self):
        self.setup_system()
        self.stopped_at_waypoint()
        self.reset()
        self.queue_restore()
        self.route.items[2].x += 1
        self.tick()
        self.assertEqual(self.status()['mission_state'], 'REFUSED')
        self.assertEqual(self.waypoint_writes, [])
        self.exchange.assert_not_called()

    def test_new_gpio_stop_revokes_queued_restore_and_reports_hold(self):
        self.setup_system()
        self.stopped_at_waypoint()
        self.reset()
        self.queue_restore()
        self.gpio('PRESSED')
        self.tick()
        state = self.status()
        self.assertTrue(state['stop_latch'])
        self.assertEqual(state['mission_state'], 'STOPPED')
        self.assertEqual(state['state_monitor']['brakes']['command'], 'HOLD')
        self.assertEqual(self.writes[-1].value, self.brake.HOLD)
        self.assertEqual(self.waypoint_writes, [])
        self.exchange.assert_not_called()

    def test_async_unknown_latches_stop_and_cannot_retry(self):
        self.setup_system(mission_opaque_id=42)
        self.stopped_at_waypoint(mission_id=42)
        self.reset()
        request_id = self.queue_restore()
        self.tick()
        self.now += .01
        self.ack()
        self.waypoint(2, mission_id=99)
        self.tick()
        state = self.status()
        self.assertEqual(state['mission_state'], 'UNKNOWN')
        self.assertTrue(state['stop_latch'])
        self.assertTrue(self.post('resume', request_id, confirm=True).json['replayed'])
        self.assertEqual(self.post('resume', str(uuid4()), confirm=True).status_code, 409)
        self.now += .1
        self.tick()
        self.assertEqual(self.writes[-1].value, self.brake.HOLD)
        self.gpio('RELEASED')
        reset = self.post('reset', str(uuid4()))
        self.assertTrue(reset.json['reset_accepted'])
        self.assertFalse(self.status()['stop_latch'])
        self.assertIsNone(self.adapter.checkpoint)
        self.assertEqual(len(self.waypoint_writes), 1)
        self.exchange.assert_not_called()


if __name__ == '__main__':
    with closing(paths()) as imports:
        sys.path[:0] = [*next(imports), str(ROOT / 'owner_externalnav/owner')]
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(StopMissionIntegrationTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
