"""Dynamic rover WORLD FLU 2D validation with fixed MVP camera extrinsic.

Localization and RAW V8 selector are inherited from camera dynamic validation.
SUBPIX camera pose is converted with pose_transform_2d, without re-zeroing.
No roll/pitch, motor control or Pixhawk publishing. No existing CSV overwrite.
"""
import csv
from datetime import datetime
import json
import math
import signal
import time

import cv2
import numpy as np

# V8's guarded main is not executed. Import reads config/calibration/map and
# prints map metadata, but does not start a camera or open output files.
import aruco_active_marker_selector_v8 as v8
from pose_transform_2d import camera_to_rover_pose

FULL_3D_AUTHORITATIVE = False
START_DELAY_SEC = 10.0
HOME_BASELINE_SEC = 1.0
END_HOME_STATIONARY_SEC = 5.0
HOME_POSITION_TOLERANCE_M = 0.30
HOME_YAW_TOLERANCE_DEG = 10.0
STATIONARY_POSITION_STEP_M = 0.02
STATIONARY_YAW_STEP_DEG = 1.0
MAX_SAMPLE_GAP_SEC = 0.5
EXPECTED_SWITCH_ORDER = ("23_TO_24", "24_TO_25", "25_TO_24", "24_TO_23")
CSV_PATH = v8.PROJECT_DIR / "aruco_rover_pose2d_dynamic_validate.csv"
FIELDS = ["timestamp", "frame_index", "SensorTimestamp", "pose_frame",
          "pose_valid", "active_marker", "pose_source_marker", "fallback_used",
          "detected_ids", "camera_world_x", "camera_world_y", "camera_yaw",
          "rover_world_x", "rover_world_y", "rover_yaw", "switch_event",
          "raw_subpix_shift_px", "previous_valid_frame", "previous_valid_gap_frames",
          "delta_x_m", "delta_y_m", "delta_position_m", "delta_yaw_deg",
          "same_frame_source_delta_x_m", "same_frame_source_delta_y_m",
          "same_frame_source_delta_position_m", "same_frame_source_delta_yaw_deg"]


def wrap(value):
    return (value + 180.) % 360. - 180.


def delta(before, after):
    dx, dy = after[0] - before[0], after[1] - before[1]
    return [float(dx), float(dy), float(math.hypot(dx, dy)), float(wrap(after[2] - before[2]))]


def main():
    if v8.MARKER_IDS != (23, 24, 25):
        raise ValueError("Requires marker chain ID23 -> ID24 -> ID25")
    if v8.DIM != (1280, 720) or v8.MARKER_SIZE != 0.283 or v8.rover_config["fisheye_balance"] != 0.9:
        raise ValueError("Requires 1280x720, marker_size=0.283 and balance=0.9")
    if v8.FULL_3D_AUTHORITATIVE:
        raise ValueError("V8 must remain shadow-only")
    world_markers = {mid: v8.marker_map_entry_to_world_transform(v8.MARKER_BY_ID[mid])
                     for mid in v8.MARKER_IDS}
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50)
    detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
    active = v8.MARKER_IDS[0]
    streaks = {mid: 0 for mid in v8.MARKER_IDS}
    below = streaks.copy()
    above = streaks.copy()
    total = valid = missing = consecutive = max_missing = 0
    switch_events = []
    samples = []
    previous = None
    previous_frame = None
    stop_requested = False
    home = None
    baseline_samples = []
    camera_baseline_samples = []
    camera_home = None
    baseline_started = None
    last_pose_time = None
    cycle_progress = 0
    cycle_complete = False
    home_hold_started = None
    auto_finished = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    with CSV_PATH.open("x", newline="", encoding="utf-8") as log:
        writer = csv.DictWriter(log, fieldnames=FIELDS)
        print("Manual run: HOME -> about +3.2 m -> HOME; Ctrl+C stops capture.")
        print("Logged pose: CAMERA and ROVER world XY/forward heading (FLU), fixed MVP planar extrinsic.")
        print("RAW V8 selector preserved; SUBPIX+IPPE shadow only. FULL_3D_AUTHORITATIVE=False.")
        print("No motor control or Pixhawk output. CSV includes invalid frames for dropout audit.")
        picam2 = None
        started = False
        old_handler = signal.signal(signal.SIGINT, request_stop)
        try:
            picam2 = v8.Picamera2()
            config = picam2.create_preview_configuration(main={"size": v8.DIM, "format": "RGB888"})
            picam2.configure(config)
            picam2.start()
            started = True
            new_K = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
                v8.K, v8.D, v8.DIM, np.eye(3), balance=v8.rover_config["fisheye_balance"])
            map1, map2 = cv2.fisheye.initUndistortRectifyMap(
                v8.K, v8.D, np.eye(3), new_K, v8.DIM, cv2.CV_16SC2)
            zero_dist = np.zeros((5, 1))
            # Required globals for the unchanged V8 estimate_pose helper.
            v8.new_K, v8.zero_dist = new_K, zero_dist
            countdown_end = time.monotonic() + START_DELAY_SEC
            last_count = None
            while not stop_requested:
                remaining = int(math.ceil(countdown_end - time.monotonic()))
                if remaining <= 0:
                    break
                if remaining != last_count:
                    print(f"Test starts in {remaining}..." if last_count is None
                          else f"{remaining}...", flush=True)
                    last_count = remaining
                # Drain startup frames without detection, selector updates or logging.
                warmup_request = picam2.capture_request()
                warmup_request.release()
            if not stop_requested:
                print("START", flush=True)
                total = valid = missing = consecutive = max_missing = 0
                active = v8.MARKER_IDS[0]
                streaks = {mid: 0 for mid in v8.MARKER_IDS}
                below, above = streaks.copy(), streaks.copy()
                switch_events.clear()
                samples.clear()
                previous = previous_frame = None
                writer.writeheader()
                log.flush()
                print("Hold at HOME for the initial stable 1-second baseline.", flush=True)
            while not stop_requested:
                request = picam2.capture_request()
                try:
                    frame = request.make_array("main").copy()
                    metadata = request.get_metadata()
                finally:
                    request.release()
                total += 1
                timestamp = datetime.now().astimezone().isoformat(timespec="microseconds")
                frame = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR)
                gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
                corners, ids, _ = detector.detectMarkers(gray)
                flat_ids = np.array([], dtype=int) if ids is None else ids.flatten()
                frame_ids = set(map(int, flat_ids))
                legacy, shadow, shifts = {}, {}, {}
                for mid in v8.MARKER_IDS:
                    indices = np.flatnonzero(flat_ids == mid)
                    if not indices.size:
                        continue
                    raw = corners[int(indices[0])][0].astype(np.float32)
                    success, rvec, tvec = v8.estimate_pose(raw)
                    if not success:
                        continue
                    pose = v8.navigation_from_pose(rvec, tvec)
                    if not all(math.isfinite(pose[key]) for key in v8.POSE_KEYS):
                        continue
                    legacy[mid] = pose
                    try:
                        refined = v8.refine_shadow_corners(gray, raw, dictionary)
                        ok, srvec, stvec = cv2.solvePnP(v8.object_points, refined, new_K,
                                                      zero_dist, flags=cv2.SOLVEPNP_IPPE_SQUARE)
                        if not ok:
                            continue
                        camera_marker = v8.rvec_tvec_to_transform(srvec, stvec)
                        world_camera = v8.compose_transform(world_markers[mid], v8.invert_transform(camera_marker))
                        forward = world_camera[:3, 2]
                        if np.linalg.norm(forward[:2]) <= 1e-10:
                            continue
                        xyh = [float(world_camera[0, 3]), float(world_camera[1, 3]),
                               math.degrees(math.atan2(forward[1], forward[0]))]
                        # No roll/pitch calculation, threshold or Euler validity gate.
                        if not np.isfinite(xyh).all():
                            continue
                        shadow[mid] = xyh
                        shifts[mid] = float(np.linalg.norm(refined - raw, axis=1).mean())
                    except (ValueError, TypeError, cv2.error):
                        pass
                old_source = v8.select_pose_source(active, legacy)
                active, event, _ = v8.update_selector(active, frame_ids, legacy, streaks, below, above)
                source = v8.select_pose_source(active, legacy)
                camera_current = shadow.get(source)
                rover_shadow = {mid: list(camera_to_rover_pose(*pose)) for mid, pose in shadow.items()}
                current = rover_shadow.get(source)
                sample_time = time.monotonic()
                row = dict(timestamp=timestamp, frame_index=total,
                           SensorTimestamp=metadata.get("SensorTimestamp", ""),
                           pose_frame="WORLD_FLU_ROVER", pose_valid=int(current is not None),
                           active_marker=active, pose_source_marker=source if source is not None else "",
                           fallback_used=int(source is not None and source != active),
                           detected_ids=json.dumps(sorted(frame_ids)), switch_event=event)
                switch = None
                if event != "NONE":
                    switch = dict(frame=total, event=event, temporal_delta=None, same_frame_delta=None)
                    switch_events.append(switch)
                    if not cycle_complete:
                        if event == EXPECTED_SWITCH_ORDER[cycle_progress]:
                            cycle_progress += 1
                        else:
                            # Require a complete consecutive route, not skipped/reversed steps.
                            cycle_progress = 1 if event == EXPECTED_SWITCH_ORDER[0] else 0
                        if cycle_progress == len(EXPECTED_SWITCH_ORDER):
                            cycle_complete = True
                            print("Full switch cycle complete; return to HOME and hold still.", flush=True)
                    if old_source in shadow and source in shadow:
                        switch["same_frame_delta"] = delta(rover_shadow[old_source], rover_shadow[source])
                        for key, value in zip(FIELDS[-4:], switch["same_frame_delta"]):
                            row[key] = value
                if current is not None:
                    continuous = (last_pose_time is not None
                                  and sample_time - last_pose_time <= MAX_SAMPLE_GAP_SEC)
                    step = delta(previous, current) if previous is not None else None
                    stationary = (continuous and step is not None
                                  and step[2] <= STATIONARY_POSITION_STEP_M
                                  and abs(step[3]) <= STATIONARY_YAW_STEP_DEG)
                    if home is None:
                        anchor_delta = delta(baseline_samples[0], current) if baseline_samples else None
                        stable_baseline = (stationary and anchor_delta is not None
                                           and anchor_delta[2] <= STATIONARY_POSITION_STEP_M
                                           and abs(anchor_delta[3]) <= STATIONARY_YAW_STEP_DEG
                                           and event == "NONE" and active == 23)
                        if not stable_baseline:
                            baseline_samples = []
                            camera_baseline_samples = []
                            baseline_started = sample_time
                        baseline_samples.append(current.copy())
                        camera_baseline_samples.append(camera_current.copy())
                        if (active == 23 and len(baseline_samples) >= 2
                                and sample_time - baseline_started >= HOME_BASELINE_SEC):
                            baseline = np.asarray(baseline_samples)
                            radians = np.radians(baseline[:, 2])
                            home = [float(baseline[:, 0].mean()), float(baseline[:, 1].mean()),
                                    math.degrees(math.atan2(np.sin(radians).mean(), np.cos(radians).mean()))]
                            cb = np.asarray(camera_baseline_samples)
                            cr = np.radians(cb[:, 2])
                            camera_home = [float(cb[:, 0].mean()), float(cb[:, 1].mean()),
                                           math.degrees(math.atan2(np.sin(cr).mean(), np.cos(cr).mean()))]
                            print(f"camera HOME pose={camera_home}", flush=True)
                            print(f"rover HOME pose={home}; expected approximately (0,0,0)", flush=True)
                            print(f"HOME baseline: home_x={home[0]:.6f} home_y={home[1]:.6f} "
                                  f"home_yaw={home[2]:.6f}; samples={len(baseline_samples)}", flush=True)
                            print("Begin route: HOME -> about +3.2 m -> HOME.", flush=True)
                    if home is not None and cycle_complete:
                        home_delta = delta(home, current)
                        at_home = (home_delta[2] <= HOME_POSITION_TOLERANCE_M
                                   and abs(home_delta[3]) <= HOME_YAW_TOLERANCE_DEG)
                        if at_home and stationary and event == "NONE":
                            if home_hold_started is None:
                                home_hold_started = sample_time
                                print("RETURNED TO HOME AREA\nHolding stationary...", flush=True)
                            elif sample_time - home_hold_started >= END_HOME_STATIONARY_SEC:
                                auto_finished = True
                        else:
                            home_hold_started = None
                    last_pose_time = sample_time
                    valid += 1
                    consecutive = 0
                    row.update(zip(("rover_world_x", "rover_world_y", "rover_yaw"), current))
                    row.update(zip(("camera_world_x", "camera_world_y", "camera_yaw"), camera_current))
                    row["raw_subpix_shift_px"] = shifts[source]
                    if switch is not None and previous is not None:
                        switch["temporal_delta"] = delta(previous, current)
                        row.update(previous_valid_frame=previous_frame,
                                   previous_valid_gap_frames=total - previous_frame)
                        for key, value in zip(("delta_x_m", "delta_y_m", "delta_position_m", "delta_yaw_deg"),
                                              switch["temporal_delta"]):
                            row[key] = value
                    samples.append(current)
                    previous, previous_frame = current, total
                else:
                    # Missing poses break both baseline stability and the continuous HOME hold.
                    baseline_samples = []
                    camera_baseline_samples = []
                    baseline_started = None
                    last_pose_time = None
                    home_hold_started = None
                    missing += 1
                    consecutive += 1
                    max_missing = max(max_missing, consecutive)
                writer.writerow(row)
                log.flush()
                if switch is not None:
                    print("SWITCH " + json.dumps(switch), flush=True)
                if auto_finished:
                    print("AUTO FINISH", flush=True)
                    break
        finally:
            try:
                if picam2 is not None:
                    try:
                        if started:
                            picam2.stop()
                    finally:
                        picam2.close()
            finally:
                signal.signal(signal.SIGINT, old_handler)
                print(f"\nFULL_3D_AUTHORITATIVE={FULL_3D_AUTHORITATIVE}")
                print(f"completion={'AUTO FINISH' if auto_finished else 'MANUAL/INTERRUPTED'} "
                      f"full_switch_cycle_complete={cycle_complete}")
                print(f"starting_rover_pose={home if home is not None else 'N/A (baseline not acquired)'}")
                print(f"total_frames={total} frames_with_valid_pose={valid} frames_without_pose={missing}")
                print(f"max_consecutive_frames_without_pose={max_missing}")
                print(f"switch_count={len(switch_events)} order={[s['event'] for s in switch_events]}")
                if samples:
                    data = np.asarray(samples)
                    print(f"rover X min/max={data[:,0].min():.6f}/{data[:,0].max():.6f} m")
                    print(f"rover Y min/max={data[:,1].min():.6f}/{data[:,1].max():.6f} m")
                    yaw = np.degrees(np.unwrap(np.radians(data[:,2])))
                    print(f"rover yaw unwrapped min/max={yaw.min():.6f}/{yaw.max():.6f} deg; range={np.ptp(yaw):.6f} deg")
                print(f"camera HOME pose={camera_home}")
                print(f"final_rover_pose={samples[-1] if samples else 'N/A'}")
                if home is not None and samples:
                    final_delta = delta(home, samples[-1])
                    print(f"final_position_difference_from_start={final_delta[2]:.6f} m")
                    print(f"final_yaw_difference_from_start={final_delta[3]:+.6f} deg")
                else:
                    print("final_position_difference_from_start=N/A final_yaw_difference_from_start=N/A")
                for kind in ("temporal_delta", "same_frame_delta"):
                    values = [s[kind] for s in switch_events if s[kind] is not None]
                    if values:
                        print(f"{kind}: measured_switches={len(values)}/{len(switch_events)} "
                              f"max_rover_position_jump={max(v[2] for v in values):.6f} m "
                              f"max_rover_abs_yaw_jump={max(abs(v[3]) for v in values):.6f} deg")
                    else:
                        print(f"{kind}: max_rover_position_jump=N/A max_rover_yaw_jump=N/A (no measurable switches)")
                print("Temporal switch jumps include motion/gaps; same-frame deltas compare sources at one instant.")
                print("Final difference is NOT automatically localization error: manual HOME placement may be offset.")
                print(f"CSV: {CSV_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
