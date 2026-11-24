"""Offline checks against the isolated frontend tree with web-frontend.patch applied."""
import re
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock

from disarm_control.engine import Rejected
from disarm_control.gateway import CommandGateway
from web_hmi.app import create_app
from web_hmi.test_support import CONFIG, PASSWORD, TestEnvironment


class BenchWebTests(unittest.TestCase):
    production = '/api/production-stop/reset'
    bench = '/api/bench-brakes/release'

    def setUp(self):
        self.env = TestEnvironment()
        self.addCleanup(self.env.close)
        self.transport = Mock()
        self.request_id = str(uuid.uuid4())
        self.body = {'request_id': self.request_id}
        self.boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()

    def client(self, boot, permitted=True):
        if permitted:
            with self.env.store.transaction() as db:
                db.execute("INSERT OR IGNORE INTO role_permissions VALUES ('admin', 'stop.reset')")
        app = create_app(config=dict(CONFIG, BENCH_RELEASE_BOOT_ID=boot),
                         auth_store=self.env.store, disarm_client=self.transport)
        client = app.test_client()
        token = re.search(r'name="csrf_token" value="([^"]+)"', client.get('/login').text)[1]
        self.assertEqual(client.post('/login', data=dict(username='testadmin', password=PASSWORD,
                                                        csrf_token=token)).status_code, 303)
        page = client.get('/').text
        self.assertIn('id="reset" disabled>RESET · без запуску', page)
        self.assertLess(page.index('id="pixhawk-arm"'), page.index('id="reset"'))
        token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page)[1]
        return client, {'X-CSRF-Token': token}

    def test_isolated_route_uses_auth_csrf_and_strict_payload(self):
        denied, headers = self.client(self.boot_id, permitted=False)
        self.assertEqual(denied.post(self.bench, json=self.body, headers=headers).status_code, 403)
        client, headers = self.client(self.boot_id)
        self.assertEqual(client.post(self.bench, json=self.body).status_code, 403)
        self.assertEqual(client.post(self.bench, json=self.body,
                                     headers=dict(headers, Origin='https://elsewhere.invalid')).status_code, 403)
        self.assertEqual(client.post(self.bench, json=dict(self.body, interlock=True),
                                     headers=headers).status_code, 400)
        self.assertEqual(client.post(self.production, json=self.body, headers=headers).status_code, 409)
        self.transport.call.assert_not_called()

    def test_boot_opt_in_scopes_route_and_stale_config_does_not_fall_back(self):
        normal, headers = self.client(None)
        self.assertEqual(normal.post(self.bench, json=self.body, headers=headers).status_code, 409)
        stale, headers = self.client(str(uuid.uuid4()))
        self.assertEqual(stale.post(self.bench, json=self.body, headers=headers).status_code, 409)
        self.assertEqual(stale.post(self.production, json=self.body, headers=headers).status_code, 409)
        self.transport.call.assert_not_called()

    def test_distinct_rpc_audit_and_status(self):
        client, headers = self.client(self.boot_id)
        result = dict(request_id=self.request_id, bench_release_accepted=True,
                      stop_latch=True, reset_available=False)
        self.transport.call.return_value = dict(ok=True, command=result)
        response = client.post(self.bench, json=self.body, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, result)
        self.assertEqual(self.transport.call.call_args.args[::2], ('BENCH_RELEASE', self.request_id))
        self.assertTrue(any(row['action'] == 'bench.brake.release' and row['result'] == 'ACCEPTED'
                            for row in self.env.store.logs()))
        self.assertFalse(any(row['action'] == 'stop.reset' for row in self.env.store.logs()))
        self.transport.call.return_value['command'] = dict(result, stop_latch=False)
        self.assertEqual(client.post(self.bench, json=self.body, headers=headers).status_code, 503)

    def test_gateway_rejects_normal_flags_and_invalid_owner_status(self):
        backend = Mock()
        gateway = CommandGateway(backend)
        args = (1, 'testadmin', self.request_id)
        with self.assertRaises(Rejected):
            gateway.bench_release(*args)
        backend.call.assert_not_called()
        gateway.bench_release_enabled = True
        gateway.inhibit = Mock()
        gateway.inhibit.path.is_file.return_value = True
        gateway.arm_enabled = True
        with self.assertRaises(Rejected):
            gateway.bench_release(*args)
        backend.call.assert_not_called()
        gateway.arm_enabled = False
        gateway.stop_reset_enabled = True
        with self.assertRaises(Rejected):
            gateway.bench_release(*args)
        backend.call.assert_not_called()
        gateway.stop_reset_enabled = False
        backend.call.return_value = dict(ok=True, command=dict(
            bench_release_available=True, stop_latch=True, reset_available=False,
            physical_drive_interlocked=False))
        result = gateway.stop_operation('STOP_STATUS', *args)
        self.assertTrue(result['bench_release_available'])
        self.assertFalse(result['physical_drive_interlocked'])
        backend.call.return_value['command']['reset_available'] = True
        self.assertFalse(gateway.stop_operation('STOP_STATUS', *args)['bench_release_available'])
        backend.call.return_value = dict(ok=True, command=dict(request_id=self.request_id,
            bench_release_accepted=True, stop_latch=True, reset_available=False))
        self.assertTrue(gateway.bench_release(*args)['bench_release_accepted'])
        with self.assertRaises(Rejected):
            gateway.stop_operation('STOP_RESET', *args)
        gateway.inhibit.path.is_file.return_value = False
        with self.assertRaises(Rejected):
            gateway.bench_release(*args)


if __name__ == '__main__':
    unittest.main(verbosity=2)
