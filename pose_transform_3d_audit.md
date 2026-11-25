# V8 coordinate audit and shadow contract

Audited sources: `aruco_active_marker_selector_v7.py`, `rover_config.json`,
`marker_map.json`, and the K/D calibration loader. No camera was opened.

## Frames and solvePnP

The marker origin is its square center. Object corners in detector order are
`(-h,+h,0), (+h,+h,0), (+h,-h,0), (-h,-h,0)`, where h=0.283/2 m.
Thus the canonical marker image has +X toward its right edge and +Y toward
its top edge. +Z completes the right-handed object frame, out of the printed
face. Physical floor/wall mounting orientation is not established by this ordering.

The code calls solvePnP with SOLVEPNP_IPPE_SQUARE, remapped image corners,
new_K and zero distortion. Its Rodrigues rvec and metric tvec express
`p_camera = R_camera_marker @ p_marker + t_camera_marker`.
`tvec` is the marker center in optical camera coordinates, not camera position.
Camera optical axes are +X image right, +Y image down, +Z optical forward.

V7 uses a planar rover representation: forward longitudinal, lateral positive
right, yaw positive right. Camera offsets are relative to the rover geometric
center; front distance is relative to its front edge. No full rover 3D axis basis
or optical-to-body rotation is defined by that planar representation.

WORLD/HOME_FRONT_DATUM and ROVER now explicitly use a right-handed FLU basis:
+X forward, +Y left, +Z up. Both origins are the floor-projected FRONT DATUM
reference; at HOME T_world_rover=identity. Marker map position.y means LEFT,
position.z means UP; x remains forward. Current y=0 markers require no map edit.
OpenCV camera and marker frames remain right-handed and unchanged.

For a common referenced lateral quantity:
`legacy_rover_lateral_positive_right = -world_y` and
`world_y = -legacy_rover_lateral_positive_right`.
The library provides explicit conversion helpers; V7 authoritative output is
not passed through them. A sign change alone does not convert V7's relative
marker lateral measurement into an absolute world rover position. World Euler
yaw also differs from the legacy relative marker heading construction.

## Exact existing authoritative equations

For optical tvec=(x,y,z), H=camera_height and psi=camera_yaw_deg:

- distance = sqrt(x*x+y*y+z*z)
- ground_distance = sqrt(max(0, distance*distance-H*H))
- camera_lateral = x
- camera_forward = sqrt(max(0, ground_distance*ground_distance-x*x))
- rover_forward = forward_offset + cos(psi)*camera_forward - sin(psi)*x
- rover_lateral = lateral_offset + sin(psi)*camera_forward + cos(psi)*x
- front_distance = rover_forward - rover_front_offset
- global_x = supplying_marker.position.x - front_distance

Angles in the trigonometric equations use radians. The positive square root
loses longitudinal sign; this is the preserved planar calculation, not the new
rigid-transform solution.

For R=Rodrigues(rvec), yaw_raw=degrees(atan2(R[0,2],R[2,2])). camera_yaw is
yaw_raw-180 if yaw_raw>=0, otherwise yaw_raw+180. rover_yaw=camera_yaw+5.4,
wrapped by one +/-360 correction into [-180,180]. V8 does not change this.

## Known and missing extrinsics

Configured values (not independently recalibrated by this task):
camera height above floor 1.215 m; camera forward offset 0.0 m and rightward
lateral offset -0.525 m relative to rover geometric center; planar camera yaw
relative to rover +5.4 degrees; center-to-front offset 0.600 m.
K and D describe camera intrinsics/distortion; they contain no mounting pose.

The configured camera_lateral_offset is explicitly legacy positive-right:
-0.525 m means the camera is LEFT of the rover center. Its FLU equivalent is
camera_y= -(-0.525) = +0.525 m. The probe prints both the original value and
this sign-converted equivalent without changing config. The planar +5.4 degree
rightward mounting yaw would have a negative sign as an FLU planar heading;
it is not a complete optical-camera Euler mounting rotation.

The frame basis and floor-projected front origin are now specified. From the
configured center offsets, the camera longitudinal reference relative to the
front datum is 0.0-0.600=-0.600 m; configured height supplies +1.215 m above
its floor projection, and lateral sign conversion supplies +0.525 m. These
are configured physical references, not independently measured extrinsics.
Missing rotation calibration: actual optical-to-FLU mounting pitch/roll and
confirmation of yaw in that full optical-axis basis. A level forward-looking
optical-to-FLU basis would have columns (0,-1,0), (0,0,-1), (1,0,0), determinant
+1. That basis is synthetic test geometry ONLY; no level mounting is assumed.

Physical marker-frame orientations must be validated before running the probe
as a trusted absolute extrinsic calibration. Zero map Euler angles assert marker
+x=world forward, +y=world left, +z=world up. Floor placement alone does not
establish printed-corner alignment; wall placement does not establish it either.
Without validation, absolute camera-to-rover roll/pitch/yaw CANNOT be trusted:
incorrect marker orientation directly biases the recovered camera orientation
and can also bias translation. A diagnostic-only run could expose inconsistency,
but cannot certify calibration. No marker orientation is changed or inferred.

Accordingly T_CAMERA_ROVER=None; no full camera/rover rigid transform is
fabricated. Future calibration must provide T_camera_rover explicitly. Then
T_world_rover=T_world_camera @ T_camera_rover, with the desired rover/front
reference and legacy comparison conventions explicitly resolved.

## Shadow interpretation

T_A_B is pose of B in A; p_A=T_A_B @ p_B. Map Euler construction is explicitly
R_world_marker=Rz(yaw) @ Ry(pitch) @ Rx(roll), in degrees. Inverse Euler uses
pitch in [-90,90]; at gimbal lock roll is fixed to zero. Orientations are
nonunique; tests compare reconstructed rotations.

T_world_camera=T_world_marker @ inverse(T_camera_marker), using the actual
solvePnP observation for the marker supplying V7's pose on this frame.
No placement-dependent correction is applied. The parser accepts floor/wall;
placement is metadata. Current zero floor orientations are not physically
validated, so camera candidates may be physically inaccurate despite valid
rigid-transform mathematics.

shadow_3d_valid=1 means a mathematically valid camera transform was computed;
it does not certify calibrated world localization or a valid rover pose.
Rover pose and shadow-minus-V7 fields stay empty because mounting extrinsics
and comparison conventions are incomplete. Missing/invalid observations do
not reuse old poses. Shadow failures do not change the authoritative pose.
FULL_3D_AUTHORITATIVE=False, with a startup guard rejecting True.

V7 authoritative pose, camera setup, detector, switching, fallback, thresholds,
metadata, row-per-frame logging and summary are preserved. No camera/dynamic
test was performed. Synthetic vertical-marker recovery tests algebraic
frame composition only, not real mounting calibration.
