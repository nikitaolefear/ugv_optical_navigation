from dataclasses import replace
from pathlib import Path
import sys
from threading import RLock
import unittest
from unittest.mock import Mock

from pymavlink.dialects.v20 import ardupilotmega as mav

sys.path.insert(0, str(Path(__file__).resolve().parent / 'owner_externalnav'))
from production_stop.mission_checkpoint import RouteSnapshot
from production_stop.mission_resume import ResumeAcceptance
from production_stop.rover_auto import RoverAutoAdapter, mission_current_sender, SET_CURRENT


class RoverAutoTests(unittest.TestCase):
    def setUp(self):
        self.reader = Mock(lock=RLock(), messages={}, state='READING')
        self.reader.clock.return_value = 1.0
        self.ipc = Mock()
        self.ipc.resume.return_value = ResumeAcceptance.ACCEPTED
        self.interlock = Mock(return_value=True)
        self.write = Mock(side_effect=lambda fd, packet: len(packet))
        encoder = mav.MAVLink(None, srcSystem=255, srcComponent=190)
        self.sender = mission_current_sender(encoder, self.write)
        self.route = RouteSnapshot(.99, tuple(
            mav.MAVLink_mission_item_int_message(255, 190, seq, 6, 16, 0, 1,
                                                0, 0, 0, 0, seq * 100, 200, 0)
            for seq in range(4)
        ))
        self.adapter = RoverAutoAdapter(
            self.reader, self.ipc, interlock_ready=self.interlock,
            read_route=lambda: self.route, send_current=self.sender,
        )

    def clock(self, now):
        self.reader.clock.return_value = now

    def report(self, msg, system=1):
        encoder = mav.MAVLink(None, srcSystem=system, srcComponent=1)
        decoded = mav.MAVLink(None).parse_char(msg.pack(encoder))
        self.adapter.observe(decoded)

    def heartbeat(self, mode, armed=True):
        flags = mav.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
        if armed:
            flags |= mav.MAV_MODE_FLAG_SAFETY_ARMED
        hb = mav.MAVLink_heartbeat_message(mav.MAV_TYPE_GROUND_ROVER,
                                         mav.MAV_AUTOPILOT_ARDUPILOTMEGA, flags, mode, 4, 3)
        encoder = mav.MAVLink(None, srcSystem=1, srcComponent=1)
        self.reader.messages['HEARTBEAT'] = (
            self.reader.clock(), mav.MAVLink(None).parse_char(hb.pack(encoder)))

    def current(self, seq, state=mav.MISSION_STATE_ACTIVE, system=1):
        self.report(mav.MAVLink_mission_current_message(seq, 4, state, 2), system)

    def ack(self, result=mav.MAV_RESULT_ACCEPTED):
        self.report(mav.MAVLink_command_ack_message(SET_CURRENT, result))

    def stop_reset_release(self):
        self.heartbeat(10)
        self.current(2)
        self.adapter.front_pressed(1.0)
        self.adapter.stop_latched()
        self.clock(1.1)
        self.heartbeat(4, armed=False)
        self.current(1, mav.MISSION_STATE_PAUSED)
        self.route = replace(self.route, observed_at=1.1)
        self.adapter.reset_accepted('reset')
        self.adapter.release_confirmed('reset')

    def send_restore(self):
        self.stop_reset_release()
        self.assertIs(self.adapter.resume('request', 'reset'), ResumeAcceptance.ACCEPTED)
        self.adapter.tick(123)
        self.write.assert_called_once()
        packet = self.write.call_args.args[1]
        command = mav.MAVLink(None).parse_char(packet)
        self.assertEqual((command.command, command.param1, command.param2, command.confirmation),
                         (SET_CURRENT, 2, 0, 0))
        self.assertEqual((command.target_system, command.target_component), (1, 1))
        self.ipc.resume.assert_not_called()

    def test_restore_waypoint_after_disarm(self):
        self.send_restore()
        self.clock(1.2)
        self.ack()
        self.current(1)
        self.adapter.tick(123)
        self.ipc.resume.assert_not_called()
        self.current(2, system=2)
        self.adapter.tick(123)
        self.ipc.resume.assert_not_called()
        self.current(2)
        self.adapter.tick(123)
        self.ipc.resume.assert_called_once_with('request', 'reset')
        self.assertEqual(self.adapter.status(), 'RESUME_ACCEPTED')
        self.clock(1.3)
        self.heartbeat(10)
        self.current(2)
        self.assertEqual(self.adapter.status(), 'RUNNING')
        self.clock(2)
        self.assertEqual(self.adapter.status(), 'RESUME_ACCEPTED')

    def test_fingerprint_ignores_dynamic_home_and_transport_fields(self):
        original = self.route.fingerprint()
        self.route.items[0].x += 100
        self.route.items[2].target_system = 88
        self.route.items[2].current = 1
        self.assertEqual(self.route.fingerprint(), original)
        self.route.items[2].x += 1
        self.assertNotEqual(self.route.fingerprint(), original)

    def test_route_changed_at_resume(self):
        self.stop_reset_release()
        self.route.items[2].x += 1
        self.assertIs(self.adapter.resume('request', 'reset'), ResumeAcceptance.REFUSED)
        self.write.assert_not_called()

    def test_route_changed_after_queuing(self):
        self.stop_reset_release()
        self.assertIs(self.adapter.resume('request', 'reset'), ResumeAcceptance.ACCEPTED)
        self.route.items[2].x += 1
        self.adapter.tick(123)
        self.assertEqual(self.adapter.status(), 'REFUSED')
        self.write.assert_not_called()

    def test_route_changed_after_write_suppresses_resume(self):
        self.send_restore()
        self.clock(1.2)
        self.ack()
        self.current(2)
        self.route.items[3].command += 1
        self.adapter.tick(123)
        self.assertEqual(self.adapter.status(), 'UNKNOWN')
        self.ipc.resume.assert_not_called()

    def test_stale_route_or_wrong_reset_blocks_dispatch(self):
        self.stop_reset_release()
        self.assertIs(self.adapter.resume('request', 'other'), ResumeAcceptance.REFUSED)
        self.clock(2.0)
        self.assertIs(self.adapter.resume('request', 'reset'), ResumeAcceptance.REFUSED)
        self.write.assert_not_called()

    def test_stale_mission_current_at_stop(self):
        self.current(2)
        self.clock(2)
        self.heartbeat(10)
        self.route = replace(self.route, observed_at=2)
        self.adapter.front_pressed(2)
        self.assertIsNone(self.adapter.checkpoint)

    def test_missing_physical_interlock_blocks_dispatch(self):
        self.stop_reset_release()
        self.adapter.interlock_ready = None
        self.assertIs(self.adapter.resume('request', 'reset'), ResumeAcceptance.REFUSED)
        self.write.assert_not_called()

    def test_new_front_pressed_invalidates_restore(self):
        self.send_restore()
        self.clock(1.2)
        self.adapter.front_pressed(1.2)
        self.clock(1.3)
        self.ack()
        self.current(2)
        self.adapter.tick(123)
        self.ipc.resume.assert_not_called()
        self.write.assert_called_once()
        self.assertIs(self.adapter.resume('other', 'reset'), ResumeAcceptance.REFUSED)

    def test_no_fresh_current_or_ack_times_out_without_retry(self):
        self.send_restore()
        self.clock(1.1)
        self.current(2)  # Same receive time as send, so not post-send evidence.
        self.clock(1.2)
        self.ack()
        self.adapter.tick(123)
        self.clock(3.2)
        self.route = replace(self.route, observed_at=3.2)
        self.adapter.tick(123)
        self.assertEqual(self.adapter.status(), 'UNKNOWN')
        self.assertIs(self.adapter.resume('other', 'reset'), ResumeAcceptance.REFUSED)
        self.write.assert_called_once()
        self.ipc.resume.assert_not_called()

    def test_negative_ack_does_not_resume(self):
        self.send_restore()
        self.clock(1.2)
        self.current(2)
        self.ack(mav.MAV_RESULT_DENIED)
        self.adapter.tick(123)
        self.assertEqual(self.adapter.status(), 'REFUSED')
        self.ipc.resume.assert_not_called()

    def test_unknown_ipc_cannot_dispatch_twice(self):
        self.send_restore()
        self.ipc.resume.return_value = ResumeAcceptance.UNKNOWN
        self.clock(1.2)
        self.ack()
        self.current(2)
        self.adapter.tick(123)
        self.adapter.tick(123)
        self.assertEqual(self.adapter.status(), 'UNKNOWN')
        self.write.assert_called_once()
        self.ipc.resume.assert_called_once()


if __name__ == '__main__':
    unittest.main()
