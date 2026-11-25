"""Offline synthetic tests. Never imports a selector or opens a camera."""
import ast
import copy
import hashlib
import json
import math
from pathlib import Path
import unittest
import cv2
import numpy as np
import pose_transform_3d as pt

ROOT = Path(__file__).resolve().parent


def function(tree, name):
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)


class TransformTests(unittest.TestCase):
    def assert_transform(self, a, b):
        np.testing.assert_allclose(a, b, atol=1e-9, rtol=0)

    def test_A_identity(self):
        T = pt.make_transform(np.eye(3), [0, 0, 0])
        self.assert_transform(pt.compose_transform(T, pt.invert_transform(T)), np.eye(4))

    def test_B_translation(self):
        T = pt.make_transform(np.eye(3), [1, -2, 3])
        self.assert_transform(pt.extract_translation(pt.invert_transform(T)), [-1, 2, -3])
        self.assert_transform(pt.compose_transform(T, pt.invert_transform(T)), np.eye(4))

    def test_C_euler(self):
        for angles in ((20, -30, 70), (0, 0, 0), (25, 90, 40), (25, -90, 40)):
            R = pt.euler_deg_to_rotation_matrix(*angles)
            recovered = pt.rotation_matrix_to_euler_deg(R)
            self.assert_transform(pt.euler_deg_to_rotation_matrix(*recovered), R)
        T = pt.make_transform(pt.euler_deg_to_rotation_matrix(20, -30, 70), [0, 0, 0])
        self.assertAlmostEqual(pt.extract_yaw_deg(T), 70)

    def recover(self, marker):
        world_marker = pt.marker_map_entry_to_world_transform(marker)
        # Synthetic optical camera looking forward in FLU: right=-Y,
        # down=-Z, forward=+X. This is test geometry, not mounting calibration.
        R_flu_optical = np.array([[0., 0., 1.], [-1., 0., 0.], [0., -1., 0.]])
        world_camera = pt.make_transform(
            pt.euler_deg_to_rotation_matrix(-12, 21, 43) @ R_flu_optical,
            [1.1, 0.525, 1.215])
        observation = pt.compose_transform(pt.invert_transform(world_camera), world_marker)
        rvec, _ = cv2.Rodrigues(observation[:3, :3])
        actual_observation = pt.rvec_tvec_to_transform(rvec, observation[:3, 3])
        recovered = pt.compose_transform(world_marker, pt.invert_transform(actual_observation))
        self.assert_transform(recovered, world_camera)
        return actual_observation, world_camera

    def test_D_floor_camera_recovery_FLU(self):
        self.recover(dict(placement='floor', position=dict(x=3, y=0, z=0), orientation=dict(roll=0, pitch=0, yaw=0)))

    def test_E_wall_recovery(self):
        # Marker XY plane becomes vertical: roll=90 degrees, plus world yaw.
        self.recover(dict(placement='wall', position=dict(x=4.2, y=-1.5, z=2.1),
                          orientation=dict(roll=90, pitch=0, yaw=35)))

    def test_F_authoritative_unchanged_and_shadow_isolated(self):
        t7 = ast.parse((ROOT / 'aruco_active_marker_selector_v7.py').read_text())
        t8 = ast.parse((ROOT / 'aruco_active_marker_selector_v8.py').read_text())
        for name in ('load_rover_config', 'load_camera_calibration', 'estimate_pose',
                     'navigation_from_pose', 'neighbours', 'update_selector',
                     'select_pose_source', 'marker_metadata'):
            self.assertEqual(ast.dump(function(t7, name)), ast.dump(function(t8, name)))

        class StripShadow(ast.NodeTransformer):
            def visit_If(self, node):
                if isinstance(node.test, ast.Name) and node.test.id == 'FULL_3D_AUTHORITATIVE':
                    return None
                return self.generic_visit(node)

            def visit_Assign(self, node):
                if any(isinstance(t, ast.Name) and t.id == 'observations' for t in node.targets):
                    return None
                return node

            def visit_Try(self, node):
                if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                       and n.func.id == 'rvec_tvec_to_transform' for statement in node.body if isinstance(statement, ast.Assign)
                       for n in ast.walk(statement)):
                    return None
                return self.generic_visit(node)

            def visit_Expr(self, node):
                if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                       and n.func.id == 'shadow_diagnostics' for n in ast.walk(node)):
                    return None
                return node

        stripped = StripShadow().visit(copy.deepcopy(function(t8, 'main')))
        self.assertEqual(ast.dump(function(t7, 'main')), ast.dump(stripped))
        # Execute only shadow helper; no selector import, main, or Picamera2.
        marker = dict(id=23, placement='floor', position=dict(x=3, y=0, z=0),
                      orientation=dict(roll=0, pitch=0, yaw=0))
        ns = {name: getattr(pt, name) for name in ('marker_map_entry_to_world_transform',
              'compose_transform', 'invert_transform', 'extract_translation',
              'rotation_matrix_to_euler_deg')}
        ns.update(MARKER_BY_ID={23: marker}, cv2=cv2)
        exec(compile(ast.Module(body=[function(t8, 'shadow_diagnostics')], type_ignores=[]), '<offline>', 'exec'), ns)
        observation, expected = self.recover(marker)
        row = ns['shadow_diagnostics'](23, {23: observation})
        self.assertEqual(row['shadow_3d_valid'], 1)
        self.assert_transform([row[f'shadow_camera_world_{k}'] for k in ('x','y','z')], expected[:3,3])
        self.assertFalse(any(k.startswith('shadow_rover') or k.startswith('shadow_minus') for k in row))
        self.assertEqual(ns['shadow_diagnostics'](None, {}), {'shadow_3d_valid': 0})
        flag = next(n.value for n in t8.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'FULL_3D_AUTHORITATIVE' for t in n.targets))
        self.assertIs(flag.value, False)

    def test_right_handed_optical_to_FLU(self):
        R = np.array([[0., 0., 1.], [-1., 0., 0.], [0., -1., 0.]])
        self.assertAlmostEqual(np.linalg.det(R), 1.)
        # Camera right/down/forward expressed in world FLU.
        self.assert_transform(R @ [1, 0, 0], [0, -1, 0])
        self.assert_transform(R @ [0, 1, 0], [0, 0, -1])
        self.assert_transform(R @ [0, 0, 1], [1, 0, 0])
        self.assert_transform(np.cross(R[:, 0], R[:, 1]), R[:, 2])
        observation = pt.rvec_tvec_to_transform([0, 0, 0], [0, 0, 2])
        self.assert_transform(observation @ [0, 0, 0, 1], [0, 0, 2, 1])
        for angles in ((0, 0, 0), (90, 0, 35), (-12, 21, 43)):
            rotation = pt.euler_deg_to_rotation_matrix(*angles)
            self.assertAlmostEqual(np.linalg.det(rotation), 1.)
            self.assert_transform(np.cross(rotation[:, 0], rotation[:, 1]), rotation[:, 2])

    def test_map_and_legacy_lateral_FLU(self):
        marker = dict(position=dict(x=3, y=0.8, z=1.2),
                      orientation=dict(roll=0, pitch=0, yaw=0))
        self.assert_transform(pt.extract_translation(pt.marker_map_entry_to_world_transform(marker)),
                              [3, 0.8, 1.2])
        for legacy in (-0.525, 0., 0.7):
            world_y = pt.legacy_rover_lateral_to_world_y(legacy)
            self.assertEqual(world_y, -legacy)
            self.assertEqual(pt.world_y_to_legacy_rover_lateral(world_y), legacy)
        config = json.loads((ROOT / 'rover_config.json').read_text())
        self.assertEqual(pt.legacy_rover_lateral_to_world_y(config['camera_lateral_offset']),
                         -config['camera_lateral_offset'])

    def test_invalid_rotations(self):
        for R in (np.diag([1, 1, -1]), np.ones((3, 3)), np.full((3, 3), np.nan)):
            with self.assertRaises(ValueError): pt.make_transform(R, [0, 0, 0])


if __name__ == '__main__':
    unittest.main()
