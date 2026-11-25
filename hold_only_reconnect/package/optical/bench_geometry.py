"""Measured 6DoF bench geometry. Pure computation; never authorizes ExternalNav TX.

T_A_B maps column coordinates from B into A. W and B/E are right-handed FLU.
C is optical (+x right,+y down,+z forward); M is canonical OpenCV ArUco.
The strict profile loader never infers missing mounting data. Separate survey
helpers below may estimate a bench mounting from measured references and fresh
ATTITUDE/PnP; their first-position fit is unqualified and cannot authorize TX.
"""
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import time

import numpy as np
from pose_transform_3d import (
    compose_transform, invert_transform, make_transform,
    rvec_tvec_to_transform, rotation_matrix_to_euler_deg,
    euler_deg_to_rotation_matrix,
)


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class BenchProfile:
    profile_id: str
    marker_id: int
    marker_side_m: float
    T_W_M: np.ndarray
    T_B_C: np.ndarray
    T_E_B: np.ndarray
    calibration_sha256: str
    mode: str = 'OBSERVE_ONLY'
    estimator_frame_alignment: str = 'UNVERIFIED'


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _description(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ProfileError(name + ' must be a measured reference description')


def _matrix(value, name):
    if (type(value) is not list or len(value) != 4
            or any(type(row) is not list or len(row) != 4 for row in value)
            or any(not _number(v) for row in value for v in row)):
        raise ProfileError(name + ' must be an explicitly measured finite 4x4 matrix')
    try:
        result = compose_transform(np.asarray(value, dtype=float))
    except ValueError as exc:
        raise ProfileError(name + ': ' + str(exc)) from exc
    result.setflags(write=False)
    return result


def load_profile(path, calibration_path):
    """Load a measured OBSERVE_ONLY profile pinned to unchanged intrinsic assets.

    A null/unmeasured template cannot be used. A separate operator/estimator
    frame alignment review is always UNVERIFIED here; this file cannot enable TX.
    T_E_B is optional ONLY when B is explicitly the configured EKF body origin.
    Otherwise a separately measured B-to-E rigid transform is mandatory.
    """
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ProfileError('Cannot read bench profile') from exc
    if (type(data) is not dict or type(data.get('schema_version')) is not int
            or data.get('schema_version') != 1):
        raise ProfileError('Unsupported bench profile schema')
    if data.get('measured') is not True:
        raise ProfileError('Bench profile measurements are incomplete')
    if data.get('mode') != 'OBSERVE_ONLY':
        raise ProfileError('Bench profile must remain OBSERVE_ONLY')
    if data.get('estimator_frame_alignment') != 'UNVERIFIED':
        raise ProfileError('This profile cannot assert estimator-frame alignment')
    if data.get('tx_permitted') is not False:
        raise ProfileError('This profile cannot authorize TX')
    name = data.get('profile_id')
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', name):
        raise ProfileError('Invalid profile_id')
    if data.get('length_unit') != 'm' or data.get('rotation_representation') != 'matrix':
        raise ProfileError('Explicit metre units and matrix rotation representation are required')
    if data.get('transform_convention') != 'T_A_B_maps_B_to_A_column_vectors':
        raise ProfileError('Explicit source-to-parent transform direction is required')
    world = data.get('world_frame', {})
    if type(world) is not dict or world.get('name') != 'W_FLU' or world.get('axes') != ['forward', 'left', 'up'] or world.get('gravity_aligned') is not True:
        raise ProfileError('W_FLU must be a fixed gravity-aligned frame')
    _description(world.get('origin_description'), 'world origin')
    _description(world.get('x_axis_description'), 'world +X reference')
    marker = data.get('marker_frame', {})
    if (type(marker) is not dict or type(marker.get('id')) is not int or marker.get('id') != 24
            or marker.get('axes') != ['printed_right', 'printed_up', 'out_of_printed_face']
            or marker.get('corner_order') != ['top_left', 'top_right', 'bottom_right', 'bottom_left']):
        raise ProfileError('Measured ID24 canonical printed axes/corner order are required')
    side = marker.get('measured_black_square_side_m')
    if not _number(side) or side <= 0:
        raise ProfileError('Measure the outer black square side, excluding white margin')
    body = data.get('body_frame', {})
    if type(body) is not dict or body.get('name') != 'B_FLU' or body.get('axes') != ['forward', 'left', 'up']:
        raise ProfileError('Rigid B_FLU axes must be declared')
    _description(body.get('origin_description'), 'rigid body origin')
    if body.get('origin_kind') == 'EKF_BODY_ORIGIN':
        if body.get('T_E_B') is not None:
            supplied = _matrix(body['T_E_B'], 'T_E_B')
            if not np.allclose(supplied, np.eye(4), atol=1e-10, rtol=0):
                raise ProfileError('B and EKF origin declared identical but T_E_B is not identity')
        T_E_B = np.eye(4)
        T_E_B.setflags(write=False)
    elif body.get('origin_kind') == 'SURVEYED_RIGID_DATUM':
        if body.get('estimator_origin_transform_measured') is not True:
            raise ProfileError('Surveyed body datum requires independently measured T_E_B')
        _description(body.get('estimator_origin_description'), 'estimator body origin')
        T_E_B = _matrix(body.get('T_E_B'), 'T_E_B')
    else:
        raise ProfileError('Use a rigid EKF body origin or a separately surveyed body-to-EKF transform; never a floor projection')
    if data.get('camera_mount_measured') is not True or data.get('marker_pose_measured') is not True:
        raise ProfileError('Camera mounting and marker pose must be independently measured')
    camera = data.get('camera_frame')
    if camera != {'name': 'C_OPTICAL', 'axes': ['right', 'down', 'forward']}:
        raise ProfileError('Explicit OpenCV optical camera frame is required')
    expected = data.get('calibration_sha256')
    if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise ProfileError('Intrinsic calibration SHA256 is required')
    calibration_path = Path(calibration_path)
    try:
        if hashlib.sha256(calibration_path.read_bytes()).hexdigest() != expected:
            raise ProfileError('Intrinsic calibration asset differs from the measured profile')
        # Optional extra pins bind immutable nominal map/config assets without using
        # their planar mounting or marker coordinates as measured bench geometry.
        extras = data.get('source_assets_sha256', {})
        if type(extras) is not dict or set(extras) - {'marker_map.json', 'rover_config.json'}:
            raise ProfileError('Unsupported source asset pins')
        for filename, digest in extras.items():
            if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
                raise ProfileError('Invalid source asset hash')
            if hashlib.sha256((calibration_path.parent/filename).read_bytes()).hexdigest() != digest:
                raise ProfileError('Nominal source asset differs: '+filename)
    except OSError as exc:
        raise ProfileError('Cannot verify intrinsic/source assets') from exc
    return BenchProfile(name, 24, float(side),
                        _matrix(data.get('T_W_M24'), 'T_W_M24'),
                        _matrix(data.get('T_B_Coptical'), 'T_B_Coptical'),
                        T_E_B, expected)


def rotation_to_quaternion_wxyz(rotation):
    """Proper 3x3 rotation -> normalized full quaternion, robust near 180 degrees."""
    R = make_transform(rotation, [0., 0., 0.])[:3, :3]
    trace = float(np.trace(R))
    if trace > 0:
        s = math.sqrt(trace + 1.) * 2.
        q = np.array([.25*s, (R[2,1]-R[1,2])/s, (R[0,2]-R[2,0])/s, (R[1,0]-R[0,1])/s])
    else:
        i = int(np.argmax(np.diag(R)))
        if i == 0:
            s = math.sqrt(max(0., 1.+R[0,0]-R[1,1]-R[2,2])) * 2.
            q = np.array([(R[2,1]-R[1,2])/s, .25*s, (R[0,1]+R[1,0])/s, (R[0,2]+R[2,0])/s])
        elif i == 1:
            s = math.sqrt(max(0., 1.+R[1,1]-R[0,0]-R[2,2])) * 2.
            q = np.array([(R[0,2]-R[2,0])/s, (R[0,1]+R[1,0])/s, .25*s, (R[1,2]+R[2,1])/s])
        else:
            s = math.sqrt(max(0., 1.+R[2,2]-R[0,0]-R[1,1])) * 2.
            q = np.array([(R[1,0]-R[0,1])/s, (R[0,2]+R[2,0])/s, (R[1,2]+R[2,1])/s, .25*s])
    q /= np.linalg.norm(q)
    # q/-q are identical; canonicalize the first nonzero component deterministically.
    for component in q:
        if abs(component) > 1e-12:
            if component < 0:q = -q
            break
    if not np.isfinite(q).all():
        raise ValueError('Invalid quaternion')
    return [float(v) for v in q]


def pose_from_observation(profile, rvec, tvec):
    """Independent survey + PnP + full mounting; no planar projection or z forcing.

    T_W_B = T_W_M24 @ inv(T_Coptical_M24) @ inv(T_B_Coptical).
    Optional T_E_B maps surveyed B to the configured estimator body E.
    Both world and body FLU bases are converted to FRD for the output quaternion.
    This output is observation-only and cannot authorize estimator-frame alignment.
    """
    if not isinstance(profile, BenchProfile) or profile.mode != 'OBSERVE_ONLY':
        raise ProfileError('A loaded measured observation-only bench profile is required')
    T_C_M = rvec_tvec_to_transform(rvec, tvec)
    T_W_C = compose_transform(profile.T_W_M, invert_transform(T_C_M))
    T_W_B = compose_transform(T_W_C, invert_transform(profile.T_B_C))
    T_W_E = compose_transform(T_W_B, invert_transform(profile.T_E_B))
    S = np.diag([1., -1., -1.])
    T_frd = make_transform(S @ T_W_E[:3,:3] @ S, S @ T_W_E[:3,3])
    return {
        'profile_id': profile.profile_id, 'mode': 'OBSERVE_ONLY', 'source_marker': 24,
        'tx_permitted': False, 'estimator_frame_alignment': 'UNVERIFIED',
        'T_W_Coptical': T_W_C.tolist(), 'T_W_B_flu': T_W_B.tolist(),
        'T_W_E_flu': T_W_E.tolist(), 'T_Wfrd_Efrd': T_frd.tolist(),
        'body_position_flu_m': T_W_B[:3,3].tolist(),
        'estimator_position_flu_m': T_W_E[:3,3].tolist(),
        'position_frd_m': T_frd[:3,3].tolist(),
        'body_rpy_flu_deg': list(rotation_matrix_to_euler_deg(T_W_B[:3,:3])),
        'estimator_rpy_frd_deg': list(rotation_matrix_to_euler_deg(T_frd[:3,:3])),
        'quaternion_frd_wxyz': rotation_to_quaternion_wxyz(T_frd[:3,:3]),
    }


class SurveyError(ValueError):
    """Incomplete physical survey or unusable live diagnostic observations."""


def body_rotation_for_bench(roll_frd_rad, pitch_frd_rad):
    """Vehicle FLU attitude in a level bench frame whose +X is body-forward XY.

    B uses vehicle axes, even when its origin is a separate rigid chassis datum.
    W's heading is DEFINED by the physical horizontal body-forward projection;
    no compass heading or zero AP yaw is assumed. For source FRD Euler angles,
    changing both bases to FLU gives Ry(-pitch) @ Rx(roll). Camera orientation is
    not derived from this function alone. Near vertical pitch has no heading.
    """
    if (not _number(roll_frd_rad) or not _number(pitch_frd_rad)
            or abs(roll_frd_rad) > math.pi + 1e-5
            or abs(pitch_frd_rad) >= math.pi / 2 - 1e-6):
        raise SurveyError('Finite vehicle ATTITUDE roll/pitch with a horizontal forward projection required')
    return euler_deg_to_rotation_matrix(math.degrees(roll_frd_rad),
                                        -math.degrees(pitch_frd_rad), 0.)


def _survey_confirmations(marker_level, canonical_axes_aligned,
                          body_axes_vehicle_aligned, stationary):
    if not all(v is True for v in (marker_level, canonical_axes_aligned,
                                   body_axes_vehicle_aligned, stationary)):
        raise SurveyError('Confirm stationary body, level marker plane, canonical marker axes and vehicle body axes')


def _survey_number(value, name, positive=False):
    if not _number(value) or (positive and value <= 0):
        raise SurveyError(name + ' must be an explicitly measured finite ' + ('positive ' if positive else '') + 'number')
    return float(value)


def _survey_samples(attitude, measurement, marker_side_m, now):
    """Check actual receipt/capture ages, not caller-provided fresh flags alone."""
    if not _number(now):
        raise SurveyError('Invalid monotonic clock')
    if (type(attitude) is not dict or attitude.get('state') != 'FRESH'
            or attitude.get('valid') is not True or attitude.get('fresh') is not True
            or attitude.get('source') != [1, 1]
            or any(type(v) is not int for v in attitude.get('source', []))):
        raise SurveyError('Fresh source 1:1 ATTITUDE diagnostics required')
    angles = [attitude.get(k) for k in ('roll_rad', 'pitch_rad', 'yaw_rad')]
    received = attitude.get('received_monotonic_s')
    declared_age, limit = attitude.get('age_s'), attitude.get('freshness_s')
    if (not all(_number(v) for v in [*angles, received, declared_age, limit])
            or not 0 < limit <= 3.0 or not 0 <= declared_age <= limit
            or not 0 <= now - received <= limit):
        raise SurveyError('ATTITUDE is stale or its receipt clock/angles are invalid')
    if (type(measurement) is not dict or measurement.get('fresh') is not True
            or measurement.get('source_marker') != 24
            or measurement.get('frame') != 'C_OPTICAL_RIGHT_DOWN_FORWARD'):
        raise SurveyError('Fresh current ID24 optical measurement required')
    acquired, snapshot = measurement.get('acquired_at'), measurement.get('snapshot_monotonic_s')
    optical_age = measurement.get('age_s')
    if (not all(_number(v) for v in (acquired, snapshot, optical_age))
            or not 0 <= optical_age <= .2 or not 0 <= snapshot - acquired <= .2
            or abs(optical_age - (snapshot - acquired)) > .001
            or not 0 <= now - snapshot <= .2 or not 0 <= now - acquired <= .2):
        raise SurveyError('Optical frame is stale, future-dated or from an invalid receipt clock')
    if abs(received - acquired) > 1.1:
        raise SurveyError('ATTITUDE and optical measurement must be paired within 1.1 seconds while stationary')
    side = _survey_number(marker_side_m, 'Actual marker side', positive=True)
    nominal = _survey_number(measurement.get('nominal_marker_side_m'), 'Nominal PnP marker side', positive=True)
    try:
        optical = np.array(_matrix(measurement.get('T_Coptical_M'), 'T_Coptical_M'), copy=True)
    except ProfileError as exc:
        raise SurveyError(str(exc)) from exc
    # PnP translation scales linearly with the object-point side; R does not.
    scale = side / nominal
    optical[:3, 3] *= scale
    if not np.isfinite(optical).all() or optical[2, 3] <= 0:
        raise SurveyError('Positive-depth finite optical observation required')
    half = side / 2.
    corners = np.array([[-half, half, 0.], [half, half, 0.],
                        [half, -half, 0.], [-half, -half, 0.]])
    depths = (optical[:3, :3] @ corners.T + optical[:3, 3:4])[2]
    if not np.isfinite(depths).all() or np.any(depths <= 1e-6):
        raise SurveyError('Every marker corner must have positive camera depth')
    rotation = body_rotation_for_bench(angles[0], angles[1])
    return optical, rotation, dict(attitude_received_monotonic_s=received,
                                  optical_acquired_at=acquired,
                                  snapshot_monotonic_s=snapshot,
                                  calculated_at_monotonic_s=now,
                                  nominal_marker_side_m=nominal, actual_marker_side_m=side,
                                  pnp_translation_scale=scale)


def derive_stationary_mount(attitude, optical_measurement, marker_side_m,
                            height_m, marker_x_m, marker_y_m, *,
                            marker_level=False, canonical_axes_aligned=False,
                            body_axes_vehicle_aligned=False, stationary=False,
                            now_monotonic_s=None):
    """First-position bench estimate from L/H/X/Y and real live observations.

    W origin is the vertical projection of a fixed physical B datum onto the
    marker plane; W+X follows the horizontal vehicle-forward reference. Marker
    M.x/M.y align with W.x/W.y and its face points up, hence R_W_M=identity.
    B translation is [0,0,H]. This does NOT identify B with the EKF origin E.

    Default receipt checking uses the actual monotonic clock: historical JSON
    cannot become current by retaining valid/fresh flags. An explicit clock is
    accepted for offline numeric review, never as proof of live freshness.
    Returned estimates contain no measured-profile approval or alignment grant;
    fixed mounting must be checked against independently surveyed held-out data.
    """
    _survey_confirmations(marker_level, canonical_axes_aligned,
                          body_axes_vehicle_aligned, stationary)
    height = _survey_number(height_m, 'Body datum height over marker plane', positive=True)
    x = _survey_number(marker_x_m, 'Surveyed marker X')
    y = _survey_number(marker_y_m, 'Surveyed marker Y')
    now = time.monotonic() if now_monotonic_s is None else now_monotonic_s
    C_M, R_W_B, evidence = _survey_samples(attitude, optical_measurement, marker_side_m, now)
    W_B = make_transform(R_W_B, [0., 0., height])
    W_M = make_transform(np.eye(3), [x, y, 0.])
    B_C = compose_transform(invert_transform(W_B), W_M, invert_transform(C_M))
    return dict(status='BENCH_MOUNT_ESTIMATE',
                provenance='FIRST_POSITION_FIT_REQUIRES_HELD_OUT_VALIDATION',
                world_heading='HORIZONTAL_VEHICLE_FORWARD_PROJECTION_AT_SURVEY',
                body_origin='SURVEYED_RIGID_DATUM_NOT_IDENTIFIED_AS_EKF_ORIGIN',
                T_W_B=W_B.tolist(), T_W_M24=W_M.tolist(), T_B_Coptical=B_C.tolist(),
                T_Coptical_M_scaled=C_M.tolist(), evidence=evidence,
                tx_permitted=False, alignment_verified=False,
                estimator_frame_alignment='UNVERIFIED')


def check_held_out_stationary_observation(estimate, attitude, optical_measurement,
                                         marker_side_m, marker_x_m, marker_y_m, *,
                                         marker_level=False, canonical_axes_aligned=False,
                                         body_axes_vehicle_aligned=False, stationary=False,
                                         now_monotonic_s=None):
    """Compute residuals with frozen mount at a later, independently surveyed M.

    No acceptance threshold, profile approval or TX authorization is inferred.
    Merely refitting at the first marker position is explicitly rejected.
    The physical B datum, W frame, camera bracket and body pose must be unchanged.
    """
    _survey_confirmations(marker_level, canonical_axes_aligned,
                          body_axes_vehicle_aligned, stationary)
    if (type(estimate) is not dict or estimate.get('provenance') !=
            'FIRST_POSITION_FIT_REQUIRES_HELD_OUT_VALIDATION'
            or estimate.get('tx_permitted') is not False
            or estimate.get('alignment_verified') is not False):
        raise SurveyError('An unqualified first-position estimate with TX OFF is required')
    try:
        W_B_first = _matrix(estimate.get('T_W_B'), 'First T_W_B')
        W_M_first = _matrix(estimate.get('T_W_M24'), 'First T_W_M24')
        B_C = _matrix(estimate.get('T_B_Coptical'), 'Frozen T_B_Coptical')
    except ProfileError as exc:
        raise SurveyError(str(exc)) from exc
    x = _survey_number(marker_x_m, 'Independently surveyed held-out marker X')
    y = _survey_number(marker_y_m, 'Independently surveyed held-out marker Y')
    if math.hypot(x - W_M_first[0, 3], y - W_M_first[1, 3]) <= 1e-6:
        raise SurveyError('Held-out validation requires a different independently surveyed marker position')
    now = time.monotonic() if now_monotonic_s is None else now_monotonic_s
    C_M, R_W_B, evidence = _survey_samples(attitude, optical_measurement, marker_side_m, now)
    first_evidence = estimate.get('evidence', {})
    first_stamp = first_evidence.get('optical_acquired_at')
    first_side = first_evidence.get('actual_marker_side_m')
    if (not _number(first_stamp) or evidence['optical_acquired_at'] <= first_stamp
            or first_side != evidence['actual_marker_side_m']):
        raise SurveyError('Use a later held-out capture and the same measured marker side')
    W_M = make_transform(np.eye(3), [x, y, 0.])
    observed_W_B = compose_transform(W_M, invert_transform(C_M), invert_transform(B_C))
    expected_W_B = make_transform(R_W_B, W_B_first[:3, 3])
    delta = observed_W_B[:3, 3] - expected_W_B[:3, 3]
    relative_R = expected_W_B[:3, :3].T @ observed_W_B[:3, :3]
    angle = math.degrees(math.acos(float(np.clip((np.trace(relative_R) - 1.) / 2., -1., 1.))))
    return dict(status='HELD_OUT_RESIDUALS_ONLY_NO_AUTOMATIC_APPROVAL',
                T_W_B_observed=observed_W_B.tolist(), T_W_B_expected=expected_W_B.tolist(),
                position_residual_m=delta.tolist(), position_residual_norm_m=float(np.linalg.norm(delta)),
                orientation_residual_deg=angle,
                measured_marker_shift_xy_m=[float(x-W_M_first[0, 3]), float(y-W_M_first[1, 3])],
                evidence=evidence, tx_permitted=False, alignment_verified=False,
                estimator_frame_alignment='UNVERIFIED')
