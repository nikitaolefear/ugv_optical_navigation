"""Run with the candidate frontend and root navigation paths on PYTHONPATH."""

from functools import partial
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import RLock
from types import SimpleNamespace as NS
import unittest

from disarm_control.gateway import CommandGateway
from disarm_control.ipc import MISSION_OPERATIONS, OwnerClient, OwnerServer
from pymavlink.dialects.v20 import ardupilotmega as mav
from web_hmi.app import create_app
from web_hmi.test_support import CONFIG, PASSWORD, TestEnvironment
from navigation.dynamic_mission import Blocked
from navigation.mission_owner import MissionOwner
from test_dynamic_mission import FIXTURE_MAP, HOME, ROOT


class MissionIPCTest(unittest.TestCase):
    def test_hmi_round_trip_backup_upload_readback_and_unadmitted_auto(self):
        spec = spec_from_file_location('disarm_control.owner_ipc',
            ROOT / 'hold_only_reconnect/package/disarm_control/ipc.py')
        owner_ipc = module_from_spec(spec)
        spec.loader.exec_module(owner_ipc)
        directory = Path(self.enterContext(TemporaryDirectory()))
        config = json.loads((ROOT / 'navigation/dynamic_mission.json').read_text())
        config.update(axis_flu_deg=0, home_map_xy_m=[0, 0], surveyed_bounds_m=[0, 5],
                      survey=dict(home_map_zero=HOME, route_azimuth_deg=0, expected_ekf_origin=HOME))
        reader = NS(now=10., lock=RLock(), state='READING', source=(1, 1), messages={}, heartbeat_safety_serial=1)
        reader.clock = lambda: reader.now
        nav = NS(tx=False, alignment_check=lambda: True,
                 route_pose=lambda: dict(acquired_at=reader.now, x=0., y=0., q=[1.,0.,0.,0.], source_marker=25))
        evidence = dict(tx_enabled=True, pose_fresh=True, sent=1)
        nav.snapshot = lambda: evidence
        diag = NS(home=HOME, origin=HOME, readback_epoch=1)
        outbound, stops = [], []
        decoder = mav.MAVLink(None)

        def write(fd, data):
            outbound.extend(decoder.parse_buffer(data))
            return len(data)

        owner = MissionOwner(reader, nav, diag, mav.MAVLink(None, srcSystem=255, srcComponent=190),
            config, FIXTURE_MAP, directory, lambda: True, lambda fd, target: stops.append(('HOLD', target)),
            surveyed_map=True, write=write, require_live_tx=True,
            latch_stop=lambda: stops.append(('STOP_LATCH', None)))
        hb = NS(autopilot=3, type=10, base_mode=1, custom_mode=4)
        for _ in range(9):
            reader.now += .1
            reader.messages.update(HEARTBEAT=(reader.now, hb), EKF_STATUS_REPORT=(reader.now, NS(flags=31)))
            owner.tick(-1)

        def operations(handler):
            return {op: partial(handler, op) for op in MISSION_OPERATIONS}

        backend = owner_ipc.OwnerServer(None, directory/'owner.sock', mission_operations=operations(owner.command),
                                        disarm_request=lambda *_: None)
        backend.start()
        self.addCleanup(backend.close)
        gateway = CommandGateway(OwnerClient(directory/'owner.sock'))
        ingress = OwnerServer(gateway, directory/'ingress.sock', mission_operations=operations(gateway.mission_request))
        ingress.start()
        self.addCleanup(ingress.close)
        env = TestEnvironment()
        self.addCleanup(env.close)
        with env.store.transaction() as db:
            db.execute("INSERT INTO role_permissions VALUES('admin','pixhawk.mission')")
        client = create_app(config={**CONFIG, 'MISSION_OWNER_ENABLED': True}, auth_store=env.store,
            disarm_client=OwnerClient(directory/'ingress.sock')).test_client()
        token = re.search(r'name="csrf_token" value="([^"]+)"', client.get('/login').text)[1]
        self.assertEqual(client.post('/login', data=dict(username='testadmin', password=PASSWORD,
                                                        csrf_token=token)).status_code, 303)
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', client.get('/').text)[1]
        route = dict(start='home', target_type='marker', marker=23, round_trip=True)
        self.assertEqual(client.post('/api/pixhawk/mission/request', json=route).status_code, 403)
        headers = {'X-CSRF-Token': csrf}
        evidence['tx_enabled'] = False
        self.assertEqual(client.post('/api/pixhawk/mission/request', json=route, headers=headers).json['state'], 'BLOCKED')
        self.assertEqual(outbound, [])
        evidence['tx_enabled'] = True
        self.assertEqual(client.post('/api/pixhawk/mission/preview', json=route, headers=headers).json['state'], 'DRAFT')
        accepted = client.post('/api/pixhawk/mission/request', json=route, headers=headers)
        self.assertEqual((accepted.status_code, accepted.json['state']), (202, 'DRAFT'))
        self.assertEqual(outbound, [])  # HTTP and IPC never send directly.

        old = [dict(seq=0, frame=5, command=16, current=0, autocontinue=1,
                    param1=0, param2=0, param3=0, param4=0, x=400000000, y=250000000, z=100,
                    target_system=255, target_component=190, mission_type=0)]
        stored, staged = old, []
        fake = mav.MAVLink(None, srcSystem=1, srcComponent=1)
        receiver = mav.MAVLink(None)

        def reply(message):
            owner.observe(receiver.parse_buffer(message.pack(fake))[0])

        def tick():
            reader.now += .1
            reader.messages.update(HEARTBEAT=(reader.now, hb), EKF_STATUS_REPORT=(reader.now, NS(flags=31)))
            owner.tick(-1)

        states = set()
        for _ in range(45):
            tick()
            states.add(owner.state)
            while outbound:
                message = outbound.pop(0)
                kind = message.get_type()
                if kind == 'MISSION_REQUEST_LIST':
                    reply(mav.MAVLink_mission_count_message(255, 190, len(stored), 0))
                elif kind == 'MISSION_REQUEST_INT':
                    reply(mav.MAVLink_mission_item_int_message(**stored[message.seq]))
                elif kind == 'MISSION_COUNT':
                    self.assertEqual(message.count, 5)
                    backups = list(directory.glob('mission-*.json'))
                    self.assertEqual(len(backups), 1)
                    self.assertEqual(json.loads(backups[0].read_text())['items'][0]['x'], old[0]['x'])
                    reply(mav.MAVLink_mission_request_int_message(255, 190, 0, 0))
                elif kind == 'MISSION_ITEM_INT':
                    item = message.to_dict()
                    item.pop('mavpackettype')
                    item.update(target_system=255, target_component=190)
                    staged.append(item)
                    if len(staged) == 5:
                        stored = staged
                        reply(mav.MAVLink_mission_ack_message(255, 190, 0, 0))
                    else:
                        reply(mav.MAVLink_mission_request_int_message(255, 190, len(staged), 0))
                else:
                    self.assertEqual(kind, 'MISSION_ACK')
            if owner.state == 'VERIFIED':
                break
        self.assertIn('UPLOADED', states)
        status = client.get('/api/pixhawk/mission/status').json
        self.assertEqual(status['state'], 'VERIFIED')
        self.assertIs(status['traction_readiness']['ready'], False)
        nav.tx = True
        with self.assertRaises(Blocked):
            owner.admit_execution()
        self.assertEqual(stored[1]['param1'], 0)
        self.assertEqual(stored[3]['param1'], 1)
        self.assertEqual((stored[4]['x'], stored[4]['y']), (old[0]['x'], old[0]['y']))
        self.assertEqual(stops, [])
        hb.custom_mode = 10
        tick()
        self.assertEqual(owner.state, 'HOLD')
        self.assertEqual(stops, [('STOP_LATCH', None), ('HOLD', (1, 1))])
        self.assertFalse(owner.snapshot()['hold_observed'])
        evidence['tx_enabled'] = False
        hb.custom_mode = 4
        tick()
        self.assertFalse(owner.snapshot()['hold_observed'])
        for _ in range(8):
            tick()
        self.assertTrue(owner.snapshot()['hold_observed'])
        self.assertEqual(owner.state, 'FAILED')
