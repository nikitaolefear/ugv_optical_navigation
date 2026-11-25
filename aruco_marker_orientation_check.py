"""Visual decoded-corner orientation check; no pose or map orientation inference.

Run manually when ready. Collects 90 frames by default, saves one annotated
image; Ctrl+C selects the best frame collected so far. No GUI is opened.
"""
from picamera2 import Picamera2
import cv2
import numpy as np
import math
import json
from pathlib import Path
import sys
import signal
import argparse

def load_rover_config(config_path):
    try:
        with config_path.open(encoding="utf-8") as config_file:
            config = json.load(config_file)

        if not isinstance(config, dict):
            raise ValueError("configuration must be a JSON object")

        numeric_keys = (
            "camera_height",
            "camera_forward_offset",
            "camera_lateral_offset",
            "camera_yaw_deg",
            "rover_front_offset",
            "marker_size",
            "fisheye_balance",
        )
        integer_keys = (
            "target_id",
            "resolution_width",
            "resolution_height",
        )

        for key in numeric_keys + integer_keys:
            if key not in config:
                raise ValueError(f"missing required setting '{key}'")

        for key in numeric_keys:
            value = config[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"'{key}' must be a finite number")

        for key in integer_keys:
            if type(config[key]) is not int:
                raise ValueError(f"'{key}' must be an integer")

        for key in (
            "camera_height",
            "marker_size",
            "resolution_width",
            "resolution_height",
        ):
            if config[key] <= 0:
                raise ValueError(f"'{key}' must be greater than zero")

        if not 0 <= config["target_id"] < 50:
            raise ValueError("'target_id' must be between 0 and 49 for DICT_5X5_50")

        if not 0.0 <= config["fisheye_balance"] <= 1.0:
            raise ValueError("'fisheye_balance' must be between 0 and 1")

        if type(config.get("log_measurements", False)) is not bool:
            raise ValueError("'log_measurements' must be a boolean")

        log_file = config.get("log_file", "measurements.csv")
        if (
            not isinstance(log_file, str)
            or not log_file.strip()
            or log_file in (".", "..")
            or Path(log_file).name != log_file
        ):
            raise ValueError("'log_file' must be a nonempty filename in the project directory")

    except (OSError, UnicodeError, ValueError, OverflowError) as exc:
        print(f"Error loading rover configuration '{config_path}': {exc}", file=sys.stderr)
        raise SystemExit(1)

    return config

def load_camera_calibration(calibration_path):
    try:
        if not calibration_path.is_file():
            raise FileNotFoundError("file is missing or is not a regular file")

        calibration = np.load(calibration_path, allow_pickle=False)
        if not isinstance(calibration, np.lib.npyio.NpzFile):
            raise ValueError("file must be an NPZ archive")

        with calibration:
            missing_keys = [key for key in ("K", "D") if key not in calibration.files]
            if missing_keys:
                raise ValueError(f"missing required keys: {', '.join(missing_keys)}")

            K = calibration["K"]
            D = calibration["D"]

            if not isinstance(K, np.ndarray) or K.shape != (3, 3):
                raise ValueError(
                    "'K' must have shape (3, 3); "
                    f"got {getattr(K, 'shape', None)}"
                )

            if not isinstance(D, np.ndarray) or D.shape not in ((4, 1), (1, 4), (4,)):
                raise ValueError(
                    "'D' must contain exactly 4 fisheye coefficients with shape "
                    "(4, 1), (1, 4), or (4,); "
                    f"got {getattr(D, 'shape', None)}"
                )

            return K, D

    except Exception as exc:
        print(f"Error loading camera calibration '{calibration_path}': {exc}", file=sys.stderr)
        raise SystemExit(1)

def validate_markers(markers):
    if not isinstance(markers, list) or not markers:
        raise ValueError("'markers' must be a nonempty list")
    for index, marker in enumerate(markers):
        if not isinstance(marker, dict):
            raise ValueError(f"markers[{index}] must be an object")
        for key in ("id", "placement", "position", "orientation"):
            if key not in marker:
                raise ValueError(f"markers[{index}] is missing '{key}'")
        if marker["placement"] not in ("floor", "wall"):
            raise ValueError(f"markers[{index}].placement must be 'floor' or 'wall'")
        for group, keys in (("position", ("x", "y", "z")),
                            ("orientation", ("roll", "pitch", "yaw"))):
            values = marker[group]
            if not isinstance(values, dict):
                raise ValueError(f"markers[{index}].{group} must be an object")
            for key in keys:
                if key not in values:
                    raise ValueError(f"markers[{index}].{group} is missing '{key}'")
                value = values[key]
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value)):
                    raise ValueError(f"markers[{index}].{group}.{key} must be a finite number")
    marker_ids = [marker["id"] for marker in markers]
    if any(type(marker_id) is not int or not 0 <= marker_id < 50
           for marker_id in marker_ids):
        raise ValueError("Marker IDs must be integers in DICT_5X5_50 (0..49)")
    if len(set(marker_ids)) != len(marker_ids):
        raise ValueError("Marker IDs must be unique")
    positions = [marker["position"]["x"] for marker in markers]
    if any(left >= right for left, right in zip(positions, positions[1:])):
        raise ValueError("Marker position.x values must be strictly increasing")

def load_marker_map(map_path):
    try:
        with map_path.open(encoding="utf-8") as map_file:
            marker_map = json.load(map_file)
        if not isinstance(marker_map, dict):
            raise ValueError("marker map must be a JSON object")
        if type(marker_map.get("version")) is not int or marker_map["version"] != 2:
            raise ValueError("marker map 'version' must be 2")
        frame = marker_map.get("coordinate_frame")
        if not isinstance(frame, str) or not frame.strip():
            raise ValueError("'coordinate_frame' must be a nonempty string")
        if marker_map.get("position_unit") != "m":
            raise ValueError("'position_unit' must be 'm'")
        if marker_map.get("orientation_unit") != "deg":
            raise ValueError("'orientation_unit' must be 'deg'")
        validate_markers(marker_map.get("markers"))
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Error loading marker map '{map_path}': {exc}", file=sys.stderr)
        raise SystemExit(1)
    return marker_map

PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = PROJECT_DIR / "marker_orientation_check.jpg"


def pixel(point):
    return tuple(int(round(v)) for v in point)


def label(image, text, point, color=(255, 255, 255), scale=0.55):
    x, y = pixel(point)
    (width, height), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    x = max(2, min(x, image.shape[1] - width - 3))
    y = max(height + 3, min(y, image.shape[0] - 4))
    cv2.putText(image, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(image, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)


def annotate(frame, detected, frame_idx):
    image = frame.copy()
    for marker_id, points in sorted(detected.items()):
        center = points.mean(axis=0)
        plus_x = (points[1] + points[2]) / 2
        plus_y = (points[0] + points[1]) / 2
        cv2.polylines(image, [np.rint(points).astype(np.int32)], True, (0, 255, 255), 2, cv2.LINE_AA)
        for index, corner in enumerate(points):
            cv2.circle(image, pixel(corner), 5, (0, 255, 255), -1, cv2.LINE_AA)
            outward = corner - center
            outward /= max(float(np.linalg.norm(outward)), 1.)
            label(image, f"c{index}", corner + 18 * outward)
        cv2.arrowedLine(image, pixel(center), pixel(plus_x), (0, 0, 255), 3, cv2.LINE_AA, tipLength=0.25)
        cv2.arrowedLine(image, pixel(center), pixel(plus_y), (0, 255, 0), 3, cv2.LINE_AA, tipLength=0.25)
        label(image, "+X MARKER", plus_x + [10, 20], (0, 0, 255))
        label(image, "+Y MARKER", plus_y + [10, -15], (0, 255, 0))
        cv2.circle(image, pixel(center), 5, (255, 255, 255), -1, cv2.LINE_AA)
        label(image, f"ID{marker_id} center", center + [10, 20])
        # Coordinates/directions refer to the original remapped image, before header.
        print(json.dumps({
            "marker_id": marker_id,
            "canonical_corners_pixel_coordinates": {f"c{i}": p.tolist() for i, p in enumerate(points)},
            "plus_x_arrow_pixel_direction": (plus_x - center).tolist(),
            "plus_y_arrow_pixel_direction": (plus_y - center).tolist(),
        }))
    panel_height = 168
    image = cv2.copyMakeBorder(image, panel_height, 0, 0, 0, cv2.BORDER_CONSTANT, value=(25, 25, 25))
    lines = [
        "WORLD convention: +X = rover forward; +Y = rover left; +Z = up",
        "For zero map orientation: marker +X must point rover forward",
        "For zero map orientation: marker +Y must point rover left",
        "Marker +Z = out of printed face; arrows follow canonical decoded corners",
        f"Selected frame={frame_idx}; visible mapped IDs={sorted(detected)}; visual verification only",
        "Terminal pixels: original remapped frame; saved image adds a 168px top header",
    ]
    for index, text in enumerate(lines):
        label(image, text, (12, 24 + 26 * index), scale=0.52)
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=90, help="number of frames to inspect (default: 90)")
    args = parser.parse_args()
    if args.frames < 1:
        parser.error("--frames must be positive")
    rover_config = load_rover_config(PROJECT_DIR / "rover_config.json")
    if rover_config["marker_size"] != 0.283:
        raise ValueError("This diagnostic requires marker_size = 0.283 m")
    marker_map = load_marker_map(PROJECT_DIR / "marker_map.json")
    mapped_ids = {marker["id"] for marker in marker_map["markers"]}
    preferred_ids = mapped_ids.intersection((23, 24, 25))
    K, D = load_camera_calibration(PROJECT_DIR / "camera_calibration_fisheye.npz")
    DIM = (rover_config["resolution_width"], rover_config["resolution_height"])
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50)
    parameters = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(dictionary, parameters)
    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    best = None
    best_score = None
    picam2 = None
    camera_started = False
    old_handler = signal.signal(signal.SIGINT, request_stop)
    try:
        picam2 = Picamera2()
        config = picam2.create_preview_configuration(
            main={"size": DIM, "format": "RGB888"}
        )
        picam2.configure(config)
        picam2.start()
        camera_started = True
        new_K = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
            K,
            D,
            DIM,
            np.eye(3),
            balance=rover_config["fisheye_balance"]
        )

        map1, map2 = cv2.fisheye.initUndistortRectifyMap(
            K,
            D,
            np.eye(3),
            new_K,
            DIM,
            cv2.CV_16SC2
        )

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
            gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
            corners, ids, _ = detector.detectMarkers(gray)
            flat_ids = np.array([], dtype=int) if ids is None else ids.flatten()
            detected = {}
            for index, marker_id in enumerate(flat_ids):
                marker_id = int(marker_id)
                if marker_id in mapped_ids and marker_id not in detected:
                    detected[marker_id] = corners[index][0].astype(np.float32).copy()
            if not detected:
                continue
            # Prefer target count first; break ties with mapped count, local
            # sharpness and smallest marker area. Detection itself is unchanged.
            sharpness = []
            for points in detected.values():
                x, y, w, h = cv2.boundingRect(points)
                crop = gray[max(0, y):min(gray.shape[0], y+h), max(0, x):min(gray.shape[1], x+w)]
                sharpness.append(float(cv2.Laplacian(crop, cv2.CV_64F).var()) if crop.size else 0.)
            score = (len(preferred_ids.intersection(detected)), len(detected),
                     min(sharpness), min(abs(cv2.contourArea(p)) for p in detected.values()))
            if best_score is None or score > best_score:
                best_score = score
                best = (frame.copy(), detected, frame_idx)
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
    if best is None:
        print("No mapped markers detected; no image saved.", file=sys.stderr)
        return 1
    print("Arrow pixel directions are (dx,dy): +dx=image right, +dy=image down.")
    annotated = annotate(*best)
    if not cv2.imwrite(str(OUTPUT_PATH), annotated):
        raise OSError(f"Could not save {OUTPUT_PATH}")
    print(f"saved={OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
