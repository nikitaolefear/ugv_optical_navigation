#!/usr/bin/env python3

import math
import signal
import time

import cv2
import numpy as np
from pymavlink import mavutil

import aruco_active_marker_selector_v8 as v8
from pose_transform_2d import camera_to_rover_pose


PORT = '/dev/serial/by-id/usb-Holybro_Pixhawk6C_260025000C51333336373039-if00'
BAUD = 115200
SEND_RATE_HZ = 10.0
QUALITY = 100
VISION_BIT = 128

# Direct optical-pose watchdog.
# Starts only after the first valid pose has been acquired.
LOSS_TIMEOUT_SEC = 0.7

# Before ExternalNav becomes the active EKF source:
# require a continuous fresh optical pose and VISION healthy.
STARTUP_VALID_SEC = 2.0
STARTUP_MAX_POSE_GAP_SEC = 0.30

CMD_SET_EKF_SOURCE = getattr(
    mavutil.mavlink,
    'MAV_CMD_SET_EKF_SOURCE_SET',
    42007
)


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


def main():

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

    print("Opening Pixhawk:", PORT)

    master = mavutil.mavlink_connection(
        PORT,
        baud=BAUD,
        source_system=1,
        source_component=191,
    )

    heartbeat = master.wait_heartbeat(timeout=10)

    if heartbeat is None:
        raise SystemExit("FAIL: no Pixhawk HEARTBEAT")

    print(
        f"PIXHAWK HEARTBEAT: "
        f"{heartbeat.get_srcSystem()}:{heartbeat.get_srcComponent()}"
    )

    armed = bool(
        heartbeat.base_mode
        & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
    )

    print("PIXHAWK ARMED:", armed)

    if armed:
        raise RuntimeError(
            "SAFETY ABORT: Pixhawk is ARMED. "
            "ExternalNav pre-rover test refused."
        )

    print("SAFETY CHECK PASSED: Pixhawk is DISARMED")

    def switch_source(source):
        print(f"SOURCE SWITCH -> SET {source}")

        master.mav.command_long_send(
            master.target_system,
            master.target_component,
            CMD_SET_EKF_SOURCE,
            0,
            float(source),
            0, 0, 0, 0, 0, 0
        )

        end = time.time() + 3.0

        while time.time() < end:
            msg = master.recv_match(
                type='COMMAND_ACK',
                blocking=True,
                timeout=0.3
            )

            if (
                msg is not None
                and msg.command == CMD_SET_EKF_SOURCE
            ):
                print(
                    f"SOURCE {source} ACK:",
                    msg.result
                )
                return (
                    msg.result
                    == mavutil.mavlink.MAV_RESULT_ACCEPTED
                )

        print(f"SOURCE {source}: NO ACK")
        return False

    # Always begin from Source Set 1.
    if not switch_source(1):
        raise RuntimeError(
            "Could not establish safe startup Source Set 1"
        )

    # SYS_STATUS @ 10 Hz for VisualOdom monitoring
    master.mav.command_long_send(
        master.target_system,
        master.target_component,
        mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
        0,
        mavutil.mavlink.MAVLINK_MSG_ID_SYS_STATUS,
        100000,
        0, 0, 0, 0, 0
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

    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    old_handler = signal.signal(signal.SIGINT, request_stop)

    picam2 = None
    started = False

    total_frames = 0
    valid_frames = 0
    sent = 0

    vision_healthy = None

    # ExternalNav startup / safety state
    last_valid_pose_time = None
    startup_valid_since = None

    source2_selected = False
    watchdog_armed = False

    failsafe_latched = False
    hold_confirmed = False
    recovery_reported = False

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
        print("SHADOW EXTERNALNAV STARTED")
        print("No motor control.")
        print("No EKF source switching.")
        print("Ctrl+C to stop.")
        print()

        while not stop_requested:

            request = picam2.capture_request()

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

                    # Fresh genuine optical pose.
                    now_pose = time.monotonic()
                    last_valid_pose_time = now_pose

                    # Before Source Set 2 is selected we require
                    # a continuous fresh-pose window.
                    if (
                        not source2_selected
                        and startup_valid_since is None
                    ):
                        startup_valid_since = now_pose
                        print(
                            "STARTUP: optical pose acquired; "
                            "stability timer started"
                        )

                    if failsafe_latched and not recovery_reported:
                        recovery_reported = True
                        print(
                            "OPTICAL POSE RECOVERED, but FAILSAFE remains LATCHED. "
                            "Automatic motion resume is NOT allowed."
                        )

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

                    if now >= next_send:

                        q = yaw_quaternion_frd(
                            yaw_frd
                        )

                        master.mav.odometry_send(
                            time.monotonic_ns() // 1000,

                            mavutil.mavlink.MAV_FRAME_LOCAL_FRD,
                            mavutil.mavlink.MAV_FRAME_BODY_FRD,

                            x_frd,
                            y_frd,
                            0.0,

                            q,

                            0.0,
                            0.0,
                            0.0,

                            0.0,
                            0.0,
                            0.0,

                            pose_covariance,
                            velocity_covariance,

                            0,
                            mavutil.mavlink.MAV_ESTIMATOR_TYPE_VISION,
                            QUALITY
                        )

                        sent += 1
                        next_send = now + send_period

            # Direct watchdog uses the local time of the last valid
            # optical pose. It does NOT wait for ArduPilot VISION=0.
            now_watchdog = time.monotonic()

            if (
                watchdog_armed
                and not failsafe_latched
                and last_valid_pose_time is not None
                and now_watchdog - last_valid_pose_time >= LOSS_TIMEOUT_SEC
            ):
                lost_for = now_watchdog - last_valid_pose_time

                print()
                print(
                    f"FAILSAFE: optical pose lost for {lost_for:.3f}s "
                    f"(limit {LOSS_TIMEOUT_SEC:.1f}s)"
                )
                print("FAILSAFE -> COMMANDING HOLD")

                master.set_mode_apm('HOLD')
                failsafe_latched = True

            # Read status/mode without blocking the camera loop.
            while True:

                msg = master.recv_match(
                    type=['SYS_STATUS', 'HEARTBEAT'],
                    blocking=False
                )

                if msg is None:
                    break

                if msg.get_type() == 'SYS_STATUS':
                    vision_healthy = int(
                        bool(
                            msg.onboard_control_sensors_health
                            & VISION_BIT
                        )
                    )

                elif (
                    msg.get_type() == 'HEARTBEAT'
                    and msg.get_srcSystem() == master.target_system
                    and failsafe_latched
                ):
                    mode = mavutil.mode_string_v10(msg)

                    if mode == 'HOLD' and not hold_confirmed:
                        hold_confirmed = True
                        print("HOLD CONFIRMED")

            # -------------------------------------------------
            # PRE-ROVER STARTUP GATE
            # -------------------------------------------------

            now_startup = time.monotonic()

            if not source2_selected and not failsafe_latched:

                if (
                    last_valid_pose_time is None
                    or
                    now_startup - last_valid_pose_time
                    > STARTUP_MAX_POSE_GAP_SEC
                ):
                    if startup_valid_since is not None:
                        print(
                            "STARTUP: pose stream interrupted; "
                            "stability timer reset"
                        )
                    startup_valid_since = None

                elif (
                    startup_valid_since is not None
                    and vision_healthy == 1
                    and
                    now_startup - startup_valid_since
                    >= STARTUP_VALID_SEC
                ):
                    print()
                    print(
                        f"STARTUP READY: continuous pose >= "
                        f"{STARTUP_VALID_SEC:.1f}s and VISION=1"
                    )
                    print("ACTIVATING EKF SOURCE SET 2")

                    if switch_source(2):
                        source2_selected = True
                        watchdog_armed = True

                        print(
                            "SOURCE SET 2 ACTIVE"
                        )
                        print(
                            f"WATCHDOG ARMED: timeout="
                            f"{LOSS_TIMEOUT_SEC:.1f}s"
                        )
                    else:
                        print(
                            "FAILSAFE: Source Set 2 activation failed"
                        )
                        master.set_mode_apm('HOLD')
                        failsafe_latched = True

            now = time.monotonic()

            if now - last_print >= 1.0:

                if last_flu is None:

                    print(
                        f"frames={total_frames} "
                        f"valid={valid_frames} "
                        f"sent={sent} "
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
                        f"sent={sent} "
                        f"VISION={vision_healthy}"
                    )

                last_print = now

    finally:

        try:
            if picam2 is not None:
                if started:
                    picam2.stop()
                picam2.close()

        finally:
            signal.signal(
                signal.SIGINT,
                old_handler
            )

        print()
        print("STOPPED")
        print("frames:", total_frames)
        print("valid:", valid_frames)
        print("ODOMETRY sent:", sent)
        print("source2_selected:", source2_selected)
        print("failsafe_latched:", failsafe_latched)
        print("hold_confirmed:", hold_confirmed)

        if source2_selected:
            print()
            print("SHUTDOWN SAFETY -> COMMANDING HOLD")
            master.set_mode_apm('HOLD')
            time.sleep(0.5)

            print("SHUTDOWN -> RESTORING SOURCE SET 1")
            switch_source(1)


if __name__ == "__main__":
    main()
