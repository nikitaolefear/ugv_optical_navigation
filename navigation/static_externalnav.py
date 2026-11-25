"""Prepare pose-only NED fields for static ExternalNav review. No MAVLink I/O."""

import math


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def preview_pose(config, sample, *, now, origin, parameters, active_source, required_source=2):
    if (config['mode'], config['tx_enabled'], config['message']) != (
            'OFFLINE_PREVIEW', False, 'VISION_POSITION_ESTIMATE'):
        raise ValueError('Static preview must use pose-only fields with TX disabled')

    blockers = []
    expected_origin = config['expected_ekf_origin']
    offset = config['map_origin_ned_m']
    azimuth = config['map_x_azimuth_deg']
    origin_keys = ('latitude_deg', 'longitude_deg', 'altitude_msl_m')
    if (not isinstance(expected_origin, dict) or
            not all(finite(expected_origin.get(key)) for key in origin_keys) or
            not -90 <= expected_origin['latitude_deg'] <= 90 or
            not -180 <= expected_origin['longitude_deg'] <= 180 or
            not isinstance(origin, dict) or
            any(origin.get(key) != expected_origin[key] for key in origin_keys)):
        blockers.append('SURVEYED_ORIGIN_NOT_CONFIRMED')
    if not isinstance(offset, list) or len(offset) != 3 or not all(map(finite, offset)) or not finite(azimuth):
        blockers.append('MAP_ALIGNMENT_NOT_MEASURED')
    if required_source not in (1, 2) or active_source != required_source or any(parameters.get(key) != value for key, value in config['required_parameters'].items()):
        blockers.append('EKF_SOURCE_OR_VISO_PROFILE_UNVERIFIED')

    stamp = sample.get('acquired_at')
    coords = [sample.get(key) for key in ('x_m', 'y_m', 'z_m', 'roll_rad', 'pitch_rad', 'yaw_rad')]
    if (sample.get('map_frame') != config['map_frame'] or
            sample.get('frame') != config['input_frame'] or
            sample.get('clock') != config['timestamp_clock'] or
            sample.get('marker') not in config['marker_ids'] or
            not finite(stamp) or not finite(now) or not 0 < stamp <= now or
            now - stamp > config['max_pose_age_s'] or not all(map(finite, coords))):
        blockers.append('POSE_OR_CAPTURE_TIME_INVALID')
    if blockers:
        return {'state': 'BLOCKED', 'reasons': blockers}

    x, y, z, roll, pitch, yaw = coords
    heading = math.radians(azimuth)
    c, s = math.cos(heading), math.sin(heading)
    return {
        'state': 'PREVIEW_ONLY', 'frame': config['output_frame'],
        'usec': int(stamp * 1e6),
        'x': offset[0] + c * x - s * y,
        'y': offset[1] + s * x + c * y,
        'z': offset[2] + z,
        'roll': roll, 'pitch': pitch, 'yaw': (heading + yaw + math.pi) % math.tau - math.pi,
    }
