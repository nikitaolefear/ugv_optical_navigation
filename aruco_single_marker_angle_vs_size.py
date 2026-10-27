"""ID23 orientation versus apparent size; capture until Ctrl+C, no GUI.

Move rover slowly straight toward the same stationary marker without turning.
Pose Euler angles describe MARKER in optical CAMERA coordinates, RzRyRx.
Camera forward heading is optical +Z projected onto MARKER XY, not WORLD yaw.
No marker map or rover extrinsic. Existing CSV is never overwritten.
"""
import csv
from datetime import datetime
import math
import signal

import cv2
import numpy as np

from aruco_subpix_pnp_compare_23_24 import (
    PROJECT_DIR, Picamera2, load_rover_config, load_camera_calibration,
    solve, invert_transform, rotation_matrix_to_euler_deg, statistics,
)

FULL_3D_AUTHORITATIVE = False
TARGET_ID = 23
CSV_PATH = PROJECT_DIR / "aruco_single_marker_angle_vs_size.csv"
FIELDS = ["timestamp", "frame_index", "SensorTimestamp", "marker_id",
          "marker_width_px", "marker_height_px", "polygon_area_px2",
          "rvec_x", "rvec_y", "rvec_z", "tvec_x", "tvec_y", "tvec_z",
          "camera_to_marker_distance", "roll", "pitch", "yaw",
          "camera_forward_heading_marker_deg"]


def local_unwrap(values):
    """Unwrap around circular center without relying on acquisition order."""
    radians = np.radians(values)
    s, c = np.sin(radians).mean(), np.cos(radians).mean()
    if math.hypot(s, c) < 1e-12:
        return None
    center = math.degrees(math.atan2(s, c))
    return center + (np.asarray(values) - center + 180.) % 360. - 180.


def correlation(x, angles):
    x = np.asarray(x, dtype=float)
    y = local_unwrap(angles)
    if len(x) < 3 or y is None or np.ptp(x) < 1e-12 or np.ptp(y) < 1e-12:
        return "N/A (insufficient variation/samples or undefined angular center)"
    return f"{np.corrcoef(x, y)[0, 1]:+.6f}"


def summary(samples, frames):
    print(f"\nframes={frames} valid_ID23_samples={len(samples)}")
    print(f"FULL_3D_AUTHORITATIVE={FULL_3D_AUTHORITATIVE}")
    print("Marker pose Euler angles in optical CAMERA, deg; population std.")
    print("Angular mean=circular; median/std use shortest residuals about mean.")
    print("Height bins: <30, [30,40), [40,50), [50,70], >70 px.")
    groups = (("<30 px", lambda h: h < 30),
              ("30-40 px", lambda h: 30 <= h < 40),
              ("40-50 px", lambda h: 40 <= h < 50),
              ("50-70 px", lambda h: 50 <= h <= 70),
              (">70 px", lambda h: h > 70))
    for label, accepts in groups:
        rows = [r for r in samples if accepts(r["marker_height_px"])]
        print(f"\n{label}: samples={len(rows)}")
        if not rows:
            print("  statistics=N/A")
            continue
        for key in ("roll", "pitch", "yaw"):
            stats = statistics([r[key] for r in rows], angular=True)
            print(f"  {key}: mean={stats['mean']} median={stats['median']} std={stats['std']}")
        for key in ("camera_to_marker_distance", "marker_width_px", "marker_height_px"):
            print(f"  mean {key}={np.mean([r[key] for r in rows]):.6f}")
    print("\nPearson correlation (angles locally unwrapped around circular center):")
    if not samples:
        print("No data; correlations=N/A")
        return
    for x in ("marker_height_px", "camera_to_marker_distance"):
        for angle in ("roll", "pitch"):
            print(f"{x} vs {angle}: " + correlation(
                [r[x] for r in samples], [r[angle] for r in samples]))
    print("Euler correlations depend on branch and become unreliable near gimbal lock.")


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
    parameters = cv2.aruco.DetectorParameters()
    parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    detector = cv2.aruco.ArucoDetector(dictionary, parameters)
    zero_dist = np.zeros((5, 1))
    samples = []
    frames = 0
    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    with CSV_PATH.open("x", newline="", encoding="utf-8") as log:
        writer = csv.DictWriter(log, fieldnames=FIELDS)
        writer.writeheader()
        log.flush()
        print("Move rover slowly in a straight line toward stationary ID23, without turning.")
        print("Capture until Ctrl+C. Native SUBPIX + IPPE_SQUARE; 1280x720 RGB888, balance=0.9.")
        print("Heading is camera optical forward in marker XY; no WORLD/map/extrinsic used.")
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
            while not stop_requested:
                request = picam2.capture_request()
                try:
                    frame = request.make_array("main").copy()
                    metadata = request.get_metadata()
                finally:
                    request.release()
                frames += 1
                timestamp = datetime.now().astimezone().isoformat(timespec="microseconds")
                frame = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR)
                gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
                corners, ids, _ = detector.detectMarkers(gray)
                indices = np.array([], dtype=int) if ids is None else np.flatnonzero(ids.flatten() == TARGET_ID)
                if len(indices) != 1:
                    if frames % 120 == 0:
                        print(f"frames={frames} valid_samples={len(samples)}; ID23 absent/ambiguous", flush=True)
                    continue
                points = corners[int(indices[0])][0].astype(np.float32)
                try:
                    camera_marker = solve(points, objects, new_K, zero_dist, cv2.SOLVEPNP_IPPE_SQUARE)
                    angles = rotation_matrix_to_euler_deg(camera_marker[:3, :3])
                    rvec, _ = cv2.Rodrigues(camera_marker[:3, :3])
                    tvec = camera_marker[:3, 3]
                    if not np.isfinite([*rvec.reshape(3), *tvec, *angles]).all():
                        continue
                    marker_camera = invert_transform(camera_marker)
                    forward = marker_camera[:3, 2]
                    heading = (math.degrees(math.atan2(forward[1], forward[0]))
                               if np.linalg.norm(forward[:2]) > 1e-10 else "")
                except (ValueError, TypeError, cv2.error) as exc:
                    print(f"Skipped frame={frames}: {exc}")
                    continue
                edges = np.linalg.norm(np.roll(points, -1, axis=0) - points, axis=1)
                row = dict(timestamp=timestamp, frame_index=frames,
                           SensorTimestamp=metadata.get("SensorTimestamp", ""), marker_id=TARGET_ID,
                           marker_width_px=float((edges[0] + edges[2]) / 2),
                           marker_height_px=float((edges[1] + edges[3]) / 2),
                           polygon_area_px2=float(cv2.contourArea(points)),
                           camera_to_marker_distance=float(np.linalg.norm(tvec)),
                           camera_forward_heading_marker_deg=heading)
                for prefix, values in (("rvec", rvec.reshape(3)), ("tvec", tvec)):
                    row.update({f"{prefix}_{axis}": float(value)
                                for axis, value in zip(("x", "y", "z"), values)})
                row.update(zip(("roll", "pitch", "yaw"), angles))
                writer.writerow(row)
                log.flush()
                samples.append(row)
                if len(samples) % 60 == 0:
                    print(f"samples={len(samples)} height={row['marker_height_px']:.2f} px "
                          f"distance={row['camera_to_marker_distance']:.3f} m", flush=True)
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
                summary(samples, frames)
                print(f"CSV: {CSV_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
