"""Rigid transforms: T_A_B maps column coordinates p_B to p_A.

Euler angles are degrees, R = Rz(yaw) @ Ry(pitch) @ Rx(roll).
WORLD and ROVER are right-handed FLU: +X forward, +Y left, +Z up.
ROVER origin is the floor-projected FRONT DATUM; at HOME T_world_rover=I.
OpenCV optical CAMERA stays right-handed: +x right, +y down, +z forward.
Marker position.y is LEFT and position.z is UP; Euler rotations are about FLU
world axes. Optical-to-FLU rotation must be supplied, never inferred here.
Legacy planar rover_lateral remains positive RIGHT, so for the same referenced
lateral quantity: legacy_rover_lateral_positive_right = -world_y and
world_y = -legacy_rover_lateral_positive_right. This sign conversion alone
does not turn marker-relative legacy navigation into a world position.
"""
import math
import cv2
import numpy as np


def _rotation(R):
    R = np.asarray(R, dtype=float)
    if (R.shape != (3, 3) or not np.isfinite(R).all()
            or not np.allclose(R.T @ R, np.eye(3), atol=1e-7, rtol=0)
            or not np.isclose(np.linalg.det(R), 1.0, atol=1e-7, rtol=0)):
        raise ValueError("R must be a finite proper orthonormal 3x3 rotation")
    return R


def _transform(T):
    T = np.asarray(T, dtype=float)
    if (T.shape != (4, 4) or not np.isfinite(T).all()
            or not np.allclose(T[3], [0, 0, 0, 1], atol=1e-10, rtol=0)):
        raise ValueError("T must be a finite homogeneous 4x4 rigid transform")
    _rotation(T[:3, :3])
    return T


def euler_deg_to_rotation_matrix(roll, pitch, yaw):
    angles = np.asarray([roll, pitch, yaw], dtype=float)
    if not np.isfinite(angles).all():
        raise ValueError("Euler angles must be finite")
    r, p, y = np.radians(angles)
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    Rx = np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]])
    Ry = np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]])
    Rz = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


def rotation_matrix_to_euler_deg(R):
    """Return roll, pitch, yaw; pitch in [-90,90]. At gimbal lock roll=0.

    Euler angles are nonunique; compare reconstructed rotations for round trips.
    """
    R = _rotation(R)
    c = math.hypot(R[0, 0], R[1, 0])
    pitch = math.atan2(-R[2, 0], c)
    if c > 1e-10:
        roll = math.atan2(R[2, 1], R[2, 2])
        yaw = math.atan2(R[1, 0], R[0, 0])
    else:
        roll = 0.0
        yaw = math.atan2(-R[0, 1], R[1, 1])
    return tuple(float(v) for v in np.degrees([roll, pitch, yaw]))


def make_transform(R, t):
    R = _rotation(R)
    t = np.asarray(t, dtype=float)
    if t.shape not in ((3,), (3, 1), (1, 3)) or not np.isfinite(t).all():
        raise ValueError("t must be a finite three-component translation")
    T = np.eye(4)
    T[:3, :3], T[:3, 3] = R, t.reshape(3)
    return T


def invert_transform(T):
    T = _transform(T)
    R = T[:3, :3].T
    return make_transform(R, -R @ T[:3, 3])


def compose_transform(*transforms):
    """Multiply in argument order; no arguments returns identity."""
    result = np.eye(4)
    for T in transforms:
        result = result @ _transform(T)
    return _transform(result)


def rvec_tvec_to_transform(rvec, tvec):
    """OpenCV solvePnP observation: T_camera_marker, never its inverse."""
    rvec = np.asarray(rvec, dtype=float)
    if rvec.shape not in ((3,), (3, 1), (1, 3)) or not np.isfinite(rvec).all():
        raise ValueError("rvec must be a finite three-component Rodrigues vector")
    R, _ = cv2.Rodrigues(rvec.reshape(3, 1))
    return make_transform(R, tvec)


def marker_map_entry_to_world_transform(marker):
    """Map orientation places the OpenCV object frame in the FLU world.

    Map position is forward/left/up, with no placement correction.

    Current zero floor orientations are NOT physically validated. Placement is
    metadata; floor and wall entries use the same rigid-transform construction.
    """
    p, o = marker['position'], marker['orientation']
    return make_transform(euler_deg_to_rotation_matrix(o['roll'], o['pitch'], o['yaw']),
                          [p['x'], p['y'], p['z']])


def extract_translation(T):
    return _transform(T)[:3, 3].copy()


def extract_yaw_deg(T):
    return rotation_matrix_to_euler_deg(_transform(T)[:3, :3])[2]


def legacy_rover_lateral_to_world_y(legacy_rover_lateral_positive_right):
    """Sign conversion for a common reference: positive right -> FLU left."""
    value = float(legacy_rover_lateral_positive_right)
    if not math.isfinite(value):
        raise ValueError("Legacy lateral must be finite")
    return -value


def world_y_to_legacy_rover_lateral(world_y):
    """Sign conversion for a common reference: FLU left -> positive right."""
    value = float(world_y)
    if not math.isfinite(value):
        raise ValueError("World y must be finite")
    return -value
