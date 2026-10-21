from picamera2 import Picamera2
import cv2
import numpy as np
import math
import json
from pathlib import Path
import sys
import csv
from datetime import datetime
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


# =================================================
# CAMERA / ROVER / MARKER GEOMETRY
# =================================================

CAMERA_HEIGHT = rover_config["camera_height"]

# Camera position relative to rover geometric center
CAMERA_FORWARD_OFFSET = rover_config["camera_forward_offset"]

# + = right
# - = left
CAMERA_LATERAL_OFFSET = rover_config["camera_lateral_offset"]

# Camera yaw relative to rover
# + = right
# - = left
CAMERA_YAW_DEG = rover_config["camera_yaw_deg"]

CAMERA_YAW_RAD = math.radians(CAMERA_YAW_DEG)

# Distance from rover geometric center
# to the front edge of the rover
ROVER_FRONT_OFFSET = rover_config["rover_front_offset"]


MARKER_SIZE = rover_config["marker_size"]
HALF = MARKER_SIZE / 2.0

TARGET_ID = rover_config["target_id"]


object_points = np.array([
    [-HALF,  HALF, 0],
    [ HALF,  HALF, 0],
    [ HALF, -HALF, 0],
    [-HALF, -HALF, 0]
], dtype=np.float32)



def estimate_pose(image_points):
    return cv2.solvePnP(
        object_points, image_points, new_K, zero_dist,
        flags=cv2.SOLVEPNP_IPPE_SQUARE
    )


def navigation_from_pose(rvec, tvec):
    # =========================================
    # CAMERA 3D COORDINATES
    # =========================================

    x = float(tvec[0][0])
    y = float(tvec[1][0])
    z = float(tvec[2][0])


    # =========================================
    # CAMERA -> MARKER DISTANCE
    # =========================================

    distance = math.sqrt(
        x*x +
        y*y +
        z*z
    )


    # =========================================
    # FLOOR DISTANCE
    # =========================================

    ground_distance = math.sqrt(
        max(
            0.0,
            distance * distance -
            CAMERA_HEIGHT * CAMERA_HEIGHT
        )
    )


    # =========================================
    # CAMERA-FRAME FLOOR COORDINATES
    # =========================================

    # + = right
    # - = left
    camera_lateral = x

    camera_forward = math.sqrt(
        max(
            0.0,
            ground_distance * ground_distance -
            camera_lateral * camera_lateral
        )
    )


    # =========================================
    # CAMERA FRAME -> ROVER FRAME
    # =========================================

    cos_yaw = math.cos(CAMERA_YAW_RAD)
    sin_yaw = math.sin(CAMERA_YAW_RAD)

    rover_forward = (
        CAMERA_FORWARD_OFFSET
        + cos_yaw * camera_forward
        - sin_yaw * camera_lateral
    )

    rover_lateral = (
        CAMERA_LATERAL_OFFSET
        + sin_yaw * camera_forward
        + cos_yaw * camera_lateral
    )


    # =========================================
    # DISTANCE FROM FRONT EDGE OF ROVER
    # =========================================

    front_distance = (
        rover_forward
        - ROVER_FRONT_OFFSET
    )


    # =========================================
    # MARKER YAW
    # =========================================

    rotation_matrix, _ = cv2.Rodrigues(rvec)

    yaw_raw = math.degrees(
        math.atan2(
            rotation_matrix[0, 2],
            rotation_matrix[2, 2]
        )
    )

    if yaw_raw >= 0:
        camera_yaw = yaw_raw - 180.0
    else:
        camera_yaw = yaw_raw + 180.0


    # Marker yaw relative to rover
    rover_yaw = (
        camera_yaw +
        CAMERA_YAW_DEG
    )

    if rover_yaw > 180:
        rover_yaw -= 360

    if rover_yaw < -180:
        rover_yaw += 360
    return {
        "x": x,
        "y": y,
        "z": z,
        "distance": distance,
        "ground_distance": ground_distance,
        "camera_lateral": camera_lateral,
        "camera_forward": camera_forward,
        "rover_forward": rover_forward,
        "rover_lateral": rover_lateral,
        "front_distance": front_distance,
        "camera_yaw": camera_yaw,
        "rover_yaw": rover_yaw,
    }


CSV_PATH = PROJECT_DIR / "measurements_multimarker_dynamic.csv"
CSV_FIELDS = [
    "frame_idx", "timestamp", "sensor_timestamp",
    "ExposureTime", "AnalogueGain", "FrameDuration",
    "id23_detected", "id24_detected",
    "id23_front_distance", "id23_rover_lateral", "id23_rover_yaw",
    "id24_front_distance", "id24_rover_lateral", "id24_rover_yaw",
    "detected_ids",
]
POSE_KEYS = ("front_distance", "rover_lateral", "rover_yaw")


def main():
    global new_K, zero_dist
    if MARKER_SIZE != 0.283:
        raise ValueError("This diagnostic requires marker_size = 0.283 m")
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50)
    parameters = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(dictionary, parameters)
    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    total_frames = 0
    counts = {23: 0, 24: 0}
    both_frames = 0
    first_only_id24_frame = None
    last_id23_frame = None
    current_neither_run = 0
    longest_neither_run = 0
    all_detected_ids = set()
    picam2 = None
    camera_started = False
    old_handler = signal.signal(signal.SIGINT, request_stop)
    try:
        # Refuse to overwrite or mix with an existing diagnostic run.
        with CSV_PATH.open("x", newline="", encoding="utf-8") as log:
            writer = csv.DictWriter(log, fieldnames=CSV_FIELDS)
            writer.writeheader()
            log.flush()
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

            zero_dist = np.zeros((5, 1))
            while not stop_requested:
                # Pixels and sensor metadata belong to the same request.
                request = picam2.capture_request()
                try:
                    frame = request.make_array("main").copy()
                    metadata = request.get_metadata()
                finally:
                    request.release()
                timestamp = datetime.now().astimezone().isoformat(timespec="microseconds")
                frame_idx = total_frames + 1
                frame = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR)
                gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
                corners, ids, _ = detector.detectMarkers(gray)
                flat_ids = np.array([], dtype=int) if ids is None else ids.flatten()
                frame_ids = set(map(int, flat_ids))
                row = {
                    "frame_idx": frame_idx,
                    "timestamp": timestamp,
                    "sensor_timestamp": metadata.get("SensorTimestamp", ""),
                    "ExposureTime": metadata.get("ExposureTime", ""),
                    "AnalogueGain": metadata.get("AnalogueGain", ""),
                    "FrameDuration": metadata.get("FrameDuration", ""),
                    "id23_detected": int(23 in frame_ids),
                    "id24_detected": int(24 in frame_ids),
                    "detected_ids": json.dumps(sorted(frame_ids)),
                }
                # Each marker is measured independently on this same frame.
                for marker_id in (23, 24):
                    if marker_id in frame_ids:
                        index = int(np.flatnonzero(flat_ids == marker_id)[0])
                        points = corners[index][0].astype(np.float32)
                        success, rvec, tvec = estimate_pose(points)
                        if success:
                            navigation = navigation_from_pose(rvec, tvec)
                            row.update({f"id{marker_id}_{key}": navigation[key]
                                        for key in POSE_KEYS})
                # DictWriter leaves absent pose values empty, including PnP failure.
                writer.writerow(row)
                log.flush()
                total_frames = frame_idx
                for marker_id in counts:
                    counts[marker_id] += int(marker_id in frame_ids)
                both_frames += int(23 in frame_ids and 24 in frame_ids)
                if 24 in frame_ids and 23 not in frame_ids and first_only_id24_frame is None:
                    first_only_id24_frame = frame_idx
                if 23 in frame_ids:
                    last_id23_frame = frame_idx
                if 23 not in frame_ids and 24 not in frame_ids:
                    current_neither_run += 1
                    longest_neither_run = max(longest_neither_run, current_neither_run)
                else:
                    current_neither_run = 0
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
            print(f"ID23_detected_frames={counts[23]}")
            print(f"ID24_detected_frames={counts[24]}")
            print(f"both_detected_frames={both_frames}")
            print(f"only_ID23_frames={counts[23] - both_frames}")
            print(f"only_ID24_frames={counts[24] - both_frames}")
            print(f"neither_frames={total_frames - counts[23] - counts[24] + both_frames}")
            print(f"ID23_detection_percent={percent(counts[23]):.2f}%")
            print(f"ID24_detection_percent={percent(counts[24]):.2f}%")
            print(f"both_detection_percent={percent(both_frames):.2f}%")
            print(f"first_only_ID24_frame={first_only_id24_frame}")
            print(f"last_ID23_frame={last_id23_frame}")
            print(f"longest_neither_sequence_frames={longest_neither_run}")
            print(f"all_detected_ids={sorted(all_detected_ids)}")


if __name__ == "__main__":
    main()
