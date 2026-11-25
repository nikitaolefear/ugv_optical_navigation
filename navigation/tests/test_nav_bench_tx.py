import ast
from hashlib import sha256
import json
import math
from pathlib import Path
import sys
from threading import RLock
from types import SimpleNamespace
import unittest

from pymavlink.dialects.v20 import ardupilotmega as mav
from stop_latch_test.diagnostics import Diagnostics, LOCAL_BENCH_ORIGIN_RAW, SRC2
from stop_latch_test.externalnav import ExternalNav, static_planar_message

ROOT = Path(__file__).resolve().parents[2]
WORKER_MAP = ROOT / 'navigation/nav_bench/marker_map.json'
OPTICAL = ROOT / 'hold_only_reconnect/package/optical'


class NavBenchTXTests(unittest.TestCase):
    def test_mission_session_transmits_past_two_minutes_and_stops_at_lease_expiry(self):
        reader = SimpleNamespace(lock=RLock(), now=10.)
        reader.clock = lambda: reader.now
        writes = []
        nav = ExternalNav(reader, write=lambda _, data: writes.append(data) or len(data))
        parameters = {f'EK3_SRC{i}_{field}': 0 for i in (1, 2, 3) for field in ('POSZ', 'VELZ', 'VELXY')}
        parameters.update(AHRS_EKF_TYPE=3, EK3_ENABLE=1, EK3_SRC_OPTIONS=0, EK3_SRC2_POSXY=6,
                          EK3_SRC2_YAW=6, EK3_SRC2_POSZ=1, VISO_TYPE=1, VISO_QUAL_MIN=0,
                          VISO_POS_X=0, VISO_POS_Y=0, VISO_POS_Z=0)
        parameters = {name: {'value': value} for name, value in parameters.items()}
        origin = dict(latitude_deg=45, longitude_deg=15, altitude_msl_m=0)
        record = {'sha256': {'marker_map.json': sha256(WORKER_MAP.read_bytes()).hexdigest()},
                  'nav_mission': dict(vesc_physically_isolated=True, expires_monotonic=141,
                                      ekf_origin_wgs84_msl=[45, 15, 0], datum_to_ekf_ned=[0, 0, 0])}
        nav.static_writer = lambda pose, now: static_planar_message(
            pose, now, origin, parameters, record, mission_session=True)
        pose = dict(source_marker=23, x=2.5, y=0., z=0., q=[1., 0., 0., 0.])
        for index in range(1048):
            reader.now = 10 + index * .125
            nav.submit(dict(pose, acquired_at=reader.now))
            nav.tick(-1)
        self.assertGreater(reader.now - 10, 120)
        self.assertEqual(len(writes), 1048)
        reader.now = 141
        nav.submit(dict(pose, acquired_at=reader.now))
        nav.tick(-1)
        self.assertTrue(nav.static_revoked)
        record['nav_mission']['expires_monotonic'] = 200
        reader.now += .125
        nav.submit(dict(pose, acquired_at=reader.now))
        nav.tick(-1)
        self.assertEqual(len(writes), 1048)

    def test_one_shot_origin_and_src2_require_distinct_readbacks(self):
        reader = SimpleNamespace(lock=RLock(), now=10., heartbeat_safety_serial=1, source=(1, 1))
        reader.clock = lambda: reader.now
        out, claims = [], []
        inbound = mav.MAVLink(None, srcSystem=1, srcComponent=1)
        decoder = mav.MAVLink(None)
        reply_decoder = mav.MAVLink(None)

        def reply(diagnostics, message):
            diagnostics.observe(reply_decoder.parse_buffer(message.pack(inbound))[0])

        def initiate(action):
            diag = Diagnostics(reader, mav.MAVLink(None, srcSystem=255, srcComponent=190),
                               navigation_readback=True,
                               write=lambda _, packet: out.extend(decoder.parse_buffer(packet)) or len(packet))
            diag.configure_local_ekf(action, lambda _: True, lambda: claims.append(action))
            diag._tick_local_ekf(-1, False)
            self.assertEqual(out[-1].command, mav.MAV_CMD_REQUEST_MESSAGE)
            self.assertEqual(out[-1].param1, 49)
            return diag

        origin = initiate('origin')
        reader.now += 3.1
        origin._tick_local_ekf(-1, False)
        self.assertEqual((out[-1].latitude, out[-1].longitude, out[-1].altitude), LOCAL_BENCH_ORIGIN_RAW)
        self.assertEqual(origin.local_ekf_state, 'WAIT_ID49')
        reader.now += .1
        reply(origin, mav.MAVLink_gps_global_origin_message(*LOCAL_BENCH_ORIGIN_RAW))
        self.assertEqual(origin.local_ekf_state, 'ORIGIN_CONFIRMED')
        self.assertFalse(origin._tick_local_ekf(-1, False))

        src2 = initiate('src2')
        reader.now += .1
        reply(src2, mav.MAVLink_gps_global_origin_message(*LOCAL_BENCH_ORIGIN_RAW))
        src2._tick_local_ekf(-1, False)
        self.assertEqual((out[-1].command, out[-1].param1), (SRC2, 2))
        reader.now += .1
        reply(src2, mav.MAVLink_command_ack_message(SRC2, mav.MAV_RESULT_ACCEPTED,
                                                  target_system=255, target_component=190))
        self.assertEqual(src2.local_ekf_state, 'SRC2_ACK_ACCEPTED_SOURCE_UNVERIFIED')
        self.assertFalse(src2._tick_local_ekf(-1, False))
        self.assertEqual(claims, ['origin', 'src2'])

        mismatch = initiate('src2')
        reader.now += .1
        reply(mismatch, mav.MAVLink_gps_global_origin_message(500000000, 300000000, 0))
        self.assertEqual(mismatch.local_ekf_state, 'BLOCKED_ORIGIN_MISMATCH')
        self.assertFalse(mismatch._tick_local_ekf(-1, False))

    def test_candidate_map_matches_worker_format_and_calibration(self):
        from pose_transform_3d import marker_map_entry_to_world_transform
        from navigation.externalnav_bench_gate import candidate_map, map_bytes

        source = OPTICAL / 'aruco_active_marker_selector_v8.py'
        loaders = [node for node in ast.parse(source.read_text()).body
                   if isinstance(node, ast.FunctionDef)
                   and node.name in ('validate_markers', 'load_marker_map')]
        namespace = dict(json=json, math=math, sys=sys)
        exec(compile(ast.Module(body=loaders, type_ignores=[]), str(source), 'exec'), namespace)
        mapping = namespace['load_marker_map'](WORKER_MAP)
        draft = json.loads((ROOT / 'navigation/aruco_station_candidate_23_25.json').read_text())
        self.assertEqual(WORKER_MAP.read_bytes(), map_bytes(candidate_map(draft)['map']))
        legacy = {row['id']: row for row in namespace['load_marker_map'](OPTICAL / 'marker_map.json')['markers']}
        self.assertEqual([row['id'] for row in mapping['markers']], [23, 24, 25])
        for row, station in zip(mapping['markers'], (2.5, 5.0, 7.5)):
            self.assertEqual(row['position']['x'], station)
            self.assertEqual(row['orientation'], legacy[row['id']]['orientation'])
            transform = marker_map_entry_to_world_transform(row)
            self.assertEqual(transform[:3, 3].tolist(), [row['position'][axis] for axis in 'xyz'])
            self.assertAlmostEqual(math.degrees(math.atan2(transform[1, 0], transform[0, 0])),
                                   row['orientation']['yaw'])
        self.assertEqual(json.loads((OPTICAL / 'rover_config.json').read_text())['marker_size'], .283)
        self.assertEqual(sha256((OPTICAL / 'camera_calibration_fisheye.npz').read_bytes()).hexdigest(),
                         draft['provenance']['camera_calibration_sha256'])
        self.assertEqual(sha256((OPTICAL / 'pose_transform_2d.py').read_bytes()).hexdigest(),
                         draft['provenance']['camera_extrinsic_sha256'])

    def test_owner_tick_sends_only_planar_pose_and_latches_loss(self):
        for lost in ('pose', 'alignment', 'priority'):
            with self.subTest(lost=lost):
                reader = SimpleNamespace(lock=RLock(), now=10.)
                reader.clock = lambda: reader.now
                writes = []
                nav = ExternalNav(reader, write=lambda _, data: writes.append(data) or len(data))
                params = {f'EK3_SRC{i}_{field}': 0
                          for i in (1, 2, 3) for field in ('POSZ', 'VELZ', 'VELXY')}
                params.update(AHRS_EKF_TYPE=3, EK3_ENABLE=1, EK3_SRC_OPTIONS=0, EK3_SRC2_POSXY=6,
                              EK3_SRC2_YAW=6, EK3_SRC2_POSZ=1, VISO_TYPE=1, VISO_QUAL_MIN=0,
                              VISO_POS_X=0, VISO_POS_Y=0, VISO_POS_Z=0)
                params = {name: {'value': value} for name, value in params.items()}
                origin = dict(latitude_deg=49, longitude_deg=29, altitude_msl_m=100)
                record = {'sha256': {'marker_map.json': sha256(WORKER_MAP.read_bytes()).hexdigest()},
                          'nav_bench': dict(vesc_physically_isolated=True, expires_monotonic=20,
                                            ekf_origin_wgs84_msl=[49, 29, 100],
                                            datum_to_ekf_ned=[2, 3, 90])}
                nav.static_writer = lambda pose, now: static_planar_message(pose, now, origin, params, record)
                pose = dict(source_marker=23, x=2.5, y=0., z=0., q=[1., 0., 0., 0.])
                nav.submit(dict(pose, acquired_at=9.95))
                nav.tick(-1)
                self.assertEqual(len(writes), 1)
                message = mav.MAVLink(None).parse_buffer(writes[0])[0]
                self.assertEqual(message.get_type(), 'VISION_POSITION_ESTIMATE')
                self.assertEqual((message.usec, message.x, message.y), (9950000, 2., 5.5))
                reader.now += .3
                if lost != 'pose':
                    nav.submit(dict(pose, acquired_at=reader.now))
                if lost == 'alignment':
                    record['sha256'].clear()
                nav.tick(-1, command_pending=lost == 'priority')
                self.assertTrue(nav.static_revoked)
                reader.now += .1
                nav.submit(dict(pose, acquired_at=reader.now))
                nav.tick(-1)
                self.assertEqual(len(writes), 1)
