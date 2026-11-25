import stat
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from disarm_control.inhibit import InhibitLatch, InhibitedControl


class InhibitTests(unittest.TestCase):
    def parent(self, *, uid=0, mode=0o755):
        return SimpleNamespace(st_uid=uid, st_mode=stat.S_IFDIR | mode)

    def test_absent_marker_with_trusted_directory_is_not_inhibited(self):
        policy = InhibitLatch('/not-a-production-path/inhibit')
        with patch('pathlib.Path.lstat', side_effect=[self.parent(), FileNotFoundError()]):
            self.assertFalse(policy.blocked())

    def test_marker_or_lookup_failure_latches_until_restart(self):
        for outcomes in ([self.parent(), Mock()], [PermissionError()], [FileNotFoundError()]):
            policy = InhibitLatch('/not-a-production-path/inhibit')
            with patch('pathlib.Path.lstat', side_effect=outcomes):
                self.assertTrue(policy.blocked())
            with patch('pathlib.Path.lstat', side_effect=AssertionError('No reenable by deleting marker')):
                self.assertTrue(policy.blocked())

    def test_untrusted_directory_cannot_enable_commands(self):
        for value in (self.parent(uid=1000), self.parent(mode=0o777),
                      SimpleNamespace(st_uid=0, st_mode=stat.S_IFLNK | 0o777)):
            policy = InhibitLatch('/not-a-production-path/inhibit')
            with patch('pathlib.Path.lstat', return_value=value):
                self.assertTrue(policy.blocked())

    def test_owner_facade_checks_every_operation_and_never_reenables(self):
        engine, policy = Mock(), Mock()
        policy.blocked.return_value = True
        control = InhibitedControl(engine, policy)
        for call in (lambda: control.request(1, 'user', 'uuid'),
                     lambda: control.status(1, 'uuid'), lambda: control.tick(42),
                     lambda: control.observe(Mock())):
            call()
        self.assertEqual(engine.set_enabled.call_count, 4)
        for args in engine.set_enabled.call_args_list:
            self.assertEqual(args.args, (False,))
        policy.blocked.return_value = False
        control.tick(42)
        self.assertEqual(engine.set_enabled.call_count, 4)
