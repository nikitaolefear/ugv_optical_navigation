"""SQLite identities, opaque server sessions and bounded audit. No hardware imports."""
from contextlib import contextmanager
import hashlib
import fcntl
from functools import wraps
import ipaddress
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
import threading
from urllib.parse import urlsplit

from werkzeug.security import generate_password_hash, check_password_hash

try:
    from .access_control import PERMISSIONS, DEFAULT_ROLES, initialize_roles, USERS_SQL
except ImportError:
    from access_control import PERMISSIONS, DEFAULT_ROLES, initialize_roles, USERS_SQL

ROLES = tuple(DEFAULT_ROLES)
PASSWORD_METHOD = 'scrypt:32768:8:1'
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


class AccountError(ValueError):
    pass


class AccessDenied(AccountError):
    pass


def diagnostic_origin(value):
    """Return only a bare HTTP(S) origin, preserving its exact spelling for diagnosis."""
    if (not isinstance(value, str) or len(value) > 320 or not value.isascii()
            or not re.fullmatch(r'https?://[A-Za-z0-9.\[\]:-]+', value, re.IGNORECASE)):
        return 'INVALID_ORIGIN'
    try:
        parts = urlsplit(value)
        # Regex excludes userinfo, whitespace, path, query and fragment, even empty delimiters.
        if parts.netloc.startswith('['):
            closing = parts.netloc.index(']')
            ipaddress.IPv6Address(parts.netloc[1:closing])
            suffix = parts.netloc[closing + 1:]
        else:
            host, separator, port = parts.netloc.partition(':')
            if len(host) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?',
                                                      label) for label in (host[:-1] if host.endswith('.') else host).split('.')):
                return 'INVALID_ORIGIN'
            suffix = separator + port
        if suffix and not re.fullmatch(r':[0-9]{1,5}', suffix):
            return 'INVALID_ORIGIN'
        if suffix and parts.port is None:
            return 'INVALID_ORIGIN'
        # Evaluating port rejects values outside 0..65535.
        parts.port
    except (ValueError, IndexError):
        return 'INVALID_ORIGIN'
    return value


def username(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{3,64}', value):
        raise AccountError('Логін: 3–64 символи, латиниця, цифри, крапка, _ або -.')
    return value.lower()


def password_hash(value):
    if not isinstance(value, str) or not MIN_PASSWORD_LENGTH <= len(value) <= MAX_PASSWORD_LENGTH:
        raise AccountError(f'Пароль має містити від {MIN_PASSWORD_LENGTH} до {MAX_PASSWORD_LENGTH} символів.')
    return generate_password_hash(value, method=PASSWORD_METHOD)


def private_file(path, contents=b'', *, create=True):
    if create:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            fd = None
        if fd is not None:
            with os.fdopen(fd, 'wb') as out:
                out.write(contents)
                out.flush()
                os.fsync(out.fileno())
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError('Private state file must be owned by current UID with mode 0600')


def protected_access(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self.access_lock():
            return method(self, *args, **kwargs)
    return guarded


class AuthStore:
    def __init__(self, directory, *, clock=time.time, idle=900, lifetime=28800,
                 audit_rows=10000, audit_days=30, source_root=None, read_only=False):
        path = Path(directory).expanduser().absolute()
        source = Path(source_root or Path(__file__).resolve().parents[1]).resolve()
        if path.resolve().is_relative_to(source):
            raise RuntimeError('Authentication state must be outside the source tree')
        if not read_only:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RuntimeError('State directory must be owned by current UID with mode 0700, no symlinks')
        # Ancestors may be root-owned, but must not let another untrusted user replace state.
        for parent in path.parents:
            info = parent.stat()
            if info.st_mode & 0o022 and not (info.st_mode & stat.S_ISVTX):
                raise RuntimeError('State parent directory is writable by group/other')
        self.directory, self.db_path = path, path / 'auth.sqlite3'
        self.clock, self.idle, self.lifetime = clock, idle, lifetime
        self.audit_rows, self.audit_seconds = audit_rows, audit_days * 86400
        self.read_only = read_only
        self._local = threading.local()
        self._access_mutex = threading.RLock()
        private_file(path / 'auth.key', secrets.token_bytes(32) if not read_only else b'', create=not read_only)
        self.key = (path / 'auth.key').read_bytes()
        if len(self.key) != 32:
            raise RuntimeError('Invalid authentication key length')
        private_file(self.db_path, create=not read_only)
        if read_only:
            return
        with self.transaction() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
            if exists:
                meta = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='access_meta'").fetchone()
                if not meta or [r[0] for r in db.execute('SELECT version FROM access_meta')] != [1]:
                    raise RuntimeError('Run the explicit migrate-access command before starting HMI')
            else:
                initialize_roles(db)
                db.execute(USERS_SQL)
                db.execute('CREATE TABLE sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id), '
                           'csrf TEXT NOT NULL, created REAL NOT NULL, seen REAL NOT NULL)')
                db.execute('CREATE TABLE audit(id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL, '
                           'actor_id INTEGER, actor TEXT, role TEXT, action TEXT NOT NULL, '
                           'result TEXT NOT NULL, detail TEXT NOT NULL)')
                db.execute('CREATE INDEX audit_actor ON audit(actor_id,id)')
                db.execute('CREATE TABLE login_limits(key TEXT PRIMARY KEY, started REAL NOT NULL, attempts INTEGER NOT NULL)')
        self.dummy = generate_password_hash(secrets.token_urlsafe(32), method=PASSWORD_METHOD)

    @contextmanager
    def access_lock(self):
        """Serialize live authorization and supported access mutations across workers/CLI.

        Separate from SQLite transactions so DEMO intent audit remains durably committed
        before simulator side effects. Reentrant per store/thread, never used by read-only CLI.
        """
        if self.read_only:
            raise RuntimeError('Read-only authentication store')
        with self._access_mutex:
            if getattr(self._local, 'access_locked', False):
                yield
                return
            path = self.directory / 'access-control.lock'
            private_file(path)
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._local.access_locked = True
                yield
            finally:
                self._local.access_locked = False
                os.close(fd)

    @contextmanager
    def transaction(self):
        if self.read_only:
            raise RuntimeError('Read-only authentication store')
        existing = getattr(self._local, 'db', None)
        if existing is not None:
            yield existing
            return
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA max_page_count=16384')
            db.execute('BEGIN IMMEDIATE')
            self._local.db = db
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            self._local.db = None
            db.close()

    def csrf_diagnostics(self):
        """Up to ten valid denials from the latest 100; count invalid rows, never expose them."""
        flags = ('origin_matches', 'fetch_site_allowed', 'cookie_present', 'session_found',
                 'csrf_present', 'csrf_matches', 'request.is_secure')
        db = sqlite3.connect(self.db_path.as_uri() + '?mode=ro', uri=True)
        try:
            db.execute('PRAGMA query_only=ON')
            rows = db.execute("SELECT time,detail FROM audit WHERE action=? ORDER BY id DESC LIMIT 100",
                              ('csrf.denied',)).fetchall()
        finally:
            db.close()
        result = []
        skipped_invalid = 0
        for timestamp, detail in rows:
            try:
                data = json.loads(detail)
                if (not isinstance(data, dict) or type(timestamp) not in (int, float)
                        or not math.isfinite(timestamp)
                        or not isinstance(data.get('request_id'), str)
                        or not re.fullmatch(r'[0-9a-f]{32}', data['request_id'])
                        or any(type(data.get(flag)) is not bool for flag in flags)):
                    raise ValueError
            except (TypeError, ValueError, RecursionError):
                skipped_invalid += 1
                continue
            if len(result) < 10:
                record = dict(time=timestamp, request_id=data['request_id'],
                              **{flag: data[flag] for flag in flags})
                # Revalidate stored values too; legacy records may omit these fields.
                for field in ('public_origin', 'request_origin'):
                    if field in data:
                        record[field] = diagnostic_origin(data[field])
                result.append(record)
        return dict(records=result, skipped_invalid=skipped_invalid)

    def _audit(self, db, actor, action, result, detail=''):
        # Only callers' fixed action/result and validated account metadata; never request payloads.
        local = actor is None and action in ('user.created', 'user.changed', 'user.change_denied')
        cursor = db.execute('INSERT INTO audit(time,actor_id,actor,role,action,result,detail) VALUES(?,?,?,?,?,?,?)',
            (self.clock(), actor['id'] if actor else None, actor['username'] if actor else ('local_uid:' + str(os.getuid()) if local else None),
             actor['role'] if actor else ('local_admin' if local else None), action, result, detail))
        db.execute('DELETE FROM audit WHERE time < ?', (self.clock() - self.audit_seconds,))
        db.execute('DELETE FROM audit WHERE id NOT IN (SELECT id FROM audit ORDER BY id DESC LIMIT ?)', (self.audit_rows,))
        return cursor.lastrowid

    def audit(self, actor, action, result, detail=''):
        with self.transaction() as db:
            return self._audit(db, actor, action, result, detail)

    def finish_event(self, event_id, result):
        with self.transaction() as db:
            db.execute('UPDATE audit SET result=? WHERE id=?', (result, event_id))

    def logs(self, user=None, before=None):
        with self.transaction() as db:
            conditions, args = [], []
            if user is not None:
                conditions.append('actor_id=?')
                args.append(user['id'])
            if before is not None:
                conditions.append('id < ?')
                args.append(before)
            where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
            return [dict(row) for row in db.execute('SELECT * FROM audit' + where + ' ORDER BY id DESC LIMIT 100', args)]

    def users(self, *, include_deleted=False):
        with self.transaction() as db:
            return [dict(row) for row in db.execute('SELECT id,username,role,active,created FROM users' +
                ('' if include_deleted else ' WHERE deleted=0') + ' ORDER BY id')]

    def _role(self, db, name):
        row = db.execute('SELECT name,rank FROM roles WHERE name=?', (name,)).fetchone()
        if row is None:
            raise AccountError('Невідома роль.')
        return dict(row, permissions=[r[0] for r in db.execute(
            'SELECT permission FROM role_permissions WHERE role=? ORDER BY permission', (name,))])

    def roles(self):
        with self.transaction() as db:
            return [self._role(db, r[0]) for r in db.execute('SELECT name FROM roles ORDER BY rank DESC,name')]

    def _protect_superadmin(self, db, row, role, active, *, deleting=False):
        if row['role'] == 'superadmin' and row['active'] and (role != 'superadmin' or not active or deleting):
            if db.execute("SELECT count(*) FROM users WHERE role='superadmin' AND active=1 AND deleted=0").fetchone()[0] <= 1:
                raise AccessDenied('Заборонено видаляти, блокувати або понижувати останнього активного суперадміна.')

    def _principal(self, db, token):
        session = self.session(token)
        if not session or not session['user_id']:
            raise AccessDenied('Сесію завершено. Увійдіть повторно.')
        return dict(id=session['user_id'], username=session['username'], role=session['role'],
                    rank=session['rank'], permissions=session['permissions'])

    @protected_access
    def _manage(self, token, action, target, operation):
        # Permission checks, target re-read, change, revocation and audit share BEGIN IMMEDIATE.
        # A denial is committed before the exception reaches HTTP/local callers.
        error = None
        result = None
        with self.transaction() as db:
            actor = None
            try:
                actor = self._principal(db, token)
                result = operation(db, actor)
                self._audit(db, actor, action, 'OK', target)
            except AccountError as exc:
                self._audit(db, actor, action, 'DENIED', target)
                error = exc
        if error:
            raise error
        return result

    def _can_manage(self, actor, permission, target_role=None, target_id=None):
        if permission not in actor['permissions']:
            raise AccessDenied('Недостатньо прав для керування доступом.')
        if actor['role'] == 'superadmin':
            return
        if target_id == actor['id'] or (target_role and target_role['rank'] >= actor['rank']):
            raise AccessDenied('Можна керувати лише користувачами й ролями нижчого рангу; власний доступ змінювати заборонено.')

    def _assignable(self, actor, role):
        self._can_manage(actor, 'users.manage', role)
        if actor['role'] != 'superadmin' and not set(role['permissions']) <= set(actor['permissions']):
            raise AccessDenied('Не можна призначити роль із дозволами, яких ви не маєте.')

    def manage_user(self, token, action, data, target_id=None):
        allowed = {'create', 'role', 'block', 'unblock', 'password', 'delete'}
        label = 'user.' + (action if action in allowed else 'invalid')
        safe_name = data.get('username')
        safe_name = safe_name if isinstance(safe_name, str) and re.fullmatch(r'[a-zA-Z0-9_.-]{3,64}', safe_name) else 'INVALID_TARGET'
        target = 'user_id:' + str(target_id) if type(target_id) is int else 'username:' + safe_name
        def operation(db, actor):
            self._can_manage(actor, 'users.manage')
            if action not in allowed:
                raise AccountError('Невідома операція.')
            if action == 'create':
                if set(data) != {'username', 'password', 'role'}:
                    raise AccountError('Очікуються логін, пароль і роль.')
                name = username(data['username'])
                role = self._role(db, data['role']) if isinstance(data['role'], str) else None
                if role is None:
                    raise AccountError('Невідома роль.')
                self._assignable(actor, role)
                hashed = password_hash(data['password'])
                if db.execute('SELECT 1 FROM users WHERE username=?', (name,)).fetchone():
                    raise AccountError('Логін уже використовується, зокрема в історії видалених акаунтів.')
                db.execute('INSERT INTO users(username,password_hash,role,active,created) VALUES(?,?,?,1,?)',
                           (name, hashed, role['name'], self.clock()))
                return
            fields = {'role': {'role'}, 'password': {'password'}}.get(action, set())
            if set(data) != fields:
                raise AccountError('Некоректні поля операції.')
            row = db.execute('SELECT * FROM users WHERE id=? AND deleted=0', (target_id,)).fetchone()
            if row is None:
                raise AccountError('Користувача не знайдено.')
            self._can_manage(actor, 'users.manage', self._role(db, row['role']), row['id'])
            new_role = row['role']
            if action == 'role':
                if not isinstance(data['role'], str):
                    raise AccountError('Невідома роль.')
                new = self._role(db, data['role'])
                self._assignable(actor, new)
                new_role = new['name']
            active = 0 if action in ('block', 'delete') else (1 if action == 'unblock' else row['active'])
            self._protect_superadmin(db, row, new_role, active, deleting=action == 'delete')
            hashed = password_hash(data['password']) if action == 'password' else row['password_hash']
            db.execute('UPDATE users SET role=?,active=?,password_hash=?,deleted=? WHERE id=?',
                       (new_role, active, self.dummy if action == 'delete' else hashed,
                        int(action == 'delete'), row['id']))
            db.execute('DELETE FROM sessions WHERE user_id=?', (row['id'],))
        return self._manage(token, label, target, operation)

    def manage_role(self, token, action, name, data):
        safe_name = name if isinstance(name, str) and re.fullmatch(r'[a-z][a-z0-9_-]{2,31}', name) else 'INVALID_TARGET'
        def operation(db, actor):
            self._can_manage(actor, 'roles.manage')
            if safe_name == 'INVALID_TARGET':
                raise AccountError('Назва ролі: 3–32 символи a–z, 0–9, _ або -, починається з літери.')
            existing = db.execute('SELECT name FROM roles WHERE name=?', (name,)).fetchone()
            if action not in ('create', 'edit', 'delete'):
                raise AccountError('Невідома операція.')
            if action == 'create' and existing:
                raise AccountError('Така роль уже існує.')
            if action != 'create' and not existing:
                raise AccountError('Роль не знайдено.')
            if existing:
                self._can_manage(actor, 'roles.manage', self._role(db, name))
            if action == 'delete':
                if data:
                    raise AccountError('Некоректні поля операції.')
                if db.execute('SELECT 1 FROM users WHERE role=?', (name,)).fetchone():
                    raise AccountError('Роль призначена користувачам, включно з історією видалених. Перепризначте чинних користувачів; історичні посилання потрібно зберегти.')
                if name == 'superadmin':
                    raise AccessDenied('Системну роль superadmin видаляти заборонено.')
                db.execute('DELETE FROM roles WHERE name=?', (name,))
                return
            if set(data) != {'rank', 'permissions'}:
                raise AccountError('Очікуються ранг і дозволи.')
            rank, permissions = data['rank'], data['permissions']
            if type(rank) is not int or not 1 <= rank <= 1000:
                raise AccountError('Ранг має бути цілим числом від 1 до 1000.')
            if (not isinstance(permissions, list) or any(not isinstance(p, str) or p not in PERMISSIONS for p in permissions)
                    or len(set(permissions)) != len(permissions)):
                raise AccountError('Невідомі або повторні дозволи.')
            if name == 'superadmin':
                if not {'users.manage', 'roles.manage'} <= set(permissions):
                    raise AccessDenied('Суперадмін має зберігати дозволи керування користувачами й ролями.')
                highest = db.execute("SELECT max(rank) FROM roles WHERE name!='superadmin'").fetchone()[0] or 0
                if rank <= highest:
                    raise AccessDenied('Ранг superadmin має залишатися вищим за всі інші ролі.')
            elif rank >= self._role(db, 'superadmin')['rank']:
                raise AccessDenied('Ранг має бути нижчим за роль superadmin.')
            if actor['role'] != 'superadmin':
                if rank >= actor['rank'] or not set(permissions) <= set(actor['permissions']):
                    raise AccessDenied('Ранг має бути нижчим за ваш; можна надавати лише власні дозволи.')
            if action == 'create':
                db.execute('INSERT INTO roles VALUES(?,?)', (name, rank))
            else:
                db.execute('UPDATE roles SET rank=? WHERE name=?', (rank, name))
                db.execute('DELETE FROM role_permissions WHERE role=?', (name,))
            db.executemany('INSERT INTO role_permissions VALUES(?,?)', [(name, p) for p in permissions])
            db.execute('DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE role=?)', (name,))
        label = 'role.' + (action if action in ('create', 'edit', 'delete') else 'invalid')
        return self._manage(token, label, 'role:' + safe_name, operation)

    @protected_access
    def add_user(self, name, password, role, *, first=False):
        name = username(name)
        hashed = password_hash(password)
        with self.transaction() as db:
            if not db.execute('SELECT 1 FROM roles WHERE name=?', (role,)).fetchone():
                raise AccountError('Невідома роль.')
            if first and db.execute("SELECT 1 FROM users WHERE active=1 AND role='admin'").fetchone():
                raise AccountError('Активний admin уже існує.')
            if first and role != 'admin':
                raise AccountError('Перший обліковий запис має бути admin.')
            try:
                db.execute('INSERT INTO users(username,password_hash,role,active,created) VALUES(?,?,?,1,?)',
                           (name, hashed, role, self.clock()))
            except sqlite3.IntegrityError:
                raise AccountError('Такий логін уже використовується.') from None
            self._audit(db, None, 'user.created', 'OK', name + ':' + role)

    @protected_access
    def change_user(self, name, *, role=None, active=None, password=None, revoke=False):
        """Trusted local OS-user CLI; HTTP must use manage_user instead."""
        name = username(name)
        hashed = password_hash(password) if password is not None else None
        denied = None
        with self.transaction() as db:
            row = db.execute('SELECT * FROM users WHERE username=? AND deleted=0', (name,)).fetchone()
            if row is None:
                raise AccountError('Обліковий запис не знайдено.')
            if role is not None:
                self._role(db, role)
            new_role = role if role is not None else row['role']
            new_active = int(active) if active is not None else row['active']
            try:
                self._protect_superadmin(db, row, new_role, new_active)
                # Until the explicit first superadmin grant, keep the legacy last-admin guard.
                if (row['role'] == 'admin' and row['active'] and
                        (new_role not in ('admin', 'superadmin') or not new_active) and
                        not db.execute("SELECT 1 FROM users WHERE role='superadmin' AND active=1 AND deleted=0").fetchone() and
                        db.execute("SELECT count(*) FROM users WHERE role='admin' AND active=1 AND deleted=0").fetchone()[0] <= 1):
                    raise AccessDenied('Заборонено деактивувати або позбавляти ролі останнього активного admin.')
            except AccessDenied as exc:
                self._audit(db, None, 'user.change_denied', 'DENIED', name)
                denied = exc
            if denied is None:
                db.execute('UPDATE users SET role=?,active=?,password_hash=? WHERE id=?',
                           (new_role, new_active, hashed or row['password_hash'], row['id']))
                db.execute('DELETE FROM sessions WHERE user_id=?', (row['id'],))
                changes = ['sessions_revoked']
                if role is not None:
                    changes.append('role:' + row['role'] + '->' + new_role)
                if active is not None:
                    changes.append('active:' + str(new_active))
                if hashed:
                    changes.append('password_changed')
                self._audit(db, None, 'user.changed', 'OK', name + ':' + ','.join(changes))
        if denied is not None:
            raise denied

    def token_hash(self, token):
        return hmac.new(self.key, token.encode(), hashlib.sha256).hexdigest()

    def _new_session(self, db, user_id=None):
        now = self.clock()
        db.execute('DELETE FROM sessions WHERE created<=? OR seen<=?', (now-self.lifetime, now-self.idle))
        db.execute('DELETE FROM sessions WHERE user_id IS NULL AND created<=?', (now-300,))
        # Bound unauthenticated login-form sessions separately from authenticated sessions.
        db.execute('DELETE FROM sessions WHERE user_id IS NULL AND token_hash NOT IN '
                   '(SELECT token_hash FROM sessions WHERE user_id IS NULL ORDER BY created DESC LIMIT 199)')
        if db.execute('SELECT count(*) FROM sessions WHERE user_id IS NOT NULL').fetchone()[0] >= 1000:
            raise RuntimeError('Session capacity reached')
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (self.token_hash(token), user_id, csrf, now, now))
        return token

    def anonymous_session(self):
        with self.transaction() as db:
            return self._new_session(db)

    def session(self, token, *, touch=False):
        if not isinstance(token, str) or not 20 <= len(token) <= 128:
            return None
        with self.transaction() as db:
            row = db.execute('SELECT s.*,u.username,u.role,u.active,u.deleted FROM sessions s '
                             'LEFT JOIN users u ON u.id=s.user_id WHERE token_hash=?', (self.token_hash(token),)).fetchone()
            if row is None:
                return None
            now = self.clock()
            limit = self.lifetime if row['user_id'] else 300
            invalid = (now < row['created'] or now < row['seen'] or now-row['created'] >= limit
                       or now-row['seen'] >= self.idle or (row['user_id'] and (not row['active'] or row['deleted'])))
            if invalid:
                db.execute('DELETE FROM sessions WHERE token_hash=?', (row['token_hash'],))
                if row['user_id']:
                    self._audit(db, dict(id=row['user_id'], username=row['username'], role=row['role']), 'session.expired', 'OK')
                return None
            if touch:
                db.execute('UPDATE sessions SET seen=? WHERE token_hash=?', (now, row['token_hash']))
            result = dict(row)
            if row['user_id']:
                role = self._role(db, row['role'])
                result.update(rank=role['rank'], permissions=role['permissions'])
            return result

    def touch(self, token):
        return self.session(token, touch=True)

    @protected_access
    def logout(self, token, actor):
        with self.transaction() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (self.token_hash(token),))
            self._audit(db, actor, 'logout', 'OK')

    @protected_access
    def login(self, name, password, ip, old_token):
        # HMAC identifiers are never exposed in audit; untrusted login text is not logged.
        candidate = name.lower() if isinstance(name, str) else ''
        keys = [('account:', candidate, 5), ('ip:', ip or '', 20)]
        now = self.clock()
        with self.transaction() as db:
            db.execute('DELETE FROM login_limits WHERE started<=? OR started>?', (now-900, now))
            db.execute('DELETE FROM login_limits WHERE key NOT IN (SELECT key FROM login_limits ORDER BY started DESC LIMIT 4998)')
            digests = []
            blocked = False
            for kind, value, limit in keys:
                digest = hmac.new(self.key, (kind+value).encode(), hashlib.sha256).hexdigest()
                digests.append(digest)
                row = db.execute('SELECT attempts FROM login_limits WHERE key=?', (digest,)).fetchone()
                if row and row['attempts'] >= limit:
                    blocked = True
            if blocked:
                self._audit(db, None, 'login.failed', 'RATE_LIMITED')
                return None, 429
            for digest in digests:
                db.execute('INSERT INTO login_limits VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET attempts=attempts+1', (digest, now))
            row = db.execute('SELECT * FROM users WHERE username=?', (candidate,)).fetchone()
        valid_input = isinstance(password, str) and len(password) <= MAX_PASSWORD_LENGTH
        valid = check_password_hash(row['password_hash'] if row else self.dummy, password if valid_input else '')
        with self.transaction() as db:
            # Re-read after hashing: CLI may have disabled/changed this account meanwhile.
            current = db.execute('SELECT * FROM users WHERE username=?', (candidate,)).fetchone()
            if not valid_input or not valid or not current or not current['active'] or current['deleted'] or current['password_hash'] != row['password_hash']:
                self._audit(db, None, 'login.failed', 'DENIED')
                return None, 401
            db.execute('DELETE FROM sessions WHERE token_hash=?', (self.token_hash(old_token),))
            token = self._new_session(db, current['id'])
            actor = dict(id=current['id'], username=current['username'], role=current['role'])
            self._audit(db, actor, 'login', 'OK')
            db.execute('DELETE FROM login_limits WHERE key=?', (digests[0],))
            return token, 200
