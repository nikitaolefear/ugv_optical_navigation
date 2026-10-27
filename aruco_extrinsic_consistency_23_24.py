"""Stationary ID23/ID24 map/pose consistency diagnostic; no file writes or GUI.

Collect exactly 300 frames with valid poses of both markers. No rover pose or
camera offsets are assumed. WORLD is FLU; optical CAMERA is right/down/forward.
Pipeline follows aruco_extrinsic_probe.py: RGB888, fisheye remap, default
DICT_5X5_50 detector and SOLVEPNP_IPPE_SQUARE with new_K/zero distortion.
"""
import json
import math
import signal

import cv2
import numpy as np
from picamera2 import Picamera2

from aruco_marker_orientation_check import (
    PROJECT_DIR, load_rover_config, load_camera_calibration, load_marker_map,
)
from pose_transform_3d import (
    rvec_tvec_to_transform, marker_map_entry_to_world_transform,
    compose_transform, invert_transform, rotation_matrix_to_euler_deg,
)

FULL_3D_AUTHORITATIVE = False
MARKER_IDS = (23, 24)
PAIRED_SAMPLE_COUNT = 300


def wrap_deg(values):
    return (np.asarray(values) + 180.) % 360. - 180.


def statistics(values, angular=False):
    values = np.asarray(values, dtype=float)
    if not angular:
        return dict(mean=float(values.mean()), median=float(np.median(values)),
                    std=float(values.std()), min=float(values.min()), max=float(values.max()))
    radians = np.radians(values)
    s, c = np.sin(radians).mean(), np.cos(radians).mean()
    if math.hypot(s, c) < 1e-12:
        return dict(mean=None, median=None, std=None, min=None, max=None)
    center = math.degrees(math.atan2(s, c))
    residuals = wrap_deg(values - center)
    # Extrema are endpoints of the local unwrapped branch, so a distribution
    # straddling +/-180 is not reported as spanning almost 360 degrees.
    return dict(mean=float(wrap_deg(center)),
                median=float(wrap_deg(center + np.median(residuals))),
                std=float(residuals.std()),
                min=float(center + residuals.min()), max=float(center + residuals.max()))


def forward_heading(transform):
    forward = transform[:3, 2]  # Optical +Z expressed in WORLD FLU.
    if np.linalg.norm(forward[:2]) <= 1e-10:
        raise ValueError("forward heading undefined for vertical optical axis")
    return math.degrees(math.atan2(forward[1], forward[0]))


def print_summary(samples, differences, total_frames):
    count = len(differences)
    print(f"\npaired_samples={count}/{PAIRED_SAMPLE_COUNT} total_frames={total_frames}")
    print(f"FULL_3D_AUTHORITATIVE={FULL_3D_AUTHORITATIVE}")
    print("Translation units=m; angles=deg; std=population; no outlier trimming.")
    print("Angular mean=circular; median/std use wrapped residuals about mean;")
    print("angular min/max use the local unwrapped branch about circular mean.")
    if not count:
        print("No valid paired samples; statistics unavailable.")
        return
    data = np.asarray(differences)
    print("Differences: ID24 minus ID23; dx/dy/dz expressed in WORLD FLU:")
    for i, name in enumerate(("dx", "dy", "dz", "translation_difference_magnitude",
                              "heading_difference_deg")):
        print(f"{name}: " + json.dumps(statistics(data[:, i], angular=i == 4)))
    for mid in MARKER_IDS:
        print(f"ID{mid}: T_world_camera statistics from the same {count} paired frames")
        values = np.asarray(samples[mid])
        for i, name in enumerate(("x", "y", "z", "roll", "pitch", "yaw", "forward_heading")):
            print(f"  {name}: " + json.dumps(statistics(values[:, i], angular=i >= 3)))
    print("Euler orientation: optical CAMERA in WORLD, R=Rz(yaw)Ry(pitch)Rx(roll);")
    print("forward_heading uses optical +Z projected into WORLD XY.")


def main():
    config = load_rover_config(PROJECT_DIR / "rover_config.json")
    marker_map = load_marker_map(PROJECT_DIR / "marker_map.json")
    markers = {m["id"]: m for m in marker_map["markers"] if m["id"] in MARKER_IDS}
    if set(markers) != set(MARKER_IDS):
        raise ValueError("marker_map.json must contain both ID23 and ID24")
    if config["marker_size"] != 0.283:
        raise ValueError("This diagnostic requires marker_size = 0.283 m")
    world_markers = {mid: marker_map_entry_to_world_transform(markers[mid])
                     for mid in MARKER_IDS}
    K, D = load_camera_calibration(PROJECT_DIR / "camera_calibration_fisheye.npz")
    dim = (config["resolution_width"], config["resolution_height"])
    half = config["marker_size"] / 2.
    object_points = np.array([[-half, half, 0], [half, half, 0],
                              [half, -half, 0], [-half, -half, 0]], dtype=np.float32)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50)
    parameters = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(dictionary, parameters)
    zero_dist = np.zeros((5, 1))
    samples = {mid: [] for mid in MARKER_IDS}
    differences = []
    total_frames = 0
    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    print("Keep rover stationary. No known rover pose or camera offset reference is used.")
    print("WORLD FLU: +X forward, +Y left, +Z up; CAMERA optical: right/down/forward.")
    print(f"marker_map coordinate_frame={marker_map['coordinate_frame']}")
    print("Collecting exactly 300 paired valid poses; no frame/time limit. Ctrl+C aborts.")
    print("FULL_3D_AUTHORITATIVE=False; map orientations are used as stored.")
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
        while len(differences) < PAIRED_SAMPLE_COUNT and not stop_requested:
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
            transforms, headings = {}, {}
            for mid in MARKER_IDS:
                indices = np.flatnonzero(flat_ids == mid)
                if not indices.size:
                    continue
                points = corners[int(indices[0])][0].astype(np.float32)
                try:
                    success, rvec, tvec = cv2.solvePnP(
                        object_points, points, new_K, zero_dist,
                        flags=cv2.SOLVEPNP_IPPE_SQUARE)
                    if not success:
                        continue
                    camera_marker = rvec_tvec_to_transform(rvec, tvec)
                    camera_points = (camera_marker[:3, :3] @ object_points.T
                                     + camera_marker[:3, 3:4])
                    if not np.all(camera_points[2] > 0):
                        continue
                    world_camera = compose_transform(
                        world_markers[mid], invert_transform(camera_marker))
                    heading = forward_heading(world_camera)
                    if not np.isfinite(world_camera).all() or not math.isfinite(heading):
                        continue
                    transforms[mid] = world_camera
                    headings[mid] = heading
                except (ValueError, TypeError, cv2.error) as exc:
                    print(f"Skipped ID{mid} frame={total_frames}: {exc}")
            if len(transforms) != 2:
                if total_frames % 120 == 0:
                    print(f"frames={total_frames} paired_samples={len(differences)}/300", flush=True)
                continue
            first, second = transforms[23], transforms[24]
            delta_T = compose_transform(invert_transform(first), second)
            delta_position = second[:3, 3] - first[:3, 3]
            magnitude = float(np.linalg.norm(delta_position))
            heading_difference = float(wrap_deg(headings[24] - headings[23]))
            differences.append([*delta_position, magnitude, heading_difference])
            for mid in MARKER_IDS:
                transform = transforms[mid]
                samples[mid].append([*transform[:3, 3],
                                     *rotation_matrix_to_euler_deg(transform[:3, :3]),
                                     headings[mid]])
            print(json.dumps({
                "paired_sample": len(differences), "frame": total_frames,
                "delta_T_inverse_world_camera23_times_world_camera24": delta_T.tolist(),
                "dx": float(delta_position[0]), "dy": float(delta_position[1]),
                "dz": float(delta_position[2]), "translation_difference_magnitude": magnitude,
                "heading23": headings[23], "heading24": headings[24],
                "heading_difference_deg": heading_difference,
            }), flush=True)
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
            print_summary(samples, differences, total_frames)
    if len(differences) != PAIRED_SAMPLE_COUNT:
        print("ABORTED: fewer than 300 paired samples collected.")
        return 130
    print("COMPLETE: exactly 300 paired valid ID23/ID24 samples collected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
