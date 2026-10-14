# UGV Optical Navigation

This file provides persistent technical context for future Codex sessions.
Treat it as authoritative unless the user explicitly provides newer measurements
or requirements. Compare it with the repository before making changes and report
conflicts instead of silently changing validated values.

Hardware details and historical test results below are user-provided project
context. Current software behavior has been checked against the repository.

## Project goal

This project implements optical localization for an indoor warehouse UGV.

The current focus is:

- Camera-based localization.
- ArUco floor marker detection.
- Rover-relative Forward / Lateral / Yaw.
- Calibration and stability testing.

Future stages include multiple ArUco markers, a marker map, global X/Y/Yaw
localization, MAVLink communication to Pixhawk 6C, autonomous navigation, and
obstacle / safety logic.

Do not add motor control or Pixhawk control until explicitly requested.

## Hardware

Main computer:

- Raspberry Pi 5.
- Debian GNU/Linux 13 / Trixie.
- User: `ugvs2`.
- Project directory: `/home/ugvs2/ugv_optical_navigation`.

Camera:

- SkyPulse Raspberry Pi Camera MIPI, Low-Light, 1080p60 class.
- Detected by the system as `HM2131L`.
- Connected to CAM0; the Raspberry Pi 5 CAM0 connector is 22-pin.
- Uses the Picamera2 / libcamera pipeline.

Use Picamera2. Do not use `cv2.VideoCapture(0)`.

Known camera modes:

- 1280x720 at up to approximately 124 fps.
- 1920x1080 at up to approximately 80 fps.

Current processing resolution is **1280x720**. Actual processing rate was
approximately **58-60 fps** in earlier testing.

## Camera resource conflict

The Raspberry Pi may also run `/home/ugvs2/stream.py`, which can start
`rpicam-vid` and occupy the HM2131L camera.

If `aruco_live.py` fails with errors such as:

```text
Pipeline handler in use by another process
Failed to acquire camera: Device or resource busy
```

First check camera-related processes:

```bash
ps -ef | grep -E 'rpicam|libcamera|aruco_live|python3' | grep -v grep
```

Do not automatically kill processes. Identify which process owns the camera first.

## Camera intrinsic calibration

Calibration is stored in `camera_calibration_fisheye.npz` and must be loaded from
the project directory, beside `aruco_live.py`.

The calibration uses the **OpenCV fisheye model**. Expected NPZ keys are `K` and
`D`. The current loader checks that the file exists, is an NPZ archive, contains
both keys, and can load both arrays with `allow_pickle=False`.

Required shapes:

- `K`: `(3, 3)`.
- `D`: exactly four fisheye coefficients, with shape `(4, 1)`, `(1, 4)`, or `(4,)`.

Loading or validation failures print an error to stderr and exit with status 1
before camera initialization. The arrays are used without reshaping or changing
their values.

Approximate original calibration:

```text
K = [[1181.45616, 0,          640.843148],
     [0,          1178.31835, 326.344961],
     [0,          0,            1]]

D = [[-0.17795253],
     [ 0.58241835],
     [-2.13579049],
     [ 2.78588214]]
```

Fisheye RMS was approximately **0.982 pixels**. Current `fisheye_balance` is
**0.9**.

Do not replace the fisheye model with the standard OpenCV distortion model unless
explicitly requested. Do not modify the calibration archive unless explicitly
instructed.

Moving the camera on the rover does not by itself require repeating intrinsic
calibration. Recalibration may be needed if the lens changes, focus changes
significantly, the optical configuration changes, or processing resolution
becomes incompatible with the calibration.

## ArUco

- Dictionary: `cv2.aruco.DICT_5X5_50`.
- Current target marker: ID **23**.
- Physical black-square size: **159 mm x 159 mm**.
- `marker_size`: **0.159 meters**.
- Pose estimation: `cv2.solvePnP` with `cv2.SOLVEPNP_IPPE_SQUARE`.

4x4 ArUco was previously tested but showed occasional misidentification. 5x5
ID 23 proved more stable.

## Verified rover / camera geometry

These are the current authoritative working values:

| Setting | Value |
|---|---:|
| `camera_height` | 1.215 m |
| `camera_forward_offset` | 0.000 m |
| `camera_lateral_offset` | -0.525 m |
| `camera_yaw_deg` | 5.4 degrees |
| `rover_front_offset` | 0.600 m |

Positive lateral means right; negative lateral means left. The camera is
**525 mm left of the rover centerline**, approximately at the geometric center
in the forward/backward direction.

Positive camera yaw means the camera is rotated right. The camera is mounted
left of rover center and aimed slightly right toward the centerline.

The geometric center is **600 mm behind the extreme front edge**. Therefore:

```text
Front Distance = Rover Forward - rover_front_offset
```

Camera pitch was adjusted experimentally. Do not assume or invent a precise
pitch value. The current navigation code does not explicitly use a camera pitch
transform.

Do not change these geometry values without explicit approval or newer
user-approved measurements.

## Verified navigation test

A validated test placed an ArUco marker **3.000 m in front of the extreme FRONT
EDGE of the rover**, approximately on the rover centerline.

A good validated result was:

```text
Front Distance = 3.032 m
ROVER Lateral  = 0.004 m
ROVER Yaw      = -0.3 deg
```

Later tests may differ slightly because the physical centerline had not yet been
aligned with a laser level. Do not change camera geometry automatically based on
a single lateral measurement. Precise centerline calibration using a laser level
is planned.

## Coordinate conventions

Camera and rover lateral use **positive = right** and **negative = left**.

| Output | Reference |
|---|---|
| ROVER Forward | Forward distance from rover geometric center toward the marker. |
| Front Distance | Forward distance from the extreme front edge toward the marker. |
| ROVER Lateral | Marker lateral displacement relative to the rover centerline. |
| ROVER Yaw | Marker orientation relative to the rover coordinate frame. |

The existing code transforms camera-frame floor coordinates into rover-frame
coordinates using the camera offsets and camera yaw offset. Preserve the
validated navigation math and these sign conventions.

## Current application outputs

The live application displays:

- Front Distance.
- ROVER Forward.
- ROVER Lateral.
- ROVER Yaw.
- Camera Forward.
- Camera Lateral.
- Camera Yaw.
- Ground.
- Distance.
- Z.
- Marker width.

Keep these outputs unless explicitly requested otherwise. The application also
displays detected marker IDs and pose axes, and shows `NO MARKER` when no target
marker is detected. Exit with `q` or Ctrl+C.

## Filtering

The system uses rolling median filtering. Current history lengths are:

| Measurement | Samples |
|---|---:|
| Z, distance, ground distance | 30 |
| Camera / rover forward, front distance | 30 |
| Camera / rover lateral | 20 |
| Camera / rover yaw | 15 |

All measurement histories reset when the current Z differs from the previous
Z-history median by more than **0.20 m**.

Do not remove or redesign filtering without explicit approval.

## Long-range limitation

At approximately **4 meters and beyond**, the 159 mm marker becomes very small.
Observed marker width was around **11-14 pixels** at long range.

Effects include detection flicker, temporary `NO MARKER` states, sensitivity to
vibration, and less stable pose estimation. This is considered primarily a
resolution / marker-size / vibration limitation rather than a confirmed
navigation math bug.

Possible future improvements:

- 1920x1080 processing.
- Larger markers.
- Vibration isolation.
- Confidence / quality filtering.
- Multi-marker tracking.
- Short-term pose hold during temporary marker loss.

Do not implement these automatically.

## Vibration mounting

Camera vibration isolation is being considered. The preferred approach is a
stiff camera bracket with small-travel, relatively firm rubber/silicone
isolators. Avoid a very soft floating mount because camera-to-rover extrinsics
must remain mechanically stable.

## Project configuration architecture

`rover_config.json` is the source of configurable rover/camera parameters.
`aruco_live.py` loads these values instead of duplicating rover-specific
constants.

Current configuration:

```json
{
  "camera_height": 1.215,
  "camera_forward_offset": 0.0,
  "camera_lateral_offset": -0.525,
  "camera_yaw_deg": 5.4,
  "rover_front_offset": 0.600,
  "marker_size": 0.159,
  "target_id": 23,
  "resolution_width": 1280,
  "resolution_height": 720,
  "fisheye_balance": 0.9,
  "log_measurements": false,
  "log_file": "measurements.csv"
}
```

Configuration and calibration paths use `Path(__file__).resolve().parent`.
No rover-specific absolute calibration path is required.

The configuration loader validates required settings, numeric types and finite
values, positive camera height / marker size / resolution, integer marker ID
and dimensions, marker ID range 0-49, and fisheye balance range 0-1.
Invalid or missing configuration prints an error to stderr and exits with
status 1 before camera initialization.

Logging settings are optional for older configuration files: omitted settings
default to `false` and `measurements.csv`. `log_measurements` must be a boolean;
`log_file` must be a nonempty filename in the project directory.

## Marker map architecture

The repository now contains `marker_map.json`, which has been created, committed,
and pushed. Its purpose is to provide a scalable warehouse marker map for future
global and simultaneous multi-marker localization, supporting hundreds of markers.

Current structure:

- `version`: 1.
- `frame`: `warehouse`.
- `units`: `m` (meters).

ArUco map configuration:

- Planned dictionary: `DICT_5X5_1000`.
- Default physical marker size: **0.159 m**.
- Supported future marker IDs: **0-999**.

**The live application has NOT yet been switched to `DICT_5X5_1000`.**
`aruco_live.py` still uses the validated `DICT_5X5_50` dictionary and target
ID **23**. Changing the live detector is a separate tested change during
**Sprint 02 — Scalable Markers**.

Markers are stored as direct lookup keys under `markers`, using string IDs:

```json
{
  "markers": {
    "23": {
      "x": 0.0,
      "y": 0.0,
      "yaw_deg": 0.0,
      "enabled": true
    }
  }
}
```

An individual marker may optionally override the default physical size with
`"size": <meters>`. The `enabled` boolean allows a marker to remain in the map
while temporarily excluded from future localization.

Coordinate convention:

- X+ = forward.
- Y+ = right.
- Y- = left.
- yaw+ = clockwise/right.
- yaw- = counterclockwise/left.

Marker **23** currently acts only as a temporary map origin/reference. Its
**X = 0, Y = 0, Yaw = 0** must NOT be treated as final surveyed warehouse
coordinates. Actual warehouse coordinates will be measured later.

Future localization behavior:

- Detect all visible known markers.
- Look up each marker in `marker_map.json`.
- Calculate an independent Global X / Y / Yaw rover estimate from each marker.
- Combine compatible estimates using multi-marker fusion.
- Reject or reduce the weight of poor observations.
- Calculate Localization Confidence.

Possible future localization quality inputs include:

- Number of valid visible markers.
- Marker pixel width.
- Distance.
- Pose stability.
- Reprojection quality.
- Agreement between marker-derived rover poses.

Localization Confidence will later support rover speed limiting using the
levels **HIGH**, **MEDIUM**, **LOW**, and **LOST**. Detection migration, global
localization, fusion, confidence calculation, and confidence-based speed limiting
are future work. **Do not implement this logic now.**

Current roadmap state:

| Sprint | Scope | State |
|---|---|---|
| Sprint 01 — Geometric Calibration | Validate current geometry and calibration. | Current active sprint. |
| Sprint 02 — Scalable Markers | Test `DICT_5X5_1000` and multiple IDs; remove the single `TARGET_ID` dependency. | Future tested change. |
| Sprint 03 — Global Localization | Use `marker_map.json` to calculate Global X / Y / Yaw. | Future work. |
| Sprint 04 — Multi-Marker Confidence | Implement multi-marker fusion and Localization Confidence. | Future work. |
| Sprint 05 — Pixhawk / MAVLink | Send validated external localization to Pixhawk 6C and validate EKF integration. | Future work. |
| Sprint 06 — Safety and Autonomy | Use localization quality for speed limits, fail-safe behavior, safety inputs, and first controlled autonomous routes. | Future work. |
| Sprint 07 — Integrated Validation | Repeated warehouse testing, logging, loaded tests, and integrated validation. | Future work. |
| Sprint 08 — Lift Platform | 4-point synchronized screw lift platform with movement interlocks and safety validation. | Future work. |

The map is not yet used by `aruco_live.py`; surveying real marker coordinates and
connecting validated observations to the map remain future work during Sprint 03.

This architecture documentation does not authorize changes to `aruco_live.py`,
`rover_config.json`, `marker_map.json`, camera calibration, or navigation math.
Runtime integration requires a separate explicit request and testing.

## CSV measurement logging

Optional CSV measurement logging is implemented and disabled by default:
`log_measurements = false`. The typical file is `measurements.csv`, which is
ignored by Git.

When enabled, the application creates or appends to the CSV in the project
directory, writes the header for new or empty files, and records only target
marker detections with successful pose estimation. No rows are written for
non-target markers, failed pose estimation, or frames showing `NO MARKER`.
Disabled logging does not create or write a CSV file.

Columns, in order:

```text
timestamp
marker_id
front_distance
rover_forward
rover_lateral
rover_yaw
camera_forward
camera_lateral
camera_yaw
ground_distance
distance
z
marker_width_px
```

Measurements use the existing filtered values shown on screen; marker width
is the current pixel measurement. Distances are in meters, yaw is in degrees,
and marker width is in pixels. Timestamps use ISO date/time with milliseconds
and the Raspberry Pi's local timezone offset. The header and every row are
flushed, and the log file is closed during application cleanup.

Logging supports calibration, stability analysis, comparing mounting solutions,
evaluating vibration, and measuring error versus distance. It does not change
the navigation math, filters, pose estimation, or UI.

## Repository

- Repository: `ugv_optical_navigation`.
- Branch: `main`.
- GitHub remote: `git@github.com:nikitaolefear/ugv_optical_navigation.git` (SSH).
- The Raspberry Pi has its own GitHub SSH key, according to project context.

Root project files:

| File | Purpose |
|---|---|
| `aruco_live.py` | Live camera capture, localization, display, and optional CSV logging. |
| `rover_config.json` | Configurable rover/camera parameters and logging settings. |
| `camera_calibration_fisheye.npz` | Intrinsic camera matrix and fisheye coefficients. |
| `.gitignore` | Excludes generated files and `measurements.csv`. |
| `PROJECT_CONTEXT.md` | Persistent technical context for future work. |

`measurements.csv` may exist locally as an ignored testing artifact. Do not
include measurement CSV logs in Git; the current ignore entry names
`measurements.csv` specifically, so check any differently named logs before
staging files.

Normal workflow, after hardware validation:

```bash
git status
git add <intended-files>
git commit -m "<description>"
git push
```

## Development environment

Development workflow:

```text
Windows laptop -> VS Code -> Remote SSH -> Raspberry Pi 5 -> Git -> GitHub
```

Codex is used inside VS Code for code changes. ChatGPT is used for architecture,
test analysis, planning, generating Codex prompts, and reviewing changes.

For substantial code changes:

1. Inspect the repository first.
2. Preserve verified behavior.
3. Explain the proposed change.
4. Identify files that will change.
5. Make incremental changes.
6. Avoid unnecessary rewrites.
7. Preserve coordinate conventions.
8. Preserve calibrated geometry.
9. Do not modify calibration data unless explicitly requested.
10. Run appropriate syntax/static checks.
11. Perform real hardware validation before considering navigation changes stable.
12. Commit only after the hardware test passes.

Prefer showing a diff before a substantial change. Do not start the camera
application for a syntax-only check. The Python syntax check is:

```bash
python3 -m py_compile aruco_live.py
```

## Next planned work

Near-term:

1. Precisely align the rover centerline with a laser level.
2. Revalidate ROVER Lateral and `camera_yaw_deg`.
3. Record measurements at multiple known distances using CSV logging.
4. Determine the practical stable operating range.
5. Analyze error versus marker pixel width.
6. Improve ArUco robustness without changing verified navigation math.

After that:

7. During Sprint 02, test DICT_5X5_1000 and support multiple known ArUco IDs.
8. During Sprint 03, survey real marker coordinates and populate marker_map.json.
9. During Sprint 03, convert validated marker observations into Global rover X / Y / Yaw.
10. Validate global localization.
11. During Sprint 04, implement multi-marker fusion and Localization Confidence.
12. During Sprint 05, integrate validated external localization with Pixhawk 6C over MAVLink.

Do not jump ahead to later stages without an explicit request.

## Rules for future Codex work

Treat this document as authoritative project context unless the user explicitly
provides newer measurements or requirements. If repository code conflicts with
it, report the conflict rather than silently choosing or changing values.

Before changing navigation math:

- Explain why.
- Show the expected effect.
- Obtain explicit approval.

Never silently change coordinate signs, camera geometry, marker physical size,
the calibration model, or rover reference points.

Prefer small, testable changes. Preserve working filtering, pose estimation,
display outputs, and configuration behavior unless explicitly asked to change
them. Do not jump ahead to Pixhawk or motor control until optical localization
is validated and integration is explicitly requested.
