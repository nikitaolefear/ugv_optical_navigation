"""Compare RAW/SUBPIX x IPPE/ITERATIVE on exactly 200 common paired frames.

No map or rover extrinsic is used. No GUI or configuration writes. CSV has
four rows per paired frame; existing CSV is never overwritten.
"""
import csv
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
MARKER_IDS = (23, 24)
PAIRED_COUNT = 200
CSV_PATH = PROJECT_DIR / "aruco_subpix_pnp_compare_23_24.csv"
METHODS = (
    ("RAW + IPPE", "RAW", cv2.SOLVEPNP_IPPE_SQUARE),
    ("SUBPIX + IPPE", "SUBPIX", cv2.SOLVEPNP_IPPE_SQUARE),
    ("RAW + ITERATIVE", "RAW", cv2.SOLVEPNP_ITERATIVE),
    ("SUBPIX + ITERATIVE", "SUBPIX", cv2.SOLVEPNP_ITERATIVE),
)
VALUE_FIELDS = ["relative_x", "relative_y", "relative_z", "translation_norm",
                "error_y", "error_3d_magnitude", "relative_roll",
                "relative_pitch", "relative_yaw"]
SHIFT_FIELDS = [f"id{mid}_c{i}_shift_{axis}" for mid in MARKER_IDS
                for i in range(4) for axis in ("dx", "dy", "norm_px")]
FIELDS = ["paired_frame_index", "frame_index", "SensorTimestamp", "method",
          *VALUE_FIELDS, *SHIFT_FIELDS]


def detect(detector, gray):
    corners, ids, _ = detector.detectMarkers(gray)
    result = {}
    for points, mid in zip(corners, () if ids is None else ids.flatten()):
        mid = int(mid)
        if mid in MARKER_IDS:
            # Duplicate IDs make physical correspondence ambiguous: skip frame.
            if mid in result:
                return {}
            result[mid] = points[0].astype(np.float32).copy()
    return result


def solve(points, objects, new_K, zero_dist, flag):
    # Independent solve: no initial rvec/tvec or useExtrinsicGuess.
    success, rvec, tvec = cv2.solvePnP(objects, points, new_K, zero_dist, flags=flag)
    if not success:
        raise ValueError("solvePnP returned false")
    transform = rvec_tvec_to_transform(rvec, tvec)
    camera_points = transform[:3, :3] @ objects.T + transform[:3, 3:4]
    if not np.all(camera_points[2] > 0):
        raise ValueError("marker corners are not all in front of camera")
    return transform


def print_summary(samples, shifts, total_frames):
    count = len(samples[METHODS[0][0]])
    print(f"\npaired_frames={count}/{PAIRED_COUNT}; total_frames={total_frames}")
    print(f"FULL_3D_AUTHORITATIVE={FULL_3D_AUTHORITATIVE}")
    print("Translation/error=m, angles=deg; population std; no outlier trimming.")
    print("Angles: circular mean, median/std of wrapped residuals about mean.")
    if not count:
        print("No common paired samples; statistics unavailable.")
        return
    for name, _, _ in METHODS:
        print(f"\n{name}")
        for field in VALUE_FIELDS:
            stats = statistics([r[field] for r in samples[name]],
                               angular=field in VALUE_FIELDS[6:])
            print(f"  {field}: mean={stats['mean']} median={stats['median']} std={stats['std']}")
    print("\nSUBPIX - RAW corner displacement, same canonical corner index:")
    for mid in MARKER_IDS:
        delta = np.asarray(shifts[mid])  # paired frames x 4 corners x (dx,dy)
        norms = np.linalg.norm(delta, axis=2)
        print(f"ID{mid} all corners: mean={norms.mean():.6f} "
              f"median={np.median(norms):.6f} max={norms.max():.6f} px")
        for i in range(4):
            values = norms[:, i]
            print(f"  c{i}: mean={values.mean():.6f} median={np.median(values):.6f} "
                  f"max={values.max():.6f} px; "
                  f"mean_dx={delta[:, i, 0].mean():+.6f} mean_dy={delta[:, i, 1].mean():+.6f}")
    print("\nMETHOD                  relative_y_mean   error_mm")
    errors = {}
    for name, _, _ in METHODS:
        mean_y = float(np.mean([r["relative_y"] for r in samples[name]]))
        errors[name] = abs(mean_y - 1.)
        print(f"{name:<24}{mean_y:>15.6f}{(mean_y - 1.) * 1000:>11.3f}")
    minimum = min(errors.values())
    best = [name for name, error in errors.items() if abs(error - minimum) <= 1e-12]
    print("Smallest absolute mean-Y error from 1.000 m: " + ", ".join(best)
          + f" ({minimum * 1000:.3f} mm)")
    if count != PAIRED_COUNT:
        print("Ranking is provisional: incomplete run.")


def main():
    config = load_rover_config(PROJECT_DIR / "rover_config.json")
    dim = (config["resolution_width"], config["resolution_height"])
    if dim != (1280, 720) or config["fisheye_balance"] != 0.9 or config["marker_size"] != 0.283:
        raise ValueError("Requires existing config: 1280x720, balance=0.9, marker_size=0.283")
    K, D = load_camera_calibration(PROJECT_DIR / "camera_calibration_fisheye.npz")
    half = config["marker_size"] / 2.
    objects = np.array([[-half, half, 0], [half, half, 0],
                        [half, -half, 0], [-half, -half, 0]], dtype=np.float32)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50)
    # Exact settings from aruco_live.py:252-260; all other fields stay default.
    subpix_parameters = cv2.aruco.DetectorParameters()
    subpix_parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    raw_parameters = cv2.aruco.DetectorParameters()
    raw_parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_NONE
    detectors = {"RAW": cv2.aruco.ArucoDetector(dictionary, raw_parameters),
                 "SUBPIX": cv2.aruco.ArucoDetector(dictionary, subpix_parameters)}
    zero_dist = np.zeros((5, 1))
    samples = {name: [] for name, _, _ in METHODS}
    shifts = {mid: [] for mid in MARKER_IDS}
    total_frames = 0
    paired = 0
    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    with CSV_PATH.open("x", newline="", encoding="utf-8") as log:
        writer = csv.DictWriter(log, fieldnames=FIELDS)
        writer.writeheader()
        log.flush()
        print("Keep rover and both 0.283 m markers stationary; expected relative translation=(0,+1,0) m.")
        print("Collecting 200 frames valid for all four methods; no frame/time limit. Ctrl+C aborts.")
        print("RAW/SUBPIX settings match aruco_live.py; no map or rover extrinsic used.")
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
            while paired < PAIRED_COUNT and not stop_requested:
                request = picam2.capture_request()
                try:
                    frame = request.make_array("main").copy()
                    metadata = request.get_metadata()
                finally:
                    request.release()
                total_frames += 1
                frame = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR)
                gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
                detected = {kind: detect(detector, gray) for kind, detector in detectors.items()}
                current = {}
                if all(set(points) == set(MARKER_IDS) for points in detected.values()):
                    for name, kind, flag in METHODS:
                        try:
                            poses = {mid: solve(detected[kind][mid], objects, new_K, zero_dist, flag)
                                     for mid in MARKER_IDS}
                            relative = compose_transform(invert_transform(poses[23]), poses[24])
                            translation = relative[:3, 3]
                            error = translation - [0., 1., 0.]
                            angles = rotation_matrix_to_euler_deg(relative[:3, :3])
                            values = [*translation, np.linalg.norm(translation), error[1],
                                      np.linalg.norm(error), *angles]
                            if not np.isfinite(values).all():
                                continue
                            current[name] = dict(zip(VALUE_FIELDS, map(float, values)))
                        except (ValueError, TypeError, cv2.error) as exc:
                            print(f"Skipped {name} frame={total_frames}: {exc}")
                if len(current) != len(METHODS):
                    if total_frames % 120 == 0:
                        print(f"frames={total_frames} paired_frames={paired}/200", flush=True)
                    continue
                delta = {mid: detected["SUBPIX"][mid] - detected["RAW"][mid] for mid in MARKER_IDS}
                shift_values = {}
                for mid in MARKER_IDS:
                    for i, vector in enumerate(delta[mid]):
                        for axis, value in zip(("dx", "dy", "norm_px"), [*vector, np.linalg.norm(vector)]):
                            shift_values[f"id{mid}_c{i}_shift_{axis}"] = float(value)
                csv_rows = []
                for name, _, _ in METHODS:
                    csv_rows.append({"paired_frame_index": paired + 1, "frame_index": total_frames,
                                     "SensorTimestamp": metadata.get("SensorTimestamp", ""),
                                     "method": name, **current[name], **shift_values})
                writer.writerows(csv_rows)
                log.flush()
                paired += 1
                for name, _, _ in METHODS:
                    samples[name].append(current[name])
                for mid in MARKER_IDS:
                    shifts[mid].append(delta[mid].copy())
                if paired % 25 == 0:
                    print(f"paired_frames={paired}/200 total_frames={total_frames}", flush=True)
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
                print_summary(samples, shifts, total_frames)
                print(f"CSV: {CSV_PATH}")
    if paired != PAIRED_COUNT:
        print("ABORTED: partial CSV retained.")
        return 130
    print("COMPLETE: exactly 200 common paired frames, 800 CSV data rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
