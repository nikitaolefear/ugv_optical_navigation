"""Static HOME-only diagnostic: T_rover_camera = T_world_camera.

WORLD/ROVER: +X forward,+Y left,+Z up (right-handed FLU), FRONT DATUM
origin projected to floor. At HOME T_world_rover=I. OpenCV optical CAMERA
remains +x right,+y down,+z forward. Map y is left and z is up.
Legacy planar rover_lateral stays positive right: legacy_rover_lateral=-world_y
for a common reference. No legacy navigation values are changed.
No calibrated values are written. Current map marker orientations are unvalidated;
absolute camera extrinsic roll/pitch/yaw cannot be trusted until physical
marker-frame orientations are validated. Any earlier probe is diagnostic only.

"""
from pose_transform_3d import (
    rvec_tvec_to_transform, marker_map_entry_to_world_transform,
    compose_transform, invert_transform, extract_translation,
    rotation_matrix_to_euler_deg, legacy_rover_lateral_to_world_y,
)
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


marker_map = load_marker_map(PROJECT_DIR / "marker_map.json")
MARKERS = marker_map["markers"]
print(f"marker_map_version={marker_map['version']}")
print(f"coordinate_frame={marker_map['coordinate_frame']}")
print(f"marker_count={len(MARKERS)}")
print(f"marker_chain={[marker['id'] for marker in MARKERS]}")
for marker in MARKERS:
    position = marker["position"]
    orientation = marker["orientation"]
    print(
        f"marker={marker['id']} placement={marker['placement']} "
        f"position=({position['x']:.3f},{position['y']:.3f},{position['z']:.3f}) "
        f"orientation=({orientation['roll']:.1f},{orientation['pitch']:.1f},{orientation['yaw']:.1f})"
    )
MARKER_IDS = tuple(marker["id"] for marker in MARKERS)
MARKER_INDEX = {marker_id: index for index, marker_id in enumerate(MARKER_IDS)}
MARKER_X = {marker["id"]: marker["position"]["x"] for marker in MARKERS}
MARKER_BY_ID = {marker["id"]: marker for marker in MARKERS}
from itertools import combinations

CSV_PATH = PROJECT_DIR / "measurements_camera_extrinsic_probe.csv"
COMPONENTS = ("x", "y", "z", "roll", "pitch", "yaw")
PAIR_COMPONENTS = ("translation_magnitude", *COMPONENTS)
CSV_FIELDS = [
    "frame_idx", "timestamp", "marker_id", "sensor_timestamp",
    "ExposureTime", "AnalogueGain", "FrameDuration", "pose_valid", "pose_error",
    *[f"camera_world_{key}" for key in COMPONENTS],
    "front_distance", "rover_lateral", "rover_yaw", "distance", "ground_distance",
    "reprojection_rms_px", "marker_area_px2",
    "rvec_x", "rvec_y", "rvec_z", "tvec_x", "tvec_y", "tvec_z",
]
for other in MARKER_IDS:
    CSV_FIELDS += [f"delta_to_id{other}_{key}" for key in PAIR_COMPONENTS]


def wrap_deg(value):
    return (np.asarray(value) + 180.0) % 360.0 - 180.0


def component_statistics(samples, angular=False):
    values = np.asarray(samples, dtype=float)
    if angular:
        # Circular center, then unwrap locally for median and residual spread.
        radians = np.radians(values)
        sin_mean, cos_mean = np.mean(np.sin(radians)), np.mean(np.cos(radians))
        if math.hypot(sin_mean, cos_mean) < 1e-12:
            return {"mean": None, "median": None, "std": None}
        center = math.degrees(math.atan2(sin_mean, cos_mean))
        residuals = wrap_deg(values - center)
        return {"mean": float(wrap_deg(center)),
                "median": float(wrap_deg(center + np.median(residuals))),
                "std": float(np.std(residuals))}
    return {"mean": float(np.mean(values)), "median": float(np.median(values)),
            "std": float(np.std(values))}


def candidate_delta(first, second):
    """second minus first; Euler differences wrapped individually, not SO(3) mean."""
    delta = np.asarray(second) - np.asarray(first)
    delta[3:] = wrap_deg(delta[3:])
    return np.r_[np.linalg.norm(delta[:3]), delta]


def print_summary(total_frames, samples, pair_samples):
    print(f"total_frames={total_frames}")
    print("statistics: translation=m angles=deg std=population; angular std=wrapped residual std")
    for marker_id, values in samples.items():
        print(f"marker={marker_id} sample_count={len(values)}")
        if values:
            data = np.asarray(values)
            for index, key in enumerate(COMPONENTS):
                print(f"marker={marker_id} {key}=" + json.dumps(
                    component_statistics(data[:, index], angular=index >= 3)))
    for (first, second), values in pair_samples.items():
        print(f"pair={first},{second} delta_direction={second}_minus_{first} sample_count={len(values)}")
        if values:
            data = np.asarray(values)
            for index, key in enumerate(PAIR_COMPONENTS):
                print(f"pair={first},{second} delta_{key}=" + json.dumps(
                    component_statistics(data[:, index], angular=index >= 4)))


def main():
    global new_K, zero_dist
    if MARKER_SIZE != 0.283:
        raise ValueError("This diagnostic requires marker_size = 0.283 m")
    if marker_map['coordinate_frame'] != 'HOME_FRONT_DATUM':
        raise ValueError("Static HOME probe requires coordinate_frame=HOME_FRONT_DATUM")
    print("STATIC HOME PROBE: keep rover stationary at HOME FRONT DATUM; diagnostic only")
    print("Candidate T_rover_camera equals T_world_camera under T_world_rover=identity")
    print("WORLD/ROVER=FLU: +X forward,+Y left,+Z up; CAMERA=right/down/forward")
    print("Map orientations require physical validation BEFORE a trusted extrinsic calibration probe.")
    print("legacy_rover_lateral_positive_right=-world_y; world_y=-legacy_rover_lateral_positive_right")
    print(f"camera_lateral_offset_FLU_y={legacy_rover_lateral_to_world_y(rover_config['camera_lateral_offset']):+.3f} m (sign conversion only)")
    for key in ("camera_height", "rover_front_offset", "camera_forward_offset",
                "camera_lateral_offset", "camera_yaw_deg"):
        unit = "deg" if key.endswith("deg") else "m"
        print(f"{key}={rover_config[key]:.3f} {unit}")
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50)
    parameters = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(dictionary, parameters)
    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    total_frames = 0
    samples = {marker_id: [] for marker_id in MARKER_IDS}
    pair_samples = {pair: [] for pair in combinations(MARKER_IDS, 2)}
    picam2 = None
    camera_started = False
    old_handler = signal.signal(signal.SIGINT, request_stop)
    try:
        # Exclusive create prevents mixing separate stationary runs.
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
                candidates = {}
                rows = {}
                for marker_id in MARKER_IDS:
                    if marker_id not in frame_ids:
                        continue
                    row = {"frame_idx": frame_idx, "timestamp": timestamp,
                           "marker_id": marker_id, "pose_valid": 0,
                           "sensor_timestamp": metadata.get("SensorTimestamp", ""),
                           **{key: metadata.get(key, "") for key in
                              ("ExposureTime", "AnalogueGain", "FrameDuration")}}
                    rows[marker_id] = row
                    index = int(np.flatnonzero(flat_ids == marker_id)[0])
                    points = corners[index][0].astype(np.float32)
                    row['marker_area_px2'] = float(abs(cv2.contourArea(points)))
                    try:
                        success, rvec, tvec = estimate_pose(points)
                        if not success:
                            row['pose_error'] = 'solvePnP returned false'
                            continue
                        T_camera_marker = rvec_tvec_to_transform(rvec, tvec)
                        T_world_marker = marker_map_entry_to_world_transform(MARKER_BY_ID[marker_id])
                        T_world_camera = compose_transform(T_world_marker, invert_transform(T_camera_marker))
                        candidate = np.r_[extract_translation(T_world_camera),
                                          rotation_matrix_to_euler_deg(T_world_camera[:3, :3])]
                        projected, _ = cv2.projectPoints(object_points, rvec, tvec, new_K, zero_dist)
                        row['reprojection_rms_px'] = float(np.sqrt(np.mean(np.sum(
                            (projected.reshape(4, 2) - points) ** 2, axis=1))))
                        navigation = navigation_from_pose(rvec, tvec)
                        for key in ('front_distance', 'rover_lateral', 'rover_yaw', 'distance', 'ground_distance'):
                            row[key] = navigation[key]
                        for prefix, vector in (('rvec', rvec), ('tvec', tvec)):
                            for key, value in zip(('x', 'y', 'z'), vector.reshape(3)):
                                row[f'{prefix}_{key}'] = float(value)
                        row.update({f'camera_world_{key}': float(value)
                                    for key, value in zip(COMPONENTS, candidate)})
                        row['pose_valid'] = 1
                        candidates[marker_id] = candidate
                    except (ValueError, KeyError, TypeError, cv2.error) as exc:
                        row['pose_error'] = str(exc)
                frame_pairs = {}
                for first, second in pair_samples:
                    if first in candidates and second in candidates:
                        delta = candidate_delta(candidates[first], candidates[second])
                        frame_pairs[first, second] = delta
                        for source, other, values in ((first, second, delta),
                                                     (second, first, candidate_delta(candidates[second], candidates[first]))):
                            rows[source].update({f'delta_to_id{other}_{key}': float(value)
                                                 for key, value in zip(PAIR_COMPONENTS, values)})
                # One row per detected mapped marker, independently, on each frame.
                writer.writerows(rows.values())
                log.flush()
                total_frames = frame_idx
                for marker_id, candidate in candidates.items():
                    samples[marker_id].append(candidate)
                for pair, delta in frame_pairs.items():
                    pair_samples[pair].append(delta)
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
            print_summary(total_frames, samples, pair_samples)


if __name__ == '__main__':
    main()
