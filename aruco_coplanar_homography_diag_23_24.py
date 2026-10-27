"""Joint/cross-prediction planar homographies and IPPE on 200 static pairs.

Native ArUco SUBPIX settings match aruco_live.py. No map or rover extrinsic.
CSV stores per-corner residual vectors/norms, homographies and relative pose.
Existing CSV is never overwritten. No GUI or production/config changes.
"""
import csv
import signal

import cv2
import numpy as np

# Importing these definitions does not start capture or read marker maps.
from aruco_subpix_pnp_compare_23_24 import (
    PROJECT_DIR, Picamera2, load_rover_config, load_camera_calibration,
    detect, solve, compose_transform, invert_transform,
    rotation_matrix_to_euler_deg, statistics,
)

FULL_3D_AUTHORITATIVE = False
PAIRED_COUNT = 200
CSV_PATH = PROJECT_DIR / "aruco_coplanar_homography_diag_23_24.csv"
GROUPS = ("joint_id23", "joint_id24", "predict24_from23", "predict23_from24")
POSE_FIELDS = ("relative_x", "relative_y", "relative_z", "translation_norm",
               "relative_roll", "relative_pitch", "relative_yaw")
FIELDS = ["paired_frame_index", "frame_index", "SensorTimestamp", *POSE_FIELDS]
FIELDS += [f"id{mid}_c{i}_{axis}" for mid in (23, 24) for i in range(4) for axis in ("x", "y")]
FIELDS += [f"{group}_c{i}_{suffix}" for group in GROUPS for i in range(4)
           for suffix in ("dx_px", "dy_px", "residual_px")]
FIELDS += [f"{group}_{stat}_px" for group in (*GROUPS, "joint_all8")
           for stat in ("mean", "median", "max")]
FIELDS += [f"{name}_h{i}{j}" for name in ("joint", "id23", "id24")
           for i in range(3) for j in range(3)]


def fit_homography(plane, pixels):
    # All correspondences participate: no RANSAC selection or outlier rejection.
    matrix, _ = cv2.findHomography(plane, pixels, method=0)
    if matrix is None or not np.isfinite(matrix).all() or np.linalg.matrix_rank(matrix) != 3:
        raise ValueError("Invalid/degenerate homography")
    return matrix


def residuals(matrix, plane, observed):
    homogeneous = np.column_stack((plane, np.ones(len(plane)))) @ matrix.T
    if np.any(np.abs(homogeneous[:, 2]) < 1e-12):
        raise ValueError("Homography prediction at infinity")
    delta = homogeneous[:, :2] / homogeneous[:, 2:3] - observed
    if not np.isfinite(delta).all():
        raise ValueError("Nonfinite reprojection")
    return delta


def report(samples, errors, frames):
    print(f"\npaired_frames={len(samples)}/{PAIRED_COUNT}; total_frames={frames}")
    print(f"FULL_3D_AUTHORITATIVE={FULL_3D_AUTHORITATIVE}")
    if not samples:
        print("No valid paired frames; statistics unavailable.")
        return
    print("Residual summaries pool per-corner Euclidean errors over all accepted frames.")
    for title, keys in (("JOINT HOMOGRAPHY", ("joint_all8", "joint_id23", "joint_id24")),
                        ("23 -> predict 24", ("predict24_from23",)),
                        ("24 -> predict 23", ("predict23_from24",))):
        print(title)
        for key in keys:
            values = np.concatenate(errors[key])
            print(f"  {key}: mean={values.mean():.6f} median={np.median(values):.6f} "
                  f"max={values.max():.6f} px")
    print("IPPE (m / deg; population std; angular mean=circular, median/std=wrapped residuals):")
    for key in POSE_FIELDS:
        summary = statistics([row[key] for row in samples], angular=key in POSE_FIELDS[4:])
        print(f"  {key}: mean={summary['mean']} median={summary['median']} std={summary['std']}")


def main():
    config = load_rover_config(PROJECT_DIR / "rover_config.json")
    dim = (config["resolution_width"], config["resolution_height"])
    if dim != (1280, 720) or config["fisheye_balance"] != 0.9 or config["marker_size"] != 0.283:
        raise ValueError("Requires existing config: 1280x720, balance=0.9, marker_size=0.283")
    K, D = load_camera_calibration(PROJECT_DIR / "camera_calibration_fisheye.npz")
    half = config["marker_size"] / 2.
    objects = np.array([[-half, half, 0], [half, half, 0],
                        [half, -half, 0], [-half, -half, 0]], dtype=np.float32)
    # Same canonical axes/orientation for both markers, in ID23 plane coordinates.
    plane23 = objects[:, :2].astype(np.float64)
    plane24 = plane23 + [0., 1.000]
    plane8 = np.vstack((plane23, plane24))
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50)
    parameters = cv2.aruco.DetectorParameters()
    parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    detector = cv2.aruco.ArucoDetector(dictionary, parameters)
    zero_dist = np.zeros((5, 1))
    samples = []
    errors = {key: [] for key in (*GROUPS, "joint_all8")}
    frames = 0
    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    with CSV_PATH.open("x", newline="", encoding="utf-8") as log:
        writer = csv.DictWriter(log, fieldnames=FIELDS)
        writer.writeheader()
        log.flush()
        print("Keep rover and coplanar markers stationary; 200 paired frames, no frame/time limit.")
        print("Assumed geometry: size=0.283 m; centers=(0,0),(0,+1) m; same orientation.")
        print("Native SUBPIX, IPPE_SQUARE; no marker map or rover extrinsic. Ctrl+C aborts.")
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
            while len(samples) < PAIRED_COUNT and not stop_requested:
                request = picam2.capture_request()
                try:
                    frame = request.make_array("main").copy()
                    metadata = request.get_metadata()
                finally:
                    request.release()
                frames += 1
                frame = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR)
                gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
                points = detect(detector, gray)
                if set(points) != {23, 24}:
                    if frames % 120 == 0:
                        print(f"frames={frames} paired_frames={len(samples)}/200", flush=True)
                    continue
                try:
                    poses = {mid: solve(points[mid], objects, new_K, zero_dist,
                                        cv2.SOLVEPNP_IPPE_SQUARE) for mid in (23, 24)}
                    relative = compose_transform(invert_transform(poses[23]), poses[24])
                    angles = rotation_matrix_to_euler_deg(relative[:3, :3])
                    joint = fit_homography(plane8, np.vstack((points[23], points[24])))
                    h23 = fit_homography(plane23, points[23])
                    h24 = fit_homography(plane24, points[24])
                    deltas = {
                        "joint_id23": residuals(joint, plane23, points[23]),
                        "joint_id24": residuals(joint, plane24, points[24]),
                        "predict24_from23": residuals(h23, plane24, points[24]),
                        "predict23_from24": residuals(h24, plane23, points[23]),
                    }
                except (ValueError, TypeError, cv2.error, np.linalg.LinAlgError) as exc:
                    print(f"Skipped frame={frames}: {exc}")
                    continue
                translation = relative[:3, 3]
                row = dict(zip(POSE_FIELDS, map(float, [*translation, np.linalg.norm(translation), *angles])))
                row.update(paired_frame_index=len(samples) + 1, frame_index=frames,
                           SensorTimestamp=metadata.get("SensorTimestamp", ""))
                for mid in (23, 24):
                    for i, corner in enumerate(points[mid]):
                        for axis, value in zip(("x", "y"), corner):
                            row[f"id{mid}_c{i}_{axis}"] = float(value)
                norms = {}
                for key, delta in deltas.items():
                    norms[key] = np.linalg.norm(delta, axis=1)
                    for i in range(4):
                        for suffix, value in zip(("dx_px", "dy_px", "residual_px"),
                                                 [*delta[i], norms[key][i]]):
                            row[f"{key}_c{i}_{suffix}"] = float(value)
                norms["joint_all8"] = np.r_[norms["joint_id23"], norms["joint_id24"]]
                for key, values in norms.items():
                    for stat, value in (("mean", values.mean()), ("median", np.median(values)),
                                        ("max", values.max())):
                        row[f"{key}_{stat}_px"] = float(value)
                for name, matrix in (("joint", joint), ("id23", h23), ("id24", h24)):
                    row.update({f"{name}_h{i}{j}": float(matrix[i, j])
                                for i in range(3) for j in range(3)})
                writer.writerow(row)
                log.flush()
                samples.append(row)
                for key, values in norms.items():
                    errors[key].append(values.copy())
                if len(samples) % 25 == 0:
                    print(f"paired_frames={len(samples)}/200 total_frames={frames}", flush=True)
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
                report(samples, errors, frames)
                print(f"CSV: {CSV_PATH}")
    if len(samples) != PAIRED_COUNT:
        print("ABORTED: incomplete run; partial CSV retained.")
        return 130
    print("COMPLETE: exactly 200 paired frames.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
