"""Stationary range/size diagnostic: exactly 200 paired ID23/ID24 frames.

Uses the consistency probe's camera/remap/detector/IPPE pipeline. CSV contains
two rows per paired frame; paired differences are repeated on both rows.
No GUI or config/map writes. Existing CSV is never overwritten.
"""
import csv
import json
import math
import signal

import cv2
import numpy as np
from picamera2 import Picamera2

from aruco_extrinsic_consistency_23_24 import (
    PROJECT_DIR, load_rover_config, load_camera_calibration, load_marker_map,
    rvec_tvec_to_transform, marker_map_entry_to_world_transform,
    compose_transform, invert_transform, forward_heading, wrap_deg, statistics,
)

FULL_3D_AUTHORITATIVE = False
MARKER_IDS = (23, 24)
PAIRED_FRAME_COUNT = 200
CSV_PATH = PROJECT_DIR / "aruco_range_bias_diag_23_24.csv"
SIZE_FIELDS = ["center_x", "center_y", "top_edge_px", "right_edge_px",
               "bottom_edge_px", "left_edge_px", "mean_width_px",
               "mean_height_px", "polygon_area_px2"]
DELTA_FIELDS = ["distance24_minus_distance23", "world_camera_x24_minus_x23",
                "world_camera_y24_minus_y23", "world_camera_z24_minus_z23",
                "heading24_minus_heading23"]
RATIO_FIELDS = ["width23_over_width24", "distance24_over_distance23",
                "width_ratio_minus_distance_ratio"]
FIELDS = ["paired_frame_index", "frame_index", "SensorTimestamp", "marker_id",
          *[f"c{i}_{axis}" for i in range(4) for axis in ("x", "y")],
          *SIZE_FIELDS, *[f"rvec_{axis}" for axis in ("x", "y", "z")],
          *[f"tvec_{axis}" for axis in ("x", "y", "z")],
          "camera_to_marker_distance", "world_camera_x", "world_camera_y",
          "world_camera_z", "forward_heading_deg", *DELTA_FIELDS, *RATIO_FIELDS]


def size_measurements(points):
    # Canonical decoded corner order; no sorting or corner refinement changes.
    top, right, bottom, left = [float(np.linalg.norm(points[(i + 1) % 4] - points[i]))
                                for i in range(4)]
    return dict(zip(SIZE_FIELDS, [*points.mean(axis=0), top, right, bottom, left,
                                 (top + bottom) / 2, (left + right) / 2,
                                 float(cv2.contourArea(points))]))


def print_summary(rows, pairs, total_frames):
    print(f"\npaired_frames={len(pairs)}/{PAIRED_FRAME_COUNT} total_frames={total_frames}")
    print(f"FULL_3D_AUTHORITATIVE={FULL_3D_AUTHORITATIVE}")
    print("std=population; no outlier trimming; distance=m, size=px, heading=deg.")
    print("Heading statistics: circular mean; wrapped residual median/std;")
    print("heading min/max on local unwrapped branch about the circular mean.")
    if not pairs:
        print("No paired data; statistics unavailable.")
        return
    for mid in MARKER_IDS:
        for field in ("mean_width_px", "mean_height_px", "camera_to_marker_distance"):
            print(f"ID{mid} {field}: " + json.dumps(statistics([r[field] for r in rows[mid]])))
    for field in DELTA_FIELDS + RATIO_FIELDS:
        print(f"{field}: " + json.dumps(statistics(
            [p[field] for p in pairs], angular=field == "heading24_minus_heading23")))
    print("Scale comparison: width23/width24 versus distance24/distance23.")
    print("Inverse-range scaling is approximate: equal physical size and comparable")
    print("foreshortening are required; perspective primarily depends on optical depth,")
    print("whereas the distance ratio here uses norm(tvec). Both come from the same corners.")


def main():
    config = load_rover_config(PROJECT_DIR / "rover_config.json")
    marker_map = load_marker_map(PROJECT_DIR / "marker_map.json")
    markers = {m["id"]: m for m in marker_map["markers"] if m["id"] in MARKER_IDS}
    if set(markers) != set(MARKER_IDS):
        raise ValueError("marker_map.json must contain ID23 and ID24")
    if config["marker_size"] != 0.283:
        raise ValueError("This diagnostic requires marker_size = 0.283 m")
    world_markers = {mid: marker_map_entry_to_world_transform(markers[mid]) for mid in MARKER_IDS}
    K, D = load_camera_calibration(PROJECT_DIR / "camera_calibration_fisheye.npz")
    dim = (config["resolution_width"], config["resolution_height"])
    half = config["marker_size"] / 2.
    object_points = np.array([[-half, half, 0], [half, half, 0],
                              [half, -half, 0], [-half, -half, 0]], dtype=np.float32)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50)
    parameters = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(dictionary, parameters)
    zero_dist = np.zeros((5, 1))
    rows = {mid: [] for mid in MARKER_IDS}
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
        print("WORLD FLU; CAMERA optical right/down/forward; no rover pose/offset reference.")
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
                        world_camera = compose_transform(
                            world_markers[mid], invert_transform(camera_marker))
                        heading = forward_heading(world_camera)
                        if not np.isfinite(world_camera).all() or not math.isfinite(heading):
                            continue
                        row = {"frame_index": total_frames,
                               "SensorTimestamp": metadata.get("SensorTimestamp", ""),
                               "marker_id": mid, **size_measurements(points),
                               "camera_to_marker_distance": float(np.linalg.norm(tvec)),
                               "forward_heading_deg": heading}
                        row.update({f"c{i}_{axis}": float(points[i, j])
                                    for i in range(4) for j, axis in enumerate(("x", "y"))})
                        for prefix, values in (("rvec", rvec.reshape(3)), ("tvec", tvec.reshape(3)),
                                               ("world_camera", world_camera[:3, 3])):
                            row.update({f"{prefix}_{axis}": float(value)
                                        for axis, value in zip(("x", "y", "z"), values)})
                        current[mid] = row
                    except (ValueError, TypeError, cv2.error) as exc:
                        print(f"Skipped ID{mid} frame={total_frames}: {exc}")
                if len(current) != 2:
                    if total_frames % 120 == 0:
                        print(f"frames={total_frames} paired_frames={len(pairs)}/200", flush=True)
                    continue
                a, b = current[23], current[24]
                difference = {"distance24_minus_distance23":
                              b["camera_to_marker_distance"] - a["camera_to_marker_distance"],
                              "heading24_minus_heading23": float(wrap_deg(
                                  b["forward_heading_deg"] - a["forward_heading_deg"]))}
                for axis in ("x", "y", "z"):
                    difference[f"world_camera_{axis}24_minus_{axis}23"] = (
                        b[f"world_camera_{axis}"] - a[f"world_camera_{axis}"])
                width_ratio = a["mean_width_px"] / b["mean_width_px"]
                range_ratio = b["camera_to_marker_distance"] / a["camera_to_marker_distance"]
                difference.update(zip(RATIO_FIELDS, (width_ratio, range_ratio, width_ratio - range_ratio)))
                for mid in MARKER_IDS:
                    current[mid].update(difference)
                    current[mid]["paired_frame_index"] = len(pairs) + 1
                writer.writerows(current[mid] for mid in MARKER_IDS)
                log.flush()
                pairs.append(difference)
                for mid in MARKER_IDS:
                    rows[mid].append(current[mid])
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
                print_summary(rows, pairs, total_frames)
                print(f"CSV: {CSV_PATH}")
    if len(pairs) != PAIRED_FRAME_COUNT:
        print("ABORTED: incomplete run; partial CSV retained.")
        return 130
    print("COMPLETE: exactly 200 paired frames (400 CSV rows).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
