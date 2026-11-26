import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from disarm_control.engine import DisarmEngine, Rejected, disarm_sender


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 10.0
        self.hb = SimpleNamespace(autopilot=3, type=10, base_mode=128,
                                  get_srcSystem=lambda: 1, get_srcComponent=lambda: 1)
        self.reader = SimpleNamespace(lock=threading.RLock(), state='READING',
            messages={'HEARTBEAT': (10.0, self.hb)}, source=(1, 1), timeout=3.0,
            heartbeat_serial=1)
        self.bench = SimpleNamespace(phase='IDLE', latched=True, run='STOPPED')
        self.sender = Mock()
        self.database = Path(self.tmp.name)/'audit.sqlite3'
        self.engine = self.open_engine()
        self.addCleanup(lambda: self.engine.close() if self.engine else None)
        self.rid = str(uuid.uuid4())

    def open_engine(self, **kwargs):
        return DisarmEngine(self.reader, self.bench, self.sender, self.database,
                            clock=lambda: self.now, **kwargs)

    def request(self):
        return self.engine.request(1, 'operator', self.rid)

    def result(self):
        result = self.engine.status(1, self.rid)
        self.assertEqual(result['state'], result['delivery_state'])
        self.assertEqual(result['command_outcome'], 'UNKNOWN')
        self.assertNotEqual(result['state'], 'CONFIRMED')
        json.dumps(result, allow_nan=False)
        return result

    def send(self):
        self.request()
        self.engine.tick(42)
        self.assertEqual(self.result()['delivery_state'], 'SENT')

    def heartbeat(self, armed=False):
        self.now += .1
        self.hb.base_mode = 128 if armed else 0
        self.reader.messages['HEARTBEAT'] = self.now, self.hb
        self.reader.heartbeat_serial += 1

    def ack(self, result=0, source=1, command=400, targeted=True):
        return SimpleNamespace(get_type=lambda: 'COMMAND_ACK', command=command, result=result,
            get_srcSystem=lambda: source, get_srcComponent=lambda: 1,
            target_system=255 if targeted else 0, target_component=190 if targeted else 0)

    def assert_unknown_observation(self, result):
        self.assertEqual(result['observation_state'], 'UNKNOWN')
        for field in ('heartbeat_age_s', 'heartbeat_rx_serial', 'heartbeat_observed_at', 'heartbeat_source'):
            self.assertIsNone(result[field], field)
        self.assertEqual(result['heartbeat_freshness_s'], 3.)

    def test_intent_accepted_without_serial_write(self):
        result = self.request()
        self.assertEqual(result['delivery_state'], 'QUEUED')
        self.assertTrue(result['observation_pending'])
        self.assert_unknown_observation(result)
        self.sender.assert_not_called()

    def test_complete_write_is_sent_not_command_success(self):
        self.send()
        self.sender.assert_called_once_with(42, (1, 1))
        self.assert_unknown_observation(self.result())

    def test_new_disarmed_heartbeat_observed_without_any_ack(self):
        self.send()
        self.heartbeat()
        result = self.result()
        self.assertEqual(result['observation_state'], 'DISARMED_OBSERVED')
        self.assertFalse(result['observation_pending'])
        self.assertEqual(result['heartbeat_source'], [1, 1])
        self.assertEqual(result['heartbeat_rx_serial'], 2)
        self.assertEqual(result['heartbeat_age_s'], 0.)
        self.assertTrue(self.bench.latched)
        self.assertEqual(self.bench.run, 'STOPPED')

    def test_positive_ack_is_diagnostic_only(self):
        self.send()
        self.engine.observe(self.ack())
        self.assert_unknown_observation(self.result())
        self.assertEqual(self.result()['ack_diagnostic']['scope'], 'UNCORRELATED')
        self.assertEqual(self.result()['delivery_state'], 'SENT')

    def test_negative_ack_cannot_be_assigned_to_this_request(self):
        self.send()
        self.engine.observe(self.ack(2))
        self.assertEqual(self.result()['delivery_state'], 'SENT')
        self.assertTrue(self.result()['observation_pending'])
        self.heartbeat()
        self.assertEqual(self.result()['observation_state'], 'DISARMED_OBSERVED')
        self.assertEqual(self.result()['ack_diagnostic']['result'], 2)

    def test_wrong_ack_source_or_command_is_ignored(self):
        self.send()
        self.engine.observe(self.ack(2, source=2))
        self.engine.observe(self.ack(2, command=176))
        self.assertIsNone(self.result()['ack_diagnostic'])

    def test_untargeted_ack_still_never_grants_proof(self):
        self.send()
        self.engine.observe(self.ack(targeted=False))
        self.assert_unknown_observation(self.result())
        self.assertEqual(self.result()['command_outcome'], 'UNKNOWN')

    def test_in_progress_ack_does_not_change_outcome(self):
        self.send()
        self.engine.observe(self.ack(5))
        self.assert_unknown_observation(self.result())
        self.assertEqual(self.result()['delivery_state'], 'SENT')

    def test_already_disarmed_still_requires_post_send_heartbeat(self):
        self.hb.base_mode = 0
        self.send()
        self.assert_unknown_observation(self.result())
        self.heartbeat()
        self.assertEqual(self.result()['observation_state'], 'DISARMED_OBSERVED')

    def test_same_timestamp_cannot_be_post_send_observation(self):
        self.send()
        self.hb.base_mode = 0
        self.reader.heartbeat_serial += 1
        self.assert_unknown_observation(self.result())

    def test_same_serial_cannot_be_post_send_observation(self):
        self.send()
        self.now += .1
        self.hb.base_mode = 0
        self.reader.messages['HEARTBEAT'] = self.now, self.hb
        self.assert_unknown_observation(self.result())

    def test_new_armed_observation_is_explicit_not_success(self):
        self.send()
        self.heartbeat(armed=True)
        self.assertEqual(self.result()['observation_state'], 'ARMED_OBSERVED')
        self.assertTrue(self.result()['observation_pending'])

    def test_rearm_after_observed_disarm_replaces_displayed_state(self):
        self.send()
        self.heartbeat()
        self.assertEqual(self.result()['observation_state'], 'DISARMED_OBSERVED')
        self.heartbeat(armed=True)
        result = self.result()
        self.assertEqual(result['observation_state'], 'ARMED_OBSERVED')
        self.assertFalse(result['observation_pending'])
        self.sender.assert_called_once()

    def test_stale_heartbeat_after_observation_is_unknown(self):
        self.send()
        self.heartbeat()
        self.assertEqual(self.result()['observation_state'], 'DISARMED_OBSERVED')
        self.now += 3.
        self.assert_unknown_observation(self.result())
        self.assertEqual(self.result()['delivery_state'], 'SENT')

    def test_link_loss_masks_known_state_but_retains_delivery_fact(self):
        self.send()
        self.heartbeat()
        self.assertEqual(self.result()['observation_state'], 'DISARMED_OBSERVED')
        self.reader.state = 'DISCONNECTED'
        self.assert_unknown_observation(self.result())
        self.assertEqual(self.result()['delivery_state'], 'SENT')
        self.sender.assert_called_once()

    def test_stale_queued_request_is_rejected_before_write(self):
        self.request()
        self.now += 3.
        self.engine.tick(42)
        self.assertEqual(self.result()['delivery_state'], 'REJECTED')
        self.assertEqual(self.result()['reason'], 'HEARTBEAT_STALE')
        self.sender.assert_not_called()

    def test_source_change_before_send_is_rejected(self):
        self.request()
        self.hb.get_srcSystem = lambda: 2
        self.reader.source = (2, 1)
        self.engine.tick(42)
        self.assertEqual(self.result()['delivery_state'], 'REJECTED')
        self.assertEqual(self.result()['reason'], 'AUTOPILOT_CHANGED')
        self.sender.assert_not_called()

    def test_source_change_after_send_masks_observation(self):
        self.send()
        self.hb.get_srcSystem = lambda: 2
        self.reader.source = (2, 1)
        self.heartbeat()
        result = self.result()
        self.assert_unknown_observation(result)
        self.assertEqual(result['reason'], 'AUTOPILOT_CHANGED')
        self.assertEqual(result['delivery_state'], 'SENT')

    def test_observation_timeout_preserves_full_write_fact(self):
        self.send()
        self.now += 5.
        self.reader.messages['HEARTBEAT'] = self.now, self.hb
        self.reader.heartbeat_serial += 1
        result = self.result()
        self.assertEqual(result['reason'], 'OBSERVATION_TIMEOUT')
        self.assertEqual(result['delivery_state'], 'SENT')
        self.assertEqual(result['observation_state'], 'ARMED_OBSERVED')
        self.assertFalse(result['observation_pending'])
        self.engine.tick(42)
        self.sender.assert_called_once()

    def test_status_alone_handles_dead_reader_without_writing(self):
        self.request()
        self.reader.state = 'DISCONNECTED'
        result = self.result()
        self.assertEqual(result['delivery_state'], 'REJECTED')
        self.assertFalse(result['observation_pending'])
        self.sender.assert_not_called()

    def test_send_failure_is_unknown_and_never_retried(self):
        self.request()
        self.sender.side_effect = OSError()
        self.engine.tick(42)
        self.engine.tick(42)
        self.assertEqual(self.result()['delivery_state'], 'UNKNOWN')
        self.assertEqual(self.result()['reason'], 'SEND_UNCERTAIN_NO_RETRY')
        self.assert_unknown_observation(self.result())
        self.heartbeat()
        self.assert_unknown_observation(self.result())
        self.sender.assert_called_once()

    def test_new_heartbeat_inside_writer_does_not_count_as_post_write(self):
        self.request()
        self.sender.side_effect = lambda fd, target: self.heartbeat()
        self.engine.tick(42)
        self.assert_unknown_observation(self.result())
        self.heartbeat()
        self.assertEqual(self.result()['observation_state'], 'DISARMED_OBSERVED')

    def test_duplicate_request_is_durable_and_never_resends(self):
        self.send()
        self.assertEqual(self.request()['delivery_state'], 'SENT')
        self.engine.tick(42)
        self.sender.assert_called_once()

    def test_owner_restart_invalidates_old_baseline_without_replay(self):
        self.send()
        self.heartbeat()
        self.assertEqual(self.result()['observation_state'], 'DISARMED_OBSERVED')
        self.engine.close()
        self.engine = None
        self.engine = self.open_engine()
        self.heartbeat()
        result = self.request()
        self.assertEqual(result['delivery_state'], 'UNKNOWN')
        self.assertEqual(result['reason'], 'OWNER_RESTARTED')
        self.assert_unknown_observation(result)
        self.engine.tick(42)
        self.sender.assert_called_once()

    def test_queued_request_restart_is_unknown_not_automatically_sent(self):
        self.request()
        self.engine.close()
        self.engine = None
        self.engine = self.open_engine()
        self.assertEqual(self.request()['delivery_state'], 'UNKNOWN')
        self.engine.tick(42)
        self.sender.assert_not_called()

    def test_busy_and_cross_user_requests_are_rejected(self):
        self.request()
        with self.assertRaisesRegex(Rejected, 'BUSY'):
            self.engine.request(1, 'operator', str(uuid.uuid4()))
        with self.assertRaises(Rejected):
            self.engine.request(2, 'other', self.rid)
        with self.assertRaises(Rejected):
            self.engine.status(2, self.rid)

    def test_hold_transaction_cancelled_and_latch_not_cleared(self):
        for phase in ('ENABLED', 'PENDING', 'WAITING'):
            self.bench.phase = phase
            self.rid = str(uuid.uuid4())
            self.request()
            self.assertEqual(self.bench.phase, 'CANCELLED_DISARM')
            self.assertTrue(self.bench.latched)
            self.engine.set_enabled(False)
            self.engine.set_enabled(True)

    def test_bad_clocks_fail_closed_and_never_recover_implicitly(self):
        for value in (9, None, True, float('nan'), float('inf'), 10**1000):
            with self.subTest(value_type=type(value).__name__):
                self.now = 10.
                self.engine.close()
                self.engine = None
                self.engine = self.open_engine()
                self.rid = str(uuid.uuid4())
                self.request()
                self.now = value
                self.engine.tick(42)
                self.assertEqual(self.result()['delivery_state'], 'REJECTED')
                self.assert_unknown_observation(self.result())
                self.now = 11.
                with self.assertRaisesRegex(Rejected, 'CLOCK_FAULT'):
                    self.engine.request(1, 'operator', str(uuid.uuid4()))
        self.sender.assert_not_called()

    def test_database_failure_before_write_blocks_delivery(self):
        self.request()
        self.engine.db.execute('DROP TABLE commands')
        self.engine.db.commit()
        self.engine.tick(42)
        self.assertTrue(self.engine.storage_fault)
        self.sender.assert_not_called()
        with self.assertRaises(Rejected):
            self.result()

    def test_database_failure_after_write_never_retries(self):
        self.request()
        def break_audit(fd, target):
            self.engine.db.execute('DROP TABLE delivery_evidence')
            self.engine.db.commit()
        self.sender.side_effect = break_audit
        self.engine.tick(42)
        self.assertTrue(self.engine.storage_fault)
        self.engine.tick(42)
        self.sender.assert_called_once()
        with self.assertRaises(Rejected):
            self.result()

    def test_second_engine_cannot_take_same_database(self):
        with self.assertRaises(BlockingIOError):
            self.open_engine()

    def test_invalid_request_does_not_select_arm_force_or_target(self):
        for value in ('ARM', '', None, '21196'):
            with self.assertRaises(Rejected):
                self.engine.request(1, 'operator', value)
        self.sender.assert_not_called()

    def test_untrusted_heartbeat_is_rejected_before_intent(self):
        self.hb.autopilot = 8
        with self.assertRaises(Rejected):
            self.request()
        self.sender.assert_not_called()

    def test_more_than_254_requests_do_not_allocate_or_modify_legacy_identities(self):
        with self.engine.db:
            self.engine.db.execute('CREATE TABLE identities(component INTEGER PRIMARY KEY, request_id TEXT UNIQUE NOT NULL)')
            self.engine.db.executemany('INSERT INTO identities VALUES(?,?)',
                [(c, str(uuid.uuid4())) for c in range(1,256) if c != 190])
        before = list(self.engine.db.execute('SELECT * FROM identities ORDER BY component'))
        for _ in range(260):
            self.rid = str(uuid.uuid4())
            self.now += .1
            self.reader.messages['HEARTBEAT'] = self.now, self.hb
            self.send()
            self.heartbeat()
            self.assertEqual(self.result()['observation_state'], 'DISARMED_OBSERVED')
        self.assertEqual(self.sender.call_count, 260)
        self.assertEqual([tuple(r) for r in before],
                         [tuple(r) for r in self.engine.db.execute('SELECT * FROM identities ORDER BY component')])

    def test_legacy_confirmed_history_is_not_returned_as_command_proof(self):
        with self.engine.db:
            self.engine.db.execute('INSERT INTO commands VALUES(?,?,?,?,?,?,?)',
                (self.rid,1,'operator','CONFIRMED','NEW_DISARMED_HEARTBEAT',10.,10.))
        self.assertEqual(self.request()['delivery_state'], 'UNKNOWN')
        self.assert_unknown_observation(self.result())
        self.assertEqual(self.engine.db.execute('SELECT state FROM commands WHERE id=?',(self.rid,)).fetchone()[0], 'CONFIRMED')
        self.sender.assert_not_called()

    def test_disable_queued_request_rejects_without_write(self):
        self.request()
        self.engine.set_enabled(False)
        self.engine.tick(42)
        result = self.result()
        self.assertEqual(result['delivery_state'], 'REJECTED')
        self.assertEqual(result['reason'], 'FEATURE_DISABLED_NO_SEND')
        self.assertEqual(result['observation_reason'], 'FEATURE_DISABLED')
        self.assertFalse(result['observation_pending'])
        self.sender.assert_not_called()
        with self.assertRaisesRegex(Rejected, 'FEATURE_DISABLED'):
            self.engine.request(1,'operator',str(uuid.uuid4()))

    def test_disable_sent_request_retains_delivery_but_masks_observation(self):
        self.send()
        self.heartbeat()
        self.engine.set_enabled(False)
        self.assertEqual(self.result()['delivery_state'], 'SENT')
        self.assert_unknown_observation(self.result())
        self.assertFalse(self.result()['observation_pending'])
        self.engine.tick(42)
        self.sender.assert_called_once()

    def test_disabled_constructor_never_accepts_new_intent(self):
        self.engine.close()
        self.engine = None
        self.engine = self.open_engine(enabled=False)
        with self.assertRaisesRegex(Rejected, 'FEATURE_DISABLED'):
            self.request()
        self.sender.assert_not_called()

    def test_legacy_identity_rows_survive_restart(self):
        with self.engine.db:
            self.engine.db.execute('CREATE TABLE identities(component INTEGER PRIMARY KEY, request_id TEXT UNIQUE NOT NULL)')
            self.engine.db.execute('INSERT INTO identities VALUES(1,?)',('historical-id',))
        self.engine.close()
        self.engine = None
        self.engine = self.open_engine()
        self.assertEqual(tuple(self.engine.db.execute('SELECT * FROM identities').fetchone()), (1,'historical-id'))

    def test_disable_with_failed_audit_remains_closed_without_stopping_reader(self):
        self.request()
        self.engine.db.execute('DROP TABLE commands')
        self.engine.db.commit()
        self.engine.set_enabled(False)
        self.assertFalse(self.engine.enabled)
        self.assertTrue(self.engine.storage_fault)
        self.engine.tick(42)
        self.sender.assert_not_called()
        self.assertEqual(self.reader.state,'READING')


class PacketTests(unittest.TestCase):
    def test_standard_packet_fixed_source_and_no_serial_open(self):
        from pymavlink.dialects.v20 import ardupilotmega as mav
        write = Mock(side_effect=lambda fd, data: len(data))
        with patch('os.open', side_effect=AssertionError('Serial open forbidden')):
            sender = disarm_sender(write=write)
            sender(42, (1, 1))
        packet = write.call_args.args[1]
        message = mav.MAVLink(None).parse_buffer(packet)[0]
        self.assertEqual(message.get_type(), 'COMMAND_LONG')
        self.assertEqual(message.command, 400)
        self.assertEqual((message.target_system,message.target_component), (1,1))
        self.assertEqual([getattr(message, f'param{i}') for i in range(1, 8)], [0]*7)
        self.assertEqual(message.confirmation, 0)
        self.assertEqual((message.get_srcSystem(),message.get_srcComponent()), (255,190))

    def test_partial_write_is_not_retried(self):
        write = Mock(return_value=1)
        with self.assertRaises(OSError):
            disarm_sender(write=write)(42, (1, 1))
        write.assert_called_once()

    def test_shared_encoder_identity_is_restored(self):
        from pymavlink.dialects.v20 import ardupilotmega as mav
        encoder = mav.MAVLink(None, srcSystem=42, srcComponent=99)
        disarm_sender(write=lambda fd,data:len(data), encoder=encoder)(42,(1,1))
        self.assertEqual((encoder.srcSystem,encoder.srcComponent),(42,99))
