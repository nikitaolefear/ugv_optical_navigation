"""Explicit, idempotent migration. Never called by application startup."""
import fcntl
import os
import sqlite3
from .auth import AuthStore, private_file
from .access_control import initialize_roles, USERS_SQL


def migrate_access(directory):
    store = AuthStore(directory, read_only=True)
    lock = store.directory / 'hmi-engine.lock'
    private_file(lock)
    fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
    db = None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Stop the test HMI before migration') from None
        db = sqlite3.connect(store.db_path.as_uri() + '?mode=rw', uri=True, timeout=5)
        db.execute('PRAGMA foreign_keys=OFF')  # Before BEGIN; rebuilt parent retains its name and IDs.
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='access_meta'").fetchone():
            if db.execute('SELECT version FROM access_meta').fetchall() != [(1,)]:
                raise RuntimeError('Unsupported access schema')
            changed = False
        else:
            # Refuse unexpected legacy layouts, never guess at user columns.
            columns = [r[1] for r in db.execute('PRAGMA table_info(users)')]
            if columns != ['id', 'username', 'password_hash', 'role', 'active', 'created']:
                raise RuntimeError('Unsupported legacy users schema')
            initialize_roles(db)
            db.execute(USERS_SQL.replace('CREATE TABLE users(', 'CREATE TABLE users_new('))
            db.execute('INSERT INTO users_new(id,username,password_hash,role,active,created) '
                       'SELECT id,username,password_hash,role,active,created FROM users')
            db.execute('DROP TABLE users')
            db.execute('ALTER TABLE users_new RENAME TO users')
            changed = True
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Foreign key check failed; migration rolled back')
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise RuntimeError('Integrity check failed; migration rolled back')
        db.commit()
        return changed
    except BaseException:
        if db is not None:
            db.rollback()
        raise
    finally:
        if db is not None:
            db.close()
        os.close(fd)
