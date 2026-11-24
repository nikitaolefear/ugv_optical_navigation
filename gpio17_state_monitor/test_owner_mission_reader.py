"""Offline readback and checkpoint tests on one serial-owner tick/receive path."""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
from uuid import uuid4

from pymavlink.dialects.v20 import ardupilotmega as mav

from test_candidate import ROOT
from test_state_monitor import owner_reader_type


class OwnerMissionReadTests(unittest.TestCase):
    def setUp(self):
        from production_stop.mission_reader import OwnerMissionReader
        from production_stop.owner_bridge import OwnerProductionStop
        from production_stop.rover_auto import RoverAutoAdapter
        from production_stop.state import ResetConditions

        self.ResetConditions = ResetConditions
        self.now = 10.0
        self.commands = []
        self.opener = Mock(side_effect=AssertionError('No serial access'))
        self.addCleanup(self.opener.assert_not_called)
        self.reader = owner_reader_type()(clock=lambda: self.now, opener=self.opener)
        self.reader.state = 'READING'
        encoder = mav.MAVLink(None, srcSystem=255, srcComponent=190)
        self.download = OwnerMissionReader(self.reader, encoder, write=self.write)
        self.ipc = Mock()
        self.restore = Mock(side_effect=AssertionError('No waypoint writes in readback tests'))
        self.addCleanup(self.restore.assert_not_called)
        self.adapter = RoverAutoAdapter(
            self.reader, self.ipc, read_route=self.download.snapshot,
            interlock_ready=lambda: True, send_current=self.restore,
        )

        def observe(message):
            self.download.observe(message)
            self.adapter.observe(message)

        self.reader.command_observer = observe
        self.native = Mock()
        self.native.enforces_elrs_stop_latch.return_value = True
        self.native.blocked_at.side_effect = lambda: self.now
        self.native.reset_conditions.side_effect = lambda: ResetConditions(
            self.now, True, True, True, True)
        self.bench = Mock(disarm_pending=lambda: False)
        folder = Path(self.enterContext(TemporaryDirectory()))
        self.owner = self.enterContext(closing(OwnerProductionStop(
            self.reader, self.bench, folder / 'stop.db', native=self.native,
            mission_supervisor=self.adapter,
        )))
        self.owner.brake = Mock(hold_only=False, phase='HOLD', fault_reason=None,
                                reset_link_ready=lambda now: True)
        self.heartbeat(auto=True)

    def write(self, fd, packet):
        self.assertEqual(fd, 123)
        msg = mav.MAVLink(None).parse_char(packet)
        self.assertIn(msg.get_type(), ('MISSION_REQUEST_LIST', 'MISSION_REQUEST_INT', 'MISSION_ACK'))
        self.assertEqual((msg.get_srcSystem(), msg.get_srcComponent()), self.download.recipient)
        self.assertEqual((msg.target_system, msg.target_component), (1, 1))
        self.assertEqual(msg.mission_type, mav.MAV_MISSION_TYPE_MISSION)
        self.commands.append(msg)
        return len(packet)

    def tick(self, kind=None, *, command_pending=False):
        self.now += .01
        before = len(self.commands)
        self.download.tick(123, command_pending=command_pending)
        if kind is None:
            self.assertEqual(len(self.commands), before)
            return None
        self.assertEqual(len(self.commands), before + 1)
        self.assertEqual(self.commands[-1].get_type(), kind)
        return self.commands[-1]

    def frame(self, message, system=1, component=1, **extensions):
        self.now += .01
        encoder = mav.MAVLink(None, srcSystem=system, srcComponent=component)
        parsed = mav.MAVLink(None).parse_char(message.pack(encoder))
        # Emulate newer MAVLink extensions absent from the installed dialect.
        for name, value in extensions.items():
            setattr(parsed, name, value)
        self.reader.ingest(parsed)

    def heartbeat(self, *, auto=False):
        flags = mav.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
        if auto:
            flags |= mav.MAV_MODE_FLAG_SAFETY_ARMED
        self.frame(mav.MAVLink_heartbeat_message(
            mav.MAV_TYPE_GROUND_ROVER, mav.MAV_AUTOPILOT_ARDUPILOTMEGA,
            flags, 10 if auto else 4, mav.MAV_STATE_STANDBY, 3,
        ))

    def current(self, seq=2, total=3, *, active=True, **extensions):
        state = mav.MISSION_STATE_ACTIVE if active else mav.MISSION_STATE_NOT_STARTED
        self.frame(mav.MAVLink_mission_current_message(seq, total, state, int(active)), **extensions)

    def count(self, count=4, *, recipient=None, source=(1, 1), mission_type=0):
        self.frame(mav.MAVLink_mission_count_message(
            *(recipient or self.download.recipient), count, mission_type,
        ), *source)

    def item(self, seq, *, delta=0, home_delta=0, recipient=None, source=(1, 1)):
        lat = 490000000 + seq * 100 + (home_delta if seq == 0 else delta)
        self.frame(mav.MAVLink_mission_item_int_message(
            *(recipient or self.download.recipient), seq, 6, 16, 0, 1,
            0, 0, 0, 0, lat, 310000000, 4,
        ), *source)

    def route(self, *, delta=0, home_delta=0, already_started=False):
        if not already_started:
            self.tick('MISSION_REQUEST_LIST')
        self.count()
        for seq in range(4):
            self.assertEqual(self.tick('MISSION_REQUEST_INT').seq, seq)
            self.item(seq, delta=delta if seq == 2 else 0, home_delta=home_delta)
        self.assertIsNone(self.download.snapshot())
        self.assertEqual(self.tick('MISSION_ACK').type, mav.MAV_MISSION_ACCEPTED)
        route = self.download.snapshot()
        self.assertEqual(len(route.items), 4)
        self.assertLess(route.started_at, route.observed_at)
        return route

    def front(self, contact):
        self.now += .01
        self.owner.gpio_observed(contact, self.now)

    def checkpoint(self):
        self.current()
        route = self.route()
        self.front('PRESSED')
        checkpoint = self.adapter.checkpoint
        self.assertIsNotNone(checkpoint)
        self.assertEqual((checkpoint.seq, checkpoint.route_sha256), (2, route.fingerprint()))
        return checkpoint

    def next_read(self):
        from production_stop.mission_reader import REFRESH_INTERVAL

        self.now += REFRESH_INTERVAL + .01

    def journal(self, event):
        row = self.owner.journal.db.execute(
            'SELECT details FROM stop_events WHERE event=? ORDER BY sequence DESC LIMIT 1',
            (event,),
        ).fetchone()
        return json.loads(row[0])

    def test_complete_read_uses_actual_seq_and_persists_one_stop_reset_binding(self):
        checkpoint = self.checkpoint()
        self.assertTrue(self.owner.core.snapshot().stop_latch)
        self.heartbeat()
        self.front('RELEASED')
        reset_id = str(uuid4())
        result = self.owner.reset(1, 'operator', reset_id)
        self.assertTrue(result['reset_accepted'])
        self.assertEqual(self.adapter.reset_id, reset_id)
        self.assertEqual(self.journal('RESET_ACCEPTED')['request_id'], reset_id)
        for event in ('FRONT_PRESSED', 'RESET_ACCEPTED'):
            self.assertEqual(self.journal(event)['stop_id'], checkpoint.stop_id)
            self.assertEqual(self.journal(event)['waypoint_seq'], 2)
            self.assertEqual(self.journal(event)['route_sha256'], checkpoint.route_sha256)
        self.assertEqual(len(checkpoint.route_sha256), 64)
        self.ipc.resume.assert_not_called()

    def test_request_timeout_and_late_response_cannot_publish_a_route(self):
        from production_stop.mission_reader import REPLY_TIMEOUT

        self.current()
        self.tick('MISSION_REQUEST_LIST')
        self.now += REPLY_TIMEOUT
        self.tick()
        self.count()
        self.item(0)
        self.tick()
        self.heartbeat(auto=True)
        self.current()
        self.front('PRESSED')
        self.assertIsNone(self.download.snapshot())
        self.assertIsNone(self.adapter.checkpoint)

    def test_incomplete_list_rejects_skipped_item_and_invalidates_previous_route(self):
        self.checkpoint()
        self.next_read()
        self.tick('MISSION_REQUEST_LIST')
        self.count()
        self.assertEqual(self.tick('MISSION_REQUEST_INT').seq, 0)
        self.item(0)
        self.assertEqual(self.tick('MISSION_REQUEST_INT').seq, 1)
        self.item(2)
        self.tick()
        self.assertIsNone(self.download.snapshot())
        self.now += .76
        self.tick()
        self.assertIsNone(self.download.snapshot())
        self.front('RELEASED')
        self.heartbeat(auto=True)
        self.current()
        self.front('PRESSED')
        self.assertIsNone(self.adapter.checkpoint)
        self.assertEqual(self.adapter.resume('request', 'reset').value, 'REFUSED')

    def test_post_stop_read_ignores_home_change_and_detects_changed_waypoint(self):
        from production_stop.mission_resume import ResumeAcceptance

        checkpoint = self.checkpoint()
        self.heartbeat()
        self.front('RELEASED')
        reset_id = str(uuid4())
        self.assertTrue(self.owner.reset(1, 'operator', reset_id)['reset_accepted'])
        self.adapter.release_confirmed(reset_id)
        self.next_read()
        changed = self.route(delta=1, home_delta=55)
        self.assertNotEqual(changed.fingerprint(), checkpoint.route_sha256)
        self.assertIs(self.adapter.resume('changed', reset_id), ResumeAcceptance.REFUSED)
        self.heartbeat()
        self.next_read()
        unchanged = self.route(home_delta=55)
        self.assertNotEqual(unchanged.items[0].x, 490000000)
        self.assertEqual(unchanged.fingerprint(), checkpoint.route_sha256)
        self.assertIs(self.adapter.resume('matching', reset_id), ResumeAcceptance.ACCEPTED)
        self.ipc.resume.assert_not_called()

    def test_stale_mission_current_never_becomes_checkpoint(self):
        self.current()
        self.now += .51
        self.heartbeat(auto=True)
        self.route()
        self.front('PRESSED')
        self.assertIsNone(self.adapter.checkpoint)
        self.assertNotIn('route_sha256', self.journal('FRONT_PRESSED'))

    def test_later_stop_without_fresh_seq_revokes_the_previous_checkpoint(self):
        previous = self.checkpoint()
        self.front('RELEASED')
        self.front('PRESSED')
        self.assertIsNone(self.adapter.checkpoint)
        self.assertNotEqual(self.journal('FRONT_PRESSED').get('stop_id'), previous.stop_id)
        self.heartbeat()
        self.front('RELEASED')
        self.assertTrue(self.owner.reset(1, 'operator', str(uuid4()))['reset_accepted'])
        self.assertNotIn('stop_id', self.journal('RESET_ACCEPTED'))
        self.assertIsNone(self.adapter.reset_id)

    def test_later_stop_with_fresh_seq_records_new_id_and_waypoint(self):
        previous = self.checkpoint()
        self.front('RELEASED')
        self.current(seq=1)
        self.front('PRESSED')
        current = self.adapter.checkpoint
        self.assertNotEqual(current.stop_id, previous.stop_id)
        self.assertEqual(current.route_sha256, previous.route_sha256)
        self.assertEqual(current.seq, 1)
        self.assertEqual(self.journal('FRONT_PRESSED')['waypoint_seq'], 1)

    def test_response_source_recipient_and_mission_type_are_checked(self):
        self.tick('MISSION_REQUEST_LIST')
        self.count(source=(42, 1))
        self.count(recipient=(255, 191))
        self.count(mission_type=mav.MAV_MISSION_TYPE_FENCE)
        self.tick()
        self.count()
        self.assertEqual(self.tick('MISSION_REQUEST_INT').seq, 0)
        self.item(0, source=(1, 2))
        self.item(0, recipient=(255, 191))
        self.tick()
        self.item(0)
        self.assertEqual(self.tick('MISSION_REQUEST_INT').seq, 1)

    def test_pending_control_preempts_read_and_disconnect_clears_cached_plan(self):
        from production_stop.mission_reader import RETRY_INTERVAL

        self.tick(command_pending=True)
        self.tick('MISSION_REQUEST_LIST')
        self.count()
        self.tick(command_pending=True)
        self.item(0)  # Leftover response after preemption.
        self.assertIsNone(self.download.snapshot())
        self.now += RETRY_INTERVAL
        self.heartbeat(auto=True)
        self.route()
        self.reader.state = 'DISCONNECTED'
        self.assertIsNone(self.download.snapshot())

    def test_transfer_started_before_stop_cannot_satisfy_post_stop_verification(self):
        from production_stop.mission_resume import ResumeAcceptance

        checkpoint = self.checkpoint()
        complete = self.download.snapshot()
        self.heartbeat()
        self.front('RELEASED')
        reset_id = str(uuid4())
        self.assertTrue(self.owner.reset(1, 'operator', reset_id)['reset_accepted'])
        self.adapter.release_confirmed(reset_id)
        # Completion arrived after STOP, but the oldest read precedes that STOP.
        self.download.route = replace(complete, observed_at=self.now)
        self.assertLess(self.download.route.started_at, checkpoint.stopped_at)
        self.assertIs(self.adapter.resume('request', reset_id), ResumeAcceptance.REFUSED)

    def test_no_mission_report_invalidates_download_and_read_only_adapter_cannot_resume(self):
        from production_stop.mission_resume import ResumeAcceptance
        from production_stop.rover_auto import RoverAutoAdapter

        self.route()
        self.current(seq=0, total=65535, active=False)
        self.assertIsNone(self.download.snapshot())
        read_only = RoverAutoAdapter(self.reader, None, read_route=self.download.snapshot)
        self.owner.mission_supervisor = read_only
        self.assertNotIn('MISSION_RESUME', self.owner.rpc_operations())
        self.assertIs(read_only.resume('request', 'reset'), ResumeAcceptance.REFUSED)

    def test_new_opaque_plan_id_invalidates_an_already_completed_read(self):
        self.tick('MISSION_REQUEST_LIST')
        self.frame(mav.MAVLink_mission_count_message(255, 190, 4, 0), opaque_id=42)
        for seq in range(4):
            self.tick('MISSION_REQUEST_INT')
            self.item(seq)
        self.tick('MISSION_ACK')
        self.assertEqual(self.download.snapshot().opaque_id, 42)
        self.current(mission_id=43)
        self.assertIsNone(self.download.snapshot())
        self.front('PRESSED')
        self.assertIsNone(self.adapter.checkpoint)


if __name__ == '__main__':
    sys.path[:0] = [str(ROOT / 'owner_externalnav'), '/opt/ugv-disarm/package']
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(OwnerMissionReadTests))
    sys.exit(not result.wasSuccessful())
