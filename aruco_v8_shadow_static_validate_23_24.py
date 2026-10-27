"""Validate V8 shadow refinement against native SUBPIX on 200 static pairs.

AST wrapper loads exact V8 function definitions without top-level map reads,
prints or camera startup. Only V8-refined corners feed relative IPPE pose.
Writes one CSV row per pair without overwriting existing CSV. No navigation.
"""
import csv
import signal

import cv2
import numpy as np
from picamera2 import Picamera2

import ast
import math
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent


def load_v8_functions():
    """Execute only selected function definitions; no V8 map/config/main side effects."""
    path = PROJECT_DIR / "aruco_active_marker_selector_v8.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = {"refine_shadow_corners", "load_rover_config", "load_camera_calibration"}
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in selected} != names:
        raise RuntimeError("Required V8 function definitions are missing")
    namespace = dict(cv2=cv2, np=np, math=math, json=json, sys=sys, Path=Path)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return tuple(namespace[name] for name in
                 ("refine_shadow_corners", "load_rover_config", "load_camera_calibration"))


refine_shadow_corners, load_rover_config, load_camera_calibration = load_v8_functions()
from pose_transform_3d import (
    rvec_tvec_to_transform, compose_transform, invert_transform,
    rotation_matrix_to_euler_deg,
)
from aruco_extrinsic_consistency_23_24 import statistics

FULL_3D_AUTHORITATIVE = False
MARKER_IDS = (23, 24)
PAIRED_COUNT = 200
CSV_PATH = PROJECT_DIR / "aruco_v8_shadow_static_validate_23_24.csv"
METHODS = (("V8 SHADOW + IPPE", "V8", cv2.SOLVEPNP_IPPE_SQUARE),)
REFERENCE_Y_MEAN = 1.009733
Y_TOLERANCE_M = 0.003
# Explicit numerical equivalence criterion; not fitted to observed data.
CORNER_EQUIVALENCE_TOLERANCE_PX = 0.001
VALUE_FIELDS = ["relative_x", "relative_y", "relative_z", "translation_norm",
                "error_y", "error_3d_magnitude", "relative_roll",
                "relative_pitch", "relative_yaw"]
COMPARISONS = ("v8_minus_raw", "native_minus_raw", "v8_minus_native")
SHIFT_FIELDS = [f"id{mid}_{kind}_c{i}_{axis}" for mid in MARKER_IDS
                for kind in COMPARISONS for i in range(4) for axis in ("dx", "dy", "norm_px")]
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
        print("V8 SHADOW VALIDATION: FAIL; reason: no valid paired samples")
        return
    for name, _, _ in METHODS:
        print(f"\n{name}")
        for field in VALUE_FIELDS:
            stats = statistics([r[field] for r in samples[name]],
                               angular=field in VALUE_FIELDS[6:])
            print(f"  {field}: mean={stats['mean']} median={stats['median']} std={stats['std']}")
    print("\nCorner comparisons on the same accepted frames (px):")
    corner_means = []
    for mid in MARKER_IDS:
        for kind in COMPARISONS:
            delta = np.asarray(shifts[mid][kind])
            norms = np.linalg.norm(delta, axis=2)
            print(f"ID{mid} {kind}: mean={norms.mean():.9f} "
                  f"median={np.median(norms):.9f} max={norms.max():.9f}")
            for i in range(4):
                v = norms[:, i]
                print(f"  c{i}: mean={v.mean():.9f} median={np.median(v):.9f} max={v.max():.9f}")
            if kind == "v8_minus_native":
                corner_means.append(float(norms.mean()))
    mean_y = float(np.mean([r["relative_y"] for r in samples[METHODS[0][0]]]))
    y_difference = abs(mean_y - REFERENCE_Y_MEAN)
    reasons = []
    if count != PAIRED_COUNT:
        reasons.append("incomplete run")
    if y_difference >= Y_TOLERANCE_M:
        reasons.append("mean relative Y differs from previous SUBPIX+IPPE by >= 0.003 m")
    if max(corner_means) > CORNER_EQUIVALENCE_TOLERANCE_PX:
        reasons.append("mean V8-native corner difference exceeds 0.001 px for at least one ID")
    print(f"relative_y_mean={mean_y:.9f}; reference={REFERENCE_Y_MEAN:.9f}; "
          f"absolute_difference={y_difference:.9f} m; required < {Y_TOLERANCE_M} m")
    print(f"V8-native mean difference by ID={corner_means} px; "
          f"required <= {CORNER_EQUIVALENCE_TOLERANCE_PX} px per ID")
    print("Physical reference: translation=(0,+1,0) m, separation=1.000 +/-0.010 m.")
    print("V8 SHADOW VALIDATION: " + ("FAIL" if reasons else "PASS"))
    print("Reason: " + ("; ".join(reasons) if reasons else
          "200 paired frames; mean Y matches previous result and V8/native corners agree within stated tolerances."))


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
    shifts = {mid: {kind: [] for kind in COMPARISONS} for mid in MARKER_IDS}
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
        print("Collecting 200 frames valid for V8 solvePnP and both corner detectors; no frame/time limit. Ctrl+C aborts.")
        print("Native SUBPIX settings match aruco_live.py; actual V8 refinement function loaded safely; no map/extrinsic used.")
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
                    try:
                        detected["V8"] = {mid: refine_shadow_corners(gray, detected["RAW"][mid], dictionary)
                                          for mid in MARKER_IDS}
                        if not all(np.isfinite(p).all() for p in detected["V8"].values()):
                            detected["V8"] = {}
                    except (ValueError, cv2.error) as exc:
                        print(f"Skipped V8 refinement frame={total_frames}: {exc}")
                        detected["V8"] = {}
                if "V8" in detected and all(set(points) == set(MARKER_IDS) for points in detected.values()):
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
                delta = {mid: {
                    "v8_minus_raw": detected["V8"][mid] - detected["RAW"][mid],
                    "native_minus_raw": detected["SUBPIX"][mid] - detected["RAW"][mid],
                    "v8_minus_native": detected["V8"][mid] - detected["SUBPIX"][mid],
                } for mid in MARKER_IDS}
                shift_values = {}
                for mid in MARKER_IDS:
                    for kind in COMPARISONS:
                        for i, vector in enumerate(delta[mid][kind]):
                            for axis, value in zip(("dx", "dy", "norm_px"), [*vector, np.linalg.norm(vector)]):
                                shift_values[f"id{mid}_{kind}_c{i}_{axis}"] = float(value)
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
                    for kind in COMPARISONS:
                        shifts[mid][kind].append(delta[mid][kind].copy())
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
    print("COMPLETE: exactly 200 common paired frames, 200 CSV data rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
