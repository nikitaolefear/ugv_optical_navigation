import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from disarm_control.ipc import OwnerClient, OwnerServer, receive


class IPCTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'owner.sock'
        self.engine = Mock()
        self.engine.request.return_value = dict(id='test', state='QUEUED')
        self.engine.status.return_value = dict(id='test', state='WAITING')
        self.server = OwnerServer(self.engine, self.path)
        self.server.start()
        self.addCleanup(self.server.close)
        self.client = OwnerClient(self.path)
        self.actor = dict(id=1,username='operator')

    def test_private_socket_request_and_status(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        rid = str(uuid.uuid4())
        self.assertTrue(self.client.call('DISARM', self.actor, rid)['ok'])
        self.engine.request.assert_called_once_with(1, 'operator', rid)
        self.assertEqual(self.client.call('STATUS', self.actor, rid)['command']['state'], 'WAITING')

    def test_only_disarm_and_status_supported(self):
        for action in ('ARM','RESET','HOLD','START','RESUME'):
            self.assertFalse(self.client.call(action,self.actor,'id')['ok'])
        self.engine.request.assert_not_called()

    def test_peer_uid_mismatch_cannot_dispatch(self):
        self.server.allowed_uid = os.getuid()+1
        with self.assertRaises(ValueError):
            self.client.call('DISARM',self.actor,'id')
        self.engine.request.assert_not_called()

    def test_oversize_or_malformed_input_is_rejected(self):
        for raw in (b'x'*4097+b'\n', b'[]\n', b'{bad}\n', b'{"op":"DISARM","param1":1}\n'):
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as stream:
                stream.settimeout(2)
                stream.connect(str(self.path))
                stream.sendall(raw)
                self.assertFalse(receive(stream)['ok'])
        self.engine.request.assert_not_called()

    def test_second_server_never_unlinks_existing_socket(self):
        inode = self.path.stat().st_ino
        with self.assertRaises(OSError):
            OwnerServer(self.engine,self.path).start()
        self.assertEqual(self.path.stat().st_ino,inode)

    def test_unknown_client_path_never_falls_back_to_serial_or_tcp(self):
        with self.assertRaises(OSError):
            OwnerClient(Path(self.tmp.name)/'absent').call('DISARM',self.actor,'id')

    def test_world_accessible_parent_is_refused(self):
        parent=Path(self.tmp.name)/'public'
        parent.mkdir(mode=0o755)
        with self.assertRaises(PermissionError):
            OwnerServer(self.engine,parent/'x.sock').start()

    def test_client_refuses_unsafe_socket_permissions(self):
        self.path.chmod(0o666)
        with self.assertRaises(PermissionError):
            self.client.call('DISARM',self.actor,'id')
        self.engine.request.assert_not_called()

    def test_client_checks_server_peer_credentials(self):
        real_uid=os.getuid()
        with patch('disarm_control.ipc.peer_uid',return_value=real_uid+1):
            with self.assertRaises(PermissionError):
                self.client.call('DISARM',self.actor,'id')
        self.engine.request.assert_not_called()

    def test_unbound_or_replayed_reply_is_refused(self):
        real_receive=receive

        def tamper(stream,**kwargs):
            value=real_receive(stream,**kwargs)
            if 'ok' in value:value['nonce']='0'*32
            return value

        with patch('disarm_control.ipc.receive',side_effect=tamper):
            with self.assertRaisesRegex(ValueError,'Unbound'):
                self.client.call('DISARM',self.actor,'id')

    def test_client_rejects_symlink_socket(self):
        linked=Path(self.tmp.name)/'link.sock'
        linked.symlink_to(self.path)
        with self.assertRaises(PermissionError):
            OwnerClient(linked).call('DISARM',self.actor,'id')
        self.engine.request.assert_not_called()

    def test_receive_enforces_total_deadline_for_slow_chunks(self):
        stream=Mock()
        stream.recv.return_value=b' '
        with patch('disarm_control.ipc.time.monotonic',side_effect=[0,.1,.8]):
            with self.assertRaises(TimeoutError):
                receive(stream)
        stream.recv.assert_called_once()

    def test_ambiguous_or_nonstandard_json_is_rejected(self):
        for raw in (b'{"op":"ARM","op":"DISARM"}\n',b'{"actor_id":NaN}\n',
                    b'{"actor_id":Infinity}\n'):
            stream=Mock()
            stream.recv.return_value=raw
            with self.assertRaises(ValueError):
                receive(stream)

    def test_server_start_failure_removes_only_own_socket(self):
        path=Path(self.tmp.name)/'failed.sock'
        server=OwnerServer(self.engine,path)
        with patch('disarm_control.ipc.Thread.start',side_effect=RuntimeError('no thread')):
            with self.assertRaises(RuntimeError):
                server.start()
        self.assertFalse(path.exists())
        server.close()
