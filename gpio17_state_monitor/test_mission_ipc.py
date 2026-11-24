from contextlib import closing
import os
from pathlib import Path
import socket
import tempfile
from threading import Thread
import unittest
from unittest.mock import Mock
from uuid import uuid4

from disarm_control.ipc import SocketPolicy
from production_stop.mission_ipc import MissionIPCClient, MissionIPCEndpoint
from production_stop.mission_resume import ResumeAcceptance


class MissionIPCTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = folder / 'mission.sock'
        self.listener = self.enterContext(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        self.listener.bind(str(self.path))
        self.path.chmod(0o600)
        self.listener.listen()
        self.client = MissionIPCClient(self.path, SocketPolicy(os.getuid()), 'mission-a')
        self.state = Mock(return_value=('PAUSED', 10.0))
        self.ready = Mock(return_value=True)
        self.endpoint = self.enterContext(closing(
            MissionIPCEndpoint(os.getuid(), folder / 'ledger.db', self.state, self.ready)))
        self.adapter = self.client.adapter(lambda: None, lambda: 10.0)
        self.reset, self.request_id = str(uuid4()), str(uuid4())
        self.endpoint.allow_reset('mission-a', self.reset)

    def exchange(self, op='resume', *, handler=None, request_id=None):
        handler = handler or self.endpoint.serve_connection

        def serve():
            connection, _ = self.listener.accept()
            with connection:
                handler(connection)

        thread = Thread(target=serve, daemon=True)
        thread.start()
        result = getattr(self.adapter, op)(request_id or self.request_id, self.reset)
        thread.join(1)
        self.assertFalse(thread.is_alive())
        return result

    def test_authentication_rejects_wrong_client_uid(self):
        self.endpoint.owner_uid = os.getuid() + 1
        self.assertIs(self.exchange(), ResumeAcceptance.UNKNOWN)
        self.ready.assert_not_called()

    def test_ack_and_status_are_separate(self):
        self.assertIs(self.exchange(), ResumeAcceptance.ACCEPTED)
        self.state.assert_not_called()
        self.assertEqual(self.exchange('mission_state'), 'PAUSED')
        self.assertEqual(self.endpoint.take_pending(), ('mission-a', self.reset, self.request_id))
        self.state.return_value = ('RUNNING', 10.0)
        self.assertEqual(self.exchange('mission_state'), 'RUNNING')

    def test_no_hardware_provider_refuses(self):
        self.endpoint.physical_ready = None
        self.assertIs(self.exchange(), ResumeAcceptance.REFUSED)
        self.assertIsNone(self.endpoint.take_pending())

    def test_mission_binding_and_replay(self):
        self.endpoint.allow_reset('mission-b', self.reset)
        self.assertIs(self.exchange(), ResumeAcceptance.REFUSED)
        self.endpoint.allow_reset('mission-a', self.reset)
        self.assertIs(self.exchange(), ResumeAcceptance.ACCEPTED)
        self.assertIs(self.exchange(), ResumeAcceptance.REFUSED)
        self.assertIs(self.exchange(request_id=str(uuid4())), ResumeAcceptance.REFUSED)

    def test_new_stop_invalidates_accepted_pending_ticket(self):
        self.assertIs(self.exchange(), ResumeAcceptance.ACCEPTED)
        self.endpoint.front_pressed()
        self.assertIsNone(self.endpoint.take_pending())
        self.assertIs(self.exchange(request_id=str(uuid4())), ResumeAcceptance.REFUSED)

    def test_timeout_does_not_retry(self):
        from disarm_control.ipc import receive
        from time import sleep

        def hold_reply(connection):
            receive(connection)
            sleep(.22)

        handler = Mock(side_effect=hold_reply)
        self.assertIs(self.exchange(handler=handler), ResumeAcceptance.UNKNOWN)
        handler.assert_called_once()

    def test_connection_loss_returns_unknown(self):
        self.assertIs(self.exchange(handler=lambda connection: None), ResumeAcceptance.UNKNOWN)
        self.listener.close()
        self.assertIs(self.adapter.resume(self.request_id, self.reset), ResumeAcceptance.UNKNOWN)


if __name__ == '__main__':
    unittest.main()
