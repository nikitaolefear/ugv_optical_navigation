"""Isolated HTTPS/proxy browser sequences; never uses deployed state or hardware."""
from concurrent.futures import ThreadPoolExecutor
import json
import re
import unittest
from werkzeug.middleware.proxy_fix import ProxyFix
from .app import create_app, COOKIE
from .test_support import TestEnvironment, PASSWORD

ORIGIN = 'https://192.168.110.59'
FLAGS = {'origin_matches', 'fetch_site_allowed', 'cookie_present', 'session_found',
         'csrf_present', 'csrf_matches', 'request.is_secure'}


class DiagnosticTest(unittest.TestCase):
    def setUp(self):
        self.now = 1700000000.0
        self.env = TestEnvironment(clock=lambda: self.now)
        self.addCleanup(self.env.close)
        self.app = create_app(auth_store=self.env.store, config={
            'TESTING': True, 'PUBLIC_ORIGIN': ORIGIN, 'COOKIE_SECURE': True})
        self.app.wsgi_app = ProxyFix(self.app.wsgi_app, x_for=1, x_proto=1, x_host=0)
        self.client = self.app.test_client()

    def get(self, client=None, path='/login', **kwargs):
        return (client or self.client).get(path, base_url=ORIGIN, **kwargs)

    def csrf(self, response):
        return re.search(r'name="csrf_token" value="([^"]+)"', response.text)[1]

    def post(self, csrf, client=None, headers=None):
        return (client or self.client).post('/login', base_url=ORIGIN,
            data={'username': 'testadmin', 'password': PASSWORD, 'csrf_token': csrf},
            headers=headers or {'Origin': ORIGIN, 'Sec-Fetch-Site': 'same-origin'})

    def detail(self, response):
        self.assertEqual(response.status_code, 403)
        row = next(r for r in self.env.store.logs() if r['action'] == 'csrf.denied')
        detail = json.loads(row['detail'])
        self.assertEqual(set(detail), FLAGS | {'request_id', 'public_origin', 'request_origin'})
        self.assertTrue(all(type(detail[k]) is bool for k in FLAGS))
        self.assertRegex(detail['request_id'], r'^[a-f0-9]{32}$')
        self.assertEqual(response.headers['X-Request-ID'], detail['request_id'])
        return detail

    def test_denial_matrix_and_secret_exclusion(self):
        csrf = self.csrf(self.get())
        raw = self.client.get_cookie(COOKIE, domain='192.168.110.59').value
        cases = [('', {}, {'csrf_present': False, 'csrf_matches': False}),
                 ('wrong-secret-marker', {}, {'csrf_present': True, 'csrf_matches': False}),
                 ('не-ascii', {}, {'csrf_matches': False}),
                 ('x'*129, {}, {'csrf_matches': False}),
                 (csrf, {'Origin': 'https://untrusted.invalid'}, {'origin_matches': False}),
                 (csrf, {'Sec-Fetch-Site': 'cross-site'}, {'fetch_site_allowed': False})]
        ids = set()
        for token, headers, changed in cases:
            expected = dict.fromkeys(FLAGS, True)
            expected.update(changed)
            d = self.detail(self.post(token, headers=headers))
            ids.add(d.pop('request_id'))
            self.assertEqual(d.pop('public_origin'), ORIGIN)
            d.pop('request_origin')
            self.assertEqual(d, expected)
        self.assertEqual(len(ids), len(cases))
        content = json.dumps(self.env.store.logs())
        for secret in (csrf, raw, PASSWORD, 'wrong-secret-marker', 'не-ascii'):
            self.assertNotIn(secret, content)

    def test_missing_unknown_expired_cookie(self):
        csrf = self.csrf(self.get())
        self.client.delete_cookie(COOKIE, domain='192.168.110.59')
        d = self.detail(self.post(csrf))
        self.assertFalse(d['cookie_present'])
        self.assertFalse(d['session_found'])
        self.client.set_cookie(COOKIE, 'unknown-session-token-value', domain='192.168.110.59')
        d = self.detail(self.post(csrf))
        self.assertTrue(d['cookie_present'])
        self.assertFalse(d['session_found'])
        csrf = self.csrf(self.get())
        self.now += 300
        d = self.detail(self.post(csrf))
        self.assertTrue(d['cookie_present'])
        self.assertFalse(d['session_found'])

    def test_repeated_login_and_favicon_redirect_preserve_session(self):
        csrf = self.csrf(self.get())
        raw = self.client.get_cookie(COOKIE, domain='192.168.110.59').value
        for _ in range(3):
            self.assertEqual(self.csrf(self.get()), csrf)
            favicon = self.get(path='/favicon.ico')
            self.assertEqual(favicon.status_code, 303)
            self.assertEqual(favicon.headers['Location'], '/login')
            self.assertNotIn('Set-Cookie', favicon.headers)
            self.assertEqual(self.csrf(self.get(path=favicon.headers['Location'])), csrf)
        self.assertEqual(self.client.get_cookie(COOKIE, domain='192.168.110.59').value, raw)
        self.assertEqual(self.post(csrf).status_code, 303)

    def test_concurrent_requests_with_existing_cookie_are_stable(self):
        csrf = self.csrf(self.get())
        raw = self.client.get_cookie(COOKIE, domain='192.168.110.59').value
        def fetch(path):
            client = self.app.test_client()
            client.set_cookie(COOKIE, raw, domain='192.168.110.59')
            response = self.get(client, path, follow_redirects=True)
            if path == '/static/style.css':
                self.assertNotIn('Set-Cookie', response.headers)
            else:
                self.assertEqual(self.csrf(response), csrf)
                self.assertEqual(client.get_cookie(COOKIE, domain='192.168.110.59').value, raw)
            response.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(fetch, ['/login', '/favicon.ico', '/static/style.css', '/login'] * 4))
        self.assertEqual(self.post(csrf).status_code, 303)

    def test_cookie_free_parallel_pages_can_mismatch_after_response_reordering(self):
        # Deterministic reproduction of two initial requests without a cookie.
        a, b = self.app.test_client(), self.app.test_client()
        with ThreadPoolExecutor(max_workers=2) as pool:
            pages = list(pool.map(lambda c: self.get(c), [a, b]))
        csrf_a, csrf_b = map(self.csrf, pages)
        self.assertNotEqual(csrf_a, csrf_b)
        a.set_cookie(COOKIE, b.get_cookie(COOKIE, domain='192.168.110.59').value,
                     domain='192.168.110.59')
        d = self.detail(self.post(csrf_a, a))
        self.assertTrue(d['cookie_present'])
        self.assertTrue(d['session_found'])
        self.assertFalse(d['csrf_matches'])
        # Reload uses the winning cookie and recovers.
        self.assertEqual(self.csrf(self.get(a)), csrf_b)
        self.assertEqual(self.post(csrf_b, a).status_code, 303)

    def test_proxy_scheme_and_cookie_security(self):
        response = self.get()
        for flag in ('Secure', 'HttpOnly', 'SameSite=Strict', 'Path=/'):
            self.assertIn(flag, response.headers['Set-Cookie'])
        for proto, secure in [('https', True), ('http', False)]:
            response = self.client.post('/login', base_url='http://192.168.110.59',
                headers={'X-Forwarded-Proto': proto}, data={'csrf_token': ''})
            self.assertEqual(self.detail(response)['request.is_secure'], secure)

    def test_valid_header_token_and_existing_absent_origin_policy(self):
        csrf = self.csrf(self.get())
        response = self.post('', headers={'X-CSRF-Token': csrf})
        self.assertEqual(response.status_code, 303)
        self.assertFalse(any(r['action'] == 'csrf.denied' for r in self.env.store.logs()))
