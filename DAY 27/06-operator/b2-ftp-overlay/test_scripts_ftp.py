import hashlib
import unittest

from disarm_control.scripts_ftp import (
    ACK, CRC, EOF, HEADER, LIST, NAK, OPEN_RO, READ, TERMINATE, U32,
    UNKNOWN_COMMAND, FTPError, ScriptsFTP, ardupilot_crc, confined_path,
)


class FakeRover:
    def __init__(self):
        self.data = b"x" * 240
        self.calls = []
        self.unsafe = False
        self.fail_guard_after_read = False
        self.bad_crc = False
        self.no_crc = False
        self.bad_sequence = False
        self.short_read = False
        self.bad_close = False
        self.directory = b"Dsub\0Fsample.lua\t240\0"

    def guard(self):
        if self.unsafe:
            raise FTPError("interlock")

    async def exchange(self, packet):
        seq, session, operation, size, _, _, _, offset = HEADER.unpack_from(packet)
        path = packet[12:12 + size]
        self.calls.append((operation, offset, path))
        opcode = ACK
        if operation == LIST:
            if offset == 0:
                data = self.directory
            else:
                opcode, data = NAK, bytes([EOF])
        elif operation == OPEN_RO:
            data = U32.pack(len(self.data))
        elif operation == READ:
            data = self.data[offset:offset + size]
            if self.short_read:
                data = data[:-1]
            if self.fail_guard_after_read:
                self.unsafe = True
        elif operation == TERMINATE:
            data = b""
            if self.bad_close:
                opcode, data = NAK, b"\x01"
        elif operation == CRC:
            data = U32.pack(ardupilot_crc(self.data) ^ int(self.bad_crc))
            if self.no_crc:
                opcode, data = NAK, bytes([UNKNOWN_COMMAND])
        else:
            raise AssertionError(operation)
        seq = (seq + 1 + int(self.bad_sequence)) & 0xffff
        header = HEADER.pack(seq, session, opcode, len(data), operation, 0, 0, offset)
        return (header + data).ljust(251, b"\0")


class Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rover = FakeRover()
        self.ftp = ScriptsFTP(self.rover.exchange, self.rover.guard, 65535)

    async def test_listing_pagination_and_full_download(self):
        self.assertEqual(await self.ftp.list_directory(), {"sub": None, "sample.lua": 240})
        data, sha, crc = await self.ftp.fetch_file(("sample.lua",), 240)
        self.assertEqual((data, sha, crc),
                         (self.rover.data, hashlib.sha256(data).hexdigest(), ardupilot_crc(data)))
        self.assertEqual([c[0] for c in self.rover.calls],
                         [LIST, LIST, OPEN_RO, READ, READ, TERMINATE, CRC])
        self.assertEqual(self.rover.calls[1][1], 2)
        self.assertEqual(self.rover.calls[4][1], 239)

    def test_crc_vector(self):
        self.assertEqual(ardupilot_crc(b"123456789"), 0x2dfd2d88)

    async def test_crc_mismatch(self):
        self.rover.bad_crc = True
        with self.assertRaisesRegex(FTPError, "CRC mismatch"):
            await self.ftp.fetch_file(("sample.lua",), 240)

    async def test_unknown_crc(self):
        self.rover.no_crc = True
        self.assertIsNone((await self.ftp.fetch_file(("sample.lua",), 240))[2])

    async def test_unsafe_before_send(self):
        self.rover.unsafe = True
        with self.assertRaisesRegex(FTPError, "interlock"):
            await self.ftp.list_directory()
        self.assertEqual(self.rover.calls, [])

    async def test_unsafe_mid_transfer(self):
        self.rover.fail_guard_after_read = True
        with self.assertRaisesRegex(FTPError, "interlock"):
            await self.ftp.fetch_file(("sample.lua",), 240)
        self.assertEqual([c[0] for c in self.rover.calls], [OPEN_RO, READ])
        self.assertTrue(self.ftp.session_uncertain)

    async def test_short_read_closes_session(self):
        self.rover.short_read = True
        with self.assertRaisesRegex(FTPError, "short"):
            await self.ftp.fetch_file(("sample.lua",), 240)
        self.assertEqual(self.rover.calls[-1][0], TERMINATE)
        self.assertFalse(self.ftp.session_uncertain)

    async def test_bad_close_latches(self):
        self.rover.bad_close = True
        with self.assertRaisesRegex(FTPError, "termination"):
            await self.ftp.fetch_file(("sample.lua",), 240)
        with self.assertRaisesRegex(FTPError, "unresolved"):
            await self.ftp.list_directory()

    async def test_uncorrelated_reply(self):
        self.rover.bad_sequence = True
        with self.assertRaisesRegex(FTPError, "uncorrelated"):
            await self.ftp.list_directory()

    async def test_untrusted_directory_names(self):
        for listing in (b"D../other\0", b"DA\0Da\0", b"Dno_null", b"D\xff\0"):
            with self.subTest(listing=listing):
                self.rover.directory = listing
                with self.assertRaises(FTPError):
                    await self.ftp.list_directory()

    def test_path_confinement(self):
        self.assertEqual(confined_path(("sub", "sample.lua")), b"/APM/scripts/sub/sample.lua")
        for name in ("", "..", ".", "x/y", "x\\y", "/absolute", "A:x", "a\x00b",
                     "my~1.lua", "a.", "a ", "é.lua", "x"*239):
            with self.subTest(name=name), self.assertRaises(FTPError):
                confined_path((name,))

    async def test_mutating_opcodes_refused(self):
        for op in (2, 6, 7, 8, 9, 10, 11, 12, 13, 15):
            with self.subTest(op=op), self.assertRaisesRegex(FTPError, "forbidden"):
                await self.ftp._request(op)
        self.assertEqual(self.rover.calls, [])
