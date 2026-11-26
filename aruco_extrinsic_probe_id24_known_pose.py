"""Known-pose ID24 probe based on aruco_extrinsic_probe_id23_static.py; no file writes.

WORLD/ROVER FLU: +X forward, +Y left, +Z up. Rover is stationary at WORLD (1, 0, 0) m with zero roll/pitch/yaw.
CAMERA optical axes remain right/down/forward. Map orientations are used as
stored; this diagnostic does not promote them to authoritative calibration.
"""
import math
import signal

import cv2
import numpy as np
from picamera2 import Picamera2

# These readers have no camera-start or output-file side effects on import.
from aruco_marker_orientation_check import (
    PROJECT_DIR, load_rover_config, load_camera_calibration, load_marker_map,
)
from pose_transform_3d import (
    rvec_tvec_to_transform, marker_map_entry_to_world_transform,
    compose_transform, invert_transform, extract_translation,
    rotation_matrix_to_euler_deg,
)

FULL_3D_AUTHORITATIVE = False
TARGET_ID = 24
SAMPLE_COUNT = 300
REFERENCE_POSITION = np.array([-0.493025, +0.450940, +1.162671])
REFERENCE_HEADING_DEG = -5.620005


def wrap_deg(values):
    return (np.asarray(values) + 180.) % 360. - 180.


def angle_statistics(values):
    """Circular mean; median/std of shortest signed residuals about mean."""
    radians = np.radians(values)
    s, c = np.sin(radians).mean(), np.cos(radians).mean()
    if math.hypot(s, c) < 1e-12:
        return "mean=N/A median=N/A std=N/A (undefined circular center)"
    center = math.degrees(math.atan2(s, c))
    residuals = wrap_deg(np.asarray(values) - center)
    return (f"mean={float(wrap_deg(center)):+.6f} "
            f"median={float(wrap_deg(center + np.median(residuals))):+.6f} "
            f"std={np.std(residuals):.6f}")


def print_summary(samples, headings, total_frames):
    print(f"\nvalid_samples={len(samples)}/{SAMPLE_COUNT} total_frames={total_frames}")
    print(f"FULL_3D_AUTHORITATIVE={FULL_3D_AUTHORITATIVE}")
    if not samples:
        print("No valid ID24 pose samples; statistics unavailable.")
        return
    data = np.asarray(samples)
    print("Camera position from T_rover_camera in ROVER FLU (m); std=population; no outlier trimming:")
    for i, name in enumerate(("x", "y", "z")):
        values = data[:, i]
        print(f"{name}: mean={values.mean():+.6f} median={np.median(values):+.6f} "
              f"std={values.std():.6f} min={values.min():+.6f} max={values.max():+.6f}")
    print("Optical camera Euler orientation in ROVER (deg), R=Rz(yaw)Ry(pitch)Rx(roll):")
    print("Angular std is population std of wrapped residuals; Euler axes may couple near gimbal lock.")
    for i, name in enumerate(("roll", "pitch", "yaw"), 3):
        print(f"{name}: {angle_statistics(data[:, i])}")
    print("Camera forward heading yaw (optical +Z projected onto ROVER XY), deg:")
    if headings:
        print(angle_statistics(headings))
    else:
        print("N/A: optical forward is vertical")
    print("ID24 measured_minus_ID23_reference (reference: ID23 HOME medians):")
    for i, name in enumerate(("x", "y", "z")):
        ref = REFERENCE_POSITION[i]
        print(f"{name}: expected={ref:+.6f} m "
              f"delta_mean={data[:, i].mean() - ref:+.6f} m "
              f"delta_median={np.median(data[:, i]) - ref:+.6f} m")
    print(f"ID23_reference_heading={REFERENCE_HEADING_DEG:+.3f} deg (forward heading)")
    if headings:
        print("heading ID24 measured_minus_ID23_reference (deg): " +
              angle_statistics(wrap_deg(np.asarray(headings) - REFERENCE_HEADING_DEG)))


def main():
    config = load_rover_config(PROJECT_DIR / "rover_config.json")
    marker_map = load_marker_map(PROJECT_DIR / "marker_map.json")
    if marker_map["coordinate_frame"] != "HOME_FRONT_DATUM":
        raise ValueError("Known-pose probe requires coordinate_frame=HOME_FRONT_DATUM")
    if config["marker_size"] != 0.283:
        raise ValueError("This diagnostic requires marker_size = 0.283 m")
    marker = next((m for m in marker_map["markers"] if m["id"] == TARGET_ID), None)
    if marker is None:
        raise ValueError("marker_map.json must contain ID24")
    map_position = np.array([marker["position"][axis] for axis in ("x", "y", "z")])
    if not np.allclose(map_position, [4.000, 0.000, 0.000], atol=1e-9, rtol=0):
        raise ValueError("ID24 must have WORLD position (4.000, 0.000, 0.000) m")
    T_world_marker = marker_map_entry_to_world_transform(marker)
    # Known rover pose: translation +1 m along WORLD X, identity rotation.
    T_world_rover = np.eye(4)
    T_world_rover[:3, 3] = [1.000, 0.000, 0.000]
    T_rover_world = invert_transform(T_world_rover)
    K, D = load_camera_calibration(PROJECT_DIR / "camera_calibration_fisheye.npz")
    dim = (config["resolution_width"], config["resolution_height"])
    half = config["marker_size"] / 2.
    object_points = np.array([[-half, half, 0], [half, half, 0],
                              [half, -half, 0], [-half, -half, 0]], dtype=np.float32)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50)
    parameters = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(dictionary, parameters)
    zero_dist = np.zeros((5, 1))
    samples, headings = [], []
    total_frames = 0
    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    print("Keep rover stationary at WORLD (1.000, 0.000, 0.000) m; roll=pitch=yaw=0. Collecting exactly 300 valid ID24 poses.")
    print("WORLD/ROVER FLU: +X forward, +Y left, +Z up; optical CAMERA: right/down/forward.")
    print("T_world_camera = T_world_marker @ inverse(T_camera_marker)")
    print("T_rover_camera = inverse(T_world_rover) @ T_world_camera")
    print("Map orientation is used as stored; FULL_3D_AUTHORITATIVE=False.")
    print("No frame/time limit: absent or invalid ID24 poses do not count. Ctrl+C aborts the run.")
    picam2 = None
    started = False
    old_handler = signal.signal(signal.SIGINT, request_stop)
    try:
        picam2 = Picamera2()
        camera_config = picam2.create_preview_configuration(
            main={"size": dim, "format": "RGB888"})
        picam2.configure(camera_config)
        picam2.start()
        started = True
        new_K = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
            K, D, dim, np.eye(3), balance=config["fisheye_balance"])
        map1, map2 = cv2.fisheye.initUndistortRectifyMap(
            K, D, np.eye(3), new_K, dim, cv2.CV_16SC2)
        print(f"Image resolution: {dim[0]}x{dim[1]}")
        while len(samples) < SAMPLE_COUNT and not stop_requested:
            request = picam2.capture_request()
            try:
                frame = request.make_array("main").copy()
                metadata = request.get_metadata()
            finally:
                request.release()
            total_frames += 1
            frame = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR)
            gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
            corners, ids, _ = detector.detectMarkers(gray)
            flat_ids = np.array([], dtype=int) if ids is None else ids.flatten()
            indices = np.flatnonzero(flat_ids == TARGET_ID)
            if not indices.size:
                if total_frames % 120 == 0:
                    print(f"frames={total_frames} valid_samples={len(samples)}/{SAMPLE_COUNT}", flush=True)
                continue
            points = corners[int(indices[0])][0].astype(np.float32)
            try:
                success, rvec, tvec = cv2.solvePnP(
                    object_points, points, new_K, zero_dist,
                    flags=cv2.SOLVEPNP_IPPE_SQUARE)
                if not success:
                    continue
                T_camera_marker = rvec_tvec_to_transform(rvec, tvec)
                camera_points = (T_camera_marker[:3, :3] @ object_points.T
                                 + T_camera_marker[:3, 3:4])
                if not np.all(camera_points[2] > 0):
                    continue
                T_world_camera = compose_transform(
                    T_world_marker, invert_transform(T_camera_marker))
                T_rover_camera = compose_transform(T_rover_world, T_world_camera)
                candidate = np.r_[extract_translation(T_rover_camera),
                                  rotation_matrix_to_euler_deg(T_rover_camera[:3, :3])]
                if not np.isfinite(candidate).all():
                    continue
            except (ValueError, TypeError, cv2.error) as exc:
                print(f"Skipped invalid ID24 pose at frame {total_frames}: {exc}")
                continue
            samples.append(candidate)
            forward = T_rover_camera[:3, 2]
            if np.linalg.norm(forward[:2]) > 1e-10:
                headings.append(math.degrees(math.atan2(forward[1], forward[0])))
            if len(samples) % 25 == 0:
                print(f"valid_samples={len(samples)}/{SAMPLE_COUNT} frames={total_frames}", flush=True)
    finally:
        try:
            if picam2 is not None:
                try:
                    if started:
                        picam2.stop()
                finally:
                    picam2.close()
        finally:
            signal.signal(signal.SIGINT, old_handler)
            print_summary(samples, headings, total_frames)
    if len(samples) != SAMPLE_COUNT:
        print("ABORTED: incomplete run; 300 valid samples were not collected.")
        return 130
    print("COMPLETE: exactly 300 valid ID24 pose samples collected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
