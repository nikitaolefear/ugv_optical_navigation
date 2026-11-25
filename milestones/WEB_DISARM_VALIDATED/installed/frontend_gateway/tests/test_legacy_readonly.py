"""Prepared legacy routes only; no HTTP requests to the running services."""
import unittest
from unittest.mock import Mock

from stop_latch_test.server import create_app
from web_hmi.app import create_app as telemetry_app


class LegacyReadonlyTests(unittest.TestCase):
    def setUp(self):
        self.bench = Mock()
        self.bench.snapshot.return_value = dict(contact='RELEASED', stop_latch=True,
                                                test_state='STOPPED', hold_phase='IDLE')
        self.client = create_app(self.bench, hold=True).test_client()

    def test_get_state_still_works_without_a_control_token(self):
        response = self.client.get('/api/state')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['legacy_control'], 'DISABLED')
        self.assertNotIn('csrf', response.json)
        self.assertTrue(response.json['stop_latch'])
        self.bench.command.assert_not_called()

    def test_all_old_control_actions_are_disabled_even_with_csrf_headers(self):
        for action in ('reset', 'arm_hold_once', 'arm_reset_probe', 'start', 'resume', 'hold'):
            response = self.client.post('/api/test-action', json={'action': action}, headers={
                'X-Test-CSRF': 'anything', 'Origin': 'http://localhost',
                'Sec-Fetch-Site': 'same-origin', 'X-CSRF-Token': 'fixture-only-0000000006'})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json['error'], 'LEGACY_CONTROL_DISABLED')
        self.bench.command.assert_not_called()

    def test_no_mutation_method_or_alternate_path_bypasses_guard(self):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            for path in ('/api/state', '/', '/api/test-action', '/other'):
                self.assertEqual(self.client.open(path, method=method).status_code, 403)
        self.bench.command.assert_not_called()

    def test_no_live_test_buttons_or_legacy_script_are_served(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('<button', response.text)
        self.assertIn('лише для читання', response.text)
        self.assertEqual(self.client.get('/app.js').status_code, 404)

    def test_8082_telemetry_is_independent_of_legacy_control_closure(self):
        reader = Mock()
        reader.snapshot.return_value = dict(connection='CONNECTED', armed={'state': 'FRESH', 'value': 'DISARMED'})
        telemetry = telemetry_app(telemetry=reader).test_client()
        self.client.post('/api/test-action', json={'action': 'reset'})
        response = telemetry.get('/api/telemetry')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['armed']['value'], 'DISARMED')
        reader.start.assert_not_called()
        self.bench.command.assert_not_called()
