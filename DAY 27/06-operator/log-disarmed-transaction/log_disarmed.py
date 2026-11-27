"""Owner-local transaction core. Transport and authorization are injected."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

PARAMETER = "LOG_DISARMED"
GUARDS = ("fresh_heartbeat", "hold", "disarmed", "inhibit", "stop_latch",
          "isolated", "tx_disabled", "tx_zero", "exclusive_owner")
HEX = re.compile(r"[0-9a-f]{32,64}")
PHASES = {"enable_intent", "enabled", "restore_intent", "restored"}


class Refused(RuntimeError):
    pass


def identity(value):
    if (type(value) is not dict or set(value) != {"hardware", "pi_boot", "fc_boot", "owner_session"}
            or any(not isinstance(v, str) or not HEX.fullmatch(v) for v in value.values())):
        raise Refused("verified hardware and session identity required")
    return dict(value)


class Journal:
    """One record, atomically updated. Retain completed records until next intent."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.record = self.directory / "log-disarmed.json"

    @contextmanager
    def lock(self):
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Refused("private persistent journal directory required")
        lock = self.directory / "log-disarmed.lock"
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            s = os.fstat(fd)
            if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_nlink != 1 or s.st_mode & 0o077:
                raise Refused("invalid lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def load(self):
        try:
            fd = os.open(self.record, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            s = os.fstat(stream.fileno())
            if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_nlink != 1 or s.st_mode & 0o077:
                raise Refused("invalid journal inode")
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise Refused("oversize journal")
        envelope = json.loads(raw)
        row = envelope["record"]
        if (envelope["sha256"] != hashlib.sha256(self.encode(row)).hexdigest()
                or set(row) != {"transaction", "identity", "baseline", "phase", "last_session"}
                or row["phase"] not in PHASES or type(row["baseline"]) is not int
                or row["baseline"] not in (0, 2, 3)
                or not isinstance(row["transaction"], str) or not HEX.fullmatch(row["transaction"])):
            raise Refused("invalid journal")
        identity(row["identity"])
        identity(row["last_session"])
        if row["last_session"]["hardware"] != row["identity"]["hardware"]:
            raise Refused("inconsistent journal hardware")
        return row

    @staticmethod
    def encode(data):
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()

    def save(self, row):
        body = self.encode(dict(record=row, sha256=hashlib.sha256(self.encode(row)).hexdigest()))
        fd, temporary = tempfile.mkstemp(prefix=".log-disarmed-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.record)
            directory = os.open(self.directory, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)


class Transaction:
    """Run away from the Owner telemetry tick, with one checked port.

    Port samples identity and guards from the same owner stream. read_log_disarmed
    must initiate a new PARAM_REQUEST_READ and wait for its own receive window.
    write_log_disarmed permits only the fixed PARAM_SET on the Owner link. The
    injected port calls check on the Owner thread immediately before actual send.
    Queued work must be cancelled at timeout, never dispatched after expiration.
    """

    def __init__(self, journal, port, *, gateway_uid, authorized, profile):
        self.journal, self.port = journal, port
        self.uid, self.authorized, self.profile = gateway_uid, authorized, profile

    def execute(self, peer, action, request):
        # peer is from SO_PEERCRED plus process generation, never client JSON.
        expected = {"begin": set(), "restore": {"transaction"}, "recover": {"transaction"}}
        if (type(peer) is not tuple or len(peer) != 3 or peer[0] != self.uid
                or type(request) is not dict or action not in expected
                or set(request) != expected[action] or self.profile != "B2-LOG"):
            raise Refused("operation, profile or peer forbidden")
        with self.journal.lock():
            current = identity(self.port.identity())
            row = self.journal.load()
            transaction = uuid.uuid4().hex if action == "begin" else request["transaction"]

            def guard():
                state = self.port.safety()
                if (identity(self.port.identity()) != current
                        or not self.authorized(peer, action, transaction, current)
                        or any(state.get(k) is not True for k in GUARDS)):
                    raise Refused("authorization, session or safety lost")

            def read():
                guard()
                value = self.port.read_log_disarmed(guard)
                guard()
                if type(value) is not int or value not in (0, 1, 2, 3):
                    raise Refused("invalid independent readback")
                return value

            guard()
            if action == "begin":
                if row and row["phase"] != "restored":
                    raise Refused("unfinished journal; explicit recovery required")
                baseline = read()
                if baseline == 1:
                    raise Refused("already enabled; no transaction")
                row = dict(transaction=transaction, identity=current, last_session=current,
                           baseline=baseline, phase="enable_intent")
                self.journal.save(row)
                guard()
                self.port.write_log_disarmed(1, guard)
                if read() != 1:
                    raise Refused("enable readback mismatch")
                row["phase"] = "enabled"
            else:
                if (not row or row["transaction"] != transaction or row["phase"] == "restored"
                        or row["identity"]["hardware"] != current["hardware"]):
                    raise Refused("no matching transaction on this hardware")
                if action == "restore" and (row["phase"] != "enabled" or current != row["identity"]):
                    raise Refused("explicit recovery required")
                baseline = row["baseline"]
                value = read()
                if value not in (baseline, 1):
                    raise Refused("unexpected live value; manual reconciliation required")
                row.update(phase="restore_intent", last_session=current)
                self.journal.save(row)
                if value != baseline:
                    guard()
                    self.port.write_log_disarmed(baseline, guard)
                if read() != baseline:
                    raise Refused("restore readback mismatch")
                row["phase"] = "restored"
            self.journal.save(row)
            return dict(row)
