"""Verify dynamic superadmin permissions through the authenticated HMI."""
import re
import unittest
from unittest.mock import patch

from .access_control import PERMISSIONS
from .app import create_app
from .auth import AuthStore
from .test_support import CONFIG, PASSWORD, TestEnvironment


class SuperadminPermissionsTest(unittest.TestCase):
    def setUp(self):
        self.env = TestEnvironment()
        self.addCleanup(self.env.close)
        for name, role in (('chief', 'superadmin'), ('chief2', 'superadmin'),
                           ('operator1', 'operator'), ('viewer1', 'observer')):
            self.env.store.add_user(name, PASSWORD, role)
        self.app = self.new_app(self.env.store)

    @staticmethod
    def new_app(store):
        return create_app(config={**CONFIG, 'BENCH_RELEASE_BOOT_ID': None,
                                  'ARM_BOOT_ID': None, 'DISARM_ENABLED': False},
                          auth_store=store)

    def login(self, name):
        client = self.app.test_client()
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get('/login').text)[1]
        result = client.post('/login', data={'username': name, 'password': PASSWORD,
                                            'csrf_token': csrf})
        self.assertEqual(result.status_code, 303)
        return client

    @staticmethod
    def principal(client):
        return client.get('/api/session').json['user']

    def test_new_permission_applies_to_existing_sessions_and_after_restart(self):
        clients = {name: self.login(name) for name in
                   ('chief', 'chief2', 'testadmin', 'operator1', 'viewer1')}
        lower = {name: self.principal(clients[name]) for name in
                 ('testadmin', 'operator1', 'viewer1')}
        for name in ('chief', 'chief2'):
            self.assertEqual(self.principal(clients[name])['permissions'], sorted(PERMISSIONS))
        with patch.dict(PERMISSIONS, {'future.test': 'New registered permission'}):
            self.assertIn('future.test', self.principal(clients['chief'])['permissions'])
            restarted = self.new_app(AuthStore(self.env.tmp.name))
            for name, old_client in clients.items():
                client = restarted.test_client()
                cookie = old_client.get_cookie('ugv_session')
                client.set_cookie('ugv_session', cookie.value)
                user = self.principal(client)
                if name in lower:
                    self.assertEqual(user, lower[name])
                else:
                    self.assertEqual(user['rank'], 1000)
                    self.assertEqual(user['permissions'], sorted(PERMISSIONS))

    def test_permission_and_bench_interlock_remain_separate(self):
        chief = self.login('chief')
        admin = self.login('testadmin')
        self.assertIn('stop.reset', self.principal(chief)['permissions'])
        self.assertNotIn('stop.reset', self.principal(admin)['permissions'])
        route = '/api/bench-brakes/release'
        for client, expected in ((chief, 409), (admin, 403)):
            page = client.get('/access')
            csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)[1]
            result = client.post(route, json={}, headers={'X-CSRF-Token': csrf})
            self.assertEqual(result.status_code, expected)
        self.assertEqual(self.app.test_client().post(route, json={}).status_code, 401)
