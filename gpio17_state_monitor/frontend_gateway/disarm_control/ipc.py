"""Private, peer-credential checked local command channel; no serial access."""
import json
import hmac
import os
from dataclasses import dataclass
from pathlib import Path
import socket
import socketserver
import stat
import struct
import secrets
import time
from threading import Thread

from .engine import Rejected, finite

LIMIT = 4096


@dataclass(frozen=True)
class SocketPolicy:
    """Explicit server filesystem identity, distinct from allowed client UID.

    Cross-UID endpoints require an exact trusted GID and 0710/0660 modes.
    Provision the directory and group outside the service; clients never gain
    directory write permission. The service only changes its newly bound inode.
    """
    uid: int
    gid: int | None = None
    directory_mode: int = 0o700
    socket_mode: int = 0o600

    def __post_init__(self):
        if type(self.uid) is not int or self.uid < 0:
            raise ValueError('Invalid server UID')
        if self.gid is not None and (type(self.gid) is not int or self.gid < 0):
            raise ValueError('Invalid trusted GID')
        if (self.directory_mode, self.socket_mode) not in ((0o700, 0o600), (0o710, 0o660)):
            raise ValueError('Unsupported socket policy modes')
        if self.directory_mode == 0o710 and self.gid is None:
            raise ValueError('Cross-UID policy requires an explicit trusted GID')

    def check_parent(self, path):
        info = path.parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != self.uid or
                stat.S_IMODE(info.st_mode) != self.directory_mode or
                (self.gid is not None and info.st_gid != self.gid)):
            raise PermissionError('Unexpected socket directory identity or mode')

    def check_socket(self, path):
        info = path.lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.uid or
                stat.S_IMODE(info.st_mode) != self.socket_mode or
                (self.gid is not None and info.st_gid != self.gid)):
            raise PermissionError('Unexpected socket identity or mode')
        return info


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate JSON field')
        value[key] = item
    return value


def reject_constant(value):
    raise ValueError('Non-finite JSON number')


def receive(stream, *, timeout=.7):
    deadline = time.monotonic() + timeout
    data = bytearray()
    while b'\n' not in data and len(data) <= LIMIT:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Request deadline expired')
        stream.settimeout(remaining)
        part = stream.recv(min(1024, LIMIT + 1 - len(data)))
        if not part:
            raise ValueError('Incomplete request')
        data.extend(part)
    if len(data) > LIMIT or not data.endswith(b'\n'):
        raise ValueError('Invalid request size')
    value = json.loads(data, object_pairs_hook=unique_object, parse_constant=reject_constant)
    if type(value) is not dict:
        raise ValueError('Object required')
    return value


def private_parent(path, uid):
    SocketPolicy(uid).check_parent(path)


def peer_uid(stream):
    return struct.unpack('3i', stream.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))[1]


class OwnerServer:
    """Explicit start only, one bounded request at a time, no unlink-on-start."""
    def __init__(self, engine, path, *, allowed_uid=None, policy=None, stop_operations=None, arm_operations=None, disarm_request=None):
        self.engine, self.path = engine, Path(path)
        self.stop_operations = stop_operations or {}
        self.arm_operations = arm_operations or {}
        if set(self.arm_operations) - {'ARM', 'ARM_READY', 'ARM_STATUS'}:
            raise ValueError('Invalid ARM operations')
        self.disarm_request = disarm_request or engine.request
        if set(self.stop_operations) - {'STOP_STATUS', 'STOP_RESET', 'MISSION_RESUME'}:
            raise ValueError('Invalid STOP operations')
        self.allowed_uid = os.getuid() if allowed_uid is None else allowed_uid
        if type(self.allowed_uid) is not int or self.allowed_uid < 0:
            raise ValueError('Invalid allowed client UID')
        self.policy = policy or SocketPolicy(os.getuid())
        self.server = self.thread = self.inode = None

    def start(self):
        if self.policy.uid != os.getuid():
            raise PermissionError('Service cannot impersonate a different server UID')
        self.policy.check_parent(self.path)
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(.7)
                body = {}
                try:
                    if peer_uid(self.request) != owner.allowed_uid:
                        raise PermissionError('Peer refused')
                    body = receive(self.request)
                    nonce = body.get('nonce')
                    if (set(body) != {'op', 'actor_id', 'actor', 'request_id', 'nonce'} or
                            type(nonce) is not str or len(nonce) != 32 or
                            any(c not in '0123456789abcdef' for c in nonce)):
                        raise Rejected('INVALID_REQUEST')
                    if body['op'] == 'DISARM':
                        value = owner.disarm_request(body['actor_id'], body['actor'], body['request_id'])
                    elif body['op'] == 'STATUS':
                        value = owner.engine.status(body['actor_id'], body['request_id'])
                    elif body['op'] in owner.stop_operations:
                        value = owner.stop_operations[body['op']](body['actor_id'], body['actor'], body['request_id'])
                    elif body['op'] in owner.arm_operations:
                        value = owner.arm_operations[body['op']](body['actor_id'], body['actor'], body['request_id'])
                    else:
                        raise Rejected('UNKNOWN_OPERATION')
                    response = {'ok': True, 'command': value}
                except Rejected as exc:
                    response = {'ok': False, 'error': str(exc)}
                except Exception:
                    response = {'ok': False, 'error': 'OWNER_UNAVAILABLE'}
                response.update(op=body.get('op'), request_id=body.get('request_id'), nonce=body.get('nonce'))
                try:
                    self.request.sendall(json.dumps(response, allow_nan=False).encode() + b'\n')
                except OSError:
                    pass  # command intent is durable; never repeat a lost reply

        self.server = socketserver.UnixStreamServer(str(self.path), Handler)
        try:
            if self.policy.gid is not None:
                os.chown(self.path, -1, self.policy.gid)
            os.chmod(self.path, self.policy.socket_mode)
            self.policy.check_socket(self.path)
        except BaseException:
            self.server.server_close()
            self.path.unlink()
            self.server = None
            raise
        self.inode = self.path.stat().st_ino
        self.thread = Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        try:
            self.thread.start()
        except BaseException:
            self.server.server_close()
            if self.path.exists() and self.path.stat().st_ino == self.inode:
                self.path.unlink()
            self.server = self.thread = None
            raise

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
            if self.path.exists() and self.path.stat().st_ino == self.inode:
                self.path.unlink()


class OwnerClient:
    def __init__(self, path, *, expected_uid=None, policy=None, timeout=.9):
        self.path = str(path)
        if policy is not None and expected_uid is not None and policy.uid != expected_uid:
            raise ValueError('Conflicting server UID policies')
        self.policy = policy or SocketPolicy(os.getuid() if expected_uid is None else expected_uid)
        self.expected_uid = self.policy.uid
        if not finite(timeout) or not 0 < timeout <= 3:
            raise ValueError('Invalid IPC timeout')
        self.timeout = timeout

    def call(self, op, actor, request_id):
        # No retries, redirects, TCP, environment proxies or serial openers.
        path = Path(self.path)
        self.policy.check_parent(path)
        info = self.policy.check_socket(path)
        nonce = secrets.token_hex(16)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(self.timeout)
            stream.connect(self.path)
            self.policy.check_parent(path)
            current = self.policy.check_socket(path)
            if (peer_uid(stream) != self.expected_uid or
                    (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)):
                raise PermissionError('Owner identity changed')
            stream.sendall(json.dumps(dict(op=op, actor_id=actor['id'],
                                          actor=actor['username'], request_id=request_id,
                                          nonce=nonce)).encode()+b'\n')
            result = receive(stream, timeout=self.timeout)
            if (result.get('op') != op or result.get('request_id') != request_id or
                    type(result.get('nonce')) is not str or not result['nonce'].isascii() or
                    not hmac.compare_digest(result['nonce'], nonce)):
                raise ValueError('Unbound owner response')
            return result
