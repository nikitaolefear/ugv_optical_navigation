"""Exercise authenticated brake IPC without opening a serial transport."""
from functools import partial
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import RLock
from types import SimpleNamespace
import unittest
from uuid import uuid4

from disarm_control.gateway import CommandGateway
from disarm_control.ipc import BRAKE_OPERATIONS, OwnerClient, OwnerServer
from production_stop.brake_link import BrakeLink
from production_stop.owner_bridge import OwnerProductionStop
from production_stop.state import ResetConditions
from web_hmi.app import create_app
from web_hmi.test_support import CONFIG, PASSWORD, TestEnvironment


class BrakeTests(unittest.TestCase):
    def test_apply_preempts_confirmed_release_and_release_requires_reset(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        reader = SimpleNamespace(clock=lambda: 10.0, lock=RLock(), heartbeat_safety_serial=0,
            snapshot=lambda: {'armed': {'state': 'FRESH', 'value': 'DISARMED'}})
        heartbeat = SimpleNamespace(base_mode=0)
        native = SimpleNamespace(bind_stop_controller=lambda core: None, cycle=lambda **kwargs: None,
            enforces_elrs_stop_latch=lambda: True, blocked_at=reader.clock,
            reset_conditions=lambda: ResetConditions(10.0, True, True, True, True))
        channels = {'3': 1500, '8': 999}
        params = dict(MODE_CH=0, RC8_OPTION=0, RCMAP_THROTTLE=3, RC3_TRIM=1500,
            RC3_DZ=30, RC_OPTIONS=2, UGVB_APPLY_ONLY=0, UGVB_CH_APPLY=999, UGVB_CH_RELEASE=2000)
        inhibit = SimpleNamespace(active=True)
        inhibit.blocked = lambda: inhibit.active
        owner = OwnerProductionStop(reader, SimpleNamespace(disarm_pending=lambda: False),
                                    directory / 'stop.db', native=native)
        self.addCleanup(owner.close)
        brake = owner.brake = BrakeLink(reader,
            SimpleNamespace(_heartbeat=lambda now: (now, heartbeat, (1, 1), 1, 0)),
            owner, None, lambda: dict(at=10.0, channels=channels), hold_only=True,
            rc_health=lambda: dict(at=10.0, present=65536, enabled=65536, health=65536),
            parameters=lambda: {key: {'value': value} for key, value in params.items()},
            release_inhibited=inhibit.blocked)
        brake.last_sent = 10.0
        owner.gpio_observed('RELEASED', 10.0)

        def operations(handler):
            return {op: partial(handler, op) for op in BRAKE_OPERATIONS}

        backend = OwnerServer(None, directory / 'owner.sock', brake_operations=operations(owner.web_brake))
        backend.start()
        self.addCleanup(backend.close)
        gateway = CommandGateway(OwnerClient(directory / 'owner.sock'))
        gateway.inhibit = inhibit
        ingress = OwnerServer(gateway, directory / 'ingress.sock',
                              brake_operations=operations(gateway.brake_request))
        ingress.start()
        self.addCleanup(ingress.close)
        env = TestEnvironment()
        self.addCleanup(env.close)
        client = create_app(config=CONFIG, auth_store=env.store,
                            disarm_client=OwnerClient(directory / 'ingress.sock')).test_client()
        token = re.search(r'name="csrf_token" value="([^"]+)"', client.get('/login').text)[1]
        self.assertEqual(client.post('/login', data=dict(
            username='testadmin', password=PASSWORD, csrf_token=token)).status_code, 303)
        page = client.get('/').text
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', page)[1]
        headers = {'X-CSRF-Token': csrf}
        self.assertEqual(client.post('/api/pixhawk/brakes/apply',
            json={'request_id': str(uuid4())}, headers=headers).status_code, 403)
        with env.store.transaction() as db:
            db.execute("INSERT INTO role_permissions VALUES('admin', 'pixhawk.brakes')")
        self.assertIn('id="brake-release"', client.get('/').text)
        self.assertEqual(client.post('/api/pixhawk/brakes/release',
            json={'request_id': str(uuid4()), 'confirmed': True}).status_code, 403)

        def post(action, state, request_id=None, reason=None):
            body = {'request_id': request_id or str(uuid4())}
            if action != 'apply':
                body['confirmed'] = True
            reply = client.post('/api/pixhawk/brakes/' + action, json=body, headers=headers)
            expected = {'request_id': body['request_id'], 'state': state}
            if reason is not None:
                expected['reason'] = reason
            self.assertEqual(reply.json, expected)

        post('release', 'BLOCKED', reason='BRAKE_RELEASE_INHIBITED')
        post('apply', 'REQUESTED')
        inhibit.active = False
        post('release', 'BLOCKED', reason='BRAKE_HOLD_ONLY')
        brake.hold_only = False    # In-memory profile only. Never call serial tick.
        post('release', 'BLOCKED', reason='BRAKE_STOP_RESET_REQUIRED')
        post('reset', 'REQUESTED')
        release_id = str(uuid4())
        post('release', 'REQUESTED', release_id)
        post('release', 'BLOCKED', release_id, reason='BRAKE_RELEASE_ALREADY_REQUESTED')
        self.assertEqual(brake._state(10.0, heartbeat), brake.HOLD)
        channels['8'] = 2000
        self.assertEqual(brake._state(10.0, heartbeat), brake.RELEASE)
        post('apply', 'REQUESTED')
        self.assertTrue(owner.committed_latch)
        self.assertEqual(brake._state(10.0, heartbeat), brake.HOLD)
