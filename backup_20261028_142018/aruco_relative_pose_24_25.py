"""Relative ID24 -> ID25 pose from 200 paired frames, without marker map.

Camera/remap/detection/canonical corner ordering/IPPE pipeline is copied from
aruco_range_bias_diag_23_24.py. No camera extrinsic values enter measurement.
Writes one CSV row per pair; an existing CSV is never overwritten. No GUI.
"""
import csv
import json
import signal

import cv2
import numpy as np
from picamera2 import Picamera2

from aruco_marker_orientation_check import (
    PROJECT_DIR, load_rover_config, load_camera_calibration,
)
from pose_transform_3d import (
    rvec_tvec_to_transform, compose_transform, invert_transform,
    rotation_matrix_to_euler_deg,
)
from aruco_extrinsic_consistency_23_24 import statistics

FULL_3D_AUTHORITATIVE = False
MARKER_IDS = (24, 25)
PAIRED_FRAME_COUNT = 200
CSV_PATH = PROJECT_DIR / "aruco_relative_pose_24_25.csv"
# Expected geometry only: Rz(-90).T @ WORLD displacement (+1,0,0).
# Rz(-90) = [[0,1,0],[-1,0,0],[0,0,1]], hence local displacement (0,+1,0).
# This constant is never used to derive the measured relative transform.
EXPECTED_TRANSLATION = np.array([0., 1., 0.])
MEASURE_FIELDS = ["relative_x", "relative_y", "relative_z", "relative_translation_norm",
                  "relative_roll", "relative_pitch", "relative_yaw"]
ERROR_FIELDS = ["error_x", "error_y", "error_z", "error_3d_magnitude"]
FIELDS = ["paired_frame_index", "frame_index", "SensorTimestamp", *MEASURE_FIELDS, *ERROR_FIELDS]


def print_summary(pairs, total_frames):
    print(f"\npaired_frames={len(pairs)}/{PAIRED_FRAME_COUNT} total_frames={total_frames}")
    print(f"FULL_3D_AUTHORITATIVE={FULL_3D_AUTHORITATIVE}")
    print("Translation/error=m; angles=deg; std=population; no outlier trimming.")
    print("Angular mean=circular; median/std use wrapped residuals; min/max use local unwrapped branch.")
    if not pairs:
        print("No paired data; statistics unavailable.")
        return
    for field in MEASURE_FIELDS + ERROR_FIELDS:
        print(f"{field}: " + json.dumps(statistics(
            [row[field] for row in pairs], angular=field in MEASURE_FIELDS[4:])))
    print("Errors: measured translation minus (0,+1,0); 3D magnitude=norm(error vector).")
    print("Expected relative rotation=(0,0,0) deg; expected center separation=1.000 +/-0.010 m.")
    within = sum(abs(row["relative_translation_norm"] - 1.) <= .010 for row in pairs)
    print(f"Samples within physical separation interval [0.990,1.010] m: {within}/{len(pairs)}")


def main():
    config = load_rover_config(PROJECT_DIR / "rover_config.json")
    if config["marker_size"] != 0.283:
        raise ValueError("This diagnostic requires marker_size = 0.283 m for both markers")
    K, D = load_camera_calibration(PROJECT_DIR / "camera_calibration_fisheye.npz")
    dim = (config["resolution_width"], config["resolution_height"])
    half = config["marker_size"] / 2.
    object_points = np.array([[-half, half, 0], [half, half, 0],
                              [half, -half, 0], [-half, -half, 0]], dtype=np.float32)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50)
    parameters = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(dictionary, parameters)
    zero_dist = np.zeros((5, 1))
    pairs = []
    total_frames = 0
    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    # Exclusive creation enforces the requirement to preserve existing files.
    with CSV_PATH.open("x", newline="", encoding="utf-8") as log:
        writer = csv.DictWriter(log, fieldnames=FIELDS)
        writer.writeheader()
        log.flush()
        print("Keep rover and markers stationary; collecting exactly 200 paired frames.")
        print("Relative translation is expressed in canonical marker24 axes; no map/extrinsics used.")
        print("Canonical object corners: (-h,+h), (+h,+h), (+h,-h), (-h,-h).")
        print("Sign check: Rz(-90).T @ (1,0,0) = (0,+1,0); expected local Y=+1 m.")
        print("Expected rotation=identity; physical center separation=1.000 +/-0.010 m.")
        print("FULL_3D_AUTHORITATIVE=False. No frame/time limit; Ctrl+C aborts.")
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
            while len(pairs) < PAIRED_FRAME_COUNT and not stop_requested:
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
                current = {}
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
                        current[mid] = camera_marker
                    except (ValueError, TypeError, cv2.error) as exc:
                        print(f"Skipped ID{mid} frame={total_frames}: {exc}")
                if len(current) != 2:
                    if total_frames % 120 == 0:
                        print(f"frames={total_frames} paired_frames={len(pairs)}/200", flush=True)
                    continue
                relative = compose_transform(invert_transform(current[24]), current[25])
                translation = relative[:3, 3]
                angles = rotation_matrix_to_euler_deg(relative[:3, :3])
                error = translation - EXPECTED_TRANSLATION
                row = dict(zip(MEASURE_FIELDS, [*translation, np.linalg.norm(translation), *angles]))
                row.update(zip(ERROR_FIELDS, [*error, np.linalg.norm(error)]))
                row.update(paired_frame_index=len(pairs) + 1, frame_index=total_frames,
                           SensorTimestamp=metadata.get("SensorTimestamp", ""))
                writer.writerow(row)
                log.flush()
                pairs.append(row)
                if len(pairs) % 25 == 0:
                    print(f"paired_frames={len(pairs)}/200 total_frames={total_frames}", flush=True)
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
                print_summary(pairs, total_frames)
                print(f"CSV: {CSV_PATH}")
    if len(pairs) != PAIRED_FRAME_COUNT:
        print("ABORTED: incomplete run; partial CSV retained.")
        return 130
    print("COMPLETE: exactly 200 paired frames (200 CSV rows).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
