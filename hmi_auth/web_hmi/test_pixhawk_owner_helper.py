"""No root, sudo execution, /proc traversal, or real tty access in these tests."""
import ast
from contextlib import ExitStack
import errno
import inspect
import io
import json
from pathlib import Path
import stat
import subprocess
import types
import unittest
from unittest.mock import Mock, patch

from web_hmi import pixhawk_owner_client as client
from web_hmi import pixhawk_telemetry as telemetry
from web_hmi.privileged import pixhawk_owners as helper

DEVICE = dict(target='/dev/ttyACM1', dev=10, ino=20, rdev=30)


def stat_result(kind=stat.S_IFCHR, uid=0, mode=0o660, rdev=30):
    return types.SimpleNamespace(st_mode=kind | mode, st_uid=uid, st_dev=10, st_ino=20, st_rdev=rdev)


def body(owners=None):
    return dict(schema=1, port=helper.PORT, complete=True, identity=DEVICE.copy(),
                owners=[] if owners is None else owners, error=None)


def completed(data, rc=0, stderr=b''):
    return subprocess.CompletedProcess(client.COMMAND, rc, json.dumps(data).encode(), stderr)


class HelperTest(unittest.TestCase):
    def test_collect_success(self):
        with patch.object(helper, 'device_identity', return_value=DEVICE), \
             patch.object(helper, 'scan_owners', return_value=[12, 34]):
            result = helper.collect()
        self.assertTrue(result['complete'])
        self.assertEqual(result['owners'], [12, 34])

    def test_proc_denied_is_incomplete_and_has_exact_error(self):
        with patch.object(helper, 'device_identity', return_value=DEVICE), \
             patch.object(helper, 'scan_owners', side_effect=PermissionError(errno.EACCES, 'denied', '/proc/1/fd')):
            result = helper.collect()
        self.assertFalse(result['complete'])
        self.assertEqual(result['owners'], [])
        self.assertEqual(result['error']['errno'], errno.EACCES)
        self.assertEqual(result['error']['path'], '/proc/1/fd')

    def test_unplug_is_incomplete(self):
        with patch.object(helper, 'device_identity', side_effect=FileNotFoundError(errno.ENOENT, 'missing', helper.PORT)):
            result = helper.collect()
        self.assertFalse(result['complete'])
        self.assertEqual(result['error']['errno'], errno.ENOENT)

    def test_identity_change_fails_closed(self):
        with patch.object(helper, 'device_identity', side_effect=[DEVICE, dict(DEVICE, ino=21)]), \
             patch.object(helper, 'scan_owners', return_value=[]):
            self.assertEqual(helper.collect()['error']['code'], 'DEVICE_CHANGED')

    def test_untrusted_directory_rejected(self):
        with patch.object(helper.os, 'lstat', return_value=stat_result(stat.S_IFDIR, uid=1000, mode=0o755)):
            with self.assertRaises(helper.CheckError):
                helper.device_identity()

    def test_arbitrary_device_target_rejected(self):
        values = [stat_result(stat.S_IFDIR, mode=0o755)] * 3 + [stat_result(stat.S_IFLNK, mode=0o777)]
        with patch.object(helper.os, 'lstat', side_effect=values), \
             patch.object(helper.os.path, 'realpath', return_value='/etc/shadow'):
            with self.assertRaises(helper.CheckError):
                helper.device_identity()

    def test_fixed_device_accepted(self):
        values = [stat_result(stat.S_IFDIR, mode=0o755)] * 3 + [stat_result(stat.S_IFLNK, mode=0o777), stat_result()]
        with patch.object(helper.os, 'lstat', side_effect=values), \
             patch.object(helper.os.path, 'realpath', return_value='/dev/ttyACM1'):
            self.assertEqual(helper.device_identity(), DEVICE)

    def test_scan_matches_character_device_rdev_not_path_text(self):
        entries = [types.SimpleNamespace(name=str(pid), path='/proc/' + str(pid)) for pid in (12, 34, 56)]
        scanner = Mock()
        scanner.__enter__ = Mock(return_value=iter(entries))
        scanner.__exit__ = Mock(return_value=False)
        with patch.object(helper.os, 'scandir', return_value=scanner), \
             patch.object(helper.os, 'listdir', return_value=['3']), \
             patch.object(helper.os, 'stat', side_effect=[stat_result(), stat_result(rdev=31), stat_result(stat.S_IFREG)]):
            self.assertEqual(helper.scan_owners(DEVICE), [12])

    def test_disappearing_process_is_skipped(self):
        scanner = Mock()
        scanner.__enter__ = Mock(return_value=iter([types.SimpleNamespace(name='12', path='/proc/12')]))
        scanner.__exit__ = Mock(return_value=False)
        with patch.object(helper.os, 'scandir', return_value=scanner), \
             patch.object(helper.os, 'listdir', side_effect=FileNotFoundError(errno.ENOENT, 'gone')):
            self.assertEqual(helper.scan_owners(DEVICE), [])

    def test_arguments_rejected_before_scanning(self):
        with patch.object(helper.sys, 'argv', ['checker', '/etc/shadow']), \
             patch.object(helper, 'collect') as collect, patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(helper.main(), 1)
        collect.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['error']['code'], 'ARGUMENTS_FORBIDDEN')

    def test_nonroot_rejected_before_scanning(self):
        with patch.object(helper.sys, 'argv', ['checker']), patch.object(helper.os, 'geteuid', return_value=1000), \
             patch.object(helper, 'collect') as collect, patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(helper.main(), 1)
        collect.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['error']['code'], 'ROOT_REQUIRED')

    def test_timeout_is_structured(self):
        with patch.object(helper, 'device_identity', side_effect=helper.CheckError('SCAN_TIMEOUT', '/proc', 'timeout', errno.ETIMEDOUT)):
            result = helper.collect()
        self.assertFalse(result['complete'])
        self.assertEqual(result['error']['errno'], errno.ETIMEDOUT)

    def test_no_device_open_commands_or_input_in_helper(self):
        tree = ast.parse(Path(helper.__file__).read_text())
        for call in (n.func for n in ast.walk(tree) if isinstance(n, ast.Call)):
            name = call.attr if isinstance(call, ast.Attribute) else call.id if isinstance(call, ast.Name) else ''
            self.assertNotIn(name, ('open', 'read', 'write', 'ioctl', 'system', 'run', 'Popen', 'input', 'exec', 'eval'))


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.run = self.enterContext(patch.object(client.subprocess, 'run', return_value=completed(body())))
        self.stat = self.enterContext(patch.object(client.os, 'stat', return_value=stat_result()))
        self.fstat = self.enterContext(patch.object(client.os, 'fstat', return_value=stat_result()))
        self.realpath = self.enterContext(patch.object(client.os.path, 'realpath', return_value=DEVICE['target']))
        self.open = self.enterContext(patch.object(client.os, 'open', side_effect=AssertionError('No hardware')))

    def test_exact_sudo_command_no_input_no_shell(self):
        client.OwnerHelper()(helper.PORT, 'fuser_before')
        args, kwargs = self.run.call_args
        self.assertEqual(args, (client.COMMAND,))
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertFalse(kwargs['shell'])
        self.assertTrue(kwargs['close_fds'])
        self.assertEqual(kwargs['cwd'], '/')
        self.assertEqual(kwargs['timeout'], 3)
        self.assertNotIn('PYTHONPATH', kwargs['env'])

    def test_arbitrary_port_rejected_before_sudo(self):
        with self.assertRaises(telemetry.PortFailure):
            client.OwnerHelper()('/dev/ttyACM0', 'fuser_before')
        self.run.assert_not_called()

    def test_known_owner_busy(self):
        self.run.return_value = completed(body([client.os.getpid() + 10000]))
        with self.assertRaises(telemetry.PortFailure) as caught:
            client.OwnerHelper()(helper.PORT, 'fuser_before')
        self.assertEqual(caught.exception.state, 'PORT_BUSY')

    def test_incomplete_denied_malformed_and_wrong_device_fail_closed(self):
        for result in (completed(dict(body(), complete=False, error={'errno':13})),
                       completed(body(), rc=1, stderr=b'sudo denied'),
                       subprocess.CompletedProcess(client.COMMAND, 0, b'not json', b''),
                       completed(dict(body(), port='/dev/ttyACM0')),
                       completed(dict(body(), owners=[True])),
                       completed(dict(body(), schema=True)),
                       completed(dict(body(), owners=[1, 1]))):
            with self.subTest(result=result), self.assertRaises(telemetry.PortFailure) as caught:
                self.run.return_value = result
                client.OwnerHelper()(helper.PORT, 'fuser_before')
            self.assertEqual(caught.exception.state, 'PORT_CHECK_FAILED')
        self.open.assert_not_called()

    def test_sudo_timeout_or_missing_fails_closed(self):
        for exc in (FileNotFoundError('sudo missing'), subprocess.TimeoutExpired(client.COMMAND, 3)):
            self.run.side_effect = exc
            with self.assertRaises(telemetry.PortFailure) as caught:
                client.OwnerHelper()(helper.PORT, 'fuser_before')
            self.assertEqual(caught.exception.state, 'PORT_CHECK_FAILED')

    def test_self_only_after_open_is_accepted(self):
        checker = client.OwnerHelper()
        checker(helper.PORT, 'fuser_before')
        self.run.return_value = completed(body([client.os.getpid()]))
        result = checker(helper.PORT, 'fuser_after', 123)
        self.assertEqual(telemetry.checked_owners(result, helper.PORT, 'fuser_after'), {client.os.getpid()})
        self.fstat.assert_called_once_with(123)

    def test_changed_descriptor_rejected(self):
        checker = client.OwnerHelper()
        checker(helper.PORT, 'fuser_before')
        self.run.return_value = completed(body([client.os.getpid()]))
        self.fstat.return_value = stat_result(rdev=99)
        with self.assertRaises(telemetry.PortFailure) as caught:
            checker(helper.PORT, 'fuser_after', 123)
        self.assertEqual(caught.exception.state, 'PORT_CHECK_FAILED')


@unittest.skipUnless('owner_check' in inspect.signature(telemetry.open_passive).parameters,
                     'Integration patch deliberately not applied to working HMI')
class IntegrationTest(unittest.TestCase):
    def test_both_checks_preserve_locks_and_read_only(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(telemetry.os, 'stat'))
            opened = stack.enter_context(patch.object(telemetry.os, 'open', return_value=123))
            close = stack.enter_context(patch.object(telemetry.os, 'close'))
            flock = stack.enter_context(patch.object(telemetry.fcntl, 'flock'))
            ioctl = stack.enter_context(patch.object(telemetry.fcntl, 'ioctl'))
            stack.enter_context(patch.object(telemetry.termios, 'tcgetattr', return_value=[0,0,0,0,0,0,[0]*32]))
            stack.enter_context(patch.object(telemetry.termios, 'tcsetattr'))
            checker = Mock(side_effect=[subprocess.CompletedProcess('', 1, b'', b''),
                                       subprocess.CompletedProcess('', 0, str(client.os.getpid()).encode(), b'')])
            self.assertEqual(telemetry.open_passive(owner_check=checker), 123)
            self.assertEqual(checker.call_args_list[0].args, (helper.PORT, 'fuser_before', None))
            self.assertEqual(checker.call_args_list[1].args, (helper.PORT, 'fuser_after', 123))
            self.assertEqual(opened.call_args.args[1] & client.os.O_ACCMODE, client.os.O_RDONLY)
            flock.assert_called_once()
            ioctl.assert_called_once()
            close.assert_not_called()


    def test_incomplete_precheck_never_opens(self):
        with patch.object(telemetry.os, 'stat'), \
             patch.object(telemetry.os, 'open') as opened, \
             self.assertLogs(telemetry.LOG, level='ERROR'):
            checker = Mock(side_effect=telemetry.PortFailure('PORT_CHECK_FAILED', 'fuser_before', helper.PORT, 'incomplete'))
            with self.assertRaises(telemetry.PortFailure):
                telemetry.open_passive(owner_check=checker)
        opened.assert_not_called()

    def test_failed_postcheck_closes_without_config_or_read(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(telemetry.os, 'stat'))
            stack.enter_context(patch.object(telemetry.os, 'open', return_value=123))
            close = stack.enter_context(patch.object(telemetry.os, 'close'))
            read = stack.enter_context(patch.object(telemetry.os, 'read'))
            stack.enter_context(patch.object(telemetry.fcntl, 'flock'))
            stack.enter_context(patch.object(telemetry.fcntl, 'ioctl'))
            attrs = stack.enter_context(patch.object(telemetry.termios, 'tcgetattr'))
            stack.enter_context(self.assertLogs(telemetry.LOG, level='ERROR'))
            checker = Mock(side_effect=[subprocess.CompletedProcess('', 1, b'', b''),
                telemetry.PortFailure('PORT_CHECK_FAILED', 'fuser_after', helper.PORT, 'timeout')])
            with self.assertRaises(telemetry.PortFailure):
                telemetry.open_passive(owner_check=checker)
            close.assert_called_once_with(123)
            attrs.assert_not_called()
            read.assert_not_called()
