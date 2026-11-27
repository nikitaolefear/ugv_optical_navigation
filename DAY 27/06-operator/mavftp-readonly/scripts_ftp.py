"""Read-only FTP protocol core. The Owner supplies authorization and transport."""
from hashlib import sha256
import re
from struct import Struct
from zlib import crc32

ROOT = "/APM/scripts"
TERMINATE, LIST, OPEN_RO, READ, CRC = 1, 3, 4, 5, 14
ACK, NAK, EOF, UNKNOWN_COMMAND = 128, 129, 6, 7
HEADER = Struct("<HBBBBBBI")
U32 = Struct("<I")
MAX_DATA = 239
MAX_FILE = 1024 * 1024
MAX_ENTRIES = 512
SEGMENT = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_. -]*")


class FTPError(RuntimeError):
    pass


def confined_path(parts):
    if not isinstance(parts, tuple) or len(parts) > 12:
        raise FTPError("invalid path depth")
    for part in parts:
        if (not isinstance(part, str) or not SEGMENT.fullmatch(part)
                or part.endswith((" ", "."))):
            raise FTPError("unsupported path segment")
    path = "/".join((ROOT, *parts)).encode("ascii")
    if len(path) >= MAX_DATA:
        raise FTPError("path too long")
    return path


def ardupilot_crc(data):
    return crc32(data, 0xffffffff) ^ 0xffffffff


class ScriptsFTP:
    """One instance per exclusive Owner operation. Invoke top-level methods serially.

    exchange awaits one bounded transaction on the existing Owner connection.
    It must validate the MAVLink envelope (peer, destination, link and message
    ID), prioritize telemetry, and recheck the guard at actual send time.
    The Owner supplies a nonconflicting initial FTP sequence.
    """

    def __init__(self, exchange, guard, sequence):
        self._exchange = exchange
        self._guard = guard
        self._sequence = sequence & 0xffff
        self.session_uncertain = False

    async def _request(self, operation, *, parts=(), offset=0, count=0):
        if operation not in (TERMINATE, LIST, OPEN_RO, READ, CRC):
            raise FTPError("operation forbidden")
        path = confined_path(parts) if operation in (LIST, OPEN_RO, CRC) else b""
        self._guard()
        seq = self._sequence
        self._sequence = (seq + 2) & 0xffff
        # Rover uses the same client-selected session ID in its reply.
        packet = HEADER.pack(seq, 0, operation, count or len(path), 0, 0, 0, offset)
        if operation == OPEN_RO:
            self.session_uncertain = True
        reply = await self._exchange((packet + path).ljust(251, b"\0"))
        if len(reply) != 251:
            raise FTPError("invalid MAVFTP payload length")
        number, session, opcode, size, request, burst, _, reply_offset = HEADER.unpack_from(reply)
        if (number != (seq + 1) & 0xffff or session != 0 or request != operation
                or burst or size > MAX_DATA):
            raise FTPError("uncorrelated MAVFTP reply")
        data = reply[HEADER.size:HEADER.size + size]
        if opcode == NAK and size in (1, 2):
            if operation == OPEN_RO:
                self.session_uncertain = False
            return opcode, data
        if opcode != ACK or reply_offset != offset:
            raise FTPError("unexpected MAVFTP reply")
        return opcode, data

    async def list_directory(self, parts=()):
        confined_path(parts)
        if self.session_uncertain:
            raise FTPError("unresolved FTP session")
        offset = 0
        entries = {}
        seen = set()
        while True:
            opcode, data = await self._request(LIST, parts=parts, offset=offset)
            if opcode == NAK:
                if data == bytes([EOF]):
                    self._guard()
                    return entries
                raise FTPError("directory listing rejected")
            if not data.endswith(b"\0"):
                raise FTPError("incomplete directory page")
            for entry in data[:-1].split(b"\0"):
                offset += 1
                if offset > MAX_ENTRIES:
                    raise FTPError("directory entry limit")
                if entry in (b"D.", b"D.."):
                    continue
                kind, name, raw_size = self._entry(entry)
                confined_path((*parts, name))
                if name.casefold() in seen:
                    raise FTPError("duplicate or case-aliased entry")
                seen.add(name.casefold())
                entries[name] = int(raw_size) if kind == "F" else None

    @staticmethod
    def _entry(entry):
        try:
            text = entry.decode("ascii")
        except UnicodeDecodeError as error:
            raise FTPError("unsupported directory entry encoding") from error
        name, sep, size = text[1:].partition("\t")
        if text.startswith("D") and not sep:
            return "D", name, ""
        if text.startswith("F") and sep and size.isdecimal() and len(size) <= 10:
            return "F", name, size
        raise FTPError("unsupported directory entry")

    async def fetch_file(self, parts, listed_size):
        confined_path(parts)
        if not parts or not 0 <= listed_size <= MAX_FILE or self.session_uncertain:
            raise FTPError("file outside limits or unresolved FTP session")
        contents = bytearray()
        try:
            opcode, data = await self._request(OPEN_RO, parts=parts)
            if opcode != ACK or len(data) != U32.size or U32.unpack(data)[0] != listed_size:
                raise FTPError("file open failed or size changed")
            while len(contents) < listed_size:
                offset = len(contents)
                size = min(MAX_DATA, listed_size - offset)
                opcode, data = await self._request(READ, offset=offset, count=size)
                if opcode != ACK or len(data) != size:
                    raise FTPError("short or rejected read")
                contents.extend(data)
        finally:
            if self.session_uncertain:
                # A failed guard prevents even cleanup traffic. Retain the
                # unresolved-session flag and let the Owner report the failure.
                opcode, data = await self._request(TERMINATE)
                if opcode != ACK or data:
                    raise FTPError("session termination not verified")
                self.session_uncertain = False

        opcode, data = await self._request(CRC, parts=parts)
        self._guard()
        if opcode == ACK and len(data) == U32.size:
            remote_crc = U32.unpack(data)[0]
            if remote_crc != ardupilot_crc(contents):
                raise FTPError("remote CRC mismatch")
        elif opcode == NAK and data == bytes([UNKNOWN_COMMAND]):
            remote_crc = None
        else:
            raise FTPError("CRC request failed")
        return bytes(contents), sha256(contents).hexdigest(), remote_crc
