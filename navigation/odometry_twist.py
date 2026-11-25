"""Offline planar velocity from time-stamped LOCAL_FRD poses and BODY_FRD gyro."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class OpticalPose:
    acquired_at: float
    marker: int
    x_m: float
    y_m: float
    yaw_rad: float


@dataclass(frozen=True)
class GyroSample:
    measured_at: float
    roll_rad_s: float
    pitch_rad_s: float
    yaw_rad_s: float


@dataclass(frozen=True)
class PlanarTwist:
    acquired_at: float
    forward_m_s: float
    right_m_s: float
    roll_rad_s: float
    pitch_rad_s: float
    yaw_rad_s: float


def estimate_planar_twist(previous, current, gyro, *, now, max_pose_age_s, max_gyro_skew_s):
    """Inputs are accepted poses and gyro time-aligned to the same host clock.

    A marker transition starts a new pair. No vertical velocity is estimated.
    A result is a candidate for offline validation, not a MAVLink payload.
    """
    if previous is None or current is None or gyro is None:
        return None

    quantities = (
        previous.acquired_at, previous.x_m, previous.y_m, current.acquired_at,
        current.x_m, current.y_m, current.yaw_rad, gyro.measured_at, gyro.roll_rad_s,
        gyro.pitch_rad_s, gyro.yaw_rad_s, now, max_pose_age_s, max_gyro_skew_s,
    )
    if not all(type(value) in (float, int) and math.isfinite(value) for value in quantities):
        return None
    dt = current.acquired_at - previous.acquired_at
    if (not 0 < dt <= max_pose_age_s or not 0 <= now - current.acquired_at <= max_pose_age_s or
            gyro.measured_at > now or not 0 <= abs(gyro.measured_at - current.acquired_at) <= max_gyro_skew_s or
            previous.marker != current.marker or current.marker not in (23, 24, 25)):
        return None

    world_x = (current.x_m - previous.x_m) / dt
    world_y = (current.y_m - previous.y_m) / dt
    cosine, sine = math.cos(current.yaw_rad), math.sin(current.yaw_rad)
    forward = cosine * world_x + sine * world_y
    right = -sine * world_x + cosine * world_y
    if not math.isfinite(forward) or not math.isfinite(right):
        return None
    return PlanarTwist(current.acquired_at, forward, right,
                       gyro.roll_rad_s, gyro.pitch_rad_s, gyro.yaw_rad_s)
