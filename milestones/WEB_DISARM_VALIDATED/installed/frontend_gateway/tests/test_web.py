import re
import os
import uuid
import unittest
from unittest.mock import Mock, patch

from web_hmi.app import create_app
from web_hmi.test_support import TestEnvironment, PASSWORD, CONFIG


class WebTests(unittest.TestCase):
    def setUp(self):
        self.env = TestEnvironment()
        with self.env.store.transaction() as db:
            db.execute("INSERT INTO role_permissions VALUES('admin','pixhawk.disarm')")
        self.addCleanup(self.env.close)
        self.owner = Mock()
        self.rid = str(uuid.uuid4())
        self.now = 10.2
        self.app = create_app(config=dict(CONFIG, DISARM_ENABLED=True, DISARM_CLOCK=lambda: self.now),
                              auth_store=self.env.store, disarm_client=self.owner)
        self.client = self.app.test_client()
        self.login()
        self.owner.call.return_value = dict(ok=True, command=self.command())

    def command(self, delivery='QUEUED', observation='UNKNOWN', **changes):
        result=dict(id=self.rid, actor_id=self.actor_id, state=delivery, delivery_state=delivery,
                    observation_state=observation, observation_reason='NO_POST_SEND_HEARTBEAT',
                    observation_pending=delivery=='QUEUED', command_outcome='UNKNOWN',
                    heartbeat_age_s=None, heartbeat_rx_serial=None, heartbeat_observed_at=None,
                    heartbeat_source=None, heartbeat_freshness_s=3.0)
        if observation!='UNKNOWN':
            result.update(heartbeat_age_s=.1,heartbeat_rx_serial=2,heartbeat_observed_at=10.1,
                          heartbeat_source=[1,1],observation_pending=False,observation_reason=None)
        result.update(changes)
        return result

    def login(self, username='testadmin'):
        page = self.client.get('/login')
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        response = self.client.post('/login', data=dict(username=username, password=PASSWORD, csrf_token=token))
        self.assertEqual(response.status_code, 303)
        page = self.client.get('/')
        self.csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text).group(1)
        self.actor_id = next(u['id'] for u in self.env.store.users() if u['username'] == username)

    def post(self, body=None, **kwargs):
        return self.client.post('/api/pixhawk/disarm', json=body or {'request_id':self.rid},
                                headers={'X-CSRF-Token':self.csrf, **kwargs})

    def test_authenticated_authorized_intent_is_audited_before_dispatch(self):
        def owner(op, actor, request_id):
            logs = self.env.store.logs()
            self.assertTrue(any(row['action']=='pixhawk.disarm' and row['result']=='PENDING' for row in logs))
            return dict(ok=True, command=self.command())
        self.owner.call.side_effect = owner
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['state'], 'QUEUED')
        self.assertTrue(any(row['action']=='pixhawk.disarm' and row['result']=='ACCEPTED_BY_GATEWAY'
                            for row in self.env.store.logs()))

    def test_login_required_and_no_owner_call(self):
        client = self.app.test_client()
        self.assertEqual(client.post('/api/pixhawk/disarm', json={'request_id':self.rid}).status_code, 401)
        self.assertEqual(client.get('/api/pixhawk/disarm/'+self.rid).status_code, 401)
        self.owner.call.assert_not_called()

    def test_observer_and_default_operator_cannot_disarm(self):
        for name, role in (('reader','observer'),('driver','operator')):
            self.env.store.add_user(name, PASSWORD, role)
            self.client = self.app.test_client()
            self.login(name)
            self.assertEqual(self.post().status_code, 403)
            self.assertEqual(self.client.get('/api/pixhawk/disarm/'+self.rid).status_code, 403)
            self.assertNotIn('id="pixhawk-disarm"', self.client.get('/').text)
        self.owner.call.assert_not_called()

    def test_missing_and_wrong_csrf_never_dispatch(self):
        for headers in ({}, {'X-CSRF-Token':'fixture-only-0000000003'}):
            response=self.client.post('/api/pixhawk/disarm', json={'request_id':self.rid}, headers=headers)
            self.assertEqual(response.status_code, 403)
        self.owner.call.assert_not_called()

    def test_cross_origin_and_fetch_site_are_denied(self):
        self.assertEqual(self.post(Origin='https://attacker.invalid').status_code, 403)
        self.assertEqual(self.post(**{'Sec-Fetch-Site':'cross-site'}).status_code, 403)
        self.owner.call.assert_not_called()

    def test_only_request_id_is_accepted_no_force_or_arm(self):
        for body in ({'request_id':self.rid,'force':True}, {'request_id':self.rid,'param1':1},
                     {'request_id':'ARM'}, {'request_id':None}, {'target_system':2}):
            self.assertEqual(self.post(body).status_code, 400)
        self.owner.call.assert_not_called()

    def test_feature_is_disabled_by_default(self):
        self.app.config['DISARM_ENABLED'] = False
        self.assertEqual(self.post().status_code, 503)
        self.owner.call.assert_not_called()
        self.assertRegex(self.client.get('/').text, r'id="pixhawk-disarm"[^>]*disabled')

    def test_ipc_failure_is_uncertain_not_success_and_no_retry(self):
        self.owner.call.side_effect = TimeoutError()
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.owner.call.assert_called_once()
        self.assertTrue(any(row['result']=='DELIVERY_UNKNOWN' for row in self.env.store.logs()))

    def test_owner_rejection_is_not_success(self):
        self.owner.call.return_value = dict(ok=False, error='HEARTBEAT_STALE')
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json['error'], 'HEARTBEAT_STALE')

    def test_delivery_and_observation_are_separate_and_degrade(self):
        self.assertEqual(self.post().json['delivery_state'],'QUEUED')
        for observation in ('DISARMED_OBSERVED','ARMED_OBSERVED','UNKNOWN'):
            self.owner.call.return_value=dict(ok=True,command=self.command('SENT',observation))
            response=self.client.get('/api/pixhawk/disarm/'+self.rid)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json['observation_state'],observation)
            self.assertEqual(response.json['command_outcome'],'UNKNOWN')
        logs=self.env.store.logs()
        self.assertTrue(any(row['action']=='pixhawk.disarm.observation' and
                            row['result']=='DISARMED_OBSERVED' for row in logs))
        self.assertFalse(any(row['result']=='CONFIRMED' for row in logs))

    def test_feature_off_still_allows_reading_existing_request(self):
        self.app.config['DISARM_ENABLED']=False
        self.assertEqual(self.post().status_code,503)
        self.assertEqual(self.client.get('/api/pixhawk/disarm/'+self.rid).status_code,200)

    def test_status_poll_does_not_flood_audit(self):
        for _ in range(5):
            self.assertEqual(self.client.get('/api/pixhawk/disarm/'+self.rid).status_code,200)
        rows=[row for row in self.env.store.logs() if row['action']=='pixhawk.disarm.observation']
        self.assertEqual(len(rows),1)

    def test_stale_or_wrong_observation_evidence_is_unknown_error(self):
        for change in (dict(heartbeat_age_s=3),dict(heartbeat_source=[2,2]),
                       dict(heartbeat_age_s=float('inf')),dict(heartbeat_rx_serial=True),
                       dict(command_outcome='CONFIRMED')):
            self.owner.call.return_value=dict(ok=True,command=self.command('SENT','DISARMED_OBSERVED',**change))
            response=self.client.get('/api/pixhawk/disarm/'+self.rid)
            self.assertEqual(response.status_code,503)
            self.assertEqual(response.json['observation_state'],'UNKNOWN')

    def test_mismatched_owner_reply_is_not_success(self):
        for field, value in (('id', str(uuid.uuid4())), ('actor_id',999), ('state','SUCCESS')):
            original = self.owner.call.return_value['command'][field]
            self.owner.call.return_value['command'][field]=value
            self.assertEqual(self.post().status_code, 503)
            self.owner.call.return_value['command'][field]=original

    def test_malformed_reply_or_confirmation_without_evidence_is_not_success(self):
        replies = (None, [], {'ok':'yes'}, {'ok':True,'command':[]},
                   {'ok':True,'command':dict(id=self.rid,actor_id=self.actor_id,
                                             state='CONFIRMED',reason=None)})
        for reply in replies:
            with self.subTest(reply=reply):
                self.owner.call.return_value=reply
                self.assertEqual(self.post().status_code,503)

    def test_audit_failure_prevents_command_delivery(self):
        with patch.object(self.env.store, 'audit', side_effect=OSError('storage')):
            self.assertEqual(self.post().status_code, 503)
        self.owner.call.assert_not_called()

    def test_page_separates_real_disarm_from_demo_and_estop(self):
        page = self.client.get('/').text
        self.assertIn('DISARM · роззброїти Pixhawk', page)
        self.assertIn('Фізичний E-STOP незалежний', page)
        self.assertIn('/static/disarm.js', page)
        self.assertIn('id="disarm-notice" role="status" aria-live="assertive"', page)

    def test_disarm_does_not_call_demo_or_open_serial(self):
        real_open = os.open

        def guarded_open(path, *args, **kwargs):
            if os.fsdecode(path).startswith('/dev/'):
                raise AssertionError('Hardware open forbidden')
            return real_open(path, *args, **kwargs)

        with patch.object(self.app.extensions['simulator'], 'command', side_effect=AssertionError('DEMO forbidden')):
            with patch('os.open', side_effect=guarded_open):
                self.assertEqual(self.post().status_code, 200)

    def test_local_telemetry_start_is_forbidden_in_disarm_frontend(self):
        from web_hmi.runtime import start_telemetry
        reader=self.app.extensions['pixhawk_telemetry']
        with patch.object(reader,'start',side_effect=AssertionError('Second serial owner forbidden')) as start:
            with self.assertRaisesRegex(RuntimeError,'existing serial owner'):
                start_telemetry(self.app)
            start.assert_not_called()

    def test_fresh_roles_have_no_implicit_disarm_permission(self):
        from web_hmi.access_control import DEFAULT_ROLES
        for role,(_,permissions) in DEFAULT_ROLES.items():
            self.assertNotIn('pixhawk.disarm',permissions,role)

    def test_explicit_permission_revocation_prevents_dispatch(self):
        with self.env.store.transaction() as db:
            db.execute("DELETE FROM role_permissions WHERE permission='pixhawk.disarm'")
        self.assertEqual(self.post().status_code,403)
        self.owner.call.assert_not_called()

    def test_enabled_real_frontend_requires_distinct_explicit_gateway_identity(self):
        for changes in ({},dict(DISARM_GATEWAY_UID=os.getuid(),DISARM_FRONTEND_GID=os.getgid()),
                        dict(DISARM_GATEWAY_UID='bad',DISARM_FRONTEND_GID=1)):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                create_app(config=dict(CONFIG,DISARM_ENABLED=True,**changes),auth_store=self.env.store)

    def test_disabled_feature_preserves_gateway_policy_for_status(self):
        with patch('disarm_control.ipc.OwnerClient') as client:
            create_app(config=dict(CONFIG,DISARM_ENABLED=False,DISARM_GATEWAY_UID=os.getuid()+1,
                                   DISARM_FRONTEND_GID=os.getgid()),auth_store=self.env.store)
            self.assertEqual(client.call_args.kwargs['policy'].uid,os.getuid()+1)

    def test_hmi_relay_latency_cannot_keep_old_observation_healthy(self):
        def slow(*args):
            self.now += 3
            return dict(ok=True,command=self.command('SENT','DISARMED_OBSERVED'))
        self.owner.call.side_effect=slow
        response=self.client.get('/api/pixhawk/disarm/'+self.rid)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['delivery_state'],'SENT')
        self.assertEqual(response.json['observation_state'],'UNKNOWN')
        self.assertEqual(response.json['observation_reason'],'STALE_DURING_HMI_RELAY')

    def test_missing_status_record_is_unknown_not_proof_of_rejection(self):
        self.owner.call.return_value=dict(ok=False,error='REQUEST_NOT_FOUND')
        response=self.client.get('/api/pixhawk/disarm/'+self.rid)
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.json['delivery_state'],'UNKNOWN')
        self.assertEqual(response.json['observation_state'],'UNKNOWN')
