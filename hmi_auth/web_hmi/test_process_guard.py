"""Deployment stop guard tests mock all process operations; no real signals."""
from contextlib import redirect_stdout
import io
import signal
import unittest
from unittest.mock import patch
from tools import test_hmi_process as guard


class ProcessGuardTest(unittest.TestCase):
    def test_status_never_opens_pidfd_or_signals(self):
        with patch('sys.argv',['guard','status']), patch.object(guard.os,'getuid',return_value=1000), \
             patch.object(guard,'identify',return_value=(123,[123,124])), \
             patch.object(guard.os,'pidfd_open') as opened, \
             patch.object(guard.signal,'pidfd_send_signal') as send, redirect_stdout(io.StringIO()):
            guard.main()
        opened.assert_not_called();send.assert_not_called()

    def test_stop_signals_only_reverified_pidfd_and_waits_for_owned_processes(self):
        with patch('sys.argv',['guard','stop']), patch.object(guard.os,'getuid',return_value=1000), \
             patch.object(guard,'identify',return_value=(123,[123,124])), \
             patch.object(guard.os,'pidfd_open',return_value=99) as opened, \
             patch.object(guard.signal,'pidfd_send_signal') as send, patch.object(guard.os,'close') as close, \
             patch.object(guard,'listeners',return_value={}), patch.object(guard,'process_alive',return_value=False), \
             redirect_stdout(io.StringIO()):
            guard.main()
        opened.assert_called_once_with(123)
        send.assert_called_once_with(99,signal.SIGTERM)
        close.assert_called_once_with(99)

    def test_identity_change_refuses_signal(self):
        with patch('sys.argv',['guard','stop']), patch.object(guard.os,'getuid',return_value=1000), \
             patch.object(guard,'identify',side_effect=[(123,[123,124]),(999,[999])]), \
             patch.object(guard.os,'pidfd_open',return_value=99), patch.object(guard.os,'close'), \
             patch.object(guard.signal,'pidfd_send_signal') as send, redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):guard.main()
        send.assert_not_called()
