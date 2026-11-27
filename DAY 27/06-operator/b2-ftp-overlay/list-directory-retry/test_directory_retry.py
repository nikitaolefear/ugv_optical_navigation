"""Synthetic ListDirectory pages with Rover 4.7.2 packing semantics."""
import json
import stat
import tempfile
from pathlib import Path
import unittest

from backup_client import backup
from disarm_control.scripts_ftp import ACK, EOF, HEADER, MAX_DATA, NAK, DirectoryError, ScriptsFTP
from offline_tests import Fake, PEER


class DirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def listing(self, page):
        offsets = []

        async def exchange(packet):
            seq, session, op, _, _, _, _, offset = HEADER.unpack_from(packet)
            offsets.append(offset)
            opcode, data, reply_offset = page(offset)
            return (HEADER.pack((seq + 1) & 65535, session, opcode, len(data), op, 0, 0,
                                reply_offset) + data).ljust(251, b'\0')

        return await ScriptsFTP(exchange, lambda: None, 65535).list_directory(), offsets

    async def test_rover_packs_multiple_pages_without_skips(self):
        records = [b'Dsub'] + [
            f'Fsample-{i}-'.encode() + b'x'*85 + b'.lua\t42' for i in range(7)
        ]

        def page(offset):
            if offset >= len(records):
                return NAK, bytes([EOF]), offset
            data = b''
            for record in records[offset:]:
                # snprintf with explicit %c NUL returns len(record)+1.
                if len(data) + len(record) + 1 >= MAX_DATA:
                    break
                data += record + b'\0\0'
            return ACK, data, offset

        result, offsets = await self.listing(page)
        self.assertEqual(list(result), ['sub'] + [entry[1:].split(b'\t')[0].decode()
                                                  for entry in records[1:]])
        self.assertEqual(list(result.values()), [None] + [42]*7)
        self.assertEqual(offsets, [0, 3, 5, 7, 8])

    async def test_single_double_and_skip_records_count_once(self):
        pages = {0: b'D.\0\0S\0D..\0Fa.lua\t4294967295\0\0',
                 4: b'Fb.lua\t0\0Dsub\0\0'}
        result, offsets = await self.listing(lambda i: (ACK, pages[i], i)
                                             if i in pages else (NAK, bytes([EOF]), i))
        self.assertEqual((result, offsets), ({'a.lua': 4294967295, 'b.lua': 0, 'sub': None}, [0, 4, 6]))

    async def test_empty_directory_eof(self):
        self.assertEqual(await self.listing(lambda i: (NAK, bytes([EOF]), i)), ({}, [0]))

    async def test_reject_invalid_records_and_capture(self):
        for invalid in (b'', b'\0', b'Fa\t1', b'Fa\t+1\0', b'Fa\t4294967296\0',
                        b'Fa\t1\0\0\0', b'D..\\escape\0', b'Xunknown\0', b'Sunknown\0',
                        b'Fa\t1\0FA\t1\0'):
            with self.subTest(invalid=invalid), self.assertRaises(DirectoryError) as failure:
                await self.listing(lambda i: (ACK, invalid, i))
            event = failure.exception.capture
            self.assertEqual((event['sequence'], event['offset'], event['session_id'], event['opcode']),
                             (0, 0, 0, ACK))
            self.assertEqual(event['declared_size'], len(invalid))
            self.assertEqual(bytes.fromhex(event['payload_hex'])[12:12+len(invalid)], invalid)

    async def test_duplicate_across_pages(self):
        with self.assertRaisesRegex(DirectoryError, 'duplicate'):
            await self.listing(lambda i: (ACK, b'Fsame.lua\t1\0', i))

    async def test_nak_only_accepts_exact_eof_and_offset(self):
        for data, offset in ((bytes([EOF, 0]), 0), (b'\x01', 0), (bytes([EOF]), 1)):
            with self.subTest(data=data, offset=offset), self.assertRaises(DirectoryError):
                await self.listing(lambda _: (NAK, data, offset))


class CaptureTests(unittest.TestCase):
    def test_owner_failure_retains_capture_for_same_peer_and_releases(self):
        f = Fake()
        f.offset = 1
        f.run()
        result = f.ftp.dispatch(PEER, 'S3_FTP_RESULT', {})
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertEqual(result['failure_capture']['offset'], 1)
        f.ftp.dispatch(PEER, 'S3_FTP_RELEASE', {})
        self.assertIsNone(f.ftp.failure_capture)

    def test_client_writes_bounded_private_capture_without_logging_bytes(self):
        event = dict(payload_hex='ab'*251, declared_size=1, offset=0, sequence=1,
                     opcode=ACK, session_id=0, reason='invalid record')
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'new'
            responses = {'S3_FTP_START': {}, 'S3_FTP_RELEASE': {},
                         'S3_FTP_RESULT': {'status': 'INCOMPLETE', 'reason': 'invalid record',
                                           'failure_capture': event}}
            with self.assertRaisesRegex(RuntimeError, '^Backup incomplete: invalid record$'):
                backup(lambda op, _: responses[op], output)
            path = output / 'list-directory-failure.json'
            self.assertEqual(json.loads(path.read_text()), event)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertLessEqual(path.stat().st_size, 1024)
            self.assertTrue((output / 'INCOMPLETE').is_file())
