import ast
import errno
import os
from pathlib import Path
import sys
import subprocess
import types
import unittest
from unittest.mock import Mock, patch

from web_hmi.app import create_app
from web_hmi.pixhawk_telemetry import PixhawkTelemetry, PORT, TYPES, open_passive, PortFailure, verify_process_visibility
from web_hmi.simulator import Simulator


class Message(types.SimpleNamespace):
    def get_type(self):
        return self.kind

    def get_srcSystem(self):
        return getattr(self, 'system', 1)

    def get_srcComponent(self):
        return getattr(self, 'component', 1)


class TelemetryTest(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.reader = PixhawkTelemetry(clock=lambda: self.now)
        self.reader.state = 'READING'

    def heartbeat(self, **kwargs):
        fields = dict(kind='HEARTBEAT', autopilot=3, type=10, base_mode=1, custom_mode=4)
        fields.update(kwargs)
        self.reader.ingest(Message(**fields))

    def test_missing_values_are_unknown(self):
        s = self.reader.snapshot()
        self.assertEqual(s['connection'], 'UNKNOWN')
        for key in ('armed', 'flight_mode', 'voltage_v', 'yaw_deg'):
            self.assertEqual(s[key], dict(state='UNKNOWN', value=None))
        self.assertTrue(all(v['state'] == 'UNKNOWN' for v in s['ekf'].values()))

    def test_heartbeat_and_independent_timeouts(self):
        self.heartbeat()
        self.reader.ingest(Message(kind='ATTITUDE', yaw=1.0))
        self.now = 3.1
        self.heartbeat(base_mode=129)
        s = self.reader.snapshot()
        self.assertEqual(s['connection'], 'CONNECTED')
        self.assertEqual(s['armed']['value'], 'ARMED')
        self.assertEqual(s['flight_mode']['value']['custom_mode'], 4)
        self.assertEqual(s['yaw_deg'], dict(state='STALE', value=None))
        self.now = 6.2
        s = self.reader.snapshot()
        self.assertEqual(s['connection'], 'STALE')
        self.assertIsNone(s['armed']['value'])
        self.assertIsNone(s['flight_mode']['value'])

    def test_disconnect_invalidates_even_fresh_values(self):
        self.heartbeat()
        self.reader.state = 'DISCONNECTED'
        s = self.reader.snapshot()
        self.assertEqual(s['connection'], 'DISCONNECTED')
        self.assertEqual(s['armed'], dict(state='STALE', value=None))

    def test_voltage_sentinels_and_fallback(self):
        self.heartbeat()
        self.reader.ingest(Message(kind='SYS_STATUS', voltage_battery=65535))
        self.assertIsNone(self.reader.snapshot()['voltage_v']['value'])
        self.reader.ingest(Message(kind='SYS_STATUS', voltage_battery=24500))
        self.assertEqual(self.reader.snapshot()['voltage_v']['value'], 24.5)
        self.reader.ingest(Message(kind='BATTERY_STATUS', id=0, voltages=[4100, 4100, 65535]))
        self.assertEqual(self.reader.snapshot()['voltage_v']['value'], 8.2)
        self.reader.ingest(Message(kind='BATTERY_STATUS', id=0, voltages=[4100, 65535], voltages_ext=[4100, 0]))
        self.assertEqual(self.reader.snapshot()['voltage_v']['value'], 8.2)
        self.now = 4
        self.assertEqual(self.reader.snapshot()['voltage_v'], dict(state='STALE', value=None))

    def test_ekf_only_when_received_and_fresh(self):
        self.heartbeat()
        for kind in TYPES[-2:]:
            self.reader.ingest(Message(kind=kind, flags=0))
            self.assertEqual(self.reader.snapshot()['ekf'][kind]['value'], {'flags': 0})
        self.now = 4
        self.assertTrue(all(v == dict(state='STALE', value=None) for v in self.reader.snapshot()['ekf'].values()))

    def test_source_filter_and_invalid_yaw(self):
        self.heartbeat(component=190)
        self.heartbeat(autopilot=8)
        self.assertEqual(self.reader.snapshot()['connection'], 'UNKNOWN')
        self.heartbeat()
        self.heartbeat(system=2, base_mode=129)
        self.assertEqual(self.reader.snapshot()['armed']['value'], 'DISARMED')
        self.reader.ingest(Message(kind='ATTITUDE', yaw=float('nan')))
        self.assertEqual(self.reader.snapshot()['yaw_deg']['state'], 'UNKNOWN')

    def modules(self):
        parser = Mock()
        parser.parse_buffer.return_value = []
        dialect = types.SimpleNamespace(MAVLink=Mock(return_value=parser))
        return {'pymavlink': types.ModuleType('pymavlink'),
                'pymavlink.dialects': types.ModuleType('pymavlink.dialects'),
                'pymavlink.dialects.v20': types.SimpleNamespace(ardupilotmega=dialect)}

    def test_dependency_missing_does_not_open_port(self):
        opener = Mock()
        with patch.dict(sys.modules, {'pymavlink': None, 'pymavlink.dialects': None,
                                      'pymavlink.dialects.v20': None,
                                      'pymavlink.dialects.v20.ardupilotmega': None}):
            reader = PixhawkTelemetry(opener=opener)
            reader._run()
        self.assertEqual(reader.snapshot()['connection'], 'DEPENDENCY_MISSING')
        opener.assert_not_called()

    def test_worker_busy_and_port_errors(self):
        for error, state in [(BlockingIOError(errno.EBUSY, 'busy'), 'PORT_BUSY'),
                             (PermissionError(errno.EACCES, 'denied'), 'PORT_ACCESS_DENIED'),
                             (PortFailure('PORT_CHECK_FAILED', 'proc_list', '/proc/1/fd', 'denied'), 'PORT_CHECK_FAILED'),
                             (FileNotFoundError('unplugged'), 'PORT_ERROR')]:
            with self.subTest(state=state), patch.dict(sys.modules, self.modules()):
                reader = PixhawkTelemetry(opener=Mock(side_effect=error))
                reader._run()
                self.assertEqual(reader.snapshot()['connection'], state)

    def test_worker_disconnect_closes_descriptor(self):
        for data in (b'', OSError('USB disconnected')):
            with self.subTest(data=data), patch.dict(sys.modules, self.modules()), \
                 patch('web_hmi.pixhawk_telemetry.select.select', return_value=([123], [], [])), \
                 patch('web_hmi.pixhawk_telemetry.os.read') as read, \
                 patch('web_hmi.pixhawk_telemetry.os.close') as close:
                if isinstance(data, Exception):
                    read.side_effect = data
                else:
                    read.return_value = data
                reader = PixhawkTelemetry(opener=lambda: 123)
                reader._run()
                self.assertEqual(reader.snapshot()['connection'], 'DISCONNECTED')
                close.assert_called_once_with(123)

    def test_disabled_import_api_and_demo_never_open_port(self):
        opener = Mock(side_effect=AssertionError('hardware access'))
        reader = PixhawkTelemetry(opener=opener)
        sim = Simulator()
        client = create_app(sim, reader).test_client()
        self.assertEqual(client.get('/api/telemetry').json['connection'], 'DISABLED')
        with patch.object(reader, 'start', side_effect=AssertionError('start')), \
             patch.object(reader, 'ingest', side_effect=AssertionError('ingest')), \
             patch.object(reader, 'snapshot', side_effect=AssertionError('telemetry')):
            for i, action in enumerate(('start', 'pause', 'resume', 'stop', 'reset')):
                response = client.post('/api/command', json=dict(action=action, confirmed=True,
                    revision=sim.snapshot()['revision'], request_id=str(i)))
                self.assertEqual(response.status_code, 200)
            self.assertEqual(client.get('/api/status').status_code, 200)
        opener.assert_not_called()
        self.assertEqual(client.post('/api/telemetry', json={'action':'arm'}).status_code, 405)

    def test_no_transmit_calls_in_module(self):
        # Structural guard supplements O_RDONLY: no MAVLink transport/sender.
        tree = ast.parse(Path('web_hmi/pixhawk_telemetry.py').read_text())
        calls = [n.func for n in ast.walk(tree) if isinstance(n, ast.Call)]
        for call in calls:
            if isinstance(call, ast.Attribute):
                self.assertFalse(call.attr.endswith('_send'))
                self.assertNotIn(call.attr, ('write', 'send', 'mavlink_connection', 'pack'))


class PortTest(unittest.TestCase):
    def setUp(self):
        self.log_context = self.assertLogs('web_hmi.pixhawk_telemetry', level='INFO')
        self.logs = self.log_context.__enter__()
        self.addCleanup(self.log_context.__exit__, None, None, None)
        visibility = patch('web_hmi.pixhawk_telemetry.verify_process_visibility')
        visibility.start()
        self.addCleanup(visibility.stop)

    @patch('web_hmi.pixhawk_telemetry.os.stat')
    @patch('web_hmi.pixhawk_telemetry.subprocess.run')
    @patch('web_hmi.pixhawk_telemetry.os.open')
    def test_busy_or_unverifiable_never_opens(self, open_, run, exists):
        for result, state in [(types.SimpleNamespace(returncode=0, stdout=b'42', stderr=b''), 'PORT_BUSY'),
                              (types.SimpleNamespace(returncode=1, stdout=b'', stderr=b'denied'), 'PORT_CHECK_FAILED')]:
            run.return_value = result
            with self.assertRaises(PortFailure) as caught:
                open_passive()
            self.assertEqual(caught.exception.state, state)
        open_.assert_not_called()

    @patch('web_hmi.pixhawk_telemetry.termios.tcsetattr')
    @patch('web_hmi.pixhawk_telemetry.termios.tcgetattr', return_value=[0,0,0,0,0,0,[0]*32])
    @patch('web_hmi.pixhawk_telemetry.fcntl.ioctl')
    @patch('web_hmi.pixhawk_telemetry.fcntl.flock')
    @patch('web_hmi.pixhawk_telemetry.os.close')
    @patch('web_hmi.pixhawk_telemetry.os.open', return_value=123)
    @patch('web_hmi.pixhawk_telemetry.subprocess.run')
    @patch('web_hmi.pixhawk_telemetry.os.stat')
    def test_read_only_exclusive_open(self, exists, run, open_, close, flock, ioctl, getattrs, setattrs):
        run.side_effect = [types.SimpleNamespace(returncode=1, stdout=b'', stderr=b''),
                           types.SimpleNamespace(returncode=0, stdout=str(os.getpid()).encode(), stderr=PORT.encode() + b':')]
        self.assertEqual(open_passive(), 123)
        flags = open_.call_args.args[1]
        self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
        flock.assert_called_once()
        ioctl.assert_called_once()
        close.assert_not_called()

    @patch('web_hmi.pixhawk_telemetry.os.stat')
    @patch('web_hmi.pixhawk_telemetry.subprocess.run')
    @patch('web_hmi.pixhawk_telemetry.os.open', return_value=123)
    @patch('web_hmi.pixhawk_telemetry.os.close')
    @patch('web_hmi.pixhawk_telemetry.fcntl.flock')
    @patch('web_hmi.pixhawk_telemetry.fcntl.ioctl')
    def test_racing_owner_closes_port_without_read(self, ioctl, flock, close, open_, run, exists):
        run.side_effect = [types.SimpleNamespace(returncode=1, stdout=b'', stderr=b''),
                           types.SimpleNamespace(returncode=0, stdout=b'99999', stderr=PORT.encode() + b':')]
        with self.assertRaises(PortFailure) as caught:
            open_passive()
        self.assertEqual(caught.exception.state, 'PORT_BUSY')
        close.assert_called_once_with(123)

    @patch('web_hmi.pixhawk_telemetry.os.stat', side_effect=FileNotFoundError(errno.ENOENT, 'missing'))
    @patch('web_hmi.pixhawk_telemetry.os.open')
    def test_missing_port(self, open_, exists):
        with self.assertRaises(PortFailure) as caught:
            open_passive()
        open_.assert_not_called()


class VisibilityTest(unittest.TestCase):
    def test_hidden_processes_fail_closed(self):
        entry = types.SimpleNamespace(name='42', path='/proc/42')
        with patch('web_hmi.pixhawk_telemetry.os.scandir', return_value=[entry]), \
             patch('web_hmi.pixhawk_telemetry.os.listdir', side_effect=PermissionError('hidden')):
            with self.assertRaises(PortFailure) as caught:
                verify_process_visibility()
        self.assertEqual(caught.exception.state, 'PORT_CHECK_FAILED')
        self.assertEqual(caught.exception.stage, 'proc_list')
        self.assertEqual(caught.exception.path, '/proc/42/fd')
        self.assertIsInstance(caught.exception.__cause__, PermissionError)
    def test_exited_process_does_not_block_visibility(self):
        entry = types.SimpleNamespace(name='42', path='/proc/42')
        with patch('web_hmi.pixhawk_telemetry.os.scandir', return_value=[entry]), \
             patch('web_hmi.pixhawk_telemetry.os.listdir', side_effect=FileNotFoundError()):
            verify_process_visibility()


class PortDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        # Every descriptor and syscall touching hardware is a mock.
        self.stat = self.enterContext(patch('web_hmi.pixhawk_telemetry.os.stat'))
        self.open = self.enterContext(patch('web_hmi.pixhawk_telemetry.os.open', return_value=123))
        self.close = self.enterContext(patch('web_hmi.pixhawk_telemetry.os.close'))
        self.flock = self.enterContext(patch('web_hmi.pixhawk_telemetry.fcntl.flock'))
        self.ioctl = self.enterContext(patch('web_hmi.pixhawk_telemetry.fcntl.ioctl'))
        self.configure = self.enterContext(patch('web_hmi.pixhawk_telemetry.termios.tcsetattr'))
        self.attrs = self.enterContext(patch('web_hmi.pixhawk_telemetry.termios.tcgetattr',
                                            return_value=[0,0,0,0,0,0,[0]*32]))
        self.visibility = self.enterContext(patch('web_hmi.pixhawk_telemetry.verify_process_visibility'))
        self.run = self.enterContext(patch('web_hmi.pixhawk_telemetry.subprocess.run'))
        self.logs = self.enterContext(self.assertLogs('web_hmi.pixhawk_telemetry', level='INFO'))

    def result(self, rc, stdout=b'', stderr=b''):
        return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)

    def refused(self, state, stage):
        with self.assertRaises(PortFailure) as caught:
            open_passive()
        self.assertEqual((caught.exception.state, caught.exception.stage), (state, stage))
        self.configure.assert_not_called()
        record = self.logs.records[-1]
        self.assertIsNotNone(record.exc_info)
        message = record.getMessage()
        for value in (state, stage, 'hmi_pid=' + str(os.getpid()), 'hmi_uid=' + str(os.getuid()),
                      'path=', 'errno=', 'owner_pids=', 'fuser_rc=', 'fuser_stdout=', 'fuser_stderr='):
            self.assertIn(value, message)
        self.assertIn('Traceback', self.logs.output[-1])
        return caught.exception

    def test_proc_eacces_logs_original_cause_and_never_opens(self):
        self.visibility.side_effect = verify_process_visibility
        entry = types.SimpleNamespace(name='1', path='/proc/1')
        with patch('web_hmi.pixhawk_telemetry.os.scandir', return_value=[entry]), \
             patch('web_hmi.pixhawk_telemetry.os.listdir',
                   side_effect=PermissionError(errno.EACCES, 'Permission denied', '/proc/1/fd')):
            self.refused('PORT_CHECK_FAILED', 'proc_list')
        self.open.assert_not_called()
        self.run.assert_not_called()
        self.assertIn("path='/proc/1/fd' errno=13", self.logs.output[-1])
        self.assertIn('PermissionError', self.logs.output[-1])

    def test_proc_readlink_failure_never_opens(self):
        self.visibility.side_effect = verify_process_visibility
        entry = types.SimpleNamespace(name='1', path='/proc/1')
        with patch('web_hmi.pixhawk_telemetry.os.scandir', return_value=[entry]), \
             patch('web_hmi.pixhawk_telemetry.os.listdir', return_value=['3']), \
             patch('web_hmi.pixhawk_telemetry.os.readlink',
                   side_effect=PermissionError(errno.EACCES, 'hidden', '/proc/1/fd/3')):
            self.refused('PORT_CHECK_FAILED', 'proc_readlink')
        self.open.assert_not_called()

    def test_missing_fuser_never_opens(self):
        self.run.side_effect = FileNotFoundError(errno.ENOENT, 'missing', 'fuser')
        self.refused('PORT_CHECK_FAILED', 'fuser_before')
        self.open.assert_not_called()
        self.assertIn('errno=2', self.logs.output[-1])

    def test_fuser_timeout_never_opens(self):
        self.run.side_effect = subprocess.TimeoutExpired(['fuser', PORT], 3, output=b'partial', stderr=b'timed out')
        self.refused('PORT_CHECK_FAILED', 'fuser_before')
        self.open.assert_not_called()
        self.assertIn("fuser_stdout=b'partial'", self.logs.output[-1])
        self.assertIn("fuser_stderr=b'timed out'", self.logs.output[-1])

    def test_fuser_execution_denied_is_check_failure(self):
        self.run.side_effect = PermissionError(errno.EACCES, 'cannot execute', 'fuser')
        self.refused('PORT_CHECK_FAILED', 'fuser_before')
        self.open.assert_not_called()

    def test_fuser_nonzero_error_is_check_failure(self):
        self.run.return_value = self.result(2, stderr=b'execution failed')
        self.refused('PORT_CHECK_FAILED', 'fuser_before')
        self.open.assert_not_called()
        self.assertIn('fuser_rc=2', self.logs.output[-1])

    def test_fuser_called_process_error_is_check_failure(self):
        self.run.side_effect = subprocess.CalledProcessError(2, ['fuser', PORT], output=b'', stderr=b'failed')
        self.refused('PORT_CHECK_FAILED', 'fuser_before')
        self.open.assert_not_called()
        self.assertIn('fuser_rc=2', self.logs.output[-1])

    def test_fuser_malformed_output_is_check_failure(self):
        self.run.return_value = self.result(0, b'not-a-pid')
        self.refused('PORT_CHECK_FAILED', 'fuser_before')
        self.open.assert_not_called()

    def test_confirmed_owner_logs_pid_and_never_opens(self):
        owner = os.getpid() + 10000
        self.run.return_value = self.result(0, str(owner).encode(), PORT.encode() + b':')
        exc = self.refused('PORT_BUSY', 'fuser_before')
        self.assertEqual(exc.owners, (owner,))
        self.assertIn('owner_pids=[' + str(owner) + ']', self.logs.output[-1])
        self.open.assert_not_called()

    def test_own_existing_descriptor_is_busy_before_open(self):
        self.run.return_value = self.result(0, str(os.getpid()).encode())
        self.refused('PORT_BUSY', 'fuser_before')
        self.open.assert_not_called()

    def test_tty_stat_permission_denied(self):
        self.stat.side_effect = PermissionError(errno.EACCES, 'tty denied', PORT)
        self.refused('PORT_ACCESS_DENIED', 'tty_stat')
        self.open.assert_not_called()
        self.run.assert_not_called()

    def test_tty_open_permission_denied(self):
        self.run.return_value = self.result(1)
        self.open.side_effect = PermissionError(errno.EPERM, 'tty denied', PORT)
        self.refused('PORT_ACCESS_DENIED', 'open')
        self.close.assert_not_called()

    def test_kernel_open_busy(self):
        self.run.return_value = self.result(1)
        self.open.side_effect = OSError(errno.EBUSY, 'exclusive owner', PORT)
        self.refused('PORT_BUSY', 'open')
        self.close.assert_not_called()

    def test_flock_conflict_closes_descriptor(self):
        self.run.return_value = self.result(1)
        self.flock.side_effect = BlockingIOError(errno.EAGAIN, 'locked')
        self.refused('PORT_BUSY', 'flock')
        self.close.assert_called_once_with(123)

    def test_tioc_excl_conflict_closes_descriptor(self):
        self.run.return_value = self.result(1)
        self.ioctl.side_effect = OSError(errno.EBUSY, 'exclusive owner')
        self.refused('PORT_BUSY', 'tioc_excl')
        self.close.assert_called_once_with(123)

    def test_postcheck_timeout_closes_descriptor(self):
        self.run.side_effect = [self.result(1), subprocess.TimeoutExpired(['fuser', PORT], 3)]
        self.refused('PORT_CHECK_FAILED', 'fuser_after')
        self.close.assert_called_once_with(123)

    def test_postcheck_proc_failure_closes_descriptor(self):
        self.run.return_value = self.result(1)
        self.visibility.side_effect = [None, PortFailure('PORT_CHECK_FAILED', 'proc_list', '/proc/1/fd', 'hidden')]
        self.refused('PORT_CHECK_FAILED', 'proc_list')
        self.close.assert_called_once_with(123)
        self.assertEqual(self.run.call_count, 1)

    def test_postcheck_missing_self_is_unverifiable(self):
        self.run.side_effect = [self.result(1), self.result(1)]
        self.refused('PORT_CHECK_FAILED', 'fuser_after')
        self.close.assert_called_once_with(123)

    def test_postcheck_warning_is_unverifiable(self):
        self.run.side_effect = [self.result(1), self.result(0, str(os.getpid()).encode(), b'cannot inspect process')]
        self.refused('PORT_CHECK_FAILED', 'fuser_after')
        self.close.assert_called_once_with(123)


if __name__ == '__main__':
    unittest.main()
