"""Authenticated local IPC endpoint for an existing mission executor to host."""
import json
import re
import secrets
import socket
import sqlite3
import time
from threading import Lock

from disarm_control.ipc import peer_uid, receive
from .mission_resume import MissionSupervisorAdapter

_ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
_MISSION = re.compile(r'[A-Za-z0-9_.-]{1,64}')


class MissionIPCClient:
    def __init__(self, path, policy, mission_id):
        from pathlib import Path
        if not _MISSION.fullmatch(mission_id):
            raise ValueError('Invalid mission ID')
        self.path, self.policy, self.mission_id = Path(path), policy, mission_id

    def exchange(self, op, request_id, reset_id, *, timeout):
        nonce = secrets.token_hex(16)
        request = dict(op=op, request_id=request_id, reset_id=reset_id,
                       mission_id=self.mission_id, nonce=nonce)
        deadline = time.monotonic() + timeout

        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError('Mission IPC deadline')
            return value

        try:
            self.policy.check_parent(self.path)
            self.policy.check_socket(self.path)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.settimeout(remaining())
                stream.connect(str(self.path))
                if peer_uid(stream) != self.policy.uid:
                    raise PermissionError('Unexpected mission IPC peer')
                stream.settimeout(remaining())
                stream.sendall(json.dumps(request).encode() + b'\n')
                reply = receive(stream, timeout=remaining())
        except (OSError, ValueError):
            return None
        if any(reply.get(key) != request[key] for key in
               ('op', 'request_id', 'reset_id', 'mission_id', 'nonce')):
            return None
        return reply

    def adapter(self, read_evidence, clock):
        return MissionSupervisorAdapter(self.exchange, read_evidence, clock)


class MissionIPCEndpoint:
    """Receives one command. The executor consumes an accepted ticket separately.

    The host must supply independent physical readiness on both acceptance and
    consumption, and call front_pressed from its STOP chain. No default enable.
    """

    def __init__(self, owner_uid, ledger, read_state, physical_ready=None):
        self.owner_uid, self.read_state = owner_uid, read_state
        self.physical_ready = physical_ready
        self.lock = Lock()
        self.permit = self.pending = None
        self.pending_until = None
        self.db = sqlite3.connect(ledger, timeout=.05, check_same_thread=False)
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS accepted_resume(
                request_id TEXT PRIMARY KEY, reset_id TEXT UNIQUE NOT NULL,
                mission_id TEXT NOT NULL)''')

    def allow_reset(self, mission_id, reset_id):
        """Called locally by the host only for its current validated RESET."""
        if not _MISSION.fullmatch(mission_id) or not _ID.fullmatch(reset_id):
            raise ValueError('Invalid mission or RESET ID')
        with self.lock:
            self.permit, self.pending = (mission_id, reset_id), None

    def front_pressed(self):
        with self.lock:
            self.permit = self.pending = None

    def _ready(self, mission_id, reset_id):
        return (self.physical_ready is not None and
                self.physical_ready(mission_id, reset_id) is True)

    def take_pending(self):
        with self.lock:
            ticket, self.pending = self.pending, None
            if (ticket is not None and time.monotonic() < self.pending_until and
                    self._ready(*ticket[:2])):
                return ticket
            return None

    def serve_connection(self, stream):
        if peer_uid(stream) != self.owner_uid:
            return
        try:
            request = receive(stream, timeout=.1)
            if set(request) != {'op', 'request_id', 'reset_id', 'mission_id', 'nonce'}:
                return
            op, rid, reset, mission, nonce = (request[k] for k in
                ('op', 'request_id', 'reset_id', 'mission_id', 'nonce'))
            if (op not in ('MISSION_RESUME', 'MISSION_STATUS') or
                    not _ID.fullmatch(rid) or not _ID.fullmatch(reset) or
                    not _MISSION.fullmatch(mission) or not re.fullmatch(r'[0-9a-f]{32}', nonce)):
                return
            with self.lock:
                if op == 'MISSION_RESUME':
                    result = self._accept(mission, reset, rid)
                elif self.db.execute('SELECT 1 FROM accepted_resume WHERE request_id=? '
                                     'AND reset_id=? AND mission_id=?', (rid, reset, mission)).fetchone():
                    state, at = self.read_state(mission, reset, rid)
                    result = dict(mission_state=state, observed_at=at)
                else:
                    result = dict(mission_state='UNKNOWN')
            stream.settimeout(.1)
            stream.sendall(json.dumps(dict(request, **result), allow_nan=False).encode() + b'\n')
        except (OSError, ValueError, TypeError, sqlite3.Error):
            return

    def _accept(self, mission, reset, rid):
        if self.permit != (mission, reset) or not self._ready(mission, reset):
            return dict(acceptance='REFUSED')
        with self.db:
            inserted = self.db.execute('INSERT OR IGNORE INTO accepted_resume VALUES(?,?,?)',
                                       (rid, reset, mission)).rowcount
        if not inserted:
            return dict(acceptance='REFUSED')
        self.permit, self.pending = None, (mission, reset, rid)
        self.pending_until = time.monotonic() + .15
        return dict(acceptance='ACCEPTED')

    def close(self):
        self.db.close()
