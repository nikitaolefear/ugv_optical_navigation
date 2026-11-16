from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from web_hmi.app import create_app, COOKIE, LOGIN_ERROR
from web_hmi.auth import AccountError, AuthStore
from web_hmi.test_support import TestEnvironment, PASSWORD, CONFIG


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.now = 1700000000.0
        self.env = TestEnvironment(clock=lambda: self.now)
        self.addCleanup(self.env.close)
        self.store = self.env.store
        self.store.add_user('operator1', PASSWORD, 'operator')
        self.store.add_user('operator2', PASSWORD, 'operator')
        self.app = self.env.app()
        self.client = self.app.test_client()

    def prelogin(self, client=None):
        client = client or self.client
        response = client.get('/login')
        self.assertEqual(response.status_code, 200)
        return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)

    def login(self, name='operator1', client=None, password=PASSWORD):
        client = client or self.client
        csrf = self.prelogin(client)
        response = client.post('/login', data=dict(username=name, password=password, csrf_token=csrf))
        self.assertEqual(response.status_code, 303)
        return re.search(r'<meta name="csrf-token" content="([^"]+)"', client.get('/').text).group(1)

    def command(self, csrf, client=None, **extra):
        return (client or self.client).post('/api/command', json=dict(action='reset', request_id='r1', revision=0, **extra),
                                          headers={'X-CSRF-Token':csrf})

    def test_every_api_and_future_api_requires_json_401(self):
        @self.app.get('/api/future')
        def future():
            return {'never':'anonymous'}
        for rule in self.app.url_map.iter_rules():
            if rule.rule.startswith('/api/'):
                for method in rule.methods - {'OPTIONS', 'HEAD'}:
                    with self.subTest(path=rule.rule, method=method):
                        response = self.client.open(rule.rule, method=method)
                        self.assertEqual(response.status_code, 401)
                        self.assertTrue(response.is_json)
        for path in ('/', '/audit', '/static/app.js', '/static/account.js'):
            self.assertEqual(self.client.get(path).status_code, 303)

    def test_no_registration_and_no_default_accounts(self):
        self.assertEqual(self.client.post('/register').status_code, 303)
        names = [row['username'] for row in self.store.users()]
        self.assertEqual(names, ['testadmin', 'operator1', 'operator2'])

    def test_admin_operator_roles_and_user_hash_exclusion(self):
        self.login()
        for path in ('/api/admin/audit', '/api/admin/users'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 403)
            self.assertTrue(response.is_json)
        self.assertEqual(self.client.get('/api/audit').status_code, 200)
        admin = self.app.test_client()
        self.login('testadmin', admin)
        self.assertEqual(admin.get('/api/admin/audit').status_code, 200)
        response = admin.get('/api/admin/users')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('password', response.text)
        self.assertNotIn('csrf', response.text)

    def test_operator_only_sees_own_actions_and_logins(self):
        csrf = self.login()
        self.command(csrf)
        other = self.app.test_client()
        other_csrf = self.login('operator2', other)
        self.command(other_csrf, other)
        own = self.client.get('/api/audit?user_id=3&scope=all').json['entries']
        self.assertTrue(own)
        self.assertEqual({row['actor'] for row in own}, {'operator1'})
        self.assertEqual(self.client.get('/api/status').json['events'], [])
        self.assertEqual(self.command(csrf).json['events'], [])
        self.assertTrue(any(row['action'] == 'login' for row in own))
        self.assertTrue(any(row['action'] == 'demo.reset' and row['result'] == 'OK' for row in own))

    def test_admin_sees_failures_role_changes_and_system_events(self):
        csrf = self.prelogin()
        self.client.post('/login', data=dict(username='no_such_user', password='wrong', csrf_token=csrf))
        self.store.change_user('operator2', role='admin')
        self.store.audit(None, 'hmi.started', 'OK')
        admin = self.app.test_client()
        self.login('testadmin', admin)
        entries = admin.get('/api/admin/audit').json['entries']
        for action in ('login.failed', 'user.changed', 'hmi.started'):
            self.assertTrue(any(row['action'] == action for row in entries))
        change = next(row for row in entries if row['action'] == 'user.changed')
        self.assertEqual(change['role'], 'local_admin')
        self.assertIn('local_uid:', change['actor'])

    def test_login_csrf_missing_wrong_and_cross_origin(self):
        csrf = self.prelogin()
        for value, headers in [('', {}), ('wrong', {}), (csrf, {'Origin':'https://evil.example'}),
                               (csrf, {'Sec-Fetch-Site':'cross-site'}), ('не-ascii', {})]:
            response = self.client.post('/login', data=dict(username='operator1', password=PASSWORD, csrf_token=value), headers=headers)
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_every_state_change_including_logout_requires_csrf(self):
        self.login()
        for path in ('/api/command', '/api/session/activity', '/logout'):
            for token in ('', 'wrong', 'не-ascii'):
                response = self.client.post(path, json={}, headers={'X-CSRF-Token':token})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get('/logout').status_code, 405)
        self.assertEqual(self.client.get('/api/status').status_code, 200)

    def test_logout_revokes_cookie_and_server_session(self):
        csrf = self.login()
        raw = self.client.get_cookie(COOKIE).value
        response = self.client.post('/logout', headers={'X-CSRF-Token':csrf})
        self.assertEqual(response.status_code, 303)
        self.assertIsNone(self.client.get_cookie(COOKIE))
        self.assertIsNone(self.store.session(raw))
        self.client.set_cookie(COOKIE, raw)
        self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_session_rotation_prevents_fixation(self):
        csrf = self.prelogin()
        old = self.client.get_cookie(COOKIE).value
        self.client.post('/login', data=dict(username='operator1', password=PASSWORD, csrf_token=csrf))
        new = self.client.get_cookie(COOKIE).value
        self.assertNotEqual(old, new)
        self.assertIsNone(self.store.session(old))
        self.assertNotEqual(csrf, re.search(r'<meta name="csrf-token" content="([^"]+)"', self.client.get('/').text).group(1))
        with self.store.transaction() as db:
            values = [row[0] for row in db.execute('SELECT token_hash FROM sessions')]
        self.assertNotIn(new, values)

    def test_polling_does_not_extend_idle_timeout(self):
        self.login()
        for _ in range(3):
            self.now += 250
            self.assertEqual(self.client.get('/api/status').status_code, 200)
            self.assertEqual(self.client.get('/api/telemetry').status_code, 200)
            self.assertEqual(self.client.get('/api/session').status_code, 200)
        self.now += 150
        self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_activity_extends_idle_but_not_absolute_lifetime(self):
        csrf = self.login()
        for _ in range(35):
            self.now += 800
            self.assertEqual(self.client.post('/api/session/activity', headers={'X-CSRF-Token':csrf}).status_code, 200)
        self.now += 800
        self.assertEqual(self.client.post('/api/session/activity', headers={'X-CSRF-Token':csrf}).status_code, 401)

    def test_clock_rollback_expires_session(self):
        self.login()
        self.now -= 1
        self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_revocation_survives_new_app_and_prevents_role_reuse(self):
        self.login()
        raw = self.client.get_cookie(COOKIE).value
        self.store.change_user('operator1', revoke=True)
        other = self.env.app().test_client()
        other.set_cookie(COOKIE, raw)
        self.assertEqual(other.get('/api/status').status_code, 401)

    def test_role_password_and_deactivation_revoke_sessions(self):
        for change in ({'role':'admin'}, {'password':'replacement-secret-long-123'}, {'active':False}):
            with self.subTest(change=list(change)):
                client = self.app.test_client()
                self.login('operator2', client)
                self.store.change_user('operator2', **change)
                self.assertEqual(client.get('/api/status').status_code, 401)
                self.store.change_user('operator2', role='operator', active=True, password=PASSWORD)

    def test_last_admin_cannot_be_deactivated_or_demoted(self):
        for changes in ({'active':False}, {'role':'operator'}):
            with self.assertRaises(AccountError):
                self.store.change_user('testadmin', **changes)
        self.store.change_user('operator1', role='admin')
        self.store.change_user('testadmin', active=False)
        with self.assertRaises(AccountError):
            self.store.change_user('operator1', active=False)
        with self.assertRaises(AccountError):
            self.store.add_user('newadmin', PASSWORD, 'admin', first=True)

    def test_concurrent_admin_deactivation_preserves_one_admin(self):
        self.store.change_user('operator1', role='admin')
        def disable(name):
            try:
                self.store.change_user(name, active=False)
                return 'OK'
            except AccountError:
                return 'DENIED'
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(disable, ['testadmin', 'operator1']))
        self.assertCountEqual(outcomes, ['OK', 'DENIED'])
        self.assertEqual(sum(row['active'] and row['role']=='admin' for row in self.store.users()), 1)

    def test_unknown_and_wrong_password_have_identical_generic_errors(self):
        csrf = self.prelogin()
        responses = []
        for name in ('operator1', 'does_not_exist'):
            responses.append(self.client.post('/login', data=dict(username=name, password='wrong', csrf_token=csrf)))
        self.assertEqual(responses[0].status_code, 401)
        # Per-response CSP nonces vary independently of the account/password result.
        normalized = [re.sub(rb'nonce="[^"]+"', b'nonce="RESPONSE_NONCE"', r.data) for r in responses]
        self.assertEqual(normalized[0], normalized[1])
        self.assertIn(LOGIN_ERROR, responses[0].text)

    def test_login_limit_is_persistent_and_recovers_after_window(self):
        csrf = self.prelogin()
        for _ in range(5):
            self.assertEqual(self.client.post('/login', data=dict(username='operator1', password='wrong', csrf_token=csrf)).status_code, 401)
        self.assertEqual(self.client.post('/login', data=dict(username='operator1', password=PASSWORD, csrf_token=csrf)).status_code, 429)
        other = create_app(config=CONFIG, auth_store=AuthStore(self.env.tmp.name, clock=lambda:self.now)).test_client()
        token = self.prelogin(other)
        self.assertEqual(other.post('/login', data=dict(username='operator1', password=PASSWORD, csrf_token=token)).status_code, 429)
        self.now += 901
        self.login()

    def test_ip_limit_applies_across_different_logins(self):
        csrf = self.prelogin()
        for index in range(20):
            self.assertEqual(self.client.post('/login', data=dict(username='unknown'+str(index), password='wrong', csrf_token=csrf)).status_code, 401)
        self.assertEqual(self.client.post('/login', data=dict(username='operator1', password=PASSWORD, csrf_token=csrf)).status_code, 429)

    def test_secure_cookie_and_headers(self):
        app = create_app(auth_store=self.store, config={'PUBLIC_ORIGIN':'https://192.168.110.59', 'COOKIE_SECURE':True})
        response = app.test_client().get('/login', base_url='https://192.168.110.59')
        value = response.headers['Set-Cookie']
        for flag in ('Secure', 'HttpOnly', 'SameSite=Strict', 'Path=/'):
            self.assertIn(flag, value)
        self.assertNotIn('Domain=', value)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn('form-action', response.headers['Content-Security-Policy'])
        nonce = re.search(r'<script nonce="([^"]+)"', response.text)[1]
        self.assertIn("'nonce-" + nonce + "'", response.headers['Content-Security-Policy'])
        self.assertNotIn('unsafe-inline', response.headers['Content-Security-Policy'])
        self.assertIn('max-age=', response.headers['Strict-Transport-Security'])

    def test_insecure_network_origin_and_bad_host_rejected(self):
        for config in ({'PUBLIC_ORIGIN':'http://192.168.110.59','COOKIE_SECURE':False},
                       {'PUBLIC_ORIGIN':'https://192.168.110.59','COOKIE_SECURE':False}):
            with self.assertRaises(RuntimeError):
                create_app(config=config, auth_store=self.store)
        self.assertEqual(self.client.get('/login', headers={'Host':'evil.example'}).status_code, 400)

    def test_passwords_are_scrypt_hashes_and_state_is_private(self):
        self.assertEqual(stat.S_IMODE(Path(self.env.tmp.name).stat().st_mode), 0o700)
        for name in ('auth.sqlite3','auth.key'):
            self.assertEqual(stat.S_IMODE((Path(self.env.tmp.name)/name).stat().st_mode), 0o600)
        with self.store.transaction() as db:
            hashes = [row[0] for row in db.execute('SELECT password_hash FROM users')]
        self.assertTrue(all(value.startswith('scrypt:32768:8:1$') for value in hashes))
        self.assertTrue(all(PASSWORD not in value for value in hashes))
        with self.assertRaises(AccountError):
            self.store.add_user('weakuser', 'short', 'operator')

    def test_secret_cookie_csrf_and_payload_never_enter_audit(self):
        csrf = self.login()
        cookie = self.client.get_cookie(COOKIE).value
        marker = 'sensitive-payload-never-log'
        self.client.post('/api/command', json={'action':marker,'password':PASSWORD,'token':cookie}, headers={'X-CSRF-Token':csrf})
        self.client.post('/api/command', json={'token':marker}, headers={'X-CSRF-Token':marker})
        self.store.change_user('operator1', password='replacement-secret-long-123')
        with self.store.transaction() as db:
            content = json.dumps([dict(row) for row in db.execute('SELECT * FROM audit')])
        for secret in (PASSWORD, csrf, cookie, marker, 'replacement-secret-long-123', self.store.key.hex(), 'scrypt:'):
            self.assertNotIn(secret, content)

    def test_audit_is_bounded_by_count_and_age(self):
        self.store.audit_rows = 5
        self.store.audit_seconds = 86400
        for _ in range(10):
            self.store.audit(None, 'hmi.test', 'OK')
        self.assertEqual(len(self.store.logs()), 5)
        self.now += 31*86400
        self.store.audit(None, 'hmi.test', 'OK')
        self.assertEqual(len(self.store.logs()), 1)

    def test_audit_pagination_is_bounded_and_rejects_injection(self):
        self.login()
        for _ in range(105):
            self.store.audit(None, 'hmi.test', 'OK')
        admin = self.app.test_client()
        self.login('testadmin', admin)
        first = admin.get('/api/admin/audit').json
        self.assertEqual(len(first['entries']), 100)
        second = admin.get('/api/admin/audit?before='+str(first['next_before'])).json
        self.assertFalse(set(row['id'] for row in first['entries']) & set(row['id'] for row in second['entries']))
        for value in ('-1', '1%20OR%201=1', '9'*30):
            self.assertEqual(admin.get('/api/admin/audit?before='+value).status_code, 400)

    def test_storage_failure_prevents_demo_command(self):
        csrf = self.login()
        with patch.object(self.store, 'audit', side_effect=sqlite3.OperationalError('fake failure')), self.assertLogs(self.app.logger, level='ERROR'):
            response = self.command(csrf)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.client.get('/api/status').json['revision'], 0)

    def test_state_cannot_live_in_repository_or_have_wide_permissions(self):
        with self.assertRaises(RuntimeError):
            AuthStore(Path(__file__).parent/'private-auth')
        with tempfile.TemporaryDirectory() as directory:
            Path(directory).chmod(0o755)
            with self.assertRaises(RuntimeError):
                AuthStore(directory)


class LifecycleAndCLITest(unittest.TestCase):
    def setUp(self):
        self.env = TestEnvironment()
        self.addCleanup(self.env.close)

    def test_import_and_wsgi_factory_do_not_start_reader(self):
        from web_hmi import wsgi
        with patch('web_hmi.pixhawk_telemetry.PixhawkTelemetry.start', side_effect=AssertionError('hardware')):
            importlib.reload(wsgi)
            with patch.dict(os.environ, {'UGV_HMI_STATE_DIR':self.env.tmp.name}):
                self.assertIsNotNone(wsgi.create_wsgi_app())

    def test_factory_rejects_root_and_proxy_does_not_trust_forwarded_host(self):
        with patch('web_hmi.app.os.geteuid', return_value=0):
            with self.assertRaises(RuntimeError):
                self.env.app()
        from web_hmi.wsgi import create_wsgi_app
        with patch.dict(os.environ, {'UGV_HMI_TRUST_PROXY':'1', 'UGV_HMI_STATE_DIR':self.env.tmp.name,
                                      'UGV_HMI_PUBLIC_ORIGIN':'https://192.168.110.59'}):
            app = create_wsgi_app()
        response = app.test_client().get('/login', base_url='https://192.168.110.59',
                                         headers={'X-Forwarded-Host':'evil.example', 'X-Forwarded-For':'192.168.110.2'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Secure', response.headers['Set-Cookie'])

    def test_worker_hook_starts_once_and_guard_rejects_second_reader(self):
        from web_hmi import runtime, gunicorn_conf
        app = self.env.app()
        other = self.env.app()
        reader = app.extensions['pixhawk_telemetry']
        with patch.object(reader, 'start') as start, patch.object(reader, 'close'):
            try:
                with patch.dict(os.environ, {'UGV_HMI_TELEMETRY':'1'}):
                    worker = Mock(wsgi=app)
                    gunicorn_conf.post_worker_init(worker)
                    runtime.start_telemetry(app)
                start.assert_called_once()
                with self.assertRaises(BlockingIOError):
                    runtime.start_telemetry(other)
                from web_hmi.pixhawk_owner_client import open_with_owner_helper
                self.assertIs(reader.opener, open_with_owner_helper)
            finally:
                runtime.stop_telemetry(app)

    def test_gunicorn_rejects_multiple_workers_preload_and_reload(self):
        from web_hmi import gunicorn_conf
        for values in ((2,False,False),(1,True,False),(1,False,True)):
            server = Mock()
            server.cfg.workers, server.cfg.preload_app, server.cfg.reload = values
            with self.assertRaises(RuntimeError):
                gunicorn_conf.on_starting(server)

    def test_cli_password_prompt_hidden_and_rejects_non_tty(self):
        from web_hmi import admin_cli
        with patch.object(admin_cli.sys.stdin, 'isatty', return_value=False), \
             patch.object(admin_cli.getpass, 'getpass') as prompt:
            with self.assertRaises(AccountError):
                admin_cli.read_password()
            prompt.assert_not_called()
        with patch.object(admin_cli.sys.stdin, 'isatty', return_value=True), \
             patch.object(admin_cli.getpass, 'getpass', side_effect=[PASSWORD, PASSWORD]) as prompt:
            self.assertEqual(admin_cli.read_password(), PASSWORD)
            self.assertEqual(prompt.call_count, 2)

    def test_cli_refuses_getpass_echo_fallback(self):
        from web_hmi import admin_cli
        with patch.object(admin_cli.sys.stdin, 'isatty', return_value=True), \
             patch.object(admin_cli.getpass, 'getpass', side_effect=admin_cli.getpass.GetPassWarning('echo unavailable')):
            with self.assertRaises(AccountError):
                admin_cli.read_password()

    def test_cli_create_change_and_revoke(self):
        from web_hmi import admin_cli
        base = ['--state-dir',self.env.tmp.name]
        with patch.object(admin_cli, 'read_password', return_value=PASSWORD):
            self.assertEqual(admin_cli.main(base+['create','localuser','operator']), 0)
        self.assertEqual(admin_cli.main(base+['role','localuser','admin']), 0)
        self.assertEqual(admin_cli.main(base+['revoke','localuser']), 0)
        self.assertEqual(admin_cli.main(base+['disable','localuser']), 0)
        self.assertFalse(next(row for row in self.env.store.users() if row['username']=='localuser')['active'])
