"""Run with the candidate frontend and root navigation paths on PYTHONPATH."""

from functools import partial
from importlib.util import module_from_spec, spec_from_file_location
from hashlib import sha256
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
from navigation.dynamic_mission import Blocked, ground_location
from navigation.mission_transfer import equivalent
from navigation.mission_owner import MissionOwner
from test_dynamic_mission import HOME, ROOT


class MissionIPCTest(unittest.TestCase):
    def test_hmi_round_trip_backup_upload_readback_and_unadmitted_auto(self):
        spec = spec_from_file_location('disarm_control.owner_ipc',
            ROOT / 'hold_only_reconnect/package/disarm_control/ipc.py')
        owner_ipc = module_from_spec(spec)
        spec.loader.exec_module(owner_ipc)
        directory = Path(self.enterContext(TemporaryDirectory()))
        config = json.loads((ROOT / 'navigation/dynamic_mission.json').read_text())
        config.update(axis_flu_deg=0, home_map_xy_m=[0, 0], surveyed_bounds_m=[0, 7.5],
                      survey=dict(home_map_zero=HOME, route_azimuth_deg=0, expected_ekf_origin=HOME))
        raw_map = (ROOT / 'navigation/nav_bench/marker_map.json').read_bytes()
        self.assertEqual(sha256(raw_map).hexdigest(), 'b1a0707d53614c83869e4adffd5ab0d82a9a593969cc1fd606c4b0fabe49039d')
        catalog = json.loads((ROOT / 'navigation/mission-survey/marker-catalog.json').read_bytes())
        self.assertEqual(catalog['confirmed_sections'], [])
        reader = NS(now=10., lock=RLock(), state='READING', source=(1, 1), messages={}, heartbeat_safety_serial=1)
        reader.clock = lambda: reader.now
        alignment = dict(verified=True)
        position = dict(x=-.08, y=-.06)
        nav = NS(tx=False, alignment_check=lambda: alignment['verified'],
                 route_pose=lambda: dict(acquired_at=reader.now, **position, q=[1.,0.,0.,0.], source_marker=23))
        evidence = dict(tx_enabled=False, pose_fresh=True, sent=0, tx_fresh=False, nav_bench_tx=False)
        nav.snapshot = lambda: evidence
        diag = NS(home=HOME, origin=HOME, readback_epoch=1)
        outbound, stops = [], []
        decoder = mav.MAVLink(None)

        def write(fd, data):
            outbound.extend(decoder.parse_buffer(data))
            return len(data)

        owner = MissionOwner(reader, nav, diag, mav.MAVLink(None, srcSystem=255, srcComponent=190),
            config, json.loads(raw_map), directory, lambda: True, lambda fd, target: stops.append(('HOLD', target)),
            surveyed_map=True, write=write, require_live_tx=True, catalog=catalog, static_upload=True,
            latch_stop=lambda: stops.append(('STOP_LATCH', None)), brake_command=lambda: 'SENT')
        hb = NS(autopilot=3, type=10, base_mode=1, custom_mode=4)
        for _ in range(9):
            reader.now += .1
            reader.messages.update(HEARTBEAT=(reader.now, hb), EKF_STATUS_REPORT=(reader.now, NS(flags=167)))
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
        env.store.add_user("testsuperadmin", PASSWORD, "superadmin")
        client = create_app(config={**CONFIG, 'MISSION_OWNER_ENABLED': True}, auth_store=env.store,
            disarm_client=OwnerClient(directory/'ingress.sock')).test_client()
        token = re.search(r'name="csrf_token" value="([^"]+)"', client.get('/login').text)[1]
        self.assertEqual(client.post('/login', data=dict(username='testsuperadmin', password=PASSWORD,
                                                        csrf_token=token)).status_code, 303)
        page = client.get('/').text
        self.assertIn('Реальна місія ArUco', page)
        self.assertIn('Місія симулятора', page)
        self.assertIn('id="route-items"', page)
        self.assertIn('dynamic_mission.js?v=stage2-id25', page)
        ordinary = create_app(config={**CONFIG, 'MISSION_OWNER_ENABLED': True}, auth_store=env.store,
                              disarm_client=OwnerClient(directory/'ingress.sock')).test_client()
        token = re.search(r'name="csrf_token" value="([^"]+)"', ordinary.get('/login').text)[1]
        self.assertEqual(ordinary.post('/login', data=dict(username='testadmin', password=PASSWORD,
                                                          csrf_token=token)).status_code, 303)
        self.assertNotIn('id="mission-route"', ordinary.get('/').text)
        self.assertEqual(ordinary.get('/api/pixhawk/mission/status').status_code, 403)
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', page)[1]
        route = dict(start='home', target_type='marker', marker=25, round_trip=True, via_markers=[23, 24])
        self.assertEqual(client.post('/api/pixhawk/mission/request', json=route).status_code, 403)
        headers = {'X-CSRF-Token': csrf}
        evidence['sent'] = 1
        self.assertEqual(client.post('/api/pixhawk/mission/request', json=route, headers=headers).status_code, 409)
        evidence['sent'] = 0
        diag.home = None
        self.assertEqual(client.post('/api/pixhawk/mission/request', json=route, headers=headers).status_code, 409)
        diag.home = HOME
        self.assertEqual(outbound, [])
        alignment['verified'] = False
        self.assertEqual(client.post('/api/pixhawk/mission/request', json=route, headers=headers).status_code, 409)
        alignment['verified'] = True
        for _ in range(8):
            reader.now += .1
            reader.messages.update(HEARTBEAT=(reader.now, hb), EKF_STATUS_REPORT=(reader.now, NS(flags=167)))
            owner.tick(-1)
        # Publish a new frame after the owner tick and before the HMI poll.
        reader.now += .05
        self.assertTrue(client.get('/api/pixhawk/mission/status').json['mission_upload_ready'])
        preview = client.post('/api/pixhawk/mission/preview', json=route, headers=headers).json
        self.assertEqual(preview['state'], 'DRAFT')
        self.assertEqual(preview['route_labels'], ['HOME', 'ID23', 'ID24', 'ID25', 'ID24', 'ID23', 'HOME'])
        self.assertEqual(preview['target_m'], 7.5)
        self.assertFalse(preview['execution_coverage_confirmed'])
        self.assertEqual(preview['target_map_xy_m'], [7.5, -0.03854])
        self.assertEqual((preview['current_m'], preview['start_m']), (-.08, 0))
        self.assertEqual(client.post('/api/pixhawk/mission/preview',
            json=dict(start='current', target_type='marker', marker=25), headers=headers).json['error'],
            'Позиція поза виміряною ділянкою')
        # A new frame displaced beyond the stationarity limit still blocks storage.
        reader.now += .05
        position['x'] = -.11
        self.assertFalse(client.get('/api/pixhawk/mission/status').json['mission_upload_ready'])
        position['x'] = -.08
        for _ in range(12):
            reader.now += .1
            reader.messages.update(HEARTBEAT=(reader.now, hb))
            owner.tick(-1)
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
            reader.messages.update(HEARTBEAT=(reader.now, hb), EKF_STATUS_REPORT=(reader.now, NS(flags=167)))
            owner.tick(-1)

        original_backup = 'mission-00000000-0000-4000-8000-000000000001.json'
        original_items = [{k: v for k, v in old[0].items() if not k.startswith('target_')}]
        (directory / original_backup).write_text(json.dumps({'items': original_items}))
        recovery = directory / 'mission-recovery.json'
        recovery.write_text(json.dumps({'backup': original_backup}))
        accepted = client.post('/api/pixhawk/mission/request', json=route, headers=headers)
        self.assertEqual((accepted.status_code, accepted.json['state']), (202, 'DRAFT'))
        self.assertEqual(outbound, [])  # HTTP and IPC never send directly.

        for _ in range(10):
            tick()
            while outbound:
                message = outbound.pop(0)
                kind = message.get_type()
                if kind == 'MISSION_REQUEST_LIST':
                    reply(mav.MAVLink_mission_count_message(255, 190, len(old), 0))
                elif kind == 'MISSION_REQUEST_INT':
                    reply(mav.MAVLink_mission_item_int_message(**old[message.seq]))
                else:
                    self.assertEqual(kind, 'MISSION_ACK')  # No write protocol.
            if not recovery.exists():
                break
        self.assertEqual(owner.reason, 'RECOVERY_BASELINE_CONFIRMED')
        self.assertFalse(recovery.exists())

        accepted = client.post('/api/pixhawk/mission/request', json=route, headers=headers)
        self.assertEqual((accepted.status_code, accepted.json['state']), (202, 'DRAFT'))
        self.assertEqual(outbound, [])  # HTTP and IPC never send directly.

        states = set()
        for _ in range(60):
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
                    self.assertEqual(message.count, 9)
                    backup = owner.transfer.backup
                    self.assertEqual(json.loads(backup.read_text())['items'][0]['x'], old[0]['x'])
                    self.assertEqual(json.loads(recovery.read_text())['backup'], backup.name)
                    reply(mav.MAVLink_mission_request_message(255, 190, 0, 0))
                elif kind == 'MISSION_ITEM_INT':
                    item = message.to_dict()
                    item.pop('mavpackettype')
                    item.update(target_system=255, target_component=190)
                    staged.append(item)
                    if len(staged) == 9:
                        stored = staged
                        reply(mav.MAVLink_mission_ack_message(255, 190, 0, 0))
                    else:
                        reply(mav.MAVLink_mission_request_message(255, 190, len(staged), 0))
                else:
                    self.assertEqual(kind, 'MISSION_ACK')
            if owner.state == 'VERIFIED':
                break
        self.assertIn('UPLOADED', states)
        status = client.get('/api/pixhawk/mission/status').json
        self.assertEqual(status['state'], 'VERIFIED')
        self.assertIs(status['traction_readiness']['ready'], False)
        self.assertIs(status['mission_upload_ready'], True)
        self.assertIs(status['mission_execution_ready'], False)
        self.assertIs(status['motion_authorized'], False)
        self.assertIs(status['execution_coverage_confirmed'], False)
        self.assertEqual(status['brake_command'], 'SENT')
        self.assertEqual(status['brake_status'], 'UNKNOWN')
        self.assertEqual(status['drive_ready'], 'UNKNOWN')
        # Exercise a future trusted provider without wiring one into the candidate.
        original_traction = owner.traction_readiness
        original_safety = owner.safety_ready
        owner.traction_readiness = lambda: dict(ready=True, reason=None)
        owner.safety_ready = lambda: False
        self.assertFalse(owner.snapshot()['mission_upload_ready'])
        self.assertFalse(owner.snapshot()['mission_execution_ready'])
        nav.tx = True
        self.assertFalse(owner.snapshot()['mission_execution_ready'])
        with self.assertRaises(Blocked):
            owner.admit_execution()
        evidence['tx_fresh'] = False
        self.assertFalse(owner.snapshot()['mission_execution_ready'])
        evidence['tx_fresh'] = True
        owner.traction_readiness = original_traction
        owner.safety_ready = original_safety
        with self.assertRaises(Blocked):
            owner.admit_execution()
        self.assertEqual(status['verified_readback'], {
            'item_count': 9, 'reverse_commands': [{'seq': 1, 'value': 0}, {'seq': 5, 'value': 1}],
        })
        for seq, along, lateral in [(2, 2.5, 0), (3, 5, -.00822), (4, 7.5, -.03854),
                                    (6, 5, -.00822), (7, 2.5, 0), (8, 0, 0)]:
            expected = ground_location(config, along, lateral)
            self.assertEqual((stored[seq]['x'], stored[seq]['y']), (expected['x'], expected['y']))
        for seq, field, wrong in [(5, 'param1', 0), (6, 'y', stored[6]['y'] + 1)]:
            observed = [dict(item) for item in stored]
            observed[seq][field] = wrong
            self.assertFalse(equivalent(owner.transfer.expected, observed))
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
