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


ID23_X = 3.000
ID24_X = 4.000
ID25_X = 5.000
MARKER_X = {23: ID23_X, 24: ID24_X, 25: ID25_X}
CSV_PATH = PROJECT_DIR / "measurements_active_marker_selector_v4.csv"
CSV_FIELDS = [
    "frame_idx", "timestamp", "id23_detected", "id24_detected",
    "id23_front_distance", "id24_front_distance", "active_marker",
    "pose_source_marker", "switch_event", "global_x", "rover_lateral", "rover_yaw",
    "id23_below_threshold_streak",  # V2-compatible alias of id23_below_1m_streak.
    "id23_detected_streak", "id24_detected_streak",
    "id23_below_1m_streak", "id23_above_1p3m_streak",
]
CSV_FIELDS += [
    "sensor_timestamp", "ExposureTime", "AnalogueGain", "FrameDuration",
    "id25_detected", "id25_front_distance",
    "id23_rover_lateral", "id23_rover_yaw",
    "id24_rover_lateral", "id24_rover_yaw",
    "id25_rover_lateral", "id25_rover_yaw",
    "fallback_used", "id25_detected_streak",
    "id24_below_1m_streak", "id24_above_1p3m_streak", "detected_ids",
]
POSE_KEYS = ("front_distance", "rover_lateral", "rover_yaw")


def update_selector(active_marker, frame_ids, poses, detected_streaks, below, above):
    # Threshold evidence must be consecutive and use valid current-frame poses.
    for marker_id in MARKER_X:
        detected_streaks[marker_id] = (
            detected_streaks[marker_id] + 1 if marker_id in frame_ids else 0
        )
    for marker_id in (23, 24):
        pose = poses.get(marker_id)
        valid = marker_id in frame_ids and pose is not None and all(
            math.isfinite(pose[key]) for key in POSE_KEYS
        )
        below[marker_id] = (
            below[marker_id] + 1 if valid and pose["front_distance"] <= 1.00 else 0
        )
        above[marker_id] = (
            above[marker_id] + 1 if valid and pose["front_distance"] >= 1.30 else 0
        )

    # One adjacent transition at most per frame; retain V3's reverse priority at 24.
    rules = (
        (23, 24, 23, below[23]),
        (24, 23, 23, above[23]),
        (24, 25, 24, below[24]),
        (25, 24, 24, above[24]),
    )
    for source, target, reference, streak in rules:
        if (active_marker == source and detected_streaks[target] >= 5
                and source in frame_ids and reference in frame_ids and streak >= 3):
            return target, f"{source}_TO_{target}", streak
    return active_marker, "NONE", None


def select_pose_source(active_marker, poses):
    if active_marker in poses:
        return active_marker
    # Numeric distance favors adjacent markers; ties deterministically favor lower ID.
    return min(poses, key=lambda marker_id: (abs(marker_id - active_marker), marker_id),
               default=None)



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
    active_marker = 23
    detected_streaks = {marker_id: 0 for marker_id in MARKER_X}
    below = {23: 0, 24: 0}
    above = {23: 0, 24: 0}
    switches = []
    switch_counts = {event: 0 for event in (
        "23_TO_24", "24_TO_23", "24_TO_25", "25_TO_24"
    )}
    pose_counts = {marker_id: 0 for marker_id in MARKER_X}
    all_detected_ids = set()
    fallback_frames = 0
    frames_without_pose = 0
    consecutive_without_pose = 0
    max_consecutive_frames_without_pose = 0
    picam2 = None
    camera_started = False
    old_handler = signal.signal(signal.SIGINT, request_stop)
    try:
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
                poses = {}
                for marker_id in MARKER_X:
                    if marker_id in frame_ids:
                        index = int(np.flatnonzero(flat_ids == marker_id)[0])
                        points = corners[index][0].astype(np.float32)
                        success, rvec, tvec = estimate_pose(points)
                        if success:
                            pose = navigation_from_pose(rvec, tvec)
                            if all(math.isfinite(pose[key]) for key in
                                   ("front_distance", "rover_lateral", "rover_yaw")):
                                poses[marker_id] = pose

                active_marker, switch_event, relevant_threshold_streak = update_selector(
                    active_marker, frame_ids, poses, detected_streaks, below, above
                )
                if switch_event != "NONE":
                    switch_counts[switch_event] += 1
                    switches.append({
                        "frame": frame_idx,
                        "direction": switch_event,
                        **{f"id{marker_id}_front_distance": (
                            poses[marker_id]["front_distance"] if marker_id in poses else None
                        ) for marker_id in MARKER_X},
                        "relevant_threshold_streak": relevant_threshold_streak,
                    })

                pose_source = select_pose_source(active_marker, poses)

                row = {
                    "frame_idx": frame_idx,
                    "timestamp": timestamp,
                    "sensor_timestamp": metadata.get("SensorTimestamp", ""),
                    "ExposureTime": metadata.get("ExposureTime", ""),
                    "AnalogueGain": metadata.get("AnalogueGain", ""),
                    "FrameDuration": metadata.get("FrameDuration", ""),
                    "detected_ids": json.dumps(sorted(frame_ids)),
                    "active_marker": active_marker,
                    "switch_event": switch_event,
                    "fallback_used": int(pose_source is not None and pose_source != active_marker),
                    "id23_below_threshold_streak": below[23],
                }
                for marker_id in MARKER_X:
                    row[f"id{marker_id}_detected"] = int(marker_id in frame_ids)
                    row[f"id{marker_id}_detected_streak"] = detected_streaks[marker_id]
                for marker_id in (23, 24):
                    row[f"id{marker_id}_below_1m_streak"] = below[marker_id]
                    row[f"id{marker_id}_above_1p3m_streak"] = above[marker_id]
                for marker_id, pose in poses.items():
                    for key in POSE_KEYS:
                        row[f"id{marker_id}_{key}"] = pose[key]
                if pose_source is not None:
                    pose = poses[pose_source]
                    row.update(
                        pose_source_marker=pose_source,
                        global_x=MARKER_X[pose_source] - pose["front_distance"],
                        rover_lateral=pose["rover_lateral"],
                        rover_yaw=pose["rover_yaw"],
                    )
                # Missing pose fields remain empty; never reuse previous poses.
                writer.writerow(row)
                log.flush()
                total_frames = frame_idx
                all_detected_ids.update(frame_ids)
                if pose_source is None:
                    frames_without_pose += 1
                    consecutive_without_pose += 1
                    max_consecutive_frames_without_pose = max(
                        max_consecutive_frames_without_pose, consecutive_without_pose
                    )
                else:
                    pose_counts[pose_source] += 1
                    fallback_frames += int(pose_source != active_marker)
                    consecutive_without_pose = 0
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
            print(f"total_frames={total_frames}")
            for event, count in switch_counts.items():
                print(f"switch_{event.lower()}_count={count}")
            for event in switches:
                print("switch=" + json.dumps(event))
            print(f"final_active_marker={active_marker}")
            print(f"frames_pose_from_23={pose_counts[23]}")
            print(f"frames_pose_from_24={pose_counts[24]}")
            print(f"frames_pose_from_25={pose_counts[25]}")
            print(f"fallback_frames={fallback_frames}")
            print(f"frames_without_pose={frames_without_pose}")
            print(f"max_consecutive_frames_without_pose={max_consecutive_frames_without_pose}")
            print(f"all_detected_ids={sorted(all_detected_ids)}")


if __name__ == "__main__":
    main()
