"""Camera WORLD FLU pose -> rover WORLD FLU pose using a planar extrinsic.

Camera offset is the camera center expressed in rover axes (forward/left).
Yaw offset is camera forward heading minus rover forward heading, degrees.
"""
import math

CAMERA_OFFSET_X = -0.4817338034
CAMERA_OFFSET_Y = +0.4723714267
CAMERA_YAW_OFFSET_DEG = -7.0289256693


def wrap_deg(angle):
    """Normalize degrees to [-180, 180)."""
    if not math.isfinite(angle):
        raise ValueError("Angle must be finite")
    return (angle + 180.0) % 360.0 - 180.0


def camera_to_rover_pose(camera_x, camera_y, camera_yaw,
                         camera_offset_x=CAMERA_OFFSET_X,
                         camera_offset_y=CAMERA_OFFSET_Y,
                         camera_yaw_offset=CAMERA_YAW_OFFSET_DEG):
    """Return (rover_x, rover_y, rover_yaw_deg), without re-zeroing WORLD."""
    if not all(math.isfinite(v) for v in (camera_x, camera_y, camera_yaw,
                                         camera_offset_x, camera_offset_y, camera_yaw_offset)):
        raise ValueError("Pose and extrinsic values must be finite")
    yaw = wrap_deg(camera_yaw - camera_yaw_offset)
    c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    return (camera_x - (c * camera_offset_x - s * camera_offset_y),
            camera_y - (s * camera_offset_x + c * camera_offset_y), yaw)
