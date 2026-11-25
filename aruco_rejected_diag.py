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


METHODS = (
    ("NONE", cv2.aruco.CORNER_REFINE_NONE),
    ("CONTOUR", cv2.aruco.CORNER_REFINE_CONTOUR),
    ("SUBPIX", cv2.aruco.CORNER_REFINE_SUBPIX),
)
NAV_KEYS = ("front_distance", "rover_lateral", "rover_yaw")
CSV_PATH = PROJECT_DIR / "measurements_rejected_diag.csv"
FRAMES_DIR = PROJECT_DIR / "rejected_diag_frames"
CSV_FIELDS = [
    "frame_idx", "timestamp", "method", "detected", "marker_id", "rejected_count",
    "ExposureTime", "AnalogueGain", "FrameDuration",
    *[f"corner{i}_{axis}" for i in range(4) for axis in ("x", "y")],
    "center_x", "center_y", "quad_area",
    *[f"side{i}" for i in range(4)],
    *NAV_KEYS,
    "nearest_rejected_center_x", "nearest_rejected_center_y",
    "nearest_rejected_center_distance_px", "nearest_rejected_area",
    *[f"nearest_rejected_side{i}" for i in range(4)],
]


def quad_geometry(quad):
    points = np.asarray(quad, dtype=np.float32).reshape(4, 2)
    center = points.mean(axis=0)
    sides = np.linalg.norm(np.roll(points, -1, axis=0) - points, axis=1)
    area = abs(float(cv2.contourArea(points)))
    return center, sides, area


def main():
    global new_K, zero_dist
    if TARGET_ID != 23 or MARKER_SIZE != 0.283:
        raise ValueError("This diagnostic requires target_id = 23 and marker_size = 0.283 m")
    FRAMES_DIR.mkdir(exist_ok=True)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50)
    detectors = {}
    for name, refinement in METHODS:
        parameters = cv2.aruco.DetectorParameters()
        parameters.cornerRefinementMethod = refinement
        detectors[name] = cv2.aruco.ArucoDetector(dictionary, parameters)

    # Finish the current frame's three rows on Ctrl+C before stopping.
    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    total_frames = 0
    counts = {name: 0 for name, _ in METHODS}
    # Diagnostic references only: never supplied to detectMarkers or solvePnP.
    last_known_centers = {name: None for name, _ in METHODS}
    missing_with_rejected = {name: 0 for name, _ in METHODS}
    missing_nearest_within_50 = {name: 0 for name, _ in METHODS}
    any_not_detected_frames = 0
    any_missing_with_rejected_frames = 0
    any_missing_nearest_within_50_frames = 0
    previous_detected = None
    started_at = None
    ended_at = None
    last_status = 0.0
    last_png = None
    picam2 = None
    camera_started = False
    old_handler = signal.signal(signal.SIGINT, request_stop)
    try:
        # A separate run must not overwrite or mix with an existing diagnostic CSV.
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
            started_at = time.monotonic()
            while not stop_requested:
                # Pixel data and metadata belong to the same camera request.
                request = picam2.capture_request()
                try:
                    frame = request.make_array("main").copy()
                    metadata = request.get_metadata()
                finally:
                    request.release()
                timestamp_dt = datetime.now().astimezone()
                timestamp = timestamp_dt.isoformat(timespec="microseconds")
                frame_idx = total_frames + 1
                frame = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR)
                gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
                frame_rows = []
                detected = {}
                frame_has_missing_rejected = False
                frame_has_missing_nearest_within_50 = False
                for name, detector in detectors.items():
                    row = {
                        "frame_idx": frame_idx,
                        "timestamp": timestamp,
                        "method": name,
                        "detected": 0,
                        "marker_id": TARGET_ID,
                        "ExposureTime": metadata.get("ExposureTime", ""),
                        "AnalogueGain": metadata.get("AnalogueGain", ""),
                        "FrameDuration": metadata.get("FrameDuration", ""),
                    }
                    corners, ids, rejected = detector.detectMarkers(gray)
                    row["rejected_count"] = len(rejected)
                    found = ids is not None and TARGET_ID in ids.flatten()
                    detected[name] = int(found)
                    row["detected"] = int(found)
                    if found:
                        index = int(np.flatnonzero(ids.flatten() == TARGET_ID)[0])
                        points = corners[index][0].astype(np.float32)
                        row.update(zip(
                            [f"corner{i}_{axis}" for i in range(4) for axis in ("x", "y")],
                            points.reshape(-1).tolist(),
                        ))
                        center, sides, area = quad_geometry(points)
                        row.update(center_x=float(center[0]), center_y=float(center[1]),
                                   quad_area=area)
                        row.update({f"side{i}": float(side) for i, side in enumerate(sides)})
                        last_known_centers[name] = center.copy()
                        success, rvec, tvec = estimate_pose(points)
                        if success:
                            navigation = navigation_from_pose(rvec, tvec)
                            row.update({key: navigation[key] for key in NAV_KEYS})
                    else:
                        if len(rejected):
                            missing_with_rejected[name] += 1
                            frame_has_missing_rejected = True
                        reference = last_known_centers[name]
                        nearest = None
                        # All candidates come from this method on this same gray frame.
                        for quad in rejected:
                            center, sides, area = quad_geometry(quad)
                            if reference is not None:
                                distance = float(np.linalg.norm(center - reference))
                                if nearest is None or distance < nearest[0]:
                                    nearest = (distance, center, sides, area)
                        if nearest is not None:
                            distance, center, sides, area = nearest
                            row.update(
                                nearest_rejected_center_x=float(center[0]),
                                nearest_rejected_center_y=float(center[1]),
                                nearest_rejected_center_distance_px=distance,
                                nearest_rejected_area=area,
                            )
                            row.update({f"nearest_rejected_side{i}": float(side)
                                        for i, side in enumerate(sides)})
                            if distance <= 50.0:
                                missing_nearest_within_50[name] += 1
                                frame_has_missing_nearest_within_50 = True
                    frame_rows.append(row)
                writer.writerows(frame_rows)
                log.flush()
                total_frames = frame_idx
                for name in counts:
                    counts[name] += detected[name]

                any_missing = any(not value for value in detected.values())
                any_not_detected_frames += int(any_missing)
                any_missing_with_rejected_frames += int(frame_has_missing_rejected)
                any_missing_nearest_within_50_frames += int(frame_has_missing_nearest_within_50)

                now = time.monotonic()
                transitioned = previous_detected is not None and detected != previous_detected
                # One shared PNG if any method transitions or remains missing.
                periodic_missing = any_missing and (last_png is None or now - last_png >= 1.0)
                if transitioned or periodic_missing:
                    stamp = timestamp_dt.strftime("%Y%m%dT%H%M%S_%f%z")
                    image_path = FRAMES_DIR / f"frame_{frame_idx:08d}_{stamp}.png"
                    # Save the same remapped, unannotated image used by detection.
                    if not cv2.imwrite(str(image_path), frame):
                        raise OSError(f"Could not save PNG: {image_path}")
                    last_png = now
                previous_detected = detected
                if now - last_status >= 1.0:
                    elapsed = now - started_at
                    fps = total_frames / elapsed if elapsed else 0.0
                    print(
                        f"frame_idx={frame_idx} effective_FPS={fps:.2f} "
                        + " ".join(f"{name}={detected[name]}" for name in counts)
                        + f" ExposureTime={metadata.get('ExposureTime', 'N/A')}"
                        + f" AnalogueGain={metadata.get('AnalogueGain', 'N/A')}",
                        flush=True,
                    )
                    last_status = now
            ended_at = time.monotonic()
    finally:
        if started_at is not None and ended_at is None:
            ended_at = time.monotonic()
        try:
            if picam2 is not None:
                try:
                    if camera_started:
                        picam2.stop()
                finally:
                    picam2.close()
        finally:
            signal.signal(signal.SIGINT, old_handler)
            if started_at is not None:
                duration = ended_at - started_at
                fps = total_frames / duration if duration else 0.0
                print(
                    f"Summary: total_frames={total_frames} duration={duration:.3f}s "
                    f"effective_FPS={fps:.2f}", flush=True,
                )
                for name, count in counts.items():
                    percentage = 100.0 * count / total_frames if total_frames else 0.0
                    print(
                        f"{name}: detected_frames={count} detection={percentage:.2f}% "
                        f"not_detected_frames={total_frames - count} "
                        f"not_detected_with_rejected={missing_with_rejected[name]} "
                        f"not_detected_nearest_within_50px={missing_nearest_within_50[name]}",
                        flush=True,
                    )
                print(
                    f"Unique frames with at least one method not detected: "
                    f"total_not_detected_frames={any_not_detected_frames} "
                    f"not_detected_with_rejected={any_missing_with_rejected_frames} "
                    f"not_detected_nearest_within_50px={any_missing_nearest_within_50_frames}",
                    flush=True,
                )


if __name__ == "__main__":
    main()
