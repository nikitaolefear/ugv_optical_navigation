#!/usr/bin/env python3

import argparse
import math
import time

from pymavlink import mavutil


def wrap_deg(angle):
    return (angle + 180.0) % 360.0 - 180.0


def flu_to_local_frd(x_flu, y_flu, yaw_flu_deg):
    """
    WORLD FLU:
      +X forward, +Y left, +Z up, positive yaw CCW

    MAV_FRAME_LOCAL_FRD:
      +X forward, +Y right, +Z down, positive yaw clockwise
    """
    return (
        x_flu,
        -y_flu,
        wrap_deg(-yaw_flu_deg),
    )


def yaw_quaternion_frd(yaw_deg):
    """Quaternion [w, x, y, z] for roll=0, pitch=0, FRD yaw."""
    half = math.radians(yaw_deg) * 0.5
    return [
        math.cos(half),
        0.0,
        0.0,
        math.sin(half),
    ]


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Bench-only MAVLink ODOMETRY transport test. "
            "Sends a static 2.5D pose with quality=-1 by default, "
            "so it must not be treated as valid ExternalNav data."
        )
    )
    parser.add_argument("connection", help="Serial device, preferably /dev/serial/by-id/...")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--rate", type=float, default=10.0, help="ODOMETRY send rate, Hz")
    parser.add_argument("--duration", type=float, default=10.0, help="Test duration, seconds")

    parser.add_argument("--x-flu", type=float, default=0.0)
    parser.add_argument("--y-flu", type=float, default=0.0)
    parser.add_argument("--yaw-flu-deg", type=float, default=0.0)

    parser.add_argument(
        "--quality",
        type=int,
        default=-1,
        help="-1=failed/invalid; intentionally used for safe transport-only bench test",
    )

    args = parser.parse_args()

    if not (4.0 <= args.rate <= 50.0):
        raise SystemExit("ERROR: --rate must be between 4 and 50 Hz")

    if args.duration <= 0:
        raise SystemExit("ERROR: --duration must be > 0")

    if not (-1 <= args.quality <= 100):
        raise SystemExit("ERROR: --quality must be from -1 to 100")

    x_frd, y_frd, yaw_frd_deg = flu_to_local_frd(
        args.x_flu,
        args.y_flu,
        args.yaw_flu_deg,
    )

    q = yaw_quaternion_frd(yaw_frd_deg)

    # NaN in element 0 marks covariance as unknown.
    pose_covariance = [float("nan")] + [0.0] * 20
    velocity_covariance = [float("nan")] + [0.0] * 20

    print(f"Opening: {args.connection}")
    print(f"Baud: {args.baud}")
    print("Waiting for Pixhawk HEARTBEAT...")

    master = mavutil.mavlink_connection(
        args.connection,
        baud=args.baud,
        source_system=245,
    )

    heartbeat = master.wait_heartbeat(timeout=10)

    if heartbeat is None:
        raise SystemExit("FAIL: no Pixhawk HEARTBEAT within 10 seconds")

    print(
        f"HEARTBEAT OK: system={master.target_system}, "
        f"component={master.target_component}"
    )

    print(
        "Static test pose: "
        f"FLU x={args.x_flu:.3f} m, y={args.y_flu:.3f} m, "
        f"yaw={args.yaw_flu_deg:.3f} deg"
    )
    print(
        "Converted LOCAL_FRD: "
        f"x={x_frd:.3f} m, y={y_frd:.3f} m, z=0.000 m, "
        f"yaw={yaw_frd_deg:.3f} deg"
    )
    print(
        f"Sending ODOMETRY at {args.rate:.1f} Hz for "
        f"{args.duration:.1f} s, quality={args.quality}"
    )

    period = 1.0 / args.rate
    start = time.monotonic()
    next_send = start
    sent = 0

    try:
        while True:
            now = time.monotonic()

            if now - start >= args.duration:
                break

            if now < next_send:
                time.sleep(next_send - now)
                continue

            time_usec = time.monotonic_ns() // 1000

            master.mav.odometry_send(
                time_usec,
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
                args.quality,
            )

            sent += 1
            next_send += period

    except KeyboardInterrupt:
        print("\nInterrupted by user.")

    elapsed = time.monotonic() - start

    print(
        f"ODOMETRY transport test complete: "
        f"sent={sent}, elapsed={elapsed:.3f} s"
    )
    print("No EKF parameter changes were made.")
    print("TEST STAGE COMPLETE: sender executed without commanding vehicle motion.")


if __name__ == "__main__":
    main()
