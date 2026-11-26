from picamera2 import Picamera2
import cv2
import numpy as np
from collections import deque
import math
import json
from pathlib import Path
import sys
import csv
from datetime import datetime


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


# =================================================
# FILTERS
# =================================================

z_history = deque(maxlen=30)

distance_history = deque(maxlen=30)
ground_history = deque(maxlen=30)

camera_forward_history = deque(maxlen=30)
camera_lateral_history = deque(maxlen=20)

rover_forward_history = deque(maxlen=30)
rover_lateral_history = deque(maxlen=20)

front_distance_history = deque(maxlen=30)

camera_yaw_history = deque(maxlen=15)
rover_yaw_history = deque(maxlen=15)

RESET_THRESHOLD = 0.20


# =================================================
# CAMERA
# =================================================

picam2 = Picamera2()

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


# =================================================
# ARUCO
# =================================================

dictionary = cv2.aruco.getPredefinedDictionary(
    cv2.aruco.DICT_5X5_50
)

detector_parameters = cv2.aruco.DetectorParameters()
detector_parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_CONTOUR
detector = cv2.aruco.ArucoDetector(dictionary, detector_parameters)

raw_detector_parameters = cv2.aruco.DetectorParameters()
raw_detector_parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_NONE
raw_detector = cv2.aruco.ArucoDetector(
    dictionary,
    raw_detector_parameters
)


# =================================================
# MAIN LOOP
# =================================================

measurement_log = None
measurement_writer = None
previous_pose_state = None
pose_transition_dir = PROJECT_DIR / "pose_transition_frames"
pose_transition_dir.mkdir(exist_ok=True)
measurement_fields = [
    "timestamp",
    "marker_id",
    "front_distance",
    "rover_forward",
    "rover_lateral",
    "rover_yaw",
    "camera_forward",
    "camera_lateral",
    "camera_yaw",
    "ground_distance",
    "distance",
    "z",
    "marker_width_px",
    "corner0_x",
    "corner0_y",
    "corner1_x",
    "corner1_y",
    "corner2_x",
    "corner2_y",
    "corner3_x",
    "corner3_y",
    "raw_tvec_x",
    "raw_tvec_y",
    "raw_tvec_z",
    "raw_rvec_x",
    "raw_rvec_y",
    "raw_rvec_z",
    "reprojection_error_px",
    "ippe0_tvec_x",
    "ippe0_tvec_y",
    "ippe0_tvec_z",
    "ippe0_rvec_x",
    "ippe0_rvec_y",
    "ippe0_rvec_z",
    "ippe0_reprojection_error_px",
    "ippe1_tvec_x",
    "ippe1_tvec_y",
    "ippe1_tvec_z",
    "ippe1_rvec_x",
    "ippe1_rvec_y",
    "ippe1_rvec_z",
    "ippe1_reprojection_error_px",
    "raw_corner0_x",
    "raw_corner0_y",
    "raw_corner1_x",
    "raw_corner1_y",
    "raw_corner2_x",
    "raw_corner2_y",
    "raw_corner3_x",
    "raw_corner3_y",
    "corner0_shift_px",
    "corner1_shift_px",
    "corner2_shift_px",
    "corner3_shift_px",
]

try:

    if rover_config.get("log_measurements", False):
        measurement_path = PROJECT_DIR / rover_config.get("log_file", "measurements.csv")
        if measurement_path.exists() and measurement_path.stat().st_size:
            with measurement_path.open(newline="", encoding="utf-8") as existing_log:
                if next(csv.reader(existing_log), []) != measurement_fields:
                    raise ValueError(
                        f"CSV header mismatch in '{measurement_path}': preserve the old "
                        "CSV and select a new log_file for diagnostic measurements."
                    )
        measurement_log = measurement_path.open("a", newline="", encoding="utf-8")
        measurement_writer = csv.writer(measurement_log)

        if measurement_log.tell() == 0:
            measurement_writer.writerow(measurement_fields)
            measurement_log.flush()

    while True:

        frame = picam2.capture_array()

        # -----------------------------------------
        # Undistort
        # -----------------------------------------

        frame = cv2.remap(
            frame,
            map1,
            map2,
            cv2.INTER_LINEAR
        )

        gray = cv2.cvtColor(
            frame,
            cv2.COLOR_RGB2GRAY
        )

        corners, ids, rejected = detector.detectMarkers(gray)

        raw_image_points = None
        if measurement_writer is not None:
            raw_corners, raw_ids, _ = raw_detector.detectMarkers(gray)
            if raw_ids is not None:
                for raw_index, raw_marker_id in enumerate(raw_ids.flatten()):
                    if raw_marker_id == TARGET_ID:
                        raw_image_points = raw_corners[raw_index][0].astype(np.float32)
                        break

        undistorted_diagnostic_frame = frame.copy()

        marker_found = False
        display_measurements = []


        # =========================================
        # MARKER DETECTED
        # =========================================

        if ids is not None:

            for i, marker_id in enumerate(ids.flatten()):

                if marker_id != TARGET_ID:
                    continue

                marker_found = True

                image_points = corners[i][0].astype(np.float32)


                # =========================================
                # MARKER WIDTH
                # =========================================

                top_width = np.linalg.norm(
                    image_points[1] - image_points[0]
                )

                bottom_width = np.linalg.norm(
                    image_points[2] - image_points[3]
                )

                marker_width_px = (
                    top_width + bottom_width
                ) / 2.0


                # =========================================
                # POSE ESTIMATION
                # =========================================

                success, rvec, tvec = cv2.solvePnP(
                    object_points,
                    image_points,
                    new_K,
                    zero_dist,
                    flags=cv2.SOLVEPNP_IPPE_SQUARE
                )

                if not success:
                    continue

                current_pose_state = "NEG" if float(rvec.reshape(-1)[0]) < 0 else "POS"
                if (
                    previous_pose_state is not None
                    and current_pose_state != previous_pose_state
                ):
                    transition_frame = undistorted_diagnostic_frame.copy()
                    transition_projected, _ = cv2.projectPoints(
                        object_points, rvec, tvec, new_K, zero_dist
                    )
                    transition_residuals = transition_projected.reshape(4, 2) - image_points
                    transition_error_px = float(np.sqrt(np.mean(np.sum(
                        transition_residuals * transition_residuals, axis=1
                    ))))
                    # Integer coordinates are used only for drawing on the copy.
                    for points, prefix, color in (
                        (image_points, "", (0, 255, 0)),
                        (raw_image_points, "R", (0, 0, 255)),
                    ):
                        if points is None:
                            continue
                        draw_points = np.rint(points).astype(int)
                        for corner_index in range(4):
                            point = tuple(draw_points[corner_index])
                            next_point = tuple(draw_points[(corner_index + 1) % 4])
                            cv2.line(transition_frame, point, next_point, color, 1)
                            cv2.circle(
                                transition_frame, point,
                                (9 if prefix else 6) if corner_index == 2 else 3,
                                color, 2
                            )
                            label_offset = (12, 24) if prefix else (12, -12)
                            label_point = (
                                point[0] + label_offset[0], point[1] + label_offset[1]
                            )
                            label = f"{prefix}{corner_index}"
                            if corner_index == 2:
                                label += " RAW corner2" if prefix else " REFINED corner2"
                            cv2.putText(
                                transition_frame, label, label_point,
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1
                            )
                    transition_text = [
                        f"transition: {previous_pose_state} -> {current_pose_state}",
                        f"raw_rvec_x: {float(rvec.reshape(-1)[0]):.6f}",
                        f"raw_tvec_z: {float(tvec.reshape(-1)[2]):.6f}",
                        f"reprojection_error_px: {transition_error_px:.6f}",
                        f"marker_width_px: {marker_width_px:.6f}",
                    ]
                    if raw_image_points is None:
                        transition_text.append("RAW ID23 NOT FOUND")
                    for line_index, text_line in enumerate(transition_text):
                        text_origin = (10, 25 + 25 * line_index)
                        cv2.putText(
                            transition_frame, text_line, text_origin,
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3
                        )
                        cv2.putText(
                            transition_frame, text_line, text_origin,
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1
                        )
                    transition_timestamp = datetime.now().astimezone().strftime(
                        "%Y%m%d_%H%M%S_%f"
                    )[:-3]
                    transition_path = pose_transition_dir / (
                        f"{transition_timestamp}_{previous_pose_state}_to_"
                        f"{current_pose_state}.jpg"
                    )
                    if not cv2.imwrite(str(transition_path), transition_frame):
                        print(f"Failed to save transition image: {transition_path}", file=sys.stderr)
                previous_pose_state = current_pose_state


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


                # =========================================
                # RESET FILTERS AFTER LARGE MOVEMENT
                # =========================================

                if len(z_history) > 0:

                    previous_z = np.median(z_history)

                    if abs(z - previous_z) > RESET_THRESHOLD:

                        z_history.clear()

                        distance_history.clear()
                        ground_history.clear()

                        camera_forward_history.clear()
                        camera_lateral_history.clear()

                        rover_forward_history.clear()
                        rover_lateral_history.clear()

                        front_distance_history.clear()

                        camera_yaw_history.clear()
                        rover_yaw_history.clear()


                # =========================================
                # FILTERING
                # =========================================

                z_history.append(z)

                distance_history.append(distance)
                ground_history.append(ground_distance)

                camera_forward_history.append(
                    camera_forward
                )

                camera_lateral_history.append(
                    camera_lateral
                )

                rover_forward_history.append(
                    rover_forward
                )

                rover_lateral_history.append(
                    rover_lateral
                )

                front_distance_history.append(
                    front_distance
                )

                camera_yaw_history.append(
                    camera_yaw
                )

                rover_yaw_history.append(
                    rover_yaw
                )


                filtered_z = np.median(
                    z_history
                )

                filtered_distance = np.median(
                    distance_history
                )

                filtered_ground = np.median(
                    ground_history
                )

                filtered_camera_forward = np.median(
                    camera_forward_history
                )

                filtered_camera_lateral = np.median(
                    camera_lateral_history
                )

                filtered_rover_forward = np.median(
                    rover_forward_history
                )

                filtered_rover_lateral = np.median(
                    rover_lateral_history
                )

                filtered_front_distance = np.median(
                    front_distance_history
                )

                filtered_camera_yaw = np.median(
                    camera_yaw_history
                )

                filtered_rover_yaw = np.median(
                    rover_yaw_history
                )

                if measurement_writer is not None:
                    projected_points, _ = cv2.projectPoints(
                        object_points, rvec, tvec, new_K, zero_dist
                    )
                    residuals = projected_points.reshape(4, 2) - image_points
                    reprojection_error_px = float(
                        np.sqrt(np.mean(np.sum(residuals * residuals, axis=1)))
                    )
                    ippe_count, ippe_rvecs, ippe_tvecs, _ = cv2.solvePnPGeneric(
                        object_points,
                        image_points,
                        new_K,
                        zero_dist,
                        flags=cv2.SOLVEPNP_IPPE_SQUARE
                    )
                    ippe_values = [""] * 14
                    for candidate_index in range(min(2, int(ippe_count))):
                        candidate_rvec = ippe_rvecs[candidate_index]
                        candidate_tvec = ippe_tvecs[candidate_index]
                        candidate_projected_points, _ = cv2.projectPoints(
                            object_points, candidate_rvec, candidate_tvec, new_K, zero_dist
                        )
                        candidate_residuals = (
                            candidate_projected_points.reshape(4, 2) - image_points
                        )
                        candidate_error_px = float(
                            np.sqrt(np.mean(np.sum(
                                candidate_residuals * candidate_residuals, axis=1
                            )))
                        )
                        ippe_values[candidate_index * 7:(candidate_index + 1) * 7] = [
                            *candidate_tvec.reshape(-1).tolist(),
                            *candidate_rvec.reshape(-1).tolist(),
                            candidate_error_px,
                        ]
                    raw_corner_values = [""] * 12
                    if raw_image_points is not None:
                        corner_shifts = np.sqrt(np.sum(
                            (image_points - raw_image_points) ** 2, axis=1
                        ))
                        raw_corner_values = [
                            *raw_image_points.reshape(-1).tolist(),
                            *corner_shifts.tolist(),
                        ]
                    measurement_writer.writerow([
                        datetime.now().astimezone().isoformat(timespec="milliseconds"),
                        int(marker_id),
                        filtered_front_distance,
                        filtered_rover_forward,
                        filtered_rover_lateral,
                        filtered_rover_yaw,
                        filtered_camera_forward,
                        filtered_camera_lateral,
                        filtered_camera_yaw,
                        filtered_ground,
                        filtered_distance,
                        filtered_z,
                        marker_width_px,
                        *image_points.reshape(-1).tolist(),
                        *tvec.reshape(-1).tolist(),
                        *rvec.reshape(-1).tolist(),
                        reprojection_error_px,
                        *ippe_values,
                        *raw_corner_values,
                    ])
                    measurement_log.flush()


                # Save screen data until all pose calculations and diagnostics finish.
                display_measurements.append((
                    marker_id,
                    rvec,
                    tvec,
                    filtered_front_distance,
                    filtered_rover_forward,
                    filtered_rover_lateral,
                    filtered_rover_yaw,
                    filtered_camera_forward,
                    filtered_camera_lateral,
                    filtered_camera_yaw,
                    filtered_ground,
                    filtered_distance,
                    filtered_z,
                    marker_width_px,
                ))

        # All screen overlays use a separate copy after processing is complete.
        display_frame = frame.copy()
        if ids is not None:
            cv2.aruco.drawDetectedMarkers(display_frame, corners, ids)

        for (
            marker_id,
            rvec,
            tvec,
            filtered_front_distance,
            filtered_rover_forward,
            filtered_rover_lateral,
            filtered_rover_yaw,
            filtered_camera_forward,
            filtered_camera_lateral,
            filtered_camera_yaw,
            filtered_ground,
            filtered_distance,
            filtered_z,
            marker_width_px,
        ) in display_measurements:

            # =========================================
            # DRAW AXES
            # =========================================

            cv2.drawFrameAxes(
                display_frame,
                new_K,
                zero_dist,
                rvec,
                tvec,
                0.08
            )


            # =========================================
            # DISPLAY
            # =========================================

            cv2.putText(
                display_frame,
                f"ID: {marker_id}",
                (30, 45),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0),
                2
            )


            # -----------------------------------------
            # MAIN ROVER DATA
            # -----------------------------------------

            cv2.putText(
                display_frame,
                f"Front Distance: {filtered_front_distance:.3f} m",
                (30, 90),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0),
                2
            )

            cv2.putText(
                display_frame,
                f"ROVER Forward: {filtered_rover_forward:.3f} m",
                (30, 130),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0),
                2
            )

            cv2.putText(
                display_frame,
                f"ROVER Lateral: {filtered_rover_lateral:.3f} m",
                (30, 170),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0),
                2
            )

            cv2.putText(
                display_frame,
                f"ROVER Yaw: {filtered_rover_yaw:.1f} deg",
                (30, 210),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0),
                2
            )


            # -----------------------------------------
            # CAMERA DATA
            # -----------------------------------------

            cv2.putText(
                display_frame,
                f"Camera Forward: {filtered_camera_forward:.3f} m",
                (30, 260),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (255, 255, 0),
                2
            )

            cv2.putText(
                display_frame,
                f"Camera Lateral: {filtered_camera_lateral:.3f} m",
                (30, 295),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (255, 255, 0),
                2
            )

            cv2.putText(
                display_frame,
                f"Camera Yaw: {filtered_camera_yaw:.1f} deg",
                (30, 330),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (255, 255, 0),
                2
            )


            # -----------------------------------------
            # DIAGNOSTICS
            # -----------------------------------------

            cv2.putText(
                display_frame,
                f"Ground: {filtered_ground:.3f} m",
                (30, 380),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (255, 255, 0),
                2
            )

            cv2.putText(
                display_frame,
                f"Distance: {filtered_distance:.3f} m",
                (30, 415),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (255, 255, 0),
                2
            )

            cv2.putText(
                display_frame,
                f"Z: {filtered_z:.3f} m",
                (30, 450),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (255, 255, 0),
                2
            )

            cv2.putText(
                display_frame,
                f"Marker width: {marker_width_px:.1f} px",
                (30, 485),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (255, 255, 0),
                2
            )


        # =========================================
        # NO MARKER
        # =========================================

        if not marker_found:

            cv2.putText(
                display_frame,
                "NO MARKER",
                (30, 50),
                cv2.FONT_HERSHEY_SIMPLEX,
                1,
                (255, 0, 0),
                2
            )


        # Reticle coordinates retain subpixel precision until display drawing.
        frame_height, frame_width = display_frame.shape[:2]
        optical_cx = float(new_K[0, 2])
        optical_cy = float(new_K[1, 2])
        frame_cx = frame_width / 2
        frame_cy = frame_height / 2
        dx = optical_cx - frame_cx
        dy = optical_cy - frame_cy
        optical_center = (int(round(optical_cx)), int(round(optical_cy)))
        frame_center = (int(round(frame_cx)), int(round(frame_cy)))
        optical_color = (0, 255, 255)
        frame_color = (160, 160, 160)

        # A dark outline keeps the thin optical reticle visible on bright scenes.
        for color, thickness in (((0, 0, 0), 3), (optical_color, 1)):
            cv2.line(display_frame, (optical_center[0], 0),
                     (optical_center[0], frame_height - 1), color, thickness, cv2.LINE_AA)
            cv2.line(display_frame, (0, optical_center[1]),
                     (frame_width - 1, optical_center[1]), color, thickness, cv2.LINE_AA)
            cv2.circle(display_frame, optical_center, 7, color, thickness, cv2.LINE_AA)

        cv2.drawMarker(display_frame, frame_center, frame_color,
                       cv2.MARKER_CROSS, 10, 1, cv2.LINE_AA)
        # Place labels on opposite sides to separate nearby centers.
        labels = (
            ("OPTICAL CENTER", (optical_center[0] + 12, optical_center[1] - 14),
             optical_color),
            ("FRAME CENTER", (frame_center[0] + 12, frame_center[1] + 22),
             frame_color),
            (f"Optical - frame: dx={dx:+.2f} px  dy={dy:+.2f} px",
             (30, frame_height - 20), optical_color),
        )
        for label, origin, color in labels:
            cv2.putText(display_frame, label, origin, cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(display_frame, label, origin, cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, color, 1, cv2.LINE_AA)

        # =========================================
        # SHOW
        # =========================================

        cv2.imshow(
            "UGV Optical Navigation",
            display_frame
        )

        if cv2.waitKey(1) & 0xFF == ord("q"):
            break


except KeyboardInterrupt:

    pass


finally:

    try:
        picam2.stop()
        cv2.destroyAllWindows()
    finally:
        if measurement_log is not None:
            measurement_log.close()

    print("Camera stopped cleanly")
