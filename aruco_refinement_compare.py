from picamera2 import Picamera2
import cv2
import numpy as np
import math
import json
from pathlib import Path
import sys
import csv
from datetime import datetime

SHOW_GUI = False


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
NAV_KEYS = ("front_distance", "rover_forward", "rover_lateral", "rover_yaw")
DELTA_KEYS = ("front_distance", "rover_lateral", "rover_yaw")
CSV_PATH = PROJECT_DIR / "measurements_refinement_compare.csv"
CSV_FIELDS = [
    "timestamp", "marker_id", "method",
    *[f"corner{i}_{axis}" for i in range(4) for axis in ("x", "y")],
    *NAV_KEYS,
    *[f"tvec_{axis}" for axis in ("x", "y", "z")],
    *[f"rvec_{axis}" for axis in ("x", "y", "z")],
    "reprojection_error_px",
]


def main():
    global new_K, zero_dist
    if TARGET_ID != 23:
        raise ValueError("This diagnostic requires target_id = 23")
    if CSV_PATH.exists() and CSV_PATH.stat().st_size:
        with CSV_PATH.open(newline="", encoding="utf-8") as existing:
            if next(csv.reader(existing), []) != CSV_FIELDS:
                raise ValueError(f"CSV header mismatch: {CSV_PATH}")

    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50)
    detectors = {}
    for name, refinement in METHODS:
        parameters = cv2.aruco.DetectorParameters()
        parameters.cornerRefinementMethod = refinement
        detectors[name] = cv2.aruco.ArucoDetector(dictionary, parameters)

    with CSV_PATH.open("a", newline="", encoding="utf-8") as log:
        writer = csv.writer(log)
        if log.tell() == 0:
            writer.writerow(CSV_FIELDS)
            log.flush()
        picam2 = Picamera2()
        try:
            config = picam2.create_preview_configuration(
                main={
                    "size": DIM,
                    "format": "RGB888"
                }
            )

            picam2.configure(config)
            picam2.start()


            # =================================================
            # FISHEYE CORRECTION
            # =================================================

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
            while True:
                # One capture, one remap and one grayscale image for all methods.
                frame = picam2.capture_array()
                timestamp = datetime.now().astimezone().isoformat(timespec="microseconds")
                frame = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR)
                gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
                results = {}
                messages = []
                for name, detector in detectors.items():
                    corners, ids, _ = detector.detectMarkers(gray)
                    if ids is None or TARGET_ID not in ids.flatten():
                        messages.append(f"{name}: Marker ID {TARGET_ID} not detected")
                        continue
                    index = int(np.flatnonzero(ids.flatten() == TARGET_ID)[0])
                    points = corners[index][0].astype(np.float32)
                    success, rvec, tvec = estimate_pose(points)
                    if not success:
                        messages.append(f"{name}: pose estimation failed")
                        continue
                    navigation = navigation_from_pose(rvec, tvec)
                    projected, _ = cv2.projectPoints(
                        object_points, rvec, tvec, new_K, zero_dist
                    )
                    residuals = projected.reshape(4, 2) - points
                    error = float(np.sqrt(np.mean(np.sum(residuals * residuals, axis=1))))
                    results[name] = navigation
                    messages.append(name + ": " + ", ".join(
                        f"{key}={navigation[key]:+.6f} {'deg' if key == 'rover_yaw' else 'm'}"
                        for key in NAV_KEYS
                    ))
                    messages.append(f"  reprojection_error_px={error:.6f}; corners={points.tolist()}")
                    writer.writerow([
                        timestamp, TARGET_ID, name, *points.reshape(-1).tolist(),
                        *[navigation[key] for key in NAV_KEYS],
                        *tvec.reshape(-1).tolist(), *rvec.reshape(-1).tolist(), error,
                    ])
                if results:
                    print(f"\n{timestamp} Marker ID={TARGET_ID} (same frame, unfiltered)")
                    print("\n".join(messages))
                    for left, right in (("CONTOUR", "NONE"), ("SUBPIX", "NONE"),
                                        ("SUBPIX", "CONTOUR")):
                        if left in results and right in results:
                            print(f"{left} - {right}: " + ", ".join(
                                f"{key}={results[left][key] - results[right][key]:+.6f} "
                                f"{'deg' if key == 'rover_yaw' else 'm'}"
                                for key in DELTA_KEYS
                            ))
                        else:
                            print(f"{left} - {right}: N/A")
                    log.flush()
                if SHOW_GUI:
                    cv2.imshow("Refinement comparison", frame)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        break
        except KeyboardInterrupt:
            pass
        finally:
            try:
                picam2.stop()
            finally:
                picam2.close()
                if SHOW_GUI:
                    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
