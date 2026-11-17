"""Authorized Flask -> private Unix socket -> owner -> mocked serial writer."""
from pathlib import Path
import re
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import uuid

from disarm_control.engine import DisarmEngine, disarm_sender
from disarm_control.ipc import OwnerClient, OwnerServer
from disarm_control.gateway import CommandGateway
from web_hmi.app import create_app
from web_hmi.test_support import TestEnvironment, CONFIG, PASSWORD


class EndToEndTests(unittest.TestCase):
    def test_authorized_disarm_reports_sent_then_observed_without_causal_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [10.0]
            heartbeat = SimpleNamespace(autopilot=3, type=10, base_mode=128,
                get_srcSystem=lambda:1, get_srcComponent=lambda:1)
            reader = SimpleNamespace(lock=threading.RLock(), state='READING',
                messages={'HEARTBEAT':(10.0, heartbeat)}, source=(1,1), timeout=3., heartbeat_serial=1)
            bench = SimpleNamespace(phase='PENDING',latched=True,run='STOPPED')
            write = Mock(side_effect=lambda fd,data:len(data))
            engine = DisarmEngine(reader,bench,disarm_sender(write=write),
                                 Path(directory)/'audit.sqlite3',clock=lambda:now[0])
            self.addCleanup(engine.close)
            server = OwnerServer(engine,Path(directory)/'control.sock')
            server.start()
            gateway = CommandGateway(OwnerClient(Path(directory)/'control.sock'),
                                     enabled=True, clock=lambda: now[0])
            ingress = OwnerServer(gateway, Path(directory)/'gateway.sock')
            ingress.start()
            try:
                env = TestEnvironment()
                with env.store.transaction() as db:
                    db.execute("INSERT INTO role_permissions VALUES('admin','pixhawk.disarm')")
                try:
                    client = create_app(config=dict(CONFIG,DISARM_ENABLED=True,DISARM_CLOCK=lambda: now[0]),auth_store=env.store,
                        disarm_client=OwnerClient(Path(directory)/'gateway.sock')).test_client()
                    token = re.search(r'name="csrf_token" value="([^"]+)"',client.get('/login').text)[1]
                    self.assertEqual(client.post('/login',data=dict(username='testadmin',password=PASSWORD,
                        csrf_token=token)).status_code,303)
                    csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"',client.get('/').text)[1]
                    rid=str(uuid.uuid4())
                    response=client.post('/api/pixhawk/disarm',json={'request_id':rid},headers={'X-CSRF-Token':csrf})
                    self.assertEqual(response.json['state'],'QUEUED')
                    self.assertEqual(bench.phase,'CANCELLED_DISARM')
                    write.assert_not_called()
                    engine.tick(123)
                    self.assertEqual(client.get('/api/pixhawk/disarm/'+rid).json['delivery_state'],'SENT')
                    engine.observe(SimpleNamespace(get_type=lambda:'COMMAND_ACK', command=400,result=0,
                        get_srcSystem=lambda:1,get_srcComponent=lambda:1,target_system=255,target_component=190))
                    now[0]=10.1
                    heartbeat.base_mode=0
                    reader.messages['HEARTBEAT']=(now[0],heartbeat)
                    reader.heartbeat_serial+=1
                    engine.tick(123)
                    result=client.get('/api/pixhawk/disarm/'+rid).json
                    self.assertEqual(result['observation_state'],'DISARMED_OBSERVED')
                    self.assertEqual(result['command_outcome'],'UNKNOWN')
                    # An explicit repeat carries a new IPC nonce but the same
                    # durable UUID: never write a second MAVLink packet.
                    repeated=client.post('/api/pixhawk/disarm',json={'request_id':rid},headers={'X-CSRF-Token':csrf})
                    self.assertEqual(repeated.json['delivery_state'],'SENT')
                    engine.tick(123)
                    now[0]=13.2
                    self.assertEqual(client.get('/api/pixhawk/disarm/'+rid).json['observation_state'],'UNKNOWN')
                    self.assertTrue(bench.latched)
                    write.assert_called_once()
                    self.assertEqual(write.call_args.args[0],123)
                finally:
                    env.close()
            finally:
                ingress.close()
                server.close()
