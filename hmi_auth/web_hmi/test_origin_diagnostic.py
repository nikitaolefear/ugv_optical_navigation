"""Origin diagnostics never relax authorization and never print malformed input."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import re
import unittest
from unittest.mock import patch
from .app import create_app
from .auth import diagnostic_origin
from . import admin_cli
from .test_support import TestEnvironment

ORIGIN = 'https://192.168.110.59'
FLAGS = ('origin_matches', 'fetch_site_allowed', 'cookie_present', 'session_found',
         'csrf_present', 'csrf_matches', 'request.is_secure')


class OriginDiagnosticTest(unittest.TestCase):
    def setUp(self):
        self.env = TestEnvironment()
        self.addCleanup(self.env.close)
        self.app = create_app(auth_store=self.env.store, config={
            'TESTING': True, 'PUBLIC_ORIGIN': ORIGIN, 'COOKIE_SECURE': True})
        self.client = self.app.test_client()
        page = self.client.get('/login', base_url=ORIGIN)
        self.csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]

    def cli(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = admin_cli.main(['--state-dir', self.env.tmp.name, 'csrf-diagnostics'])
        self.assertEqual((status, err.getvalue()), (0, ''))
        return out.getvalue(), json.loads(out.getvalue())

    def test_bare_origin_parser(self):
        valid = [ORIGIN, 'http://localhost', 'https://example.org:443',
                 'https://example.org:8443', 'HTTP://Example.ORG',
                 'https://[::1]', 'https://[2001:db8::1]:443', 'https://example.org.']
        invalid = [None, '', 'null', 'SECRET', 'https://user:SECRET@example.org',
                   'https://SECRET@example.org', ORIGIN+'/', ORIGIN+'?', ORIGIN+'#',
                   ORIGIN+'/SECRET', ORIGIN+'?password=SECRET', ORIGIN+'#SECRET',
                   ORIGIN+'\nSECRET', '\t'+ORIGIN, ORIGIN+' ', ORIGIN+'\n',
                   'https://example.org\\SECRET', 'ftp://example.org',
                   'https://example.org:SECRET', 'https://example.org:65536',
                   'https://example.org:', 'https://-bad.org', 'https://example..org',
                   'https://example.org..', 'https://', 'https://[::xyz]',
                   'https://[::1]SECRET', 'https://[::1]:', 'https://[::1]:65536',
                   'https://example.org https://other.org', 'https://приклад.org',
                   123, True, {}, [], 'https://'+'a'*321]
        for value in valid:
            with self.subTest(valid=value):
                self.assertEqual(diagnostic_origin(value), value)
        for value in invalid:
            with self.subTest(invalid=value):
                self.assertEqual(diagnostic_origin(value), 'INVALID_ORIGIN')

    def test_actual_denial_passes_safe_origins_to_cli(self):
        response = self.client.post('/login', base_url=ORIGIN,
            headers={'Origin': ORIGIN+':443', 'Sec-Fetch-Site': 'same-origin'},
            data={'csrf_token': self.csrf, 'password': 'PASSWORD_SECRET'})
        self.assertEqual(response.status_code, 403)  # No origin normalization for policy.
        out, diagnostics = self.cli()
        record = diagnostics['records'][0]
        self.assertEqual(set(record), {'time', 'request_id', 'public_origin', 'request_origin', *FLAGS})
        self.assertEqual(record['public_origin'], ORIGIN)
        self.assertEqual(record['request_origin'], ORIGIN+':443')
        self.assertEqual(record['request_id'], response.headers['X-Request-ID'])
        self.assertFalse(record['origin_matches'])
        for flag in FLAGS[1:]:
            self.assertTrue(record[flag])
        self.assertNotIn(self.csrf, out)
        self.assertNotIn('PASSWORD_SECRET', out)

    def test_invalid_header_never_enters_audit_or_cli(self):
        for origin in [ORIGIN+'/FORM_SECRET', 'https://user:COOKIE_SECRET@example.org',
                       ORIGIN+'?token=TOKEN_SECRET', 'null', 'https://host#FORM_SECRET']:
            response = self.client.post('/login', base_url=ORIGIN,
                headers={'Origin': origin}, data={'csrf_token': self.csrf})
            self.assertEqual(response.status_code, 403)
            detail = next(r['detail'] for r in self.env.store.logs() if r['action']=='csrf.denied')
            self.assertEqual(json.loads(detail)['request_origin'], 'INVALID_ORIGIN')
        out, diagnostics = self.cli()
        audit = json.dumps(self.env.store.logs())
        for secret in ('FORM_SECRET', 'COOKIE_SECRET', 'TOKEN_SECRET', self.csrf):
            self.assertNotIn(secret, out + audit)
        self.assertTrue(all(r['request_origin']=='INVALID_ORIGIN' for r in diagnostics['records']))

    def test_cli_revalidates_stored_values_and_preserves_legacy_records(self):
        base = dict(request_id='a'*32, **dict.fromkeys(FLAGS, True))
        self.env.store.audit(None, 'csrf.denied', 'DENIED', json.dumps(base))
        for value in ['https://user:SECRET@example.org', 'https://host/?SECRET',
                      {'SECRET': True}, 123, None, 'https://host\nSECRET']:
            self.env.store.audit(None, 'csrf.denied', 'DENIED', json.dumps({**base,
                'public_origin': value, 'request_origin': value, 'password': 'SECRET'}))
        self.env.store.audit(None, 'csrf.denied', 'DENIED', json.dumps({**base,
            'public_origin': ORIGIN, 'request_origin': 'http://localhost:8080'}))
        out, diagnostics = self.cli()
        self.assertNotIn('SECRET', out)
        self.assertEqual(diagnostics['skipped_invalid'], 0)
        self.assertEqual(diagnostics['records'][0]['request_origin'], 'http://localhost:8080')
        for record in diagnostics['records'][1:-1]:
            self.assertEqual(record['public_origin'], 'INVALID_ORIGIN')
            self.assertEqual(record['request_origin'], 'INVALID_ORIGIN')
        self.assertNotIn('request_origin', diagnostics['records'][-1])
        self.assertNotIn('public_origin', diagnostics['records'][-1])

    def test_config_source_is_selected_environment_key_or_default(self):
        with patch.dict('os.environ', {'UGV_HMI_PUBLIC_ORIGIN':'https://test.example:8443'}):
            app = create_app(auth_store=self.env.store)
        self.assertEqual(app.config['PUBLIC_ORIGIN'], 'https://test.example:8443')
        # Factory configuration overrides environment when explicitly supplied.
        with patch.dict('os.environ', {'UGV_HMI_PUBLIC_ORIGIN':'https://test.example:8443'}):
            app = create_app(auth_store=self.env.store, config={'PUBLIC_ORIGIN': ORIGIN})
        self.assertEqual(app.config['PUBLIC_ORIGIN'], ORIGIN)
