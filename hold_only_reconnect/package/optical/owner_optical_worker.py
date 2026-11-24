#!/usr/bin/env python3

import math
import time

import cv2
import numpy as np

import aruco_active_marker_selector_v8 as v8
from pose_transform_2d import camera_to_rover_pose


PORT = '/dev/serial/by-id/usb-Holybro_Pixhawk6C_260025000C51333336373039-if00'
BAUD = 115200
SEND_RATE_HZ = 10.0
QUALITY = 100
VISION_BIT = 128


def wrap_deg(angle):
    return (angle + 180.0) % 360.0 - 180.0


def flu_to_local_frd(x_flu, y_flu, yaw_flu_deg):
    return (
        x_flu,
        -y_flu,
        wrap_deg(-yaw_flu_deg),
    )


def yaw_quaternion_frd(yaw_deg):
    half = math.radians(yaw_deg) * 0.5
    return [
        math.cos(half),
        0.0,
        0.0,
        math.sin(half),
    ]


def optical_measurement(camera_marker, source_marker, acquired_at, marker_side_m):
    """Direct camera-frame PnP evidence; no world alignment/pose qualification."""
    return dict(nominal_marker_side_m=float(marker_side_m), source_marker=int(source_marker),
                T_Coptical_M=camera_marker.tolist(), acquired_at=acquired_at,
                frame='C_OPTICAL_RIGHT_DOWN_FORWARD', physical_alignment_verified=False,
                model='REFINED_IPPE_SQUARE_SINGLE_SOLUTION_UNQUALIFIED')


def run(on_pose, stop_event):

    if v8.MARKER_IDS != (23, 24, 25):
        raise ValueError("Requires marker chain ID23 -> ID24 -> ID25")

    if (
        v8.DIM != (1280, 720)
        or v8.MARKER_SIZE != 0.283
        or v8.rover_config["fisheye_balance"] != 0.9
    ):
        raise ValueError(
            "Requires 1280x720, marker_size=0.283 and balance=0.9"
        )

    world_markers = {
        mid: v8.marker_map_entry_to_world_transform(
            v8.MARKER_BY_ID[mid]
        )
        for mid in v8.MARKER_IDS
    }

    dictionary = cv2.aruco.getPredefinedDictionary(
        cv2.aruco.DICT_5X5_50
    )

    detector = cv2.aruco.ArucoDetector(
        dictionary,
        cv2.aruco.DetectorParameters()
    )

    active = v8.MARKER_IDS[0]
    streaks = {mid: 0 for mid in v8.MARKER_IDS}
    below = streaks.copy()
    above = streaks.copy()

    pose_covariance = [float("nan")] + [0.0] * 20
    velocity_covariance = [float("nan")] + [0.0] * 20

    picam2 = None
    started = False

    total_frames = 0
    valid_frames = 0
    sent = 0

    vision_healthy = None

    send_period = 1.0 / SEND_RATE_HZ
    next_send = time.monotonic()
    last_print = time.monotonic()

    last_flu = None
    last_frd = None
    last_source = None

    try:

        picam2 = v8.Picamera2()

        config = picam2.create_preview_configuration(
            main={
                "size": v8.DIM,
                "format": "RGB888"
            }
        )

        picam2.configure(config)
        picam2.start()
        started = True

        new_K = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
            v8.K,
            v8.D,
            v8.DIM,
            np.eye(3),
            balance=v8.rover_config["fisheye_balance"]
        )

        map1, map2 = cv2.fisheye.initUndistortRectifyMap(
            v8.K,
            v8.D,
            np.eye(3),
            new_K,
            v8.DIM,
            cv2.CV_16SC2
        )

        zero_dist = np.zeros((5, 1))

        # Required by V8 estimate_pose()
        v8.new_K = new_K
        v8.zero_dist = zero_dist

        print()
        print("OWNER-INTEGRATED OPTICAL WORKER STARTED")
        print("No transport or motor control in camera worker.")
        print("No EKF source switching.")
        print("Ctrl+C to stop.")
        print()

        while not stop_event.is_set():

            request = picam2.capture_request()
            metadata = request.get_metadata()
            sensor_ns = metadata.get("SensorTimestamp")
            # libcamera SensorTimestamp is nanoseconds in CLOCK_BOOTTIME.
            acquired_at = None
            if type(sensor_ns) is int and sensor_ns > 0:
                age = time.clock_gettime(time.CLOCK_BOOTTIME) - sensor_ns / 1e9
                if 0 <= age <= 0.2:
                    acquired_at = time.monotonic() - age
            published = False

            try:
                frame = request.make_array("main").copy()
            finally:
                request.release()

            total_frames += 1

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

            corners, ids, _ = detector.detectMarkers(gray)

            flat_ids = (
                np.array([], dtype=int)
                if ids is None
                else ids.flatten()
            )

            frame_ids = set(map(int, flat_ids))

            legacy = {}
            shadow = {}
            raw_optical_measurements = {}

            for mid in v8.MARKER_IDS:

                indices = np.flatnonzero(flat_ids == mid)

                if not indices.size:
                    continue

                raw = corners[int(indices[0])][0].astype(np.float32)

                success, rvec, tvec = v8.estimate_pose(raw)

                if not success:
                    continue

                pose = v8.navigation_from_pose(rvec, tvec)

                if not all(
                    math.isfinite(pose[key])
                    for key in v8.POSE_KEYS
                ):
                    continue

                legacy[mid] = pose

                try:

                    refined = v8.refine_shadow_corners(
                        gray,
                        raw,
                        dictionary
                    )

                    ok, srvec, stvec = cv2.solvePnP(
                        v8.object_points,
                        refined,
                        new_K,
                        zero_dist,
                        flags=cv2.SOLVEPNP_IPPE_SQUARE
                    )

                    if not ok:
                        continue

                    camera_marker = v8.rvec_tvec_to_transform(
                        srvec,
                        stvec
                    )

                    world_camera = v8.compose_transform(
                        world_markers[mid],
                        v8.invert_transform(camera_marker)
                    )

                    forward = world_camera[:3, 2]

                    if np.linalg.norm(forward[:2]) <= 1e-10:
                        continue

                    camera_pose = [
                        float(world_camera[0, 3]),
                        float(world_camera[1, 3]),
                        math.degrees(
                            math.atan2(
                                forward[1],
                                forward[0]
                            )
                        )
                    ]

                    if not np.isfinite(camera_pose).all():
                        continue

                    shadow[mid] = camera_pose
                    raw_optical_measurements[mid] = camera_marker.copy()

                except (ValueError, TypeError, cv2.error):
                    pass

            active, event, _ = v8.update_selector(
                active,
                frame_ids,
                legacy,
                streaks,
                below,
                above
            )

            source = v8.select_pose_source(
                active,
                legacy
            )

            camera_current = shadow.get(source)

            if camera_current is not None:

                rover_flu = camera_to_rover_pose(
                    *camera_current
                )

                if all(math.isfinite(v) for v in rover_flu):

                    valid_frames += 1

                    x_flu, y_flu, yaw_flu = rover_flu

                    x_frd, y_frd, yaw_frd = flu_to_local_frd(
                        x_flu,
                        y_flu,
                        yaw_flu
                    )

                    last_flu = rover_flu
                    last_frd = (
                        x_frd,
                        y_frd,
                        yaw_frd
                    )
                    last_source = source

                    now = time.monotonic()


                    q = yaw_quaternion_frd(
                        yaw_frd
                    )

                    if acquired_at is not None:
                        on_pose(dict(acquired_at=acquired_at, source_marker=int(source),
                                     x=x_frd, y=y_frd, z=0.0, q=q,
                                     optical_measurement=optical_measurement(
                                         raw_optical_measurements[source], source, acquired_at, v8.MARKER_SIZE)))
                        published = True

                    sent += 1
                    next_send = now + send_period

            if not published:
                on_pose(None)

            now = time.monotonic()

            if now - last_print >= 1.0:

                if last_flu is None:

                    print(
                        f"frames={total_frames} "
                        f"valid={valid_frames} "
                        f"callbacks={sent} "
                        f"VISION={vision_healthy} "
                        f"POSE=NONE"
                    )

                else:

                    print(
                        f"source={last_source} | "
                        f"FLU "
                        f"x={last_flu[0]:+.3f} "
                        f"y={last_flu[1]:+.3f} "
                        f"yaw={last_flu[2]:+.2f}° | "
                        f"FRD "
                        f"x={last_frd[0]:+.3f} "
                        f"y={last_frd[1]:+.3f} "
                        f"yaw={last_frd[2]:+.2f}° | "
                        f"callbacks={sent} "
                        f"VISION={vision_healthy}"
                    )

                last_print = now

    finally:

        if picam2 is not None:
            if started:
                picam2.stop()
            picam2.close()

        print()
        print("STOPPED")
        print("frames:", total_frames)
        print("valid:", valid_frames)
        print("Optical callbacks:", sent)
        print("No EKF source switch was performed.")
