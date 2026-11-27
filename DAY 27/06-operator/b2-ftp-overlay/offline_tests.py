import os
from pathlib import Path
import shlex
import tempfile
import unittest
import uuid

from pymavlink.dialects.v20 import ardupilotmega as mav
from disarm_control.ftp_backup import ScriptsBackup
from disarm_control.ipc import OwnerClient, OwnerServer, SocketPolicy
from disarm_control.scripts_ftp import ACK, CRC, EOF, HEADER, LIST, NAK, OPEN_RO, READ, TERMINATE, U32, ardupilot_crc
from disarm_control.stage3 import Refused
from stop_latch_test.hold_runner import parse_args

from backup_client import backup
from profile import IDENTITIES, render, resolve_identities

PEER = (42, 123, 0)
ALL_GUARDS = ('heartbeat', 'hold', 'disarmed', 'inhibit', 'stop_latch', 'tx0', 'profile', 'marker')


def packet(msg, src=(1, 1)):
    data = msg.pack(mav.MAVLink(None, srcSystem=src[0], srcComponent=src[1]))
    return mav.MAVLink(None).parse_char(data)


class Fake:
    def __init__(self):
        self.now = 0.
        self.allowed = dict.fromkeys(ALL_GUARDS, True)
        self.alive = True
        self.ftp = ScriptsBackup(lambda: self.now, lambda: all(self.allowed.values()),
                                 lambda peer: self.alive and peer == PEER, (255, 190))
        self.writes = []
        self.session = 0
        self.offset = 0
        self.data = b'synthetic'
        self.list_pages = 0
        self.change_listing = False

    def start(self, budget=1024):
        self.ftp.dispatch(PEER, 'S3_FTP_START', {'budget': budget})

    def tick(self, priority=True, connected=True):
        self.now += .1
        self.ftp.tick(self.writes.append, connected=connected, priority_clear=priority)

    def capability(self, value=32, src=(1, 1)):
        fields = [value, 0, 0, 0, 0, [0]*8, [0]*8, [0]*8, 0, 0, 0]
        self.ftp.observe(packet(mav.MAVLink_autopilot_version_message(*fields), src))

    def reply(self, *, dest=(255, 190), network=0, src=(1, 1), seq_delta=0):
        seq, _, operation, size, _, _, _, offset = HEADER.unpack_from(self.writes[-1])
        opcode, data = ACK, b''
        if operation == LIST:
            if offset:
                opcode, data = NAK, bytes([EOF])
            else:
                self.list_pages += 1
                data = b'Ftest.lua\t' + str(len(self.data)).encode() + b'\0'
                if self.change_listing and self.list_pages > 1:
                    data = b'Fchanged.lua\t9\0'
        elif operation == OPEN_RO:
            data = U32.pack(len(self.data))
        elif operation == READ:
            data = self.data[offset:offset + size]
        elif operation == CRC:
            data = U32.pack(ardupilot_crc(self.data))
        elif operation != TERMINATE:
            raise AssertionError(operation)
        reply = HEADER.pack((seq + 1 + seq_delta) & 65535, self.session, opcode, len(data),
                            operation, 0, 0, offset + self.offset)
        message = mav.MAVLink_file_transfer_protocol_message(network, *dest, (reply+data).ljust(251,b'\0'))
        self.ftp.observe(packet(message, src))

    def run(self):
        self.start()
        count = 0
        for _ in range(250):
            self.tick()
            if len(self.writes) > count:
                count += 1
                if self.writes[-1] is None:
                    self.capability()
                else:
                    self.reply()
            if self.ftp.task is None:
                return
        raise AssertionError('test transport did not finish')


class IntegrationTests(unittest.TestCase):
    def test_backup_via_owner_tick_and_real_mavlink_parser(self):
        f = Fake()
        f.run()
        self.assertEqual(f.ftp.result, 'COMPLETE')
        self.assertEqual(f.ftp.files[0][1], f.data)
        self.assertEqual([HEADER.unpack_from(w)[2] for w in f.writes[1:]],
                         [LIST, LIST, OPEN_RO, READ, TERMINATE, CRC, LIST, LIST])

    def test_live_address_filter(self):
        f = Fake()
        f.start()
        f.tick()
        f.capability(0, src=(2, 1))
        self.assertIsNone(f.ftp.reply)
        f.capability()
        f.tick()
        f.tick()
        for options in ({'src': (2, 1)}, {'src': (1, 2)}, {'dest': (0, 0)},
                        {'dest': (255, 1)}, {'network': 1}, {'seq_delta': 1}):
            f.reply(**options)
            self.assertIsNone(f.ftp.reply)
        f.reply()
        self.assertIsNotNone(f.ftp.reply)

    def test_bad_session_and_offset_are_refused(self):
        for key in ('session', 'offset'):
            with self.subTest(key=key):
                f = Fake()
                setattr(f, key, 1)
                f.run()
                self.assertEqual(f.ftp.result, 'INCOMPLETE')
                self.assertEqual(len(f.writes), 2)

    def test_heartbeat_and_each_guard(self):
        for guard in ALL_GUARDS:
            with self.subTest(guard=guard):
                f = Fake()
                f.start()
                f.tick()
                f.allowed[guard] = False
                f.tick()
                self.assertEqual(f.ftp.result, 'INCOMPLETE')
                self.assertEqual(f.writes, [None])
                with self.assertRaises(Refused):
                    f.start()

    def test_priority_and_timeout(self):
        f = Fake()
        f.start()
        f.tick(False)
        self.assertEqual(f.writes, [])
        f.tick()
        f.now += 1.1
        f.tick()
        self.assertEqual(f.ftp.result, 'INCOMPLETE')
        self.assertEqual(f.writes, [None])

    def test_timeout_open_then_verified_close(self):
        f = Fake()
        f.start()
        f.tick()
        f.capability()
        for _ in range(2):
            f.tick()
            f.tick()
            f.reply()
        f.tick()
        f.tick()
        self.assertEqual(HEADER.unpack_from(f.writes[-1])[2], OPEN_RO)
        f.now += 1.1
        f.tick()
        f.tick()
        self.assertEqual(HEADER.unpack_from(f.writes[-1])[2], TERMINATE)
        f.reply()
        f.tick()
        self.assertFalse(f.ftp.unresolved)
        self.assertEqual(f.ftp.result, 'INCOMPLETE')

    def test_heartbeat_loss_during_open_latches(self):
        f = Fake()
        f.start()
        f.tick()
        f.capability()
        for _ in range(2):
            f.tick()
            f.tick()
            f.reply()
        f.tick()
        f.tick()
        self.assertEqual(HEADER.unpack_from(f.writes[-1])[2], OPEN_RO)
        f.allowed['heartbeat'] = False
        f.tick()
        self.assertTrue(f.ftp.unresolved)
        self.assertEqual(HEADER.unpack_from(f.writes[-1])[2], OPEN_RO)

    def test_tree_change_discards_result(self):
        f = Fake()
        f.change_listing = True
        f.run()
        self.assertEqual(f.ftp.reason, 'FTP_TREE_CHANGED')
        self.assertEqual(f.ftp.files, [])

    def test_no_ftp_capability(self):
        f = Fake()
        f.start()
        f.tick()
        f.capability(0)
        f.tick()
        self.assertEqual(f.ftp.reason, 'FTP_CAPABILITY_ABSENT')
        self.assertEqual(f.writes, [None])

    def test_local_incomplete_backup_artifact(self):
        with tempfile.TemporaryDirectory() as top:
            path = Path(top) / 'partial'
            responses = {'S3_FTP_START': {}, 'S3_FTP_RESULT': {'status': 'INCOMPLETE'},
                         'S3_FTP_RELEASE': {}}
            with self.assertRaisesRegex(RuntimeError, 'Backup incomplete'):
                backup(lambda op, _: responses[op], path)
            self.assertTrue((path / 'INCOMPLETE').is_file())
            self.assertFalse((path / 'SHA256SUMS').exists())

    def test_ipc_b1_b2_disabled_and_wrong_uid(self):
        for enabled, uid, expected in ((False, os.getuid(), False),
                                       (True, os.getuid()+1, False),
                                       (True, os.getuid(), True)):
            with self.subTest(enabled=enabled, uid=uid), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'owner.sock'
                seen = []
                server = OwnerServer(object(), path, allowed_uid=uid, disarm_request=lambda *_: None,
                                     stage3_dispatch=lambda peer, op, payload: seen.append(peer),
                                     ftp_enabled=enabled)
                server.start()
                try:
                    client = OwnerClient(path, policy=SocketPolicy(os.getuid()))
                    try:
                        reply = client.call('S3_FTP_RESULT', {'id': 1, 'username': 'test'},
                                            uuid.uuid4().hex, payload={})
                    except (OSError, ValueError, PermissionError):
                        self.assertFalse(expected)
                    else:
                        self.assertEqual(reply['ok'], expected)
                    self.assertEqual(len(seen), int(expected))
                    if seen:
                        self.assertEqual(seen[0][:2], (os.getuid(), os.getpid()))
                finally:
                    server.close()

    def test_peer_and_interleaving(self):
        f = Fake()
        with self.assertRaises(Refused):
            f.ftp.dispatch(PEER, 'S3_FTP_START', {'budget': 1}, other_busy=True)
        f.start()
        with self.assertRaises(Refused):
            f.ftp.dispatch((42, 123, 1), 'S3_FTP_RESULT', {})
        f.alive = False
        f.tick()
        self.assertEqual(f.ftp.result, 'INCOMPLETE')
        self.assertEqual(f.writes, [])

    def test_real_parser_full_b2_command(self):
        reference = os.getenv('B2_REFERENCE')
        if reference:
            text = Path(reference).read_text()
        else:
            text = ('[Service]\nWorkingDirectory=/baseline/owner\n'
                    'Environment=PYTHONPATH=/baseline/owner:/baseline/optical:/baseline\nExecStart=\n'
                    'ExecStart=/usr/bin/python3 -u -m stop_latch_test.hold_runner --journal /run/mock/journal '
                    '--web-disarm-socket /run/mock/socket --web-disarm-db /run/mock/db '
                    f'--web-disarm-client-uid {os.getuid()+1} --web-disarm-client-gid 123 '
                    '--inhibit-file /run/mock/inhibit --externalnav-observe-only --brake-hold-only '
                    '--observe-brake-pwm --functional-brake-boot-id mock --navigation-readback '
                    '--dynamic-mission-profile /run/mock/mission --stage3-operator-mode B2 '
                    '--stage3-bench-approval-file /run/mock/isolation --stage3-lua-attestation-file /run/mock/lua\n')
        base = next(s.split('=', 1)[1].removesuffix('/owner') for s in text.splitlines()
                    if s.startswith('WorkingDirectory='))
        config = render(text, base, '/overlay', '/run/mock/ftp-marker')
        argv = shlex.split(config.splitlines()[-1].removeprefix('ExecStart='))[4:]
        # The real unit template uses two systemd credential substitutions.
        values = dict(zip(IDENTITIES.values(),
                          (str(os.getuid()+1), '123')))
        argv = resolve_identities(argv, values)
        args = parse_args(argv)
        self.assertEqual((args.stage3_operator_mode, args.stage3_ftp_approval_file),
                         ('B2', '/run/mock/ftp-marker'))
        self.assertTrue(args.externalnav_observe_only)
        self.assertFalse(args.externalnav_tx or args.nav_bench_tx or args.nav_mission_tx)
        for item in ('--nav-bench-tx', '--nav-mission-tx'):
            with self.subTest(item=item), self.assertRaises(SystemExit):
                parse_args([*argv, item])
        with self.assertRaises(SystemExit):
            parse_args([*argv, '--stage3-operator-mode', 'B1'])


if __name__ == '__main__':
    unittest.main()
