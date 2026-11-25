"""Independent measured-ID24 camera observation; never opens a vehicle transport.

The thresholds below reject poor/ambiguous image observations. They are numerical
diagnostic gates, not a qualification of physical survey or estimator alignment.
Only an independently measured OBSERVE_ONLY bench profile can load here.
"""
import json
import math
from pathlib import Path
import time

import cv2
import numpy as np

from bench_geometry import load_profile, pose_from_observation


MAX_REPROJECTION_RMS_PX = 2.0
MIN_CORNER_DEPTH_M = 1e-6
AMBIGUITY_ABSOLUTE_GAP_PX = 0.10
AMBIGUITY_RELATIVE_GAP = 0.10
MAX_FRAME_AGE_S = 0.20
ASSET_DIRECTORY = Path(__file__).resolve().parent


def observation_thresholds():
    return dict(max_reprojection_rms_px=MAX_REPROJECTION_RMS_PX,
                min_corner_depth_m=MIN_CORNER_DEPTH_M,
                ambiguity_absolute_gap_px=AMBIGUITY_ABSOLUTE_GAP_PX,
                ambiguity_relative_gap=AMBIGUITY_RELATIVE_GAP,
                max_frame_age_s=MAX_FRAME_AGE_S)


def marker_object_points(side_m):
    """OpenCV IPPE_SQUARE order: printed top-left, top-right, bottom-right, bottom-left."""
    if isinstance(side_m, bool) or not math.isfinite(side_m) or side_m <= 0:
        raise ValueError('Measured positive marker side is required')
    half = side_m / 2.0
    return np.array([[-half, half, 0.], [half, half, 0.],
                     [half, -half, 0.], [-half, -half, 0.]], dtype=np.float64)


def _candidate(object_points, corners, K, distortion, rvec, tvec, index):
    """Evaluate one PnP alternative independently of OpenCV's reported error."""
    diagnostic = dict(index=index, valid=False, reason='NONFINITE_PNP',
                      rms_px=None, min_corner_depth_m=None)
    rvec, tvec = np.asarray(rvec, dtype=float), np.asarray(tvec, dtype=float)
    if rvec.size != 3 or tvec.size != 3 or not np.isfinite(rvec).all() or not np.isfinite(tvec).all():
        return diagnostic
    rvec, tvec = rvec.reshape(3, 1), tvec.reshape(3, 1)
    rotation = cv2.Rodrigues(rvec)[0]
    camera_corners = (rotation @ object_points.T + tvec).T
    depth = float(np.min(camera_corners[:, 2]))
    diagnostic['min_corner_depth_m'] = depth
    if not np.isfinite(camera_corners).all() or depth <= MIN_CORNER_DEPTH_M:
        diagnostic['reason'] = 'NONPOSITIVE_CORNER_DEPTH'
        return diagnostic
    projected = cv2.projectPoints(object_points, rvec, tvec, K, distortion)[0].reshape(4, 2)
    residual = projected - corners
    rms = float(np.sqrt(np.mean(np.sum(residual * residual, axis=1))))
    if not math.isfinite(rms):
        return diagnostic
    diagnostic['rms_px'] = rms
    if rms > MAX_REPROJECTION_RMS_PX:
        diagnostic['reason'] = 'REPROJECTION_RMS_TOO_HIGH'
        return diagnostic
    diagnostic.update(valid=True, reason='CANDIDATE_OK', rvec=rvec.reshape(3).tolist(),
                      tvec=tvec.reshape(3).tolist())
    return diagnostic


def estimate_from_corners(profile, corners, camera_matrix, distortion=None):
    """Pure image-point computation; full IPPE alternatives and rejection reasons.

    Corners are measured in the rectified image, in canonical detector order.
    Returns a JSON-ready diagnostic object and, only when accepted,
    ``bench_geometry``. Both valid alternatives participate in ambiguity gating.
    """
    result = dict(valid=False, reason='INVALID_IMAGE_POINTS', candidates=[],
                  min_rms_px=None, thresholds=observation_thresholds(),
                  tx_permitted=False, estimator_frame_alignment='UNVERIFIED')
    try:
        corners = np.asarray(corners, dtype=np.float64)
        K = np.asarray(camera_matrix, dtype=np.float64)
        distortion = np.zeros((5, 1)) if distortion is None else np.asarray(distortion, dtype=np.float64)
        if corners.shape not in ((4, 2), (1, 4, 2), (4, 1, 2)) or not np.isfinite(corners).all():
            return result
        corners = corners.reshape(4, 2)
        if (K.shape != (3, 3) or not np.isfinite(K).all()
                or K[0, 0] <= 0 or K[1, 1] <= 0
                or distortion.size not in (4, 5, 8, 12, 14) or not np.isfinite(distortion).all()):
            result['reason'] = 'INVALID_INTRINSICS'
            return result
        if not cv2.isContourConvex(corners.astype(np.float32)) or abs(cv2.contourArea(corners.astype(np.float32))) <= 1e-3:
            result['reason'] = 'DEGENERATE_IMAGE_QUADRILATERAL'
            return result
        object_points = marker_object_points(profile.marker_side_m)
        solved = cv2.solvePnPGeneric(object_points, corners, K, distortion,
                                     flags=cv2.SOLVEPNP_IPPE_SQUARE)
        if not solved[0] or len(solved[1]) != len(solved[2]):
            result['reason'] = 'PNP_FAILED'
            return result
        candidates = [_candidate(object_points, corners, K, distortion, r, t, i)
                      for i, (r, t) in enumerate(zip(solved[1], solved[2]))]
        result['candidates'] = candidates
        finite_errors = [c['rms_px'] for c in candidates if c['rms_px'] is not None]
        result['min_rms_px'] = min(finite_errors) if finite_errors else None
        # An alternative just outside the 2px acceptance threshold can still be
        # indistinguishable from the best one. Include every finite positive-
        # depth alternative in ambiguity comparison, even if its RMS is >2px.
        alternatives = sorted((c for c in candidates if c['rms_px'] is not None),
                              key=lambda c: c['rms_px'])
        accepted = [c for c in alternatives if c['valid']]
        if not accepted:
            result['reason'] = 'NO_VALID_PNP_ALTERNATIVE'
            return result
        best = accepted[0]
        others = [c for c in alternatives if c['index'] != best['index']]
        if others:
            gap = others[0]['rms_px'] - best['rms_px']
            minimum_gap = max(AMBIGUITY_ABSOLUTE_GAP_PX,
                              AMBIGUITY_RELATIVE_GAP * best['rms_px'])
            result.update(alternative_error_gap_px=gap, required_error_gap_px=minimum_gap)
            if gap <= minimum_gap:
                result['reason'] = 'AMBIGUOUS_IPPE_ALTERNATIVES'
                return result
        geometry = pose_from_observation(profile, best['rvec'], best['tvec'])
        result.update(valid=True, reason='OBSERVE_ONLY_POSE', selected_candidate=best['index'],
                      bench_geometry=geometry)
        return result
    except (ValueError, TypeError, OverflowError, cv2.error):
        result['reason'] = 'INVALID_PNP_OR_GEOMETRY'
        return result


def sensor_acquired_at(sensor_ns, *, boottime_now=None, monotonic_now=None):
    """Map libcamera CLOCK_BOOTTIME capture time without restamping old frames."""
    if type(sensor_ns) is not int or sensor_ns <= 0:
        return None
    if boottime_now is None:
        before = time.monotonic()
        boottime_now = time.clock_gettime(time.CLOCK_BOOTTIME)
        after = time.monotonic()
        if monotonic_now is None:
            monotonic_now = (before + after) / 2.0
    if monotonic_now is None:
        monotonic_now = time.monotonic()
    age = boottime_now - sensor_ns / 1e9
    if not math.isfinite(age) or not 0 <= age <= MAX_FRAME_AGE_S:
        return None
    return monotonic_now - age


def _camera_assets(directory):
    config = json.loads((directory / 'rover_config.json').read_text())
    # Calibration archive has no image-size metadata; retain the verified
    # capture size and balance used with these immutable existing intrinsics.
    if (type(config.get('resolution_width')) is not int
            or type(config.get('resolution_height')) is not int
            or (config['resolution_width'], config['resolution_height']) != (1280, 720)
            or isinstance(config.get('fisheye_balance'), bool)
            or config.get('fisheye_balance') != 0.9):
        raise ValueError('Existing calibration requires 1280x720 and fisheye balance 0.9')
    dim = (config['resolution_width'], config['resolution_height'])
    with np.load(directory / 'camera_calibration_fisheye.npz', allow_pickle=False) as calibration:
        K = np.asarray(calibration['K'], dtype=np.float64)
        D = np.asarray(calibration['D'], dtype=np.float64)
    if (K.shape != (3, 3) or D.shape not in ((4,), (4, 1), (1, 4))
            or not np.isfinite(K).all() or not np.isfinite(D).all()
            or K[0, 0] <= 0 or K[1, 1] <= 0
            or not np.allclose(K[2], [0, 0, 1], atol=1e-10, rtol=0)):
        raise ValueError('Invalid existing fisheye calibration')
    D = D.reshape(4, 1)
    rectified_K = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
        K, D, dim, np.eye(3), balance=config['fisheye_balance'])
    maps = cv2.fisheye.initUndistortRectifyMap(K, D, np.eye(3), rectified_K, dim, cv2.CV_16SC2)
    return dim, rectified_K, maps


def run(on_pose, stop_event, profile_path, on_status=None):
    """Camera only. Null callback on invalid/unseen/old frames or worker exit.

    Each accepted observation retains its original sensor acquisition time.
    Profile validation happens before camera initialization. No serial, MAVLink,
    parameter writes, estimator reset or TX permission exists in this module.
    """
    profile = load_profile(profile_path, ASSET_DIRECTORY / 'camera_calibration_fisheye.npz')
    dim, rectified_K, maps = _camera_assets(ASSET_DIRECTORY)
    # Importing the helper for offline numerical tests never initializes a camera.
    from picamera2 import Picamera2
    parameters = cv2.aruco.DetectorParameters()
    parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    parameters.cornerRefinementWinSize = 5
    parameters.cornerRefinementMaxIterations = 30
    parameters.cornerRefinementMinAccuracy = 0.01
    detector = cv2.aruco.ArucoDetector(
        cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50), parameters)
    camera, started = None, False
    try:
        camera = Picamera2()
        camera.configure(camera.create_preview_configuration(main=dict(size=dim, format='RGB888')))
        camera.start()
        started = True
        while not stop_event.is_set():
            request = camera.capture_request()
            try:
                acquired_at = sensor_acquired_at(request.get_metadata().get('SensorTimestamp'))
                frame = request.make_array('main').copy()
            finally:
                request.release()
            result = dict(valid=False, reason='INVALID_SENSOR_TIMESTAMP', candidates=[], min_rms_px=None,
                          thresholds=observation_thresholds(), tx_permitted=False)
            if acquired_at is not None:
                frame = cv2.remap(frame, maps[0], maps[1], cv2.INTER_LINEAR)
                gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
                corners, ids, _ = detector.detectMarkers(gray)
                indices = [] if ids is None else np.flatnonzero(ids.reshape(-1) == 24)
                result['reason'] = 'ID24_NOT_VISIBLE'
                if len(indices) == 1:
                    result = estimate_from_corners(profile, corners[int(indices[0])], rectified_K)
                elif len(indices) > 1:
                    result['reason'] = 'DUPLICATE_ID24'
                age = time.monotonic() - acquired_at
                result['frame_age_s'] = age
                if not 0 <= age <= MAX_FRAME_AGE_S:
                    result.update(valid=False, reason='STALE_AFTER_PROCESSING')
            # Owner uses status to invalidate an earlier pending observation;
            # publish this frame's pose only after that invalidation.
            result['acquired_at'] = acquired_at
            if on_status is not None:
                on_status(result)
            if result['valid']:
                geometry = result['bench_geometry']
                x, y, z = geometry['position_frd_m']
                on_pose(dict(acquired_at=acquired_at, source_marker=24, x=x, y=y, z=z,
                             q=geometry['quaternion_frd_wxyz'], bench_geometry=geometry,
                             reprojection_candidates=result['candidates'],
                             min_reprojection_rms_px=result['min_rms_px'], tx_permitted=False))
            else:
                on_pose(None)
    finally:
        try:
            on_pose(None)
        finally:
            if camera is not None:
                try:
                    if started:
                        camera.stop()
                finally:
                    camera.close()
