from picamera2 import Picamera2
import cv2
import numpy as np
from collections import deque
import math


# =================================================
# CALIBRATION
# =================================================

data = np.load("/home/ugvs2/camera_calibration_fisheye.npz")

K = data["K"]
D = data["D"]

DIM = (1280, 720)


# =================================================
# CAMERA / ROVER / MARKER GEOMETRY
# =================================================

CAMERA_HEIGHT = 1.215

# Camera position relative to rover geometric center
CAMERA_FORWARD_OFFSET = 0.000

# + = right
# - = left
CAMERA_LATERAL_OFFSET = -0.525

# Camera yaw relative to rover
# + = right
# - = left
CAMERA_YAW_DEG = 5.4

CAMERA_YAW_RAD = math.radians(CAMERA_YAW_DEG)

# Distance from rover geometric center
# to the front edge of the rover
ROVER_FRONT_OFFSET = 0.600


MARKER_SIZE = 0.159
HALF = MARKER_SIZE / 2.0

TARGET_ID = 23


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
    balance=0.9
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

detector = cv2.aruco.ArucoDetector(dictionary)


# =================================================
# MAIN LOOP
# =================================================

try:

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

        marker_found = False


        # =========================================
        # MARKER DETECTED
        # =========================================

        if ids is not None:

            cv2.aruco.drawDetectedMarkers(
                frame,
                corners,
                ids
            )

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


                # =========================================
                # DRAW AXES
                # =========================================

                cv2.drawFrameAxes(
                    frame,
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
                    frame,
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
                    frame,
                    f"Front Distance: {filtered_front_distance:.3f} m",
                    (30, 90),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (0, 255, 0),
                    2
                )

                cv2.putText(
                    frame,
                    f"ROVER Forward: {filtered_rover_forward:.3f} m",
                    (30, 130),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (0, 255, 0),
                    2
                )

                cv2.putText(
                    frame,
                    f"ROVER Lateral: {filtered_rover_lateral:.3f} m",
                    (30, 170),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (0, 255, 0),
                    2
                )

                cv2.putText(
                    frame,
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
                    frame,
                    f"Camera Forward: {filtered_camera_forward:.3f} m",
                    (30, 260),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.65,
                    (255, 255, 0),
                    2
                )

                cv2.putText(
                    frame,
                    f"Camera Lateral: {filtered_camera_lateral:.3f} m",
                    (30, 295),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.65,
                    (255, 255, 0),
                    2
                )

                cv2.putText(
                    frame,
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
                    frame,
                    f"Ground: {filtered_ground:.3f} m",
                    (30, 380),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.65,
                    (255, 255, 0),
                    2
                )

                cv2.putText(
                    frame,
                    f"Distance: {filtered_distance:.3f} m",
                    (30, 415),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.65,
                    (255, 255, 0),
                    2
                )

                cv2.putText(
                    frame,
                    f"Z: {filtered_z:.3f} m",
                    (30, 450),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.65,
                    (255, 255, 0),
                    2
                )

                cv2.putText(
                    frame,
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
                frame,
                "NO MARKER",
                (30, 50),
                cv2.FONT_HERSHEY_SIMPLEX,
                1,
                (255, 0, 0),
                2
            )


        # =========================================
        # SHOW
        # =========================================

        cv2.imshow(
            "UGV Optical Navigation",
            frame
        )

        if cv2.waitKey(1) & 0xFF == ord("q"):
            break


except KeyboardInterrupt:

    pass


finally:

    picam2.stop()
    cv2.destroyAllWindows()

    print("Camera stopped cleanly")
