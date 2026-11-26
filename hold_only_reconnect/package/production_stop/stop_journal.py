"""Private crash-recoverable STOP state and audit in the existing RESET DB."""
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import time


class StopJournal:
    def __init__(self, path):
        self.fd = self.db = None
        path = Path(path)
        info = path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError('Private STOP state directory required')
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            self.fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            new = True
        except FileExistsError:
            self.fd = os.open(path, flags)
            new = False
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PermissionError('Untrusted STOP journal')
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not new and info.st_size == 0:
                raise ValueError('Incomplete STOP journal')
            self.db = sqlite3.connect(path, timeout=.1, check_same_thread=False)
            if self.db.execute('PRAGMA journal_mode=DELETE').fetchone() != ('delete',):
                raise ValueError('Cannot configure durable STOP journal')
            # EXTRA syncs the database and rollback journal and, in DELETE
            # mode, the parent directory following journal unlink at commit.
            self.db.execute('PRAGMA synchronous=EXTRA')
            if self.db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('Corrupt STOP journal')
            tables = {row[0] for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not new and ('reset_requests' not in tables or
                            ('stop_state' in tables) != ('stop_events' in tables)):
                raise ValueError('Incomplete STOP schema')
            self.boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                self.db.execute('''CREATE TABLE IF NOT EXISTS reset_requests(
                    id TEXT PRIMARY KEY,actor_id INTEGER NOT NULL,actor TEXT NOT NULL,
                    time REAL NOT NULL,accepted INTEGER NOT NULL,reasons TEXT NOT NULL)''')
                self.db.execute('''CREATE TABLE IF NOT EXISTS stop_state(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    latched INTEGER NOT NULL CHECK(latched IN (0,1)))''')
                self.db.execute('''CREATE TABLE IF NOT EXISTS stop_events(
                    sequence INTEGER PRIMARY KEY, boot_id TEXT NOT NULL,
                    utc_ns INTEGER NOT NULL, event TEXT NOT NULL,
                    latched INTEGER NOT NULL CHECK(latched IN (0,1)), details TEXT NOT NULL)''')
                self.db.execute('CREATE TABLE IF NOT EXISTS web_brake_releases(id TEXT PRIMARY KEY)')
                prior = self.db.execute('SELECT * FROM stop_state').fetchall()
                if 'stop_state' in tables:
                    latest = self.db.execute(
                        'SELECT latched FROM stop_events ORDER BY sequence DESC LIMIT 1').fetchone()
                    if prior not in ([(1, 0)], [(1, 1)]) or latest != (prior[0][1],):
                        raise ValueError('Inconsistent STOP journal')
                self._write('OWNER_START', True, {'previous_latch': bool(prior[0][1]) if prior else None})
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            self.close()
            raise

    def _write(self, event, latched, details):
        self.db.execute('INSERT OR REPLACE INTO stop_state VALUES(1,?)', (int(latched),))
        self.db.execute('INSERT INTO stop_events(boot_id,utc_ns,event,latched,details) VALUES(?,?,?,?,?)',
                        (self.boot_id, time.time_ns(), event, int(latched), json.dumps(details)))

    def record(self, event, latched, details):
        with self.db:
            self._write(event, latched, details)

    def previous_actor(self, request_id):
        return self.db.execute('SELECT actor_id FROM reset_requests WHERE id=?', (request_id,)).fetchone()

    def record_web_release(self, request_id, actor_id, actor):
        with self.db:
            inserted = self.db.execute('INSERT OR IGNORE INTO web_brake_releases VALUES(?)',
                                       (request_id,)).rowcount
            if inserted:
                self._write('WEB_BRAKE_RELEASE_REQUESTED', False,
                            {'request_id': request_id, 'actor_id': actor_id, 'actor': actor})
            return inserted == 1

    def record_reset(self, request_id, actor_id, actor, now, accepted, latched, reasons):
        with self.db:
            self.db.execute('INSERT INTO reset_requests VALUES(?,?,?,?,?,?)',
                            (request_id, actor_id, actor, now, int(accepted), json.dumps(reasons)))
            self._write('RESET_ACCEPTED' if accepted else 'RESET_DENIED', latched,
                        {'request_id': request_id, 'actor_id': actor_id, 'reasons': reasons})

    def close(self):
        if self.db is not None:
            self.db.close()
        if self.fd is not None:
            os.close(self.fd)
