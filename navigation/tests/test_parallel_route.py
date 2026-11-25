from copy import deepcopy
import json
import math
from pathlib import Path
import runpy
import unittest

from navigation.dynamic_mission import Blocked, OwnerEvidence, Pose, compile_leg, preview_route


ROOT = Path(__file__).resolve().parents[2]


class ParallelRouteTest(unittest.TestCase):
    def test_offset_route_keeps_home_y_and_body_heading(self):
        config = json.loads((ROOT / 'navigation/dynamic_mission.json').read_text())
        georef = dict(latitude_deg=40., longitude_deg=25., altitude_msl_m=100.)
        # Synthetic geometry. Map +Y is forward; the marker line is offset in map X.
        config.update(axis_flu_deg=90, home_map_xy_m=[1., 2.], surveyed_bounds_m=[0., 10.],
                      survey=dict(home_map_zero=georef, route_azimuth_deg=0, expected_ekf_origin=georef))
        measured_map = dict(coordinate_frame='HOME_FRONT_DATUM', position_unit='m',
            markers=[dict(id=i, position=dict(x=3., y=2. + station))
                     for i, station in ((23, 2.5), (24, 5.), (25, 7.5))])

        transform = runpy.run_path(str(ROOT / 'hold_only_reconnect/package/optical/pose_transform_2d.py'))
        lever_x, lever_y = transform['CAMERA_OFFSET_X'], transform['CAMERA_OFFSET_Y']
        self.assertNotEqual((lever_x, lever_y), (0., 0.))
        transform_camera = transform['camera_to_rover_pose']
        camera_yaw = 90 + transform['CAMERA_YAW_OFFSET_DEG']

        def observed(route_x):
            base = transform_camera(1. - lever_y, 2. + route_x + lever_x, camera_yaw)
            self.assertEqual(base[2], 90)
            return OwnerEvidence(Pose(*base, 10., 23), 10., 10., georef, georef)

        home = observed(0)
        outbound = preview_route(dict(start='home', target_type='marker', marker=25),
                                 config, measured_map, home, 10.1)
        self.assertAlmostEqual(outbound['target_m'], 7.5)
        self.assertEqual(outbound['target_y_m'], 0.)
        self.assertAlmostEqual(outbound['target_map_xy_m'][0], 1.)
        self.assertAlmostEqual(outbound['target_map_xy_m'][1], 9.5)
        self.assertEqual(compile_leg(outbound, config, home)[1]['param1'], 0)

        far = observed(7.5)
        returning = preview_route(dict(start='current', target_type='distance', distance_from='home', distance_m=0),
                                  config, measured_map, far, 10.1)
        self.assertEqual(returning['target_map_xy_m'], [1., 2.])
        self.assertEqual(compile_leg(returning, config, far)[1]['param1'], 1)
        relative = preview_route(dict(start='current',target_type='distance',distance_from='current',
                                      distance_m=2.5,direction='reverse'), config, measured_map, far, 10.1)
        self.assertAlmostEqual(relative['target_m'], 5.)
        self.assertAlmostEqual(relative['target_map_xy_m'][0], 1.)
        self.assertEqual(compile_leg(outbound, config, home)[2]['y'], compile_leg(relative, config, far)[2]['y'])
        self.assertTrue(math.isclose(far.pose.yaw_flu_deg, home.pose.yaw_flu_deg))

        centered_map = deepcopy(measured_map)
        for marker in centered_map['markers']:
            marker['position']['x'] = 1.
        centered_outbound = preview_route(dict(start='home', target_type='marker', marker=25),
                                          config, centered_map, home, 10.1)
        centered_return = preview_route(dict(start='current', target_type='distance',
                                             distance_from='home', distance_m=0),
                                        config, centered_map, far, 10.1)
        self.assertEqual(centered_outbound, outbound)
        self.assertEqual(centered_return, returning)
        self.assertEqual(centered_return['target_y_m'], 0)
        self.assertEqual([marker['position']['y'] - config['home_map_xy_m'][1]
                          for marker in centered_map['markers']], [2.5, 5., 7.5])

        unknown = deepcopy(config)
        unknown['home_map_xy_m'] = None
        with self.assertRaisesRegex(Blocked, 'Y_HOME UNKNOWN'):
            preview_route(dict(start='home', target_type='marker', marker=25),
                          unknown, measured_map, home, 10.1)
