import json
from dataclasses import replace
from pathlib import Path
import unittest

from navigation.dynamic_mission import (
    Blocked, MissionSession, OwnerEvidence, Pose, map_geometry, preview_route, route_geometry,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_MAP = {
    'coordinate_frame': 'HOME_FRONT_DATUM', 'position_unit': 'm',
    'markers': [{'id': ident, 'position': {'x': x, 'y': 0}}
                for ident, x in ((25, 0), (23, 5), (24, 2.5))],
}
HOME = {'latitude_deg': 40, 'longitude_deg': 25, 'altitude_msl_m': 100}


class DynamicMissionTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / 'navigation/dynamic_mission.json').read_text())
        self.config.update(axis_flu_deg=0, home_map_xy_m=[0, 0], surveyed_bounds_m=[0, 5],
                           survey=dict(home_map_zero=HOME, route_azimuth_deg=0, expected_ekf_origin=HOME))
        self.now = 10.0
        self.update_pose(0)
        self.hold_calls = []
        self.session = MissionSession(self.config, FIXTURE_MAP, lambda: self.evidence, lambda: self.now,
                                      request_hold=lambda: self.hold_calls.append(self.now))

    def update_pose(self, at_m, *, yaw=0):
        self.evidence = OwnerEvidence(Pose(at_m, 0, yaw, self.now - .03, 25),
                                      self.now - .05, self.now - .04, HOME, HOME)

    def make(self, **request):
        return self.session.prepare(request, create=True)

    def test_home_to_marker_and_reverse_to_home(self):
        result = self.make(start='home', target_type='marker', marker=23)
        self.assertEqual((result['target_m'], result['reverse']), (5, False))
        self.assertEqual(self.session.items[1]['param1'], 0)
        self.update_pose(5)
        second = MissionSession(self.config, FIXTURE_MAP, lambda: self.evidence, lambda: self.now)
        result = second.prepare(dict(start='current', target_type='marker', marker=25), create=True)
        self.assertEqual((result['target_m'], result['reverse']), (0, True))
        self.assertEqual(second.items[1]['param1'], 1)

    def test_signed_distances_and_no_turn(self):
        result = self.session.prepare(dict(start='home', target_type='distance', distance_m=5, direction='forward'))
        self.assertEqual((result['target_m'], result['reverse']), (5, False))
        self.update_pose(5)
        result = self.make(start='current', target_type='distance', distance_m=2.5, direction='reverse')
        self.assertEqual((result['target_m'], result['reverse']), (2.5, True))
        self.assertEqual(self.session.items[2]['x'], 400000225)

    def test_sequential_actual_stop_and_navigation_loss(self):
        self.make(start='home', target_type='marker', marker=23)
        self.session.owner_started()
        self.assertEqual(self.session.owner_progress(navigation_valid=True, finished=True), 'HOLD_REQUIRED')
        self.now += .2
        self.update_pose(4.95)
        self.assertEqual(self.session.owner_progress(navigation_valid=True), 'COMPLETED')
        self.assertEqual(self.session.snapshot()['stop']['route_m'], 4.95)
        with self.assertRaises(Blocked):
            self.make(start='current', target_type='distance', distance_m=2.5, direction='reverse')
        self.now += .1
        self.update_pose(4.94)
        result = self.make(start='current', target_type='distance', distance_m=2.5, direction='reverse')
        self.assertAlmostEqual(result['target_m'], 2.44)
        self.session.owner_started()
        self.now += .05
        self.assertEqual(self.session.owner_progress(navigation_valid=False), 'HOLD_REQUIRED')
        self.assertEqual(self.session.owner_progress(navigation_valid=True), 'HOLD_REQUIRED')
        self.assertEqual(len(self.hold_calls), 2)
        self.now += .25
        self.update_pose(4.6)
        self.assertEqual(self.session.owner_progress(navigation_valid=True), 'ABORTED')

    def test_route_and_evidence_guards(self):
        request = dict(start='current', target_type='distance', distance_m=5.5, direction='forward')
        with self.assertRaises(Blocked):
            self.session.prepare(request)
        request = dict(start='home', target_type='marker', marker=23)
        original = self.evidence
        for evidence in (replace(original, hold_at=None),
                         replace(original, pose=replace(original.pose, acquired_at=9.0)),
                         replace(original, pose=replace(original.pose, yaw_flu_deg=90))):
            with self.subTest(evidence=evidence):
                with self.assertRaises(Blocked):
                    preview_route(request, self.config, FIXTURE_MAP, evidence, self.now)
        self.evidence = replace(original, origin=None)
        self.assertTrue(self.session.prepare(request)['blocker'])
        with self.assertRaises(Blocked):
            self.session.prepare(request, create=True)
        actual_map = json.loads((ROOT / 'marker_map.json').read_text())
        _, order, steps = map_geometry(actual_map, 0)
        self.assertEqual(order, [23, 24, 25])
        self.assertEqual([round(step, 5) for step in steps], [1.04537, 1.04265])
        with self.assertRaisesRegex(Blocked, '2,5'):
            route_geometry(actual_map, self.config)

    def test_local_round_trip_repeated_without_restarting_owner(self):
        self.config['survey'] = dict(reference='LOCAL_HOME_NORTH', expected_ekf_origin=HOME)
        request = dict(start='home', target_type='marker', marker=23, round_trip=True)
        for _ in range(15):
            self.now += 1
            self.update_pose(0)
            self.session.prepare(request, create=True)
            items = self.session.items
            self.assertEqual([items[i]['param1'] for i in (1, 3)], [0, 1])
            self.assertGreater(items[2]['x'], items[0]['x'])
            self.assertEqual((items[4]['x'], items[4]['y']), (items[0]['x'], items[0]['y']))
            self.session.owner_started()
            self.session.owner_progress(navigation_valid=True, finished=True)
            self.now += .8
            self.update_pose(0)
            self.assertEqual(self.session.owner_progress(navigation_valid=True), 'COMPLETED')
        self.assertEqual(self.session.snapshot()['completed_passes'], 30)

        self.now += 1
        self.update_pose(0)
        self.session.prepare(request, create=True)
        self.session.owner_started()
        self.session.owner_progress(navigation_valid=True, finished=True)
        self.now += .8
        self.update_pose(1)
        self.assertEqual(self.session.owner_progress(navigation_valid=True), 'ABORTED')
        self.assertEqual(self.session.snapshot()['completed_passes'], 30)
