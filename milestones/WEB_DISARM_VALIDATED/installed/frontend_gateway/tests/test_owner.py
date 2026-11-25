from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from disarm_control.engine import DisarmEngine, disarm_sender
from stop_latch_test.hold_bench import HoldBench, hold_sender
from stop_latch_test.stop_latch import Rejected
from web_hmi.pixhawk_telemetry import PixhawkTelemetry


class OwnerIntegrationTests(unittest.TestCase):
    def setUp(self):
        from pymavlink.dialects.v20 import ardupilotmega as mav
        self.now = 10.0
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.reader = PixhawkTelemetry(clock=lambda:self.now, opener=Mock(side_effect=AssertionError('Serial forbidden')))
        self.reader.state = 'READING'
        self.encoder = mav.MAVLink(None,srcSystem=1,srcComponent=1)
        self.mav = mav
        self.hold_sender = Mock()
        self.bench = HoldBench(Path(self.tmp.name)/'bench.jsonl',self.reader,self.hold_sender)
        self.disarm_sender = Mock()
        self.control = DisarmEngine(self.reader,self.bench,self.disarm_sender,
            Path(self.tmp.name)/'disarm.sqlite3',clock=lambda:self.now)
        self.addCleanup(self.control.close)
        self.reader.command_observer=self.control.observe
        self.bench.disarm_pending=lambda:self.control.active is not None
        self.rid=str(uuid.uuid4())
        self.heartbeat(armed=True)

    def heartbeat(self, armed=False):
        self.now += .1
        packet=self.mav.MAVLink_heartbeat_message(10,3,128 if armed else 0,0,4,3).pack(self.encoder)
        msg=self.mav.MAVLink(None).parse_buffer(packet)[0]
        self.reader.ingest(msg)

    def test_same_reader_lock_and_existing_fd_only(self):
        self.assertIs(self.control.lock,self.reader.lock)
        self.control.request(1,'operator',self.rid)
        self.control.tick(42)
        ack=self.mav.MAVLink_command_ack_message(400,0,0,0,*(255,190)).pack(self.encoder)
        self.reader.ingest(self.mav.MAVLink(None).parse_buffer(ack)[0])
        self.heartbeat()
        self.control.tick(42)
        self.assertEqual(self.control.status(1,self.rid)['observation_state'],'DISARMED_OBSERVED')
        self.assertEqual(self.control.status(1,self.rid)['command_outcome'],'UNKNOWN')
        self.reader.opener.assert_not_called()
        self.hold_sender.assert_not_called()
        self.disarm_sender.assert_called_once_with(42,(1,1))

    def test_pending_hold_is_cancelled_before_serial_loop_runs(self):
        self.bench.phase='PENDING'
        self.control.request(1,'operator',self.rid)
        self.control.tick(42)
        self.bench.tick(42)
        self.hold_sender.assert_not_called()
        self.assertEqual(self.bench.phase,'CANCELLED_DISARM')
        self.assertTrue(self.bench.latched)

    def test_hold_cannot_be_enabled_during_disarm(self):
        self.control.request(1,'operator',self.rid)
        with self.assertRaises(Rejected):
            self.bench.command('arm_hold_once')
        self.hold_sender.assert_not_called()

    def test_reset_cannot_clear_latch_during_disarm(self):
        self.control.request(1,'operator',self.rid)
        with self.assertRaises(Rejected):
            self.bench.command('reset')
        self.assertTrue(self.bench.latched)

    def test_denied_ack_flows_through_real_ingest_hook(self):
        self.control.request(1,'operator',self.rid)
        self.control.tick(42)
        ack=self.mav.MAVLink_command_ack_message(400,2,0,0,*(255,190)).pack(self.encoder)
        self.reader.ingest(self.mav.MAVLink(None).parse_buffer(ack)[0])
        self.assertEqual(self.control.status(1,self.rid)['delivery_state'],'SENT')
        self.assertEqual(self.control.status(1,self.rid)['observation_state'],'UNKNOWN')
        self.assertEqual(self.control.status(1,self.rid)['ack_diagnostic']['scope'],'UNCORRELATED')

    def test_hold_and_disarm_share_encoder_sequence_on_same_owner(self):
        encoder=self.mav.MAVLink(None,srcSystem=255,srcComponent=190)
        packets=[]

        def write(fd, packet):
            self.assertEqual(fd,42)
            packets.append(packet)
            return len(packet)

        with patch('os.write',side_effect=write):
            hold_sender(encoder=encoder)(42,(1,1))
        disarm_sender(write=write,encoder=encoder)(42,(1,1))
        messages=[self.mav.MAVLink(None).parse_buffer(packet)[0] for packet in packets]
        self.assertEqual([message.get_seq() for message in messages],[0,1])
        self.assertEqual([message.get_type() for message in messages],['SET_MODE','COMMAND_LONG'])
