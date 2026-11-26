"""Local A→B→A mission preview and a latched navigation-loss decision."""
import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def fresh(age, limit):
    return finite(age) and 0 <= age <= limit


def tracking_reasons(nav, telemetry, config):
    """Evaluate read-only owner snapshots on each poll; never emit commands."""
    reasons = []
    if (nav.get('camera_thread_alive') is not True or nav.get('fault') is not False or
            nav.get('pose_fresh') is not True or nav.get('marker') not in config['marker_ids'] or
            not fresh(nav.get('pose_age_s'), config['optical_max_age_s'])):
        reasons.append('OPTICAL_POSE_UNAVAILABLE')
    if nav.get('alignment_verified') is not True:
        reasons.append('ALIGNMENT_UNVERIFIED')
    if nav.get('tx_enabled') is not True:
        reasons.append('EXTERNALNAV_OBSERVE_ONLY')

    ekf = telemetry.get('ekf', {}).get('EKF_STATUS_REPORT', {})
    message = telemetry.get('messages', {}).get('EKF_STATUS_REPORT', {})
    flags = (ekf.get('value') or {}).get('flags')
    # MAVLink EKF_STATUS_FLAGS: absolute horizontal position is bit 16.
    # Constant-position mode (128) and uninitialized (1024) are unusable.
    if (telemetry.get('connection') != 'CONNECTED' or ekf.get('state') != 'FRESH' or
            not fresh(message.get('age_s'), config['ekf_max_age_s']) or type(flags) is not int or
            flags < 0 or not flags & 16 or flags & (128 | 1024)):
        reasons.append('EKF_HORIZONTAL_POSITION_UNAVAILABLE')
    return reasons


@dataclass
class NavigationLossGuard:
    """A preview run stays in HOLD after loss, even if telemetry recovers."""
    hold_reasons: tuple[str, ...] = ()

    def update(self, nav, telemetry, config):
        if not self.hold_reasons:
            self.hold_reasons = tuple(tracking_reasons(nav, telemetry, config))
        return 'HOLD' if self.hold_reasons else 'TRACKING'


def build_preview(config, marker_map):
    marker_ids = config['marker_ids']
    if (config['coordinate_frame'] != 'HOME_FRONT_DATUM' or
            marker_map['coordinate_frame'] != config['coordinate_frame'] or
            marker_map['position_unit'] != 'm' or marker_ids != [23, 24, 25] or
            sorted(marker['id'] for marker in marker_map['markers']) != marker_ids):
        raise ValueError('Expected the calibrated HOME_FRONT_DATUM map for IDs 23/24/25')
    cycles, dwell = config['cycles'], config['stop_at_endpoint_s']
    if type(cycles) is not int or cycles < 1 or type(dwell) is not int or not 1 <= dwell <= 32767:
        raise ValueError('Positive integer cycles and a waypoint dwell of 1–32767 s are required')
    for key in ('optical_max_age_s', 'ekf_max_age_s'):
        if not finite(config[key]) or config[key] <= 0:
            raise ValueError(f'Invalid {key}')

    endpoints = config['endpoints_m']
    missing = [name for name in ('A', 'B') if endpoints[name] is None]
    if missing:
        return {'state': 'BLOCKED', 'cycles': cycles,
                'missing_survey_endpoints': missing, 'legs': []}
    for name in ('A', 'B'):
        point = endpoints[name]
        if type(point) is not list or len(point) != 2 or not all(finite(value) for value in point):
            raise ValueError(f'{name} must be a surveyed [x, y] in metres')
    a, b = endpoints['A'], endpoints['B']
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == dy == 0:
        raise ValueError('A and B must be distinct')

    legs = []
    for cycle in range(1, cycles + 1):
        for start, end, reverse in (('A', 'B', 0), ('B', 'A', 1)):
            legs.append({'cycle': cycle, 'from': start, 'to': end,
                         'target_xy_m': list(endpoints[end]),
                         'DO_SET_REVERSE_param1': reverse, 'NAV_WAYPOINT_param1_s': dwell})
    return {'state': 'PREVIEW_ONLY', 'cycles': cycles, 'coordinate_frame': config['coordinate_frame'],
            'required_start_xy_m': a, 'nominal_body_yaw_flu_deg': math.degrees(math.atan2(dy, dx)),
            'on_navigation_loss': 'LATCH_HOLD', 'on_completion': 'HOLD', 'legs': legs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    marker_map = json.loads((args.config.parent / config['map_file']).read_text())
    preview = build_preview(config, marker_map)
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    return 2 if preview['state'] == 'BLOCKED' else 0


if __name__ == '__main__':
    raise SystemExit(main())
