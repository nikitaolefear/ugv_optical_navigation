"""Argument validation only; main()/serial/GPIO are never started."""
from contextlib import redirect_stderr
import io
import unittest
from unittest.mock import patch

from stop_latch_test.hold_runner import parse_args


class OwnerConfigTests(unittest.TestCase):
    def test_default_has_no_disarm_channel_and_no_hold_commands(self):
        args = parse_args(['--journal', '/tmp/not-used.jsonl'])
        self.assertFalse(args.enable_bench_hold)
        self.assertIsNone(args.web_disarm_socket)

    def test_gateway_channel_requires_distinct_nonroot_uid_and_explicit_gid(self):
        start = ['--journal', '/tmp/not-used.jsonl', '--web-disarm-socket', '/tmp/not-used.sock',
                 '--web-disarm-db', '/tmp/not-used.sqlite3']
        with patch('os.getuid', return_value=1000):
            for extra in ([], ['--web-disarm-client-uid', '1000', '--web-disarm-client-gid', '2000'],
                          ['--web-disarm-client-uid', '0', '--web-disarm-client-gid', '2000'],
                          ['--web-disarm-client-uid', '1001']):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    parse_args(start + extra)
            args = parse_args(start + ['--web-disarm-client-uid', '1001', '--web-disarm-client-gid', '2000'])
            self.assertEqual(args.web_disarm_client_uid, 1001)

    def test_partial_configuration_fails_before_any_device_open(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(['--journal', '/tmp/not-used.jsonl', '--web-disarm-socket', '/tmp/not-used.sock'])
