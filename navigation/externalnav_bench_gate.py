"""Offline review of a surveyed map and same-owner pose-only ExternalNav evidence."""

from hashlib import sha256
import json
import math

from .static_externalnav import finite, preview_pose

STATIONS = {23: 2.5, 24: 5., 25: 7.5}


def candidate_map(draft, *, distance_tolerance_m=.1):
    """Return a version-2 optical map only after all survey fields are supplied."""
    if (draft['coordinate_frame'] != 'HOME_FRONT_DATUM' or draft['route_home_xy_m'] != [0, 0] or
            draft['route_forward_xy'] != [1, 0] or not finite(distance_tolerance_m) or
            not 0 < distance_tolerance_m < 1.25):
        raise ValueError('Invalid candidate frame or station tolerance')
    rows = draft['markers']
    if [row['id'] for row in rows] != list(STATIONS):
        raise ValueError('Expected ordered marker IDs 23, 24, 25')

    markers, blockers = [], []
    for row in rows:
        ident = row['id']
        station = STATIONS[ident]
        if row['nominal_route_xy_m'] != [station, 0]:
            raise ValueError('Unexpected nominal marker station')
        pos, rot = row['measured_position_m'], row['measured_orientation_deg']
        if not isinstance(pos, dict) or not all(finite(pos.get(k)) for k in ('x', 'y')):
            blockers.append(f'ID{ident}_MEASURED_XY_REQUIRED')
        elif math.hypot(pos['x'] - station, pos['y']) > distance_tolerance_m:
            blockers.append(f'ID{ident}_MEASURED_STATION_MISMATCH')
        if not isinstance(pos, dict) or not finite(pos.get('z')):
            blockers.append(f'ID{ident}_MEASURED_HEIGHT_REQUIRED')
        if not isinstance(rot, dict) or not all(finite(rot.get(k)) for k in ('roll', 'pitch', 'yaw')):
            blockers.append(f'ID{ident}_MEASURED_ORIENTATION_REQUIRED')
        ref = row['survey_reference']
        if row['placement'] not in ('floor', 'wall') or not isinstance(ref, str) or not ref.strip():
            blockers.append(f'ID{ident}_SURVEY_REFERENCE_REQUIRED')
        markers.append(dict(id=ident, placement=row['placement'], position=pos, orientation=rot))
    if blockers:
        return {'state': 'BLOCKED', 'reasons': blockers}
    return {'state': 'MAP_PREVIEW', 'map': dict(version=2, coordinate_frame='HOME_FRONT_DATUM',
                                              position_unit='m', orientation_unit='deg', markers=markers)}


def map_bytes(mapping):
    """Canonical artifact; hash these exact bytes for the existing owner alignment record."""
    return (json.dumps(mapping, indent=2, sort_keys=True, allow_nan=False) + '\n').encode()


def route_flu_to_local_frd(sample):
    """Convert a measured rover-base pose, preserving its original capture time."""
    keys = ('x_m', 'y_m', 'z_m', 'roll_rad', 'pitch_rad', 'yaw_rad')
    if sample.get('frame') != 'ROUTE_FLU' or not all(finite(sample.get(key)) for key in keys):
        raise ValueError('A measured ROUTE_FLU pose with all six fields is required')
    return dict(sample, frame='LOCAL_FRD', y_m=-sample['y_m'], z_m=-sample['z_m'],
                pitch_rad=-sample['pitch_rad'], yaw_rad=-sample['yaw_rad'])


def bench_preview(draft, config, sample, owner, *, now, required_source=2):
    candidate = candidate_map(draft)
    reasons = list(candidate.get('reasons', []))
    if 'map' in candidate:
        digest = sha256(map_bytes(candidate['map'])).hexdigest()
        if (owner.get('alignment_verified') is not True or owner.get('map_sha256') != digest or
                sample.get('map_sha256') != digest):
            reasons.append('OWNER_MAP_ALIGNMENT_UNVERIFIED')
    boot = owner.get('boot_id')
    if (not isinstance(boot, str) or not boot or
            owner.get('origin_boot_id') != boot or sample.get('owner_boot_id') != boot or
            owner.get('source_set_boot_id') != boot):
        reasons.append('OWNER_EVIDENCE_EPOCH_UNVERIFIED')
    if (owner.get('source') != [1, 1] or owner.get('connected') is not True or
            owner.get('disarmed') is not True or owner.get('vesc_isolated') is not True or
            owner.get('hold_only') is not True or owner.get('inhibit_active') is not True or
            owner.get('tx_enabled') is not False or owner.get('sent') != 0):
        reasons.append('STATIC_OWNER_PRECONDITIONS_UNVERIFIED')
    if (sample.get('point') != 'ROVER_BASE' or sample.get('capture_time_verified') is not True or
            sample.get('physical_pose_verified') is not True):
        reasons.append('PHYSICAL_POSE_OR_TIMESTAMP_UNVERIFIED')
    if sample.get('frame') == 'ROUTE_FLU':
        try:
            sample = route_flu_to_local_frd(sample)
        except ValueError:
            pass  # The preview reports invalid frames and never supplies missing axes.
    pose = preview_pose(config, sample, now=now, origin=owner.get('origin'),
                        parameters=owner.get('parameters') or {}, active_source=owner.get('active_source'),
                        required_source=required_source)
    reasons.extend(pose.get('reasons', []))
    if reasons:
        return {'state': 'BLOCKED', 'tx_enabled': False, 'reasons': reasons}
    return {'state': 'PREVIEW_ONLY', 'tx_enabled': False, 'map_sha256': digest, 'pose': pose}


def main():
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('candidate', type=Path)
    args = parser.parse_args()
    result = candidate_map(json.loads(args.candidate.read_text()))
    if result['state'] == 'MAP_PREVIEW':
        print(map_bytes(result['map']).decode(), end='')
        return 0
    print(json.dumps(result, indent=2))
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
