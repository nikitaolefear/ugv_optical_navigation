"""Inspect rejected ArUco quads over 120 frames, stationary at HOME, without GUI.

Rejected candidates have no decoded identity. R labels are per-frame vertical
ranks, not marker IDs. Cluster association is a spatial diagnostic heuristic.
"""
import signal

import cv2
import numpy as np
from picamera2 import Picamera2

from aruco_marker_pixel_size_diag import (
    PROJECT_DIR, TARGET_IDS, load_camera_calibration, load_rover_config, measure,
)


def image_order(points):
    """Order an undecoded convex quad clockwise from its upper-left corner.

    Unlike decoded corners, this image-relative convention has no marker axes.
    """
    points = np.asarray(points, dtype=np.float32).reshape(4, 2).copy()
    center = points.mean(axis=0)
    angles = np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0])
    points = points[np.argsort(angles)]
    return np.roll(points, -int(np.argmin(points.sum(axis=1))), axis=0)


def associate(clusters, candidates, frame_idx):
    """Greedy nearest association, at most one quad per cluster per frame.

    Centers must be within max(10 px, 25% of mean largest dimension),
    and corresponding widths/heights must differ by at most a factor of 1.5.
    """
    edges = []
    for ci, cluster in enumerate(clusters):
        reference = np.mean(cluster["rows"], axis=0)
        for ri, (_, row) in enumerate(candidates):
            ratio = np.asarray(row[6:8]) / reference[6:8]
            distance = float(np.linalg.norm(np.asarray(row[:2]) - reference[:2]))
            gate = max(10., .25 * max(reference[6:8]))
            if distance <= gate and np.all((ratio >= 1 / 1.5) & (ratio <= 1.5)):
                edges.append((distance, ci, ri))
    used_clusters, used_rows = set(), set()
    for _, ci, ri in sorted(edges):
        if ci in used_clusters or ri in used_rows:
            continue
        clusters[ci]["rows"].append(candidates[ri][1])
        clusters[ci]["frames"].add(frame_idx)
        used_clusters.add(ci)
        used_rows.add(ri)
    for ri, (_, row) in enumerate(candidates):
        if ri not in used_rows:
            clusters.append({"rows": [row], "frames": {frame_idx}})


def annotate(frame, decoded, candidates, frame_idx):
    image = frame.copy()
    for points, text, color in (
        [(p, f"ID{mid}", (0, 255, 0)) for mid, p in decoded]
        + [(p, f"R{i} {row[6]:.1f}x{row[7]:.1f} px", (0, 0, 255))
           for i, (p, row) in enumerate(candidates, 1)]
    ):
        cv2.polylines(image, [np.rint(points).astype(np.int32)], True, color, 2)
        x, y = np.min(points, axis=0).astype(int)
        (_, text_height), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, .5, 1)
        origin = (max(0, x), max(text_height + 2, y - 5))
        cv2.putText(image, text, origin, cv2.FONT_HERSHEY_SIMPLEX, .5, (0, 0, 0), 3)
        cv2.putText(image, text, origin, cv2.FONT_HERSHEY_SIMPLEX, .5, color, 1)
    cv2.putText(image, f"Frame {frame_idx}; R labels: vertical rank, not ID",
                (8, image.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, .5,
                (255, 255, 255), 1)
    return image


def main():
    config = load_rover_config(PROJECT_DIR / "rover_config.json")
    K, D = load_camera_calibration(PROJECT_DIR / "camera_calibration_fisheye.npz")
    dim = (config["resolution_width"], config["resolution_height"])
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50)
    parameters = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(dictionary, parameters)
    clusters = []
    decoded_frames = {mid: 0 for mid in TARGET_IDS}
    best = None
    best_score = None
    captured = 0
    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

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
        print("Keep rover stationary at HOME; capturing 120 frames.")
        print(f"Configured image resolution: {dim[0]}x{dim[1]}")
        print("Rejected filter: mean_width_px >= 20 and area_px2 >= 150")
        print("type,frame,label,center_x,center_y,top_px,bottom_px,left_px,"
              "right_px,mean_width_px,mean_height_px,area_px2")
        for frame_idx in range(1, 121):
            if stop_requested:
                break
            request = picam2.capture_request()
            try:
                frame = request.make_array("main").copy()
                metadata = request.get_metadata()
            finally:
                request.release()
            frame = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR)
            captured += 1
            if captured == 1:
                print(f"Remapped image resolution: {frame.shape[1]}x{frame.shape[0]}")
            gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
            corners, ids, rejectedImgPoints = detector.detectMarkers(gray)
            decoded = []
            seen = set()
            for points, mid in zip(corners, () if ids is None else ids.flatten()):
                mid = int(mid)
                points = points[0].astype(np.float32).copy()
                decoded.append((mid, points))
                if mid in TARGET_IDS:
                    row = (*points.mean(axis=0), *measure(points))
                    print(f"DECODED,{frame_idx},ID{mid}," +
                          ",".join(f"{v:.3f}" for v in row))
                    seen.add(mid)
            for mid in seen:
                decoded_frames[mid] += 1
            candidates = []
            for quad in rejectedImgPoints:
                points = image_order(quad)
                row = (*points.mean(axis=0), *measure(points))
                if row[6] >= 20 and row[8] >= 150:
                    candidates.append((points, row))
            candidates.sort(key=lambda item: (item[1][1], item[1][0]))
            for rank, (_, row) in enumerate(candidates, 1):
                print(f"REJECTED,{frame_idx},R{rank}," +
                      ",".join(f"{v:.3f}" for v in row))
            associate(clusters, candidates, frame_idx)
            score = (len(candidates), len(seen))
            if best_score is None or score > best_score:
                best = (frame.copy(), decoded, candidates, frame_idx)
                best_score = score
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

    print(f"\nCaptured frames: {captured}")
    for mid in TARGET_IDS:
        print(f"DECODED ID{mid}: detected_frames={decoded_frames[mid]}")
    print("Rejected clusters: ranked by frames_seen, then center stability; "
          "spatial matches do not establish ID24/ID25 identity.")
    clusters.sort(key=lambda c: (-len(c["frames"]),
                  float(np.std(np.asarray(c["rows"])[:, :2], axis=0).sum())))
    if not clusters:
        print("No rejected quads passed the size filter.")
    for rank, cluster in enumerate(clusters, 1):
        rows = np.asarray(cluster["rows"])
        print(f"Cluster C{rank}: frames_seen={len(cluster['frames'])}/{captured}")
        for name, column in (("center_x", 0), ("center_y", 1)):
            print(f"  {name}: mean={rows[:, column].mean():.3f} "
                  f"std={rows[:, column].std():.3f}")
        for name, column in (("width_px", 6), ("height_px", 7), ("area_px2", 8)):
            print(f"  {name}: mean={rows[:, column].mean():.3f} "
                  f"median={np.median(rows[:, column]):.3f}")
    if best is not None:
        output = PROJECT_DIR / "aruco_rejected_pixel_size_diag.jpg"
        if not cv2.imwrite(str(output), annotate(*best)):
            raise OSError(f"Could not save {output}")
        print(f"saved={output}; selected_frame={best[3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
