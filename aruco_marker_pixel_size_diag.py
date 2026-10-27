"""Measure mapped ArUco markers in remapped pixels with the rover stationary.

Collects 120 frames by default. Prints measurements and summary statistics;
opens no GUI and writes no images, configuration, or map files.
"""
import argparse
import signal

import cv2
import numpy as np
from picamera2 import Picamera2

from aruco_marker_orientation_check import (
    PROJECT_DIR,
    load_camera_calibration,
    load_marker_map,
    load_rover_config,
)


TARGET_IDS = (23, 24, 25)


def measure(points):
    """Use decoded canonical order c0, c1, c2, c3 (never image-sort)."""
    top = float(np.linalg.norm(points[1] - points[0]))
    bottom = float(np.linalg.norm(points[2] - points[3]))
    left = float(np.linalg.norm(points[3] - points[0]))
    right = float(np.linalg.norm(points[2] - points[1]))
    return (top, bottom, left, right, (top + bottom) / 2,
            (left + right) / 2, float(cv2.contourArea(points)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=120,
                        help="number of frames to capture (default: 120)")
    args = parser.parse_args()
    if args.frames < 1:
        parser.error("--frames must be positive")

    rover_config = load_rover_config(PROJECT_DIR / "rover_config.json")
    marker_map = load_marker_map(PROJECT_DIR / "marker_map.json")
    mapped_ids = {marker["id"] for marker in marker_map["markers"]}
    target_ids = mapped_ids.intersection(TARGET_IDS)
    K, D = load_camera_calibration(PROJECT_DIR / "camera_calibration_fisheye.npz")
    dim = (rover_config["resolution_width"], rover_config["resolution_height"])
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50)
    parameters = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(dictionary, parameters)
    samples = {marker_id: [] for marker_id in TARGET_IDS}
    stop_requested = False
    captured_frames = 0

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    picam2 = None
    camera_started = False
    old_handler = signal.signal(signal.SIGINT, request_stop)
    try:
        picam2 = Picamera2()
        config = picam2.create_preview_configuration(
            main={"size": dim, "format": "RGB888"}
        )
        picam2.configure(config)
        picam2.start()
        camera_started = True
        new_K = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
            K, D, dim, np.eye(3), balance=rover_config["fisheye_balance"]
        )
        map1, map2 = cv2.fisheye.initUndistortRectifyMap(
            K, D, np.eye(3), new_K, dim, cv2.CV_16SC2
        )
        print("Keep the rover stationary during capture.")
        print(f"Configured image resolution: {dim[0]}x{dim[1]}")
        print("frame,id,top_px,bottom_px,left_px,right_px,mean_width_px,"
              "mean_height_px,area_px2")
        for frame_idx in range(1, args.frames + 1):
            if stop_requested:
                break
            request = picam2.capture_request()
            try:
                frame = request.make_array("main").copy()
                metadata = request.get_metadata()
            finally:
                request.release()
            frame = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR)
            if captured_frames == 0:
                print(f"Remapped image resolution: {frame.shape[1]}x{frame.shape[0]}")
            captured_frames += 1
            gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
            corners, ids, _ = detector.detectMarkers(gray)
            flat_ids = np.array([], dtype=int) if ids is None else ids.flatten()
            detected = set()
            for index, marker_id in enumerate(flat_ids):
                marker_id = int(marker_id)
                if marker_id not in target_ids or marker_id in detected:
                    continue
                detected.add(marker_id)
                points = corners[index][0].astype(np.float32).copy()
                values = measure(points)
                samples[marker_id].append(values)
                print(f"{frame_idx},{marker_id}," +
                      ",".join(f"{value:.3f}" for value in values))
    finally:
        try:
            if picam2 is not None:
                try:
                    if camera_started:
                        picam2.stop()
                finally:
                    picam2.close()
        finally:
            signal.signal(signal.SIGINT, old_handler)

    print(f"\nCaptured frames: {captured_frames}")
    for marker_id in TARGET_IDS:
        rows = samples[marker_id]
        print(f"ID {marker_id}: detected_frames={len(rows)}")
        for name, column in (("width_px", 4), ("height_px", 5), ("area_px2", 6)):
            if not rows:
                print(f"  {name}: mean=N/A median=N/A min=N/A max=N/A")
                continue
            values = np.asarray(rows)[:, column]
            print(f"  {name}: mean={np.mean(values):.3f} "
                  f"median={np.median(values):.3f} min={np.min(values):.3f} "
                  f"max={np.max(values):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
