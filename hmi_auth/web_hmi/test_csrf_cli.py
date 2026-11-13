"""CLI diagnostics use only disposable private state, never deployed state."""
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from . import admin_cli
from .auth import AuthStore
from .test_support import TestEnvironment

FLAGS = ('origin_matches', 'fetch_site_allowed', 'cookie_present', 'session_found',
         'csrf_present', 'csrf_matches', 'request.is_secure')


class CSRFCLITest(unittest.TestCase):
    def setUp(self):
        self.env = TestEnvironment()
        self.addCleanup(self.env.close)
        self.path = Path(self.env.tmp.name)

    def run_cli(self, path=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = admin_cli.main(['--state-dir', str(path or self.path), 'csrf-diagnostics'])
        return status, out.getvalue(), err.getvalue()

    def event(self, index, **extra):
        return dict(request_id=f'{index:032x}', **{flag: index % 2 == 0 for flag in FLAGS}, **extra)

    def snapshot(self):
        return {p.name: (p.stat().st_mode, p.stat().st_size, p.stat().st_mtime_ns,
                         hashlib.sha256(p.read_bytes()).hexdigest()) for p in self.path.iterdir()}

    def test_last_ten_filtered_before_limit_and_no_secrets_or_writes(self):
        store = self.env.store
        store.anonymous_session()
        marker = 'NEVER-EXPOSE-private-form-cookie-password'
        for index in range(15):
            store.audit(None, 'csrf.denied', 'DENIED', json.dumps(self.event(index,
                password=marker, cookie=marker, csrf=marker, form=marker)))
            for _ in range(3):
                store.audit(None, 'login.failed', 'DENIED', marker)
        # Full table snapshots also cover sessions, users, login_limits and audit.
        with store.transaction() as db:
            before_rows = {name: [tuple(r) for r in db.execute('SELECT * FROM '+name)]
                           for name in ('users', 'sessions', 'login_limits', 'audit')}
        before = self.snapshot()
        with patch.object(AuthStore, 'transaction', side_effect=AssertionError('write path')):
            status, out, err = self.run_cli()
        self.assertEqual((status, err), (0, ''))
        diagnostics = json.loads(out)
        self.assertEqual(set(diagnostics), {'records', 'skipped_invalid'})
        self.assertEqual(diagnostics['skipped_invalid'], 0)
        rows = diagnostics['records']
        self.assertEqual([r['request_id'] for r in rows], [f'{i:032x}' for i in range(14, 4, -1)])
        for row in rows:
            self.assertEqual(set(row), {'time', 'request_id', *FLAGS})
            self.assertTrue(all(type(row[f]) is bool for f in FLAGS))
        for secret in (marker, self.env.store.key.hex(), 'testadmin', 'login.failed'):
            self.assertNotIn(secret, out + err)
        self.assertEqual(self.snapshot(), before)
        with store.transaction() as db:
            after_rows = {name: [tuple(r) for r in db.execute('SELECT * FROM '+name)] for name in before_rows}
        self.assertEqual(after_rows, before_rows)

    def test_no_denials_empty_list(self):
        self.env.store.audit(None, 'other.event', 'OK', 'SECRET')
        status, out, err = self.run_cli()
        self.assertEqual((status, err), (0, ''))
        diagnostics = json.loads(out)
        self.assertEqual(diagnostics['records'], [])
        self.assertEqual(diagnostics['skipped_invalid'], 0)
        self.assertIn('немає', diagnostics['message'])

    def test_mixed_old_malformed_and_new_events_are_skipped_without_secrets(self):
        invalid = ['', 'SECRET', '[]', json.dumps({'request_id': 'SECRET'}),
                   json.dumps({**self.event(1), 'csrf_matches': 'SECRET'}),
                   json.dumps({**self.event(1), 'request_id': 'SECRET'}),
                   json.dumps({**self.event(1), 'origin_matches': 1})]
        for index, detail in enumerate(invalid):
            self.env.store.audit(None, 'csrf.denied', 'DENIED', detail)
            self.env.store.audit(None, 'csrf.denied', 'DENIED', json.dumps(self.event(index)))
            self.env.store.audit(None, 'login.failed', 'DENIED', 'SECRET')
        before = self.snapshot()
        status, out, err = self.run_cli()
        self.assertEqual((status, err), (0, ''))
        diagnostics = json.loads(out)
        self.assertEqual(diagnostics['skipped_invalid'], len(invalid))
        self.assertEqual([r['request_id'] for r in diagnostics['records']],
                         [f'{i:032x}' for i in range(6, -1, -1)])
        self.assertNotIn('SECRET', out + err)
        self.assertEqual(self.snapshot(), before)

    def test_window_is_100_denials_and_invalid_count_covers_whole_window(self):
        # This valid row is outside the window and must never be returned.
        self.env.store.audit(None, 'csrf.denied', 'DENIED', json.dumps(self.event(999)))
        for index in range(100):
            detail = json.dumps(self.event(index)) if index >= 85 else 'SECRET'
            self.env.store.audit(None, 'csrf.denied', 'DENIED', detail)
            self.env.store.audit(None, 'other.event', 'OK', 'SECRET')
        status, out, err = self.run_cli()
        self.assertEqual((status, err), (0, ''))
        diagnostics = json.loads(out)
        self.assertEqual(diagnostics['skipped_invalid'], 85)
        self.assertEqual([r['request_id'] for r in diagnostics['records']],
                         [f'{i:032x}' for i in range(99, 89, -1)])
        self.assertNotIn('SECRET', out)

    def test_no_valid_records_within_window_explicit_message_and_no_writes(self):
        self.env.store.audit(None, 'csrf.denied', 'DENIED', json.dumps(self.event(999)))
        for _ in range(101):
            self.env.store.audit(None, 'csrf.denied', 'DENIED', 'SECRET')
        before = self.snapshot()
        status, out, err = self.run_cli()
        self.assertEqual((status, err), (0, ''))
        diagnostics = json.loads(out)
        self.assertEqual(set(diagnostics), {'records', 'skipped_invalid', 'message'})
        self.assertEqual(diagnostics['records'], [])
        self.assertEqual(diagnostics['skipped_invalid'], 100)
        self.assertIn('немає', diagnostics['message'])
        self.assertNotIn('SECRET', out)
        self.assertEqual(self.snapshot(), before)

    def test_invalid_timestamp_is_skipped(self):
        with self.env.store.transaction() as db:
            db.execute("INSERT INTO audit(time,action,result,detail) VALUES(?,?,?,?)",
                       ('SECRET', 'csrf.denied', 'DENIED', json.dumps(self.event(1))))
        status, out, err = self.run_cli()
        self.assertEqual((status, err), (0, ''))
        diagnostics = json.loads(out)
        self.assertEqual(diagnostics['records'], [])
        self.assertEqual(diagnostics['skipped_invalid'], 1)
        self.assertNotIn('SECRET', out)

    def test_missing_directory_or_files_not_created(self):
        missing = self.path/'missing'
        status, out, _ = self.run_cli(missing)
        self.assertEqual((status, out), (2, ''))
        self.assertFalse(missing.exists())
        with tempfile.TemporaryDirectory() as empty:
            status, out, _ = self.run_cli(empty)
            self.assertEqual((status, out), (2, ''))
            self.assertEqual(list(Path(empty).iterdir()), [])
        for filename in ('auth.key', 'auth.sqlite3'):
            with tempfile.TemporaryDirectory() as directory:
                other = AuthStore(directory)
                (Path(directory)/filename).unlink()
                before = {p.name: p.read_bytes() for p in Path(directory).iterdir()}
                self.assertEqual(self.run_cli(directory)[0], 2)
                self.assertEqual({p.name: p.read_bytes() for p in Path(directory).iterdir()}, before)

    def test_existing_privacy_checks_reject_wide_modes_symlinks_and_source(self):
        self.path.chmod(0o755)
        self.assertEqual(self.run_cli()[0], 2)
        self.path.chmod(0o700)
        for filename in ('auth.key', 'auth.sqlite3'):
            p = self.path/filename
            p.chmod(0o644)
            self.assertEqual(self.run_cli()[0], 2)
            p.chmod(0o600)
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory)/'linked-state'
            link.symlink_to(self.path, target_is_directory=True)
            self.assertEqual(self.run_cli(link)[0], 2)
        source = Path(admin_cli.__file__).parent/'must-not-create-state'
        self.assertEqual(self.run_cli(source)[0], 2)
        self.assertFalse(source.exists())
        for filename in ('auth.key', 'auth.sqlite3'):
            original = self.path/filename
            saved = self.path/(filename+'.saved')
            original.rename(saved)
            original.symlink_to(saved)
            try:
                self.assertEqual(self.run_cli()[0], 2)
            finally:
                original.unlink()
                saved.rename(original)

    def test_readonly_store_guards_mutation_and_sqlite_connection_mode(self):
        store = AuthStore(self.path, read_only=True)
        with self.assertRaises(RuntimeError):
            store.audit(None, 'unwanted.event', 'OK')
        import sqlite3
        real_connect = sqlite3.connect
        def connect(database, **kwargs):
            self.assertTrue(database.endswith('?mode=ro'))
            db = real_connect(database, **kwargs)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM sessions")
            return db
        with patch('web_hmi.auth.sqlite3.connect', side_effect=connect):
            self.assertEqual(store.csrf_diagnostics(), {'records': [], 'skipped_invalid': 0})

    def test_owner_and_unsafe_parent_checks_preserved(self):
        with patch('web_hmi.auth.os.getuid', return_value=-1):
            self.assertEqual(self.run_cli()[0], 2)
        with tempfile.TemporaryDirectory() as parent:
            state = Path(parent)/'state'
            AuthStore(state)
            Path(parent).chmod(0o777)
            try:
                self.assertEqual(self.run_cli(state)[0], 2)
            finally:
                Path(parent).chmod(0o700)
