"""Review the archived startup order with TX off, or display live readback via owner GET."""

import json
import math
from statistics import median
from urllib.request import ProxyHandler, build_opener

from .externalnav_bench_gate import bench_preview
from .static_externalnav import finite

EXPECTED_PIXHAWK = 'usb-Holybro_Pixhawk6C_DEVICE_ID-if00'
SOURCE_SET_COMMAND = 42007
HISTORICAL_TEST_ORIGIN = (50, 30, 0)
ORIGIN_FIELDS = ('latitude_deg', 'longitude_deg', 'altitude_msl_m')


def bootstrap_alignment(pairs):
    """Assess static HOME geometry and yaw before any EKF position exists."""
    result = {'alignment': 'BLOCKED', 'ekf_fusion': 'UNVERIFIED', 'pairs': len(pairs)}
    if (len(pairs) < 5 or pairs[-1]['frame_at'] - pairs[0]['frame_at'] < 1 or
            len({p['frame_at'] for p in pairs}) != len(pairs) or
            len({p['attitude_at'] for p in pairs}) != len(pairs)):
        return dict(result, reason='INSUFFICIENT_DISTINCT_PAIRED_OBSERVATIONS')
    if any(abs(p['frame_at'] - p['attitude_at']) > .1 for p in pairs):
        return dict(result, reason='CAPTURE_AND_ATTITUDE_TIMES_TOO_FAR_APART')
    if any(math.hypot(p['north_m'], p['east_m']) > .15 for p in pairs):
        return dict(result, reason='OPTICAL_HOME_OUTSIDE_TOLERANCE')
    n = median(p['north_m'] for p in pairs)
    e = median(p['east_m'] for p in pairs)
    if any(math.hypot(p['north_m'] - n, p['east_m'] - e) > .025 for p in pairs):
        return dict(result, reason='OPTICAL_HOME_NOT_STABLE')
    offsets = [(p['attitude_yaw_deg'] - p['optical_yaw_deg'] + 180) % 360 - 180 for p in pairs]
    correction = median(offsets)
    if any(abs(v) > 5 or abs(v - correction) > 1 for v in offsets):
        return dict(result, reason='YAW_COMPARISON_INCONSISTENT')
    return dict(result, alignment='BOOTSTRAP_ALIGNMENT_CANDIDATE',
                optical_home_ned_m=[n, e], observed_yaw_offset_deg=correction)


def collect_bootstrap(optical):
    """Collect read-only evidence from the active owner. Never issue a grant."""
    from hashlib import sha256
    from pathlib import Path
    import time

    from pose_transform_2d import camera_to_rover_pose
    from pose_transform_3d import compose_transform, invert_transform, marker_map_entry_to_world_transform

    opener = build_opener(ProxyHandler({}))

    def get(port, endpoint):
        with opener.open(f'http://127.0.0.1:{port}/api/{endpoint}', timeout=2) as response:
            return json.load(response)

    files = ('marker_map.json', 'camera_calibration_fisheye.npz', 'rover_config.json', 'pose_transform_2d.py')

    def hashes():
        return {name: sha256((optical / name).read_bytes()).hexdigest() for name in files}

    boot_path = Path('/proc/sys/kernel/random/boot_id')
    boot_id, before = boot_path.read_text().strip(), hashes()
    canonical = Path(__file__).with_name('nav_bench') / 'marker_map.json'
    if before['marker_map.json'] != sha256(canonical.read_bytes()).hexdigest():
        return {'alignment': 'BLOCKED', 'reason': 'OPTICAL_MAP_HASH_MISMATCH'}
    markers = {row['id']: row for row in json.loads(canonical.read_text())['markers']}
    pairs = []
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        nav = get(8084, 'externalnav')
        diag = get(8084, 'owner-diagnostics')
        frame, att = nav.get('optical_measurement') or {}, diag.get('attitude') or {}
        f, a = frame.get('acquired_at'), att.get('received_monotonic_s')
        marker = frame.get('source_marker')
        if nav.get('tx_enabled') is not False or nav.get('sent') != 0:
            return {'alignment': 'BLOCKED', 'reason': 'TX_NOT_OFF'}
        controller_ms = att.get('time_boot_ms')
        if (frame.get('fresh') is True and att.get('fresh') is True and att.get('source') == [1, 1]
                and marker in markers and all(map(finite, (f, a, att.get('yaw_deg'))))
                and type(controller_ms) is int
                and abs(f - a) <= .1
                and (not pairs or (f > pairs[-1]['frame_at'] and a > pairs[-1]['attitude_at']))):
            if pairs and controller_ms <= pairs[-1]['pixhawk_time_boot_ms']:
                return {'alignment': 'BLOCKED', 'reason': 'PIXHAWK_ATTITUDE_CLOCK_RESET'}
            camera = compose_transform(marker_map_entry_to_world_transform(markers[marker]),
                                       invert_transform(frame['T_Coptical_M']))
            x, y, yaw = camera_to_rover_pose(camera[0, 3], camera[1, 3],
                                            math.degrees(math.atan2(camera[1, 2], camera[0, 2])))
            pairs.append(dict(frame_at=f, attitude_at=a, pixhawk_time_boot_ms=controller_ms,
                              north_m=float(x), east_m=float(-y),
                              optical_yaw_deg=-yaw, attitude_yaw_deg=att['yaw_deg'], marker=marker))
        time.sleep(.05)
    t, stop = get(8082, 'telemetry'), get(8084, 'production-stop')
    if (t.get('armed') != {'state': 'FRESH', 'value': 'DISARMED'} or t.get('connection') != 'CONNECTED'
            or stop.get('stop_latch') is not True or 'BRAKE_HOLD_ONLY' not in stop.get('blocking_reasons', [])
            or not Path('/run/ugv-disarm-policy/inhibit').is_file()
            or boot_path.read_text().strip() != boot_id or hashes() != before):
        return {'alignment': 'BLOCKED', 'reason': 'OWNER_SAFETY_OR_OPTICAL_ASSETS_CHANGED'}
    return dict(bootstrap_alignment(pairs), pi_boot_id=boot_id, sha256=before,
                observed_markers=sorted({p['marker'] for p in pairs}), samples=pairs)


def origin_diagnosis(origin, parameters):
    """Keep recorded AHRS coordinates separate from the live EKF origin."""
    options = parameters.get('AHRS_OPTIONS')
    recorded = [parameters.get(f'AHRS_ORIGIN_{axis}') for axis in ('LAT', 'LON', 'ALT')]
    if origin is not None:
        state = 'LIVE_ORIGIN_REPORTED'
    elif not finite(options) or not all(map(finite, recorded)):
        state = 'ORIGIN_READBACK_INCOMPLETE'
    elif not int(options) & 16 or not any(recorded):
        state = 'RECORDED_ORIGIN_RESTORE_UNAVAILABLE'
    elif parameters.get('EK3_SRC1_POSXY') == 3:
        state = 'LIVE_ORIGIN_UNCONFIRMED_GPS_SOURCE_MAY_PREVENT_RESTORE'
    else:
        state = 'LIVE_ORIGIN_UNCONFIRMED'
    return {
        'state': state,
        'recorded_ahrs_origin': recorded if all(map(finite, recorded)) else None,
        'historical_test_origin_recorded': tuple(recorded) == HISTORICAL_TEST_ORIGIN,
    }


def static_origin_plan(config, live_origin):
    """Resolve origin admission before changing source set, without writing anything."""
    surveyed = config.get('expected_ekf_origin')
    coordinates = tuple(surveyed.get(key) for key in ORIGIN_FIELDS) if isinstance(surveyed, dict) else ()
    if (not coordinates or not all(map(finite, coordinates)) or
            not -90 <= coordinates[0] <= 90 or not -180 <= coordinates[1] <= 180 or
            not any(coordinates[:2]) or coordinates == HISTORICAL_TEST_ORIGIN):
        return {'state': 'BLOCKED', 'reason': 'SURVEYED_ORIGIN_REQUIRED'}
    expected = dict(zip(ORIGIN_FIELDS, coordinates))
    if live_origin is None:
        return {'state': 'ORIGIN_APPROVAL_REQUIRED', 'surveyed_origin': expected}
    if not isinstance(live_origin, dict) or any(live_origin.get(k) != v for k, v in expected.items()):
        return {'state': 'BLOCKED', 'reason': 'LIVE_ORIGIN_MISMATCH'}
    return {'state': 'ORIGIN_CONFIRMED'}


class StaticRecovery:
    """Offline sequence decisions. Instantiate again only for an explicit new test."""
    def __init__(self):
        self.first_pose_at = self.last_pose_at = None
        self.src2_observed = False
        self.hold_latched = False
        self.source_request_at = None

    def source_request(self, draft, config, sample, owner, *, now):
        """Prepare a one-shot intent after rechecking the live bench evidence."""
        if self.source_request_at is not None:
            return {'state': 'BLOCKED', 'reasons': ['SRC2_ALREADY_REQUESTED']}
        origin_plan = static_origin_plan(config, owner.get('origin'))
        if origin_plan['state'] != 'ORIGIN_CONFIRMED':
            return origin_plan
        decision = self.update(draft, config, sample, owner, now=now)
        if decision['state'] != 'SRC2_APPROVAL_REQUIRED':
            return decision
        if owner.get('src2_approved') is not True:
            return decision
        self.source_request_at = now
        return {'state': 'SRC2_INTENT', 'target': (1, 1),
                'command': SOURCE_SET_COMMAND, 'param1': 2, 'tx_enabled': False}

    def source_result(self, owner, ack, *, now):
        """ACK and independent post-command estimator evidence are both required."""
        sent_at = self.source_request_at
        if sent_at is None or not 0 <= now - sent_at <= 3:
            return 'SRC2_CONFIRMATION_UNAVAILABLE'
        ack_at = ack.get('received_at')
        if (ack.get('source') != [1, 1] or ack.get('command') != SOURCE_SET_COMMAND or
                not finite(ack_at) or not sent_at < ack_at <= now):
            return 'WAIT_SRC2_ACK'
        if ack.get('result') != 0:
            return 'SRC2_REJECTED'
        if (owner.get('active_source') != 2 or not finite(owner.get('source_observed_at')) or
                not sent_at < owner['source_observed_at'] <= now):
            return 'WAIT_ACTIVE_SRC2'
        flags, stamp = owner.get('ekf_flags'), owner.get('ekf_observed_at')
        if (type(flags) is not int or not flags & 16 or flags & (128 | 1024) or
                not finite(stamp) or not sent_at < stamp <= now or now - stamp >= .5):
            return 'WAIT_EKF_XY'
        return 'SRC2_CONFIRMED'

    def update(self, draft, config, sample, owner, *, now):
        if self.hold_latched:
            return {'state': 'HOLD_REQUIRED', 'tx_enabled': False}
        if owner.get('pixhawk_by_id') != EXPECTED_PIXHAWK or owner.get('rover_identity_confirmed') is not True:
            return {'state': 'BLOCKED', 'tx_enabled': False, 'reasons': ['HARDWARE_IDENTITY_UNCONFIRMED']}
        source = owner.get('active_source')
        preview = bench_preview(draft, config, sample, owner, now=now,
                                required_source=2 if source == 2 else 1)
        if preview['state'] == 'BLOCKED':
            if self.src2_observed and self.last_pose_at is not None and now - self.last_pose_at >= .7:
                self.hold_latched = True
                return {'state': 'HOLD_REQUIRED', 'tx_enabled': False, 'reasons': preview['reasons']}
            self.first_pose_at = None
            return preview
        stamp = sample['acquired_at']
        if self.last_pose_at is None or stamp > self.last_pose_at:
            if self.first_pose_at is None or stamp - self.last_pose_at > .3:
                self.first_pose_at = stamp
            self.last_pose_at = stamp
        if source == 2:
            self.src2_observed = True
            return dict(preview, state='STATIC_EKF_OBSERVED' if owner.get('ekf_xy_valid') is True
                        and owner.get('vision_healthy') is True else 'WAIT_EKF_XY')
        ready = (self.first_pose_at is not None and self.last_pose_at - self.first_pose_at >= 2
                 and owner.get('vision_healthy') is True)
        return dict(preview, state='SRC2_APPROVAL_REQUIRED' if ready else 'WAIT_STABLE_VISION')


def readback():
    opener = build_opener(ProxyHandler({}))

    def get(port, path):
        with opener.open(f'http://127.0.0.1:{port}/api/{path}', timeout=2) as response:
            return json.load(response)

    diagnostics = get(8084, 'owner-diagnostics')
    nav = get(8084, 'externalnav')
    telemetry = get(8082, 'telemetry')
    params = diagnostics['parameters']
    keys = ('AHRS_OPTIONS', 'AHRS_ORIGIN_LAT', 'AHRS_ORIGIN_LON', 'AHRS_ORIGIN_ALT',
            'EK3_SRC1_POSXY', 'EK3_SRC2_POSXY', 'EK3_SRC2_YAW')
    values = {name: params.get(name, {}).get('value', 'UNKNOWN') for name in keys}
    origin = diagnostics.get('ekf_origin')
    events = [row['text'] for row in diagnostics.get('statustext', ()) if 'source set' in row['text'].lower()]
    return dict(connection=telemetry['connection'], armed=telemetry['armed'],
                origin=origin, src2_events=events[-3:], parameters=values,
                origin_diagnosis=origin_diagnosis(origin, values),
                ekf=telemetry['ekf'], marker=nav['marker'], pose_fresh=nav['pose_fresh'],
                tx_enabled=nav['tx_enabled'], sent=nav['sent'])


if __name__ == '__main__':
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--origin-plan', type=Path, help='Validate the static profile without owner or MAVLink access')
    modes.add_argument('--bootstrap-optical-dir', type=Path, help='Collect static HOME evidence with TX off')
    args = parser.parse_args()
    if args.bootstrap_optical_dir:
        result = collect_bootstrap(args.bootstrap_optical_dir)
        print(json.dumps(result, indent=2))
        raise SystemExit(2 if result['alignment'] == 'BLOCKED' else 0)
    if args.origin_plan:
        plan = static_origin_plan(json.loads(args.origin_plan.read_text()), None)
        print(json.dumps(plan, indent=2))
        raise SystemExit(2 if plan['state'] == 'BLOCKED' else 0)
    print(json.dumps(readback(), indent=2))
