"""Hardware-less gateway validation; all owner calls are fakes."""
from contextlib import redirect_stderr
import copy
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from disarm_control.engine import Rejected
from disarm_control.gateway import CommandGateway, main
from disarm_control.ipc import OwnerClient, OwnerServer, SocketPolicy


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.2
        self.rid = str(uuid.uuid4())
        self.backend = Mock()
        self.gateway = CommandGateway(self.backend, enabled=True, clock=lambda: self.now)
        self.backend.call.return_value = dict(ok=True, command=self.command())

    def command(self, delivery='QUEUED', observation='UNKNOWN'):
        value = dict(id=self.rid, actor_id=1, state=delivery, delivery_state=delivery,
                     command_outcome='UNKNOWN', observation_state=observation,
                     observation_reason='NO_POST_SEND_HEARTBEAT', observation_pending=delivery=='QUEUED',
                     heartbeat_age_s=None, heartbeat_rx_serial=None, heartbeat_observed_at=None,
                     heartbeat_source=None, heartbeat_freshness_s=3.)
        if observation != 'UNKNOWN':
            value.update(heartbeat_age_s=.1, heartbeat_rx_serial=2, heartbeat_observed_at=10.1,
                         heartbeat_source=[1,1], observation_reason='FRESH_POST_SEND_HEARTBEAT')
        return value

    def test_default_is_disabled_with_no_backend_call(self):
        gateway = CommandGateway(self.backend)
        with self.assertRaisesRegex(Rejected, 'DISARM_DISABLED'):
            gateway.request(1, 'operator', self.rid)
        self.backend.call.assert_not_called()

    def test_explicit_request_forwards_only_identity_and_uuid_once(self):
        result = self.gateway.request(1, 'operator', self.rid)
        self.backend.call.assert_called_once_with('DISARM', {'id':1,'username':'operator'}, self.rid)
        self.assertEqual(result['command_outcome'],'UNKNOWN')
        self.assertEqual(result['delivery_state'],'QUEUED')

    def test_backend_failure_never_retries_or_invents_success(self):
        self.backend.call.side_effect=TimeoutError()
        with self.assertRaisesRegex(Rejected,'BACKEND_RESULT_UNKNOWN'):
            self.gateway.request(1,'operator',self.rid)
        self.backend.call.assert_called_once()

    def test_invalid_actor_or_uuid_cannot_reach_owner(self):
        for actor_id, actor, rid in ((True,'operator',self.rid),(1,'bad\n',self.rid),(1,'operator','ARM')):
            with self.assertRaises(Rejected):
                self.gateway.request(actor_id,actor,rid)
        self.backend.call.assert_not_called()

    def test_status_remains_readable_but_unknown_when_disabled(self):
        self.backend.call.return_value=dict(ok=True,command=self.command('SENT','DISARMED_OBSERVED'))
        self.gateway.set_enabled(False)
        result=self.gateway.status(1,self.rid)
        self.assertEqual(result['delivery_state'],'SENT')
        self.assertEqual(result['observation_state'],'UNKNOWN')
        self.assertIsNone(result['heartbeat_age_s'])
        self.assertEqual(self.backend.call.call_args.args[0],'STATUS')

    def test_inhibit_blocks_new_request_and_masks_observation(self):
        policy=Mock()
        policy.blocked.return_value=True
        self.gateway.inhibit=policy
        with self.assertRaisesRegex(Rejected,'DISARM_INHIBITED'):
            self.gateway.request(1,'operator',self.rid)
        self.backend.call.assert_not_called()
        self.backend.call.return_value=dict(ok=True,command=self.command('SENT','DISARMED_OBSERVED'))
        self.assertEqual(self.gateway.status(1,self.rid)['observation_state'],'UNKNOWN')

    def test_old_confirmed_or_wrong_actor_reply_is_rejected(self):
        for change in ({'state':'CONFIRMED'}, {'command_outcome':'CONFIRMED'}, {'actor_id':2},
                       {'id':str(uuid.uuid4())}, {'delivery_state':'SUCCESS'}):
            value=self.command()
            value.update(change)
            self.backend.call.return_value=dict(ok=True,command=value)
            with self.assertRaisesRegex(Rejected,'INVALID_BACKEND_RESPONSE'):
                self.gateway.request(1,'operator',self.rid)

    def test_ack_diagnostic_does_not_become_result(self):
        value=self.command('SENT')
        value['ack_diagnostic']={'scope':'UNCORRELATED','command':400,'result':0}
        self.backend.call.return_value=dict(ok=True,command=value)
        result=self.gateway.status(1,self.rid)
        self.assertEqual(result['command_outcome'],'UNKNOWN')
        self.assertEqual(result['observation_state'],'UNKNOWN')

    def test_relay_delay_cannot_refresh_old_heartbeat(self):
        value=self.command('SENT','DISARMED_OBSERVED')
        original=copy.deepcopy(value)
        def delayed(*args):
            self.now+=3
            return dict(ok=True,command=value)
        self.backend.call.side_effect=delayed
        result=self.gateway.status(1,self.rid)
        self.assertEqual(result['delivery_state'],'SENT')
        self.assertEqual(result['observation_state'],'UNKNOWN')
        self.assertEqual(value,original)

    def test_future_or_inconsistent_evidence_cannot_look_healthy(self):
        for change in ({'heartbeat_observed_at':11.}, {'heartbeat_age_s':float('nan')},
                       {'heartbeat_source':[2,2]}, {'heartbeat_rx_serial':True}):
            value=self.command('SENT','DISARMED_OBSERVED')
            value.update(change)
            self.backend.call.return_value=dict(ok=True,command=value)
            try:
                result=self.gateway.status(1,self.rid)
            except Rejected:
                continue
            self.assertEqual(result['observation_state'],'UNKNOWN')

    def test_clock_regression_or_nonfinite_is_unknown_not_a_retry(self):
        for times in ((10.2,10.1),(10.2,float('nan')),(None,)):
            with patch.object(self.gateway,'clock',side_effect=times):
                with self.assertRaises(Rejected):
                    self.gateway.status(1,self.rid)

    def test_cli_refuses_root_or_shared_uids_before_binding(self):
        args=['--socket','/tmp/not-used.sock','--frontend-uid','1001','--ingress-gid','2001',
              '--backend-socket','/tmp/not-used-backend.sock','--owner-uid','1002','--backend-gid','2002']
        with patch('os.getuid',return_value=1000), patch('os.geteuid',return_value=0):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(args)
        with patch('os.getuid',return_value=1001), patch('os.geteuid',return_value=1001):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(args)


class CrossPolicyTests(unittest.TestCase):
    def test_explicit_group_policy_socket_and_peer_enforcement(self):
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory)/'ingress'
            parent.mkdir(mode=0o710)
            policy=SocketPolicy(os.getuid(),os.getgid(),0o710,0o660)
            engine=Mock()
            engine.request.return_value={'state':'QUEUED'}
            server=OwnerServer(engine,parent/'control.sock',policy=policy)
            server.start()
            try:
                client=OwnerClient(parent/'control.sock',policy=policy)
                self.assertTrue(client.call('DISARM',{'id':1,'username':'operator'},str(uuid.uuid4()))['ok'])
                self.assertEqual((parent/'control.sock').stat().st_mode & 0o777,0o660)
                server.allowed_uid=os.getuid()+1
                with self.assertRaises(ValueError):
                    client.call('DISARM',{'id':1,'username':'operator'},str(uuid.uuid4()))
                engine.request.assert_called_once()
            finally:
                server.close()

    def test_group_policy_never_accepts_directory_write_or_unspecified_gid(self):
        for args in ((1000,None,0o710,0o660),(1000,1000,0o770,0o660),
                     (1000,1000,0o710,0o666)):
            with self.assertRaises(ValueError):
                SocketPolicy(*args)
