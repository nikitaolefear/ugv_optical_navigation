from picamera2 import Picamera2
import cv2
import numpy as np
import math
import json
from pathlib import Path
import sys
import signal
import time



# =================================================
# ROVER CONFIGURATION
# =================================================

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


PROJECT_DIR = Path(__file__).resolve().parent
rover_config = load_rover_config(PROJECT_DIR / "rover_config.json")


# =================================================
# CALIBRATION
# =================================================

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


K, D = load_camera_calibration(PROJECT_DIR / "camera_calibration_fisheye.npz")

DIM = (rover_config["resolution_width"], rover_config["resolution_height"])


# Marker size is validated for this setup; visibility counting needs no pose.
MARKER_SIZE = rover_config["marker_size"]
DURATION_SECONDS = 10.0


def main():
    if MARKER_SIZE != 0.283:
        raise ValueError("This check requires marker_size = 0.283 m")
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50)
    parameters = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(dictionary, parameters)
    total_frames = 0
    id23_frames = 0
    id24_frames = 0
    id25_frames = 0
    all_three_frames = 0
    frames_only_23 = 0
    frames_only_24 = 0
    frames_only_25 = 0
    frames_23_24_only = 0
    frames_24_25_only = 0
    frames_23_25_only = 0
    frames_neither = 0
    all_detected_ids = set()
    stop_requested = False
    picam2 = None
    camera_started = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

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

        started_at = time.monotonic()
        while not stop_requested and time.monotonic() - started_at < DURATION_SECONDS:
            request = picam2.capture_request()
            try:
                frame = request.make_array("main").copy()
            finally:
                request.release()
            frame = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR)
            gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
            corners, ids, rejected = detector.detectMarkers(gray)
            frame_ids = set() if ids is None else set(map(int, ids.flatten()))
            total_frames += 1
            id23_frames += int(23 in frame_ids)
            id24_frames += int(24 in frame_ids)
            id25_frames += int(25 in frame_ids)
            target_ids = frame_ids & {23, 24, 25}
            all_three_frames += int(target_ids == {23, 24, 25})
            frames_only_23 += int(target_ids == {23})
            frames_only_24 += int(target_ids == {24})
            frames_only_25 += int(target_ids == {25})
            frames_23_24_only += int(target_ids == {23, 24})
            frames_24_25_only += int(target_ids == {24, 25})
            frames_23_25_only += int(target_ids == {23, 25})
            frames_neither += int(not target_ids)
            all_detected_ids.update(frame_ids)
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
            def percent(count):
                return 100.0 * count / total_frames if total_frames else 0.0

            print(f"total_frames={total_frames}")
            print(f"ID23_detected_frames={id23_frames}")
            print(f"ID24_detected_frames={id24_frames}")
            print(f"ID25_detected_frames={id25_frames}")
            print(f"ID23_detection_percent={percent(id23_frames):.2f}%")
            print(f"ID24_detection_percent={percent(id24_frames):.2f}%")
            print(f"ID25_detection_percent={percent(id25_frames):.2f}%")
            print(f"all_three_detected_frames={all_three_frames}")
            print(f"all_three_detection_percent={percent(all_three_frames):.2f}%")
            print(f"frames_only_23={frames_only_23}")
            print(f"frames_only_24={frames_only_24}")
            print(f"frames_only_25={frames_only_25}")
            print(f"frames_23_24_only={frames_23_24_only}")
            print(f"frames_24_25_only={frames_24_25_only}")
            print(f"frames_23_25_only={frames_23_25_only}")
            print(f"frames_neither={frames_neither}")
            print(f"all_detected_ids={sorted(all_detected_ids)}")


if __name__ == "__main__":
    main()
