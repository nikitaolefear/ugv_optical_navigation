"""Offline regression tests for the GPIO17 candidate. No device or service access."""
from contextlib import ExitStack, closing
from pathlib import Path
import hashlib
import json
import re
import sys
import tempfile
import unittest
import uuid
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parent
BASES = {
    'frontend_gateway': Path('/opt/ugv-disarm/package'),
    'owner_externalnav': Path('/opt/ugv-tilted-observe/20261117/package'),
}


def verify_bases():
    for name, files in json.loads((ROOT / 'base-sha256.json').read_text()).items():
        for relative, expected in files.items():
            source = BASES[name] / relative
            if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                raise RuntimeError(f'Installed base differs: {source}')
    for path in ROOT.glob('*_*/**/*.py'):
        compile(path.read_bytes(), str(path), 'exec')


def paths():
    # Use installed dependencies and existing test fixtures. Override only
    # modified modules and assets in a disposable frontend copy.
    import shutil
    with tempfile.TemporaryDirectory() as tmp:
        frontend = Path(tmp) / 'frontend'
        shutil.copytree(BASES['frontend_gateway'], frontend)
        shutil.copytree(ROOT / 'frontend_gateway', frontend, dirs_exist_ok=True)
        yield [str(frontend / 'auth'), str(frontend), str(ROOT / 'owner_externalnav')]


class StopTests(unittest.TestCase):
    def test_gpio_latch_and_disabled_reset(self):
        from production_stop.owner_bridge import OwnerProductionStop

        reader = Mock()
        from threading import RLock
        reader.lock = RLock()
        reader.clock.return_value = 10.0
        reader.snapshot.return_value = {'armed': {'state': 'FRESH', 'value': 'DISARMED'}}
        bench = Mock()
        bench.disarm_pending.return_value = False
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / 'reset.sqlite3')
            stop = OwnerProductionStop(reader, bench, db)
            try:
                stop.gpio_observed('RELEASED', 10.0)
                status = stop.status(1, 'operator', '')
                self.assertEqual(status['contacts']['FRONT'], 'RELEASED')
                self.assertTrue(status['stop_latch'])
                self.assertFalse(status['physical_drive_interlocked'])
                self.assertFalse(status['reset_available'])
                self.assertEqual(set(stop.rpc_operations()), {'STOP_STATUS'})
                reader.clock.return_value = 10.1
                stop.gpio_observed('PRESSED', 10.1)
                self.assertEqual(stop.status(1, 'operator', '')['contacts']['FRONT'], 'PRESSED')
                request_id = str(uuid.uuid4())
                result = stop.reset(1, 'operator', request_id)
                self.assertFalse(result['reset_accepted'])
                self.assertFalse(result['movement_allowed'])
            finally:
                stop.close()
            stop = OwnerProductionStop(reader, bench, db)
            try:
                self.assertTrue(stop.reset(1, 'operator', request_id)['replayed'])
                with self.assertRaises(PermissionError):
                    stop.reset(2, 'another', request_id)
            finally:
                stop.close()

    def test_reset_requires_fresh_evidence_and_does_not_permit_motion(self):
        from threading import RLock
        from production_stop.owner_bridge import OwnerProductionStop
        from production_stop.state import ResetConditions

        reader = Mock(lock=RLock())
        reader.clock.return_value = 10.0
        reader.snapshot.return_value = {'armed': {'state': 'FRESH', 'value': 'DISARMED'}}
        bench = Mock()
        bench.disarm_pending.return_value = False
        native = Mock()
        native.enforces_elrs_stop_latch.return_value = True
        native.blocked_at.return_value = 10.0
        native.reset_conditions.return_value = ResetConditions(9.0, True, True, True, True)
        with tempfile.TemporaryDirectory() as tmp:
            stop = OwnerProductionStop(reader, bench, str(Path(tmp) / 'reset.db'), native=native)
            self.enterContext(closing(stop))
            stop.gpio_observed('RELEASED', 10.0)
            self.assertFalse(stop.reset(1, 'operator', str(uuid.uuid4()))['reset_accepted'])
            native.reset_conditions.return_value = ResetConditions(10.0, True, True, True, True)
            result = stop.reset(1, 'operator', str(uuid.uuid4()))
            self.assertTrue(result['reset_accepted'])
            self.assertFalse(result['stop_latch'])
            self.assertFalse(result['movement_allowed'])
            self.assertFalse(result['motion_permitted'])
            reader.clock.return_value = 11.0
            self.assertTrue(stop.status(1, 'operator', '')['stop_latch'])

    def test_hardwired_feedback_and_watchdog(self):
        from dataclasses import replace
        from threading import RLock
        from production_stop.hardwired_interlock import HardwiredInterlock, InterlockFeedback
        from production_stop.owner_bridge import OwnerProductionStop

        reader = Mock(lock=RLock())
        reader.clock.return_value = 10.0
        reader.snapshot.return_value = {'armed': {'state': 'FRESH', 'value': 'DISARMED'}}
        bench = Mock()
        bench.disarm_pending.return_value = False
        io = Mock()
        native = HardwiredInterlock(io, reader)
        safe = InterlockFeedback(
            observed_at=10.0, controller_healthy=True, stop_inputs_released=True,
            outputs_deenergized=True, k1_open=True, k2_open=True, traction_bus_safe=True,
            restart_required=True, wheels_stationary=True,
            traction_observed_at=10.0, wheels_observed_at=10.0,
            left_brake_hold_confirmed=True, left_brake_observed_at=10.0,
            right_brake_hold_confirmed=True, right_brake_observed_at=10.0,
        )
        with tempfile.TemporaryDirectory() as tmp, closing(OwnerProductionStop(
                reader, bench, str(Path(tmp) / 'reset.db'), native=native)) as stop:
            def observe(at, contact='RELEASED', **changes):
                reader.clock.return_value = at
                io.exchange.return_value = replace(
                    safe, observed_at=at, traction_observed_at=at, wheels_observed_at=at,
                    left_brake_observed_at=at, right_brake_observed_at=at, **changes,
                )
                stop.gpio_observed(contact, at)
                return stop.status(1, 'operator', '')

            status = observe(10.0, k1_open=False)
            self.assertFalse(status['physical_drive_interlocked'])
            io.exchange.assert_called_with(pulse_watchdog=False)
            status = observe(10.1, traction_bus_safe=False)
            self.assertFalse(status['reset_available'])
            self.assertIsNone(native.arm_gate_verified_at())
            status = observe(10.3, wheels_stationary=False)
            self.assertFalse(status['reset_available'])
            status = observe(10.4)
            self.assertTrue(status['reset_available'])
            io.exchange.reset_mock()
            self.assertTrue(stop.reset(1, 'operator', str(uuid.uuid4()))['reset_accepted'])
            io.exchange.assert_not_called()
            status = observe(10.5)
            io.exchange.assert_called_once_with(pulse_watchdog=True)
            self.assertFalse(status['movement_allowed'])
            status = observe(10.6, 'PRESSED', stop_inputs_released=False)
            io.exchange.assert_called_with(pulse_watchdog=False)
            self.assertTrue(status['physical_drive_interlocked'])
            self.assertFalse(status['reset_available'])
            self.assertIsNone(native.arm_gate_verified_at())
            # Repeating a cached frame loses verification, even within its age limit.
            reader.clock.return_value = 10.7
            stop.gpio_observed('RELEASED', 10.7)
            self.assertFalse(stop.status(1, 'operator', '')['physical_drive_interlocked'])
            self.assertTrue(observe(10.8)['reset_available'])
            reader.clock.return_value = 11.4
            self.assertFalse(stop.status(1, 'operator', '')['reset_available'])

    def _assert_missing_physical_evidence_blocks_reset(self, changes):
        from threading import RLock
        from production_stop.hardwired_interlock import HardwiredInterlock, InterlockFeedback
        from production_stop.owner_bridge import OwnerProductionStop

        reader = Mock(lock=RLock())
        reader.clock.return_value = 10.0
        reader.snapshot.return_value = {'armed': {'state': 'FRESH', 'value': 'DISARMED'}}
        bench = Mock()
        bench.disarm_pending.return_value = False
        sample = dict(
            observed_at=10.0, controller_healthy=True, stop_inputs_released=True,
            outputs_deenergized=True, k1_open=True, k2_open=True, traction_bus_safe=True,
            restart_required=True, wheels_stationary=True,
            traction_observed_at=10.0, wheels_observed_at=10.0,
            left_brake_hold_confirmed=True, left_brake_observed_at=10.0,
            right_brake_hold_confirmed=True, right_brake_observed_at=10.0,
        )
        sample.update(changes)
        io = Mock()
        io.exchange.return_value = InterlockFeedback(**sample)
        native = HardwiredInterlock(io, reader)
        with tempfile.TemporaryDirectory() as tmp, closing(OwnerProductionStop(
                reader, bench, str(Path(tmp) / 'stop.db'), native=native)) as stop:
            stop.gpio_observed('RELEASED', 10.0)
            status = stop.status(1, 'operator', '')
            self.assertFalse(status['reset_available'])
            self.assertTrue(status['stop_latch'])
            io.exchange.assert_called_once_with(pulse_watchdog=False)

    def test_reset_blocks_each_missing_physical_timestamp(self):
        for channel in ('traction', 'wheels', 'left_brake', 'right_brake'):
            with self.subTest(channel=channel):
                self._assert_missing_physical_evidence_blocks_reset({f'{channel}_observed_at': None})

    def test_reset_blocks_each_stale_physical_timestamp(self):
        for channel in ('traction', 'wheels', 'left_brake', 'right_brake'):
            with self.subTest(channel=channel):
                self._assert_missing_physical_evidence_blocks_reset({f'{channel}_observed_at': 9.5})

    def test_reset_blocks_each_unknown_physical_signal(self):
        for signal in ('traction_bus_safe', 'wheels_stationary',
                       'left_brake_hold_confirmed', 'right_brake_hold_confirmed'):
            with self.subTest(signal=signal):
                self._assert_missing_physical_evidence_blocks_reset({signal: None})

    def test_power_loss_rolls_back_partial_reset_and_latches_on_start(self):
        import sqlite3
        import subprocess
        from production_stop.stop_journal import StopJournal

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'stop.db'
            request_id = str(uuid.uuid4())
            with closing(StopJournal(path)) as journal:
                journal.record('FRONT_PRESSED', True, {})
                journal.record_reset(request_id, 1, 'operator', 10.0, True, False, [])
                self.assertEqual(journal.db.execute('PRAGMA synchronous').fetchone(), (3,))
            child = r"""
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA cache_size=4')
db.execute('BEGIN IMMEDIATE')
db.execute('UPDATE stop_state SET latched=1')
db.execute("DELETE FROM reset_requests")
for i in range(15):
    db.execute("INSERT INTO stop_events(boot_id,utc_ns,event,latched,details) VALUES('boot',0,'PARTIAL',1,?)",
               ('x' * 8192,))
os._exit(52)
"""
            result = subprocess.run([sys.executable, '-B', '-c', child, str(path)], check=False)
            self.assertEqual(result.returncode, 52)
            self.assertTrue(path.with_name(path.name + '-journal').is_file())
            with closing(StopJournal(path)) as journal:
                self.assertEqual(journal.previous_actor(request_id), (1,))
                self.assertEqual(journal.db.execute('SELECT latched FROM stop_state').fetchone(), (1,))
                events = journal.db.execute('SELECT event,latched FROM stop_events ORDER BY sequence').fetchall()
                self.assertEqual(events, [('OWNER_START', 1), ('FRONT_PRESSED', 1),
                                          ('RESET_ACCEPTED', 0), ('OWNER_START', 1)])
            with sqlite3.connect(path) as database:
                self.assertEqual(database.execute('PRAGMA integrity_check').fetchone(), ('ok',))

    def test_corrupt_or_incomplete_journal_blocks_reset_and_preserves_evidence(self):
        from threading import RLock
        from production_stop.owner_bridge import OwnerProductionStop

        reader = Mock(lock=RLock())
        reader.clock.return_value = 10.0
        reader.snapshot.return_value = {'armed': {'state': 'FRESH', 'value': 'DISARMED'}}
        bench = Mock()
        bench.disarm_pending.return_value = False
        for original in (b'', b'not a SQLite journal'):
            with self.subTest(original=original), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'stop.db'
                path.touch(mode=0o600)
                path.write_bytes(original)
                with self.assertLogs('production_stop.owner_bridge', level='ERROR'):
                    stop = OwnerProductionStop(reader, bench, path)
                with closing(stop):
                    stop.gpio_observed('RELEASED', 10.0)
                    status = stop.reset(1, 'operator', str(uuid.uuid4()))
                    self.assertTrue(status['stop_latch'])
                    self.assertFalse(status['reset_available'])
                    self.assertFalse(status['reset_accepted'])
                    self.assertIn('RESET_STORAGE_FAULT', status['blocking_reasons'])
                    self.assertEqual(path.read_bytes(), original)

    def test_reset_commit_failure_keeps_watchdog_blocked(self):
        import sqlite3
        from threading import RLock
        from production_stop.owner_bridge import OwnerProductionStop
        from production_stop.state import ResetConditions

        reader = Mock(lock=RLock())
        reader.clock.return_value = 10.0
        reader.snapshot.return_value = {'armed': {'state': 'FRESH', 'value': 'DISARMED'}}
        bench = Mock()
        bench.disarm_pending.return_value = False
        native = Mock()
        native.enforces_elrs_stop_latch.return_value = True
        native.blocked_at.return_value = 10.0
        native.reset_conditions.return_value = ResetConditions(10.0, True, True, True, True)
        with tempfile.TemporaryDirectory() as tmp, closing(OwnerProductionStop(
                reader, bench, Path(tmp) / 'stop.db', native=native)) as stop:
            stop.gpio_observed('RELEASED', 10.0)
            self.assertTrue(stop.status(1, 'operator', '')['reset_available'])

            def fail_commit(*_):
                self.assertTrue(stop.committed_latch)
                raise sqlite3.OperationalError('simulated sync failure')

            stop.journal.record_reset = fail_commit
            with self.assertLogs('production_stop.owner_bridge', level='ERROR'):
                result = stop.reset(1, 'operator', str(uuid.uuid4()))
            self.assertFalse(result['reset_accepted'])
            self.assertTrue(result['stop_latch'])
            self.assertTrue(stop.committed_latch)

    def test_brake_diagnostics_are_opt_in_and_emit_only_parameter_reads(self):
        import importlib.util
        from threading import RLock
        from pymavlink.dialects.v20 import ardupilotmega as mav

        path = ROOT / 'owner_externalnav/owner/stop_latch_test/diagnostics.py'
        spec = importlib.util.spec_from_file_location('candidate_diagnostics', path)
        diagnostics = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(diagnostics)
        reader = Mock(lock=RLock(), state='READING', source=(1, 1))
        hb = Mock(base_mode=0)
        reader.clock.return_value = 0.0
        reader.messages = {'HEARTBEAT': (0.0, hb)}
        encoder = mav.MAVLink(None, srcSystem=255, srcComponent=190)
        packets = []

        def write(fd, packet):
            self.assertIsNone(fd)
            packets.append(mav.MAVLink(None).parse_char(packet))
            return len(packet)

        diagnostics.Diagnostics(reader, encoder, write=write).tick(None)
        self.assertEqual(packets, [])
        query = diagnostics.Diagnostics(reader, encoder, write=write, brake_query=True)
        query.tick(None, command_pending=True)
        hb.base_mode = 128
        query.tick(None)
        hb.base_mode = 0
        reader.messages['HEARTBEAT'] = (-2.0, hb)
        query.tick(None)
        self.assertEqual(packets, [])
        for second in range(len(diagnostics.BRAKE_PARAMS)):
            reader.clock.return_value = float(second)
            reader.messages['HEARTBEAT'] = (float(second), hb)
            query.tick(None)
            query.tick(None)  # Rate limit survives duplicate ticks.
        self.assertEqual(len(packets), len(diagnostics.BRAKE_PARAMS))
        self.assertEqual([packet.get_type() for packet in packets],
                         ['PARAM_REQUEST_READ'] * len(diagnostics.BRAKE_PARAMS))
        self.assertEqual([packet.param_id for packet in packets], list(diagnostics.BRAKE_PARAMS))
        self.assertTrue(all((p.target_system, p.target_component, p.param_index) == (1, 1, -1)
                            for p in packets))
        self.assertEqual(query.snapshot()['parameters'], {})

    def test_reset_requests_brake_release_without_position_sensors(self):
        from dataclasses import replace
        from threading import RLock
        from pymavlink.dialects.v20 import ardupilotmega as mav
        from production_stop.brake_link import BrakeLink
        from disarm_control.ipc import OwnerClient, OwnerServer
        from stop_latch_test.diagnostics import Diagnostics
        from production_stop.hardwired_interlock import HardwiredInterlock, InterlockFeedback
        from production_stop.owner_bridge import OwnerProductionStop

        reader = Mock(lock=RLock())
        reader.clock.return_value = 10.0
        reader.source, reader.state = (1, 1), 'READING'
        reader.heartbeat_safety_serial = 0
        reader.snapshot.return_value = {'armed': {'state': 'FRESH', 'value': 'DISARMED'}}
        disarm = Mock(active=None)
        disarm._heartbeat.return_value = (10.0, Mock(base_mode=0), (1, 1), 1, 0)
        bench = Mock()
        bench.disarm_pending.return_value = False
        io = Mock()
        native = HardwiredInterlock(io, reader)
        base = InterlockFeedback(
            observed_at=10.0, controller_healthy=True, stop_inputs_released=True,
            outputs_deenergized=True, k1_open=True, k2_open=True, traction_bus_safe=True,
            restart_required=True, wheels_stationary=True,
            traction_observed_at=10.0, wheels_observed_at=10.0,
            left_brake_hold_confirmed=True, left_brake_observed_at=10.0,
            right_brake_hold_confirmed=True, right_brake_observed_at=10.0,
        )
        rc = {'at': 10.0, 'channels': {'8': 2000}}
        encoder = mav.MAVLink(None, srcSystem=255, srcComponent=190)
        packets = []

        def write(fd, packet):
            self.assertIsNone(fd)
            packets.append(mav.MAVLink(None).parse_char(packet))
            return len(packet)

        with tempfile.TemporaryDirectory() as tmp, closing(OwnerProductionStop(
                reader, bench, Path(tmp) / 'stop.db', native=native)) as stop, ExitStack() as resources:
            brake = BrakeLink(reader, disarm, stop, encoder, lambda: rc, write=write)
            stop.brake = brake
            diagnostics = Diagnostics(reader, encoder, pwm_observe=True)
            diagnostics.brake = brake

            def pwm(left, right, *, port=0, system=1):
                frame = mav.MAVLink_servo_output_raw_message(0, port, 0, 0, 0, 0, left, right, 0, 0)
                payload = frame.pack(mav.MAVLink(None, srcSystem=system, srcComponent=1))
                diagnostics.observe(mav.MAVLink(None).parse_char(payload))

            def confirmation():
                return diagnostics.snapshot()['brake_pwm']['release_confirmation']

            brake.hold_only = True
            self.assertEqual(set(stop.rpc_operations()), {'STOP_STATUS'})
            brake.hold_only = False
            actor = {'id': 1, 'username': 'operator'}
            path = Path(tmp) / 'owner.sock'
            server = OwnerServer(disarm, path, stop_operations=stop.rpc_operations())
            server.start()
            resources.callback(server.close)
            client = OwnerClient(path)

            def step(at, channel=True, contact='RELEASED', healthy=True):
                reader.clock.return_value = rc['at'] = at
                rc['channels'] = {'8': 2000} if channel else {}
                io.exchange.return_value = replace(base, observed_at=at, traction_observed_at=at, wheels_observed_at=at,
                                                   left_brake_observed_at=at, right_brake_observed_at=at,
                                                   controller_healthy=healthy,
                                                   stop_inputs_released=contact == 'RELEASED')
                stop.gpio_observed(contact, at)
                brake.tick(None)

            step(10.0, channel=False)
            self.assertFalse(stop.reset(1, 'operator', str(uuid.uuid4()))['reset_accepted'])
            step(10.1, healthy=False)
            self.assertFalse(stop.reset(1, 'operator', str(uuid.uuid4()))['reset_accepted'])
            step(10.2, contact='PRESSED')
            self.assertFalse(stop.reset(1, 'operator', str(uuid.uuid4()))['reset_accepted'])
            step(10.3)
            self.assertEqual((packets[-1].get_type(), packets[-1].name, packets[-1].value),
                             ('NAMED_VALUE_INT', 'UGVBRAKE03', 0))
            request_id = str(uuid.uuid4())
            pwm(2000, 2000)
            reply = client.call('STOP_RESET', actor, request_id)
            self.assertTrue(reply['ok'])
            result = reply['command']
            self.assertTrue(result['reset_accepted'])
            self.assertFalse(result['stop_latch'])
            self.assertFalse(result['motion_permitted'])
            self.assertFalse(result['movement_allowed'])
            self.assertEqual(result['brake_release'], 'PENDING')
            self.assertFalse(brake.traction_ready())
            step(10.4)
            self.assertEqual((brake.phase, packets[-1].value), ('SENT', 2))
            io.exchange.assert_called_with(pulse_watchdog=False)
            self.assertEqual(confirmation()['state'], 'AWAITING_POST_RELEASE_PWM')
            step(10.5)
            pwm(2000, 2000, port=1)
            pwm(2000, 2000, system=2)
            self.assertEqual(confirmation()['state'], 'AWAITING_POST_RELEASE_PWM')
            pwm(2000, 999)
            self.assertEqual(confirmation()['state'], 'MISMATCH')
            pwm(2000, 2000)
            self.assertEqual(confirmation(), {'state': 'CONFIRMED', 'request_id': request_id})
            reader.heartbeat_safety_serial += 1
            self.assertEqual(confirmation()['state'], 'AWAITING_POST_RELEASE_PWM')
            reader.heartbeat_safety_serial -= 1
            reader.clock.return_value = 13.5
            self.assertEqual(confirmation()['state'], 'AWAITING_POST_RELEASE_PWM')
            reader.clock.return_value = 10.5
            self.assertEqual(set(stop.rpc_operations()), {'STOP_STATUS', 'STOP_RESET'})
            self.assertTrue(all(packet.get_type() == 'NAMED_VALUE_INT' for packet in packets))
            self.assertEqual(packets[-1].value, 1)
            self.assertFalse(brake.traction_ready())
            io.exchange.assert_called_with(pulse_watchdog=False)
            self.assertFalse(stop.status(1, 'operator', '')['movement_allowed'])
            self.assertTrue(stop.reset(1, 'operator', request_id)['replayed'])
            self.assertEqual(sum(packet.value == 2 for packet in packets), 1)

            step(10.6, channel=False)
            self.assertEqual(brake.fault_reason, 'BRAKE_RC_LINK_LOST')
            self.assertTrue(stop.core.snapshot().stop_latch)
            self.assertEqual(packets[-1].value, 0)
            io.exchange.assert_called_with(pulse_watchdog=False)

            step(10.7)
            self.assertTrue(stop.reset(1, 'operator', str(uuid.uuid4()))['reset_accepted'])
            step(10.8)
            step(10.9, contact='PRESSED')
            self.assertEqual((brake.phase, packets[-1].value), ('HOLD', 0))
            step(11.0)
            self.assertTrue(stop.reset(1, 'operator', str(uuid.uuid4()))['reset_accepted'])
            step(11.1)
            disarm.active = str(uuid.uuid4())
            brake.on_disarm(disarm.active, None)
            brake.tick(None)
            self.assertTrue(stop.core.snapshot().stop_latch)
            self.assertEqual(packets[-1].value, 0)
            events = stop.journal.db.execute('SELECT event, details FROM stop_events').fetchall()
            self.assertTrue(any(event == 'BRAKE_RELEASE_FAULT' for event, _ in events))
            self.assertTrue(any(event == 'BRAKE_RELEASE_REQUEST_SENT' and
                                'PHYSICAL_POSITION_UNCONFIRMED' in details for event, details in events))

    def test_private_rpc_dispatch_only_by_allowlisted_operation(self):
        from disarm_control.ipc import OwnerClient, OwnerServer

        engine = Mock()
        status = Mock(return_value={'stop_latch': True, 'reset_available': False})
        broken_arm = Mock(side_effect=OSError('ARM database unavailable'))
        priority_disarm = Mock(return_value={'state': 'QUEUED'})
        actor = {'id': 1, 'username': 'operator'}
        request_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'owner.sock'
            server = OwnerServer(engine, path, stop_operations={'STOP_STATUS': status},
                                 arm_operations={'ARM': broken_arm}, disarm_request=priority_disarm)
            server.start()
            try:
                client = OwnerClient(path)
                self.assertTrue(client.call('STOP_STATUS', actor, request_id)['ok'])
                status.assert_called_once_with(1, 'operator', request_id)
                self.assertFalse(client.call('STOP_RESET', actor, request_id)['ok'])
                self.assertFalse(client.call('ARM', actor, request_id)['ok'])
                self.assertTrue(client.call('DISARM', actor, request_id)['ok'])
                priority_disarm.assert_called_once_with(1, 'operator', request_id)
                engine.request.assert_not_called()
            finally:
                server.close()

    def test_gateway_off_does_not_forward_reset(self):
        from disarm_control.gateway import CommandGateway
        from disarm_control.engine import Rejected

        backend = Mock()
        backend.call.return_value = {'ok': True, 'command': {
            'stop_latch': True, 'reset_available': True, 'physical_drive_interlocked': True,
        }}
        gateway = CommandGateway(backend)
        request_id = str(uuid.uuid4())
        self.assertFalse(gateway.stop_operation('STOP_STATUS', 1, 'operator',
                                                request_id)['reset_available'])
        backend.reset_mock()
        with self.assertRaises(Rejected):
            gateway.stop_operation('STOP_RESET', 1, 'operator', request_id)
        backend.call.assert_not_called()

    def test_mission_reset_authorization_and_audit(self):
        from web_hmi.app import create_app
        from web_hmi.test_support import CONFIG, PASSWORD, TestEnvironment

        env = TestEnvironment()
        self.addCleanup(env.close)
        owner = Mock()
        app = create_app(config=CONFIG, auth_store=env.store, disarm_client=owner)
        client = app.test_client()
        route = '/api/production-stop/reset'
        request_id = str(uuid.uuid4())
        body = {'request_id': request_id}

        def login():
            token = re.search(r'name="csrf_token" value="([^"]+)"', client.get('/login').text)[1]
            self.assertEqual(client.post('/login', data={
                'username': 'testadmin', 'password': PASSWORD, 'csrf_token': token,
            }).status_code, 303)
            page = client.get('/').text
            self.assertIn('id="pixhawk-arm"', page)
            self.assertIn('id="reset" disabled>RESET · без запуску', page)
            return {'X-CSRF-Token': re.search(r'<meta name="csrf-token" content="([^"]+)"', page)[1]}

        self.assertEqual(client.post(route, json=body).status_code, 401)
        self.assertEqual(client.post(route, json=body, headers=login()).status_code, 403)
        owner.call.assert_not_called()
        with env.store.transaction() as db:
            db.execute("INSERT INTO role_permissions VALUES ('admin', 'stop.reset')")
        client = app.test_client()
        headers = login()
        self.assertEqual(client.post(route, json=body).status_code, 403)
        self.assertEqual(client.post(route, json=body, headers={
            **headers, 'Origin': 'https://outside.invalid',
        }).status_code, 403)
        self.assertEqual(client.post(route, json={**body, 'stationary': True}, headers=headers).status_code, 400)
        owner.call.assert_not_called()

        def reply(operation, actor, rid):
            self.assertEqual((operation, rid), ('STOP_RESET', request_id))
            self.assertTrue(any(row['action'] == 'stop.reset' and row['result'] == 'PENDING'
                                for row in env.store.logs()))
            return {'ok': True, 'command': {'request_id': rid, 'stop_latch': True,
                                             'reset_available': False, 'reset_accepted': False}}

        owner.call.side_effect = reply
        self.assertEqual(client.post(route, json=body, headers=headers).status_code, 409)
        self.assertTrue(any(row['action'] == 'stop.reset' and row['result'] == 'DENIED'
                            for row in env.store.logs()))


    def _mission_fixture(self):
        from threading import RLock
        from production_stop.mission_resume import PhysicalConfirmation, ResumeAcceptance, ResumeEvidence
        from production_stop.owner_bridge import OwnerProductionStop
        from production_stop.state import ResetConditions

        folder = self.enterContext(tempfile.TemporaryDirectory())
        reader = Mock(lock=RLock())
        reader.clock.return_value = 10.0
        reader.snapshot.return_value = {'armed': {'state': 'FRESH', 'value': 'DISARMED'}}
        bench = Mock()
        bench.disarm_pending.return_value = False
        native = Mock()
        native.enforces_elrs_stop_latch.return_value = True
        native.blocked_at.return_value = 10.0
        native.reset_conditions.return_value = ResetConditions(10.0, True, True, True, True)
        mission = Mock()
        mission.resume.return_value = ResumeAcceptance.ACCEPTED
        sample = PhysicalConfirmation(True, 10.2)
        mission.evidence.return_value = ResumeEvidence(sample, sample, sample, sample)
        stop = self.enterContext(closing(OwnerProductionStop(
            reader, bench, Path(folder) / 'stop.db', native=native, mission_supervisor=mission)))
        brake = Mock(hold_only=False, phase='HOLD')
        brake.reset_link_ready.return_value = True
        brake.traction_ready.return_value = False
        stop.brake = brake
        stop.gpio_observed('RELEASED', 10.0)

        def reset_and_report_release():
            reset_id = str(uuid.uuid4())
            self.assertTrue(stop.reset(1, 'operator', reset_id)['reset_accepted'])
            mission.resume.assert_not_called()
            brake.phase, brake.request_id, brake.release_sent_at = 'SENT', reset_id, 10.1
            reader.clock.return_value = 10.3
            return reset_id

        return stop, mission, reset_and_report_release

    def test_mission_resume_requires_independent_physical_evidence(self):
        from dataclasses import replace
        from production_stop.mission_resume import PhysicalConfirmation

        stop, mission, prepare = self._mission_fixture()
        command = stop.rpc_operations()['MISSION_RESUME']
        self.assertFalse(command(1, 'operator', str(uuid.uuid4()))['resume_accepted'])
        prepare()
        evidence = mission.evidence.return_value
        for field in evidence.__dataclass_fields__:
            for signal in (None, PhysicalConfirmation(False, 10.2),
                           PhysicalConfirmation(True, 9.5), PhysicalConfirmation(True, 10.1)):
                with self.subTest(field=field, signal=signal):
                    mission.evidence.return_value = replace(evidence, **{field: signal})
                    reply = command(1, 'operator', str(uuid.uuid4()))
                    self.assertEqual(reply['reason'], 'FRESH_PHYSICAL_EVIDENCE_REQUIRED')
        mission.resume.assert_not_called()

    def test_mission_resume_once_per_reset_and_request(self):
        stop, mission, prepare = self._mission_fixture()
        reset_id = prepare()
        request_id = str(uuid.uuid4())
        reply = stop.mission_resume(1, 'operator', request_id)
        self.assertTrue(reply['resume_accepted'])
        self.assertTrue(stop.mission_resume(1, 'operator', request_id)['replayed'])
        self.assertFalse(stop.mission_resume(1, 'operator', str(uuid.uuid4()))['resume_accepted'])
        with self.assertRaises(PermissionError):
            stop.mission_resume(2, 'other-operator', request_id)
        mission.resume.assert_called_once_with(request_id, reset_id)

    def test_new_gpio_stop_revokes_mission_resume(self):
        stop, mission, prepare = self._mission_fixture()
        prepare()
        stop.gpio_observed('PRESSED', 10.3)
        self.assertIsNone(stop.resume_reset_id)
        stop.gpio_observed('RELEASED', 10.3)
        self.assertFalse(stop.mission_resume(1, 'operator', str(uuid.uuid4()))['resume_accepted'])
        mission.resume.assert_not_called()

    def test_uncertain_mission_handoff_is_not_retried(self):
        stop, mission, prepare = self._mission_fixture()
        prepare()
        mission.resume.side_effect = TimeoutError()
        request_id = str(uuid.uuid4())
        self.assertEqual(stop.mission_resume(1, 'operator', request_id)['reason'],
                         'RESUME_SUPERVISOR_ERROR')
        self.assertTrue(stop.core.snapshot().stop_latch)
        self.assertTrue(stop.mission_resume(1, 'operator', request_id)['replayed'])
        self.assertFalse(stop.mission_resume(1, 'operator', str(uuid.uuid4()))['resume_accepted'])
        mission.resume.assert_called_once()

    def _mission_adapter_fixture(self):
        from production_stop.mission_resume import MissionSupervisorAdapter

        stop, supervisor, prepare = self._mission_fixture()
        exchange = Mock()
        stop.mission_supervisor = MissionSupervisorAdapter(
            exchange, supervisor.evidence, stop.reader.clock)
        return stop, exchange, prepare

    def test_adapter_acceptance_is_separate_from_running(self):
        stop, exchange, prepare = self._mission_adapter_fixture()
        reset_id, request_id = prepare(), str(uuid.uuid4())
        ids = {'request_id': request_id, 'reset_id': reset_id}
        exchange.return_value = dict(ids, acceptance='ACCEPTED', mission_state='RUNNING')
        reply = stop.mission_resume(1, 'operator', request_id)
        self.assertTrue(reply['resume_accepted'])
        self.assertEqual(reply['mission_state'], 'UNKNOWN')
        exchange.assert_called_once_with('MISSION_RESUME', request_id, reset_id, timeout=.15)
        exchange.return_value = dict(ids, mission_state='RUNNING', observed_at=9.0)
        self.assertEqual(stop.mission_supervisor.mission_state(request_id, reset_id), 'UNKNOWN')
        exchange.return_value = dict(ids, mission_state='RUNNING', observed_at=10.3)
        self.assertEqual(stop.mission_supervisor.mission_state(request_id, reset_id), 'RUNNING')
        exchange.assert_called_with('MISSION_STATUS', request_id, reset_id, timeout=.15)

    def test_adapter_refusal_consumes_reset(self):
        stop, exchange, prepare = self._mission_adapter_fixture()
        reset_id, request_id = prepare(), str(uuid.uuid4())
        exchange.return_value = dict(request_id=request_id, reset_id=reset_id, acceptance='REFUSED')
        reply = stop.mission_resume(1, 'operator', request_id)
        self.assertFalse(reply['resume_accepted'])
        self.assertEqual(reply['reason'], 'RESUME_SUPERVISOR_REFUSED')
        self.assertFalse(stop.mission_resume(1, 'operator', str(uuid.uuid4()))['resume_accepted'])
        exchange.assert_called_once()

    def test_adapter_timeout_is_unknown_and_never_retried(self):
        stop, exchange, prepare = self._mission_adapter_fixture()
        prepare()
        request_id = str(uuid.uuid4())
        exchange.side_effect = TimeoutError()
        result = stop.mission_resume(1, 'operator', request_id)
        self.assertEqual((result['reason'], result['mission_state']),
                         ('RESUME_OUTCOME_UNKNOWN', 'UNKNOWN'))
        self.assertTrue(stop.core.snapshot().stop_latch)
        self.assertTrue(stop.mission_resume(1, 'operator', request_id)['replayed'])
        self.assertFalse(stop.mission_resume(1, 'operator', str(uuid.uuid4()))['resume_accepted'])
        exchange.assert_called_once()

    def test_adapter_uncorrelated_ack_is_unknown(self):
        stop, exchange, prepare = self._mission_adapter_fixture()
        reset_id = prepare()
        exchange.return_value = dict(request_id=str(uuid.uuid4()), reset_id=reset_id,
                                     acceptance='ACCEPTED')
        self.assertEqual(stop.mission_resume(1, 'operator', str(uuid.uuid4()))['reason'],
                         'RESUME_OUTCOME_UNKNOWN')
        exchange.assert_called_once()

    def test_adapter_new_front_press_prevents_dispatch(self):
        stop, exchange, prepare = self._mission_adapter_fixture()
        prepare()
        stop.gpio_observed('PRESSED', 10.3)
        stop.gpio_observed('RELEASED', 10.3)
        self.assertFalse(stop.mission_resume(1, 'operator', str(uuid.uuid4()))['resume_accepted'])
        exchange.assert_not_called()

    def test_adapter_replayed_acceptance_does_not_dispatch(self):
        stop, exchange, prepare = self._mission_adapter_fixture()
        reset_id, request_id = prepare(), str(uuid.uuid4())
        exchange.return_value = dict(request_id=request_id, reset_id=reset_id, acceptance='ACCEPTED')
        self.assertTrue(stop.mission_resume(1, 'operator', request_id)['resume_accepted'])
        self.assertTrue(stop.mission_resume(1, 'operator', request_id)['replayed'])
        exchange.assert_called_once()

    def test_mission_resume_gateway_requires_opt_in(self):
        from disarm_control.engine import Rejected
        from disarm_control.gateway import CommandGateway

        backend = Mock()
        gateway = CommandGateway(backend)
        request_id = str(uuid.uuid4())
        with self.assertRaisesRegex(Rejected, 'MISSION_RESUME_DISABLED'):
            gateway.stop_operation('MISSION_RESUME', 1, 'operator', request_id)
        backend.call.assert_not_called()
        gateway.mission_resume_enabled = True
        result = {'request_id': request_id, 'resume_accepted': True}
        backend.call.return_value = {'ok': True, 'command': result}
        self.assertEqual(gateway.stop_operation('MISSION_RESUME', 1, 'operator', request_id), result)
        backend.call.assert_called_once_with(
            'MISSION_RESUME', {'id': 1, 'username': 'operator'}, request_id)

    def test_mission_resume_hmi_confirmation_and_permission(self):
        from web_hmi.app import create_app
        from web_hmi.test_support import CONFIG, PASSWORD, TestEnvironment

        env = TestEnvironment()
        self.addCleanup(env.close)
        owner = Mock()
        app = create_app(config=CONFIG, auth_store=env.store, disarm_client=owner)
        route = '/api/production-stop/resume'
        request_id = str(uuid.uuid4())
        body = {'request_id': request_id, 'confirm': True}

        def session():
            client = app.test_client()
            token = re.search(r'name="csrf_token" value="([^"]+)"', client.get('/login').text)[1]
            self.assertEqual(client.post('/login', data={
                'username': 'testadmin', 'password': PASSWORD, 'csrf_token': token,
            }).status_code, 303)
            csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', client.get('/').text)[1]
            return client, {'X-CSRF-Token': csrf}

        client, headers = session()
        self.assertEqual(client.post(route, json=body, headers=headers).status_code, 403)
        with env.store.transaction() as db:
            db.execute("INSERT INTO role_permissions VALUES ('admin', 'mission.resume')")
        client, headers = session()
        self.assertEqual(client.post(route, json=body).status_code, 403)
        self.assertEqual(client.post(route, json={'request_id': request_id}, headers=headers).status_code, 400)
        owner.call.assert_not_called()
        owner.call.return_value = {'ok': True, 'command': {
            'request_id': request_id, 'resume_accepted': True,
        }}
        self.assertEqual(client.post(route, json=body, headers=headers).status_code, 200)
        self.assertEqual(owner.call.call_args.args[0], 'MISSION_RESUME')
        self.assertTrue(any(row['action'] == 'mission.resume' and row['result'] == 'ACCEPTED'
                            for row in env.store.logs()))

        unknown_id = str(uuid.uuid4())
        owner.call.return_value = {'ok': True, 'command': {
            'request_id': unknown_id, 'resume_accepted': False,
            'reason': 'RESUME_OUTCOME_UNKNOWN', 'mission_state': 'UNKNOWN',
        }}
        response = client.post(route, json={'request_id': unknown_id, 'confirm': True},
                               headers=headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json['mission_state'], 'UNKNOWN')
        self.assertTrue(any(row['action'] == 'mission.resume' and row['result'] == 'UNKNOWN'
                            for row in env.store.logs()))

    def test_arm_sender_framework_and_heartbeat(self):
        from types import SimpleNamespace
        from threading import RLock
        from pymavlink.dialects.v20 import ardupilotmega as mav
        from production_stop.web_arm import WebArm, arm_sender

        reader = Mock(lock=RLock(), heartbeat_serial=1)
        reader.clock.return_value = 10.0
        target = (1, 1)
        hb = SimpleNamespace(base_mode=0, system_status=mav.MAV_STATE_STANDBY, custom_mode=4)
        hb_sample = [10.0, hb, target, 1, 0.0]
        disarm = Mock(active=None, storage_fault=False, clock_fault=False, enabled=True)
        disarm._heartbeat.side_effect = lambda now: hb_sample
        flags = mav.MAV_SYS_STATUS_PREARM_CHECK | mav.MAV_SYS_STATUS_SENSOR_RC_RECEIVER
        sys_status = Mock(onboard_control_sensors_present=flags, onboard_control_sensors_enabled=flags,
                          onboard_control_sensors_health=flags)
        sys_status.get_srcSystem.return_value, sys_status.get_srcComponent.return_value = target
        reader.messages = {'SYS_STATUS': (10.0, sys_status)}
        stop = Mock(storage_fault=False)
        stop.core.snapshot.return_value.stop_latch = False
        stop.native.enforces_elrs_stop_latch.return_value = True
        stop.native.arm_gate_verified_at.return_value = 10.0
        sent = []

        def record_packet(fd, packet):
            self.assertIsNone(fd)
            sent.append(packet)
            return len(packet)

        sender = arm_sender(mav.MAVLink(None, srcSystem=255, srcComponent=190), write=record_packet)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'arm.db'
            with closing(WebArm(reader, disarm, stop, db, sender)) as arm:
                def queue():
                    rid = str(uuid.uuid4())
                    return rid, arm.request(1, 'operator', rid)['state']

                stop.core.snapshot.return_value.stop_latch = True
                self.assertEqual(queue()[1], 'REJECTED')
                stop.core.snapshot.return_value.stop_latch = False
                sys_status.onboard_control_sensors_health &= ~mav.MAV_SYS_STATUS_SENSOR_RC_RECEIVER
                self.assertEqual(queue()[1], 'REJECTED')
                sys_status.onboard_control_sensors_health = flags
                rid, state = queue()
                self.assertEqual(state, 'QUEUED')
                arm.preempt()
                arm.tick(None)
                self.assertEqual(arm.status(1, 'operator', rid)['reason'], 'DISARM_PRIORITY')
                self.assertEqual(sent, [])
                rid, state = queue()
                self.assertEqual(state, 'QUEUED')
                arm.tick(None)
                arm.tick(None)
                self.assertEqual(len(sent), 1)
                packet = mav.MAVLink(None).parse_char(sent[0])
                self.assertEqual(packet.command, mav.MAV_CMD_COMPONENT_ARM_DISARM)
                self.assertEqual((packet.param1, packet.param2, packet.confirmation), (1, 0, 0))
                self.assertEqual(arm.status(1, 'operator', rid)['state'], 'SENT')
                hb.base_mode = mav.MAV_MODE_FLAG_SAFETY_ARMED
                arm.tick(None)
                self.assertEqual(arm.status(1, 'operator', rid)['state'], 'SENT')
                reader.clock.return_value = 10.1
                hb_sample[0], hb_sample[3] = 10.1, 2
                arm.tick(None)
                self.assertEqual(arm.status(1, 'operator', rid)['state'], 'ARMED_OBSERVED')
                self.assertEqual(arm.request(1, 'operator', rid)['state'], 'ARMED_OBSERVED')
                hb.base_mode = 0
                queued_at_restart, state = queue()
                self.assertEqual(state, 'QUEUED')
            with closing(WebArm(reader, disarm, stop, db, sender)) as arm:
                self.assertEqual(arm.request(1, 'operator', queued_at_restart)['reason'], 'OWNER_RESTARTED')
                arm.tick(None)
                self.assertEqual(len(sent), 1)

    def test_arm_frontend_boot_rbac_and_csrf(self):
        from web_hmi.app import create_app
        from web_hmi.test_support import CONFIG, PASSWORD, TestEnvironment

        env = TestEnvironment()
        self.addCleanup(env.close)
        transport = Mock()
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        request_id = str(uuid.uuid4())
        route = '/api/pixhawk/arm'

        def client_with_token(boot):
            app = create_app(config=dict(CONFIG, ARM_BOOT_ID=boot, DISARM_ENABLED=True),
                             auth_store=env.store, disarm_client=transport)
            client = app.test_client()
            token = re.search(r'name="csrf_token" value="([^"]+)"', client.get('/login').text)[1]
            self.assertEqual(client.post('/login', data={
                'username': 'testadmin', 'password': PASSWORD, 'csrf_token': token,
            }).status_code, 303)
            token = re.search(r'<meta name="csrf-token" content="([^"]+)"', client.get('/').text)[1]
            return client, {'X-CSRF-Token': token}

        body = {'request_id': request_id}
        client, headers = client_with_token(boot_id)
        self.assertEqual(client.post(route, json=body, headers=headers).status_code, 403)
        with env.store.transaction() as db:
            db.execute("INSERT INTO role_permissions VALUES ('admin', 'pixhawk.arm')")
        client, headers = client_with_token(str(uuid.uuid4()))
        self.assertEqual(client.post(route, json=body, headers=headers).status_code, 503)
        client, headers = client_with_token(boot_id)
        self.assertEqual(client.post(route, json=body).status_code, 403)
        self.assertEqual(client.post(route, json={**body, 'force': True}, headers=headers).status_code, 400)
        transport.call.assert_not_called()
        transport.call.return_value = {'ok': True, 'command': {'request_id': request_id, 'state': 'QUEUED'}}
        self.assertEqual(client.post(route, json=body, headers=headers).json['state'], 'QUEUED')
        self.assertTrue(any(row['action'] == 'pixhawk.arm' and row['result'] == 'QUEUED'
                            for row in env.store.logs()))


if __name__ == '__main__':
    verify_bases()
    for imports in paths():
        sys.path[:0] = imports
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(StopTests))
    sys.exit(not result.wasSuccessful())
