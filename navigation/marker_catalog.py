"""Review proposed marker layouts and record measured positions."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .aruco_shuttle import finite


def validate(catalog):
    version = catalog['schema_version']
    count = {1: 40, 2: 69}.get(version)
    if (count is None or catalog['coordinate_frame'] != 'HOME_FRONT_DATUM' or
            (catalog['home_m'], catalog['route_end_m'], catalog['spacing_m']) != (0, count * 2.5, 2.5)
            or (version == 2 and catalog.get('planned_dictionary') != 'DICT_5X5_100')):
        raise ValueError('Invalid catalog frame or route dimensions')
    ids = range(23, 23 + count)
    markers = {}
    for row in catalog['markers']:
        ident = row['id']
        if (type(ident) is not int or ident in markers or ident not in ids or
                row['nominal_m'] != (ident - 22) * 2.5):
            raise ValueError('Duplicate or invalid marker ID / nominal station')
        measured = row['measured_position']
        if row['status'] not in ('INSTALLED', 'PLANNED') or row['geometry_status'] not in (
                'UNCONFIRMED', 'POSITION_CONFIRMED'):
            raise ValueError('Unknown marker status')
        if row['geometry_status'] == 'POSITION_CONFIRMED':
            if (row['status'] != 'INSTALLED' or not isinstance(measured, dict) or
                    not all(finite(measured.get(k)) for k in ('x_m', 'y_m')) or
                    not evidence_present(measured.get('evidence'))):
                raise ValueError('Position confirmation requires a surveyed, installed marker')
        elif measured is not None:
            raise ValueError('Unconfirmed marker has measured data')
        markers[ident] = row
    if set(markers) != set(ids):
        raise ValueError(f'Catalog must contain exactly IDs 23–{ids.stop - 1}')
    for section in catalog['confirmed_sections']:
        start, end = section['from_m'], section['to_m']
        if (not all(map(finite, (start, end))) or not 0 <= start < end <= catalog['route_end_m'] or
                not evidence_present(section.get('evidence'))):
            raise ValueError('Invalid confirmed section')
    return markers


def evidence_present(value):
    return type(value) is str and bool(value.strip())


def confirm_position(catalog, ident, x_m, y_m, evidence):
    row = validate(catalog).get(ident)
    if row is None or not all(map(finite, (x_m, y_m))) or not evidence_present(evidence):
        raise ValueError('Requires a catalog ID, measured map coordinates and a survey reference')
    row.update(status='INSTALLED', geometry_status='POSITION_CONFIRMED',
               measured_position=dict(x_m=x_m, y_m=y_m, evidence=evidence.strip(),
                                      recorded_at_utc=datetime.now(timezone.utc).isoformat()))
    validate(catalog)


def section_covers(catalog, start, end):
    lo, hi = sorted((start, end))
    return any(section['from_m'] <= lo <= hi <= section['to_m']
               for section in catalog['confirmed_sections'])


def save(path, data):
    with NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as output:
        temp = Path(output.name)
        try:
            output.write(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
            output.flush()
            os.fsync(output.fileno())
            os.chmod(temp, path.stat().st_mode & 0o777)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('catalog', type=Path)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('check')
    confirm = commands.add_parser('confirm')
    confirm.add_argument('id', type=int)
    confirm.add_argument('--x-m', type=float, required=True)
    confirm.add_argument('--y-m', type=float, required=True)
    confirm.add_argument('--evidence', required=True, help='Reference to the actual site survey')
    args = parser.parse_args()
    data = json.loads(args.catalog.read_text())
    if args.action == 'confirm':
        confirm_position(data, args.id, args.x_m, args.y_m, args.evidence)
        save(args.catalog, data)
    entries = validate(data)
    print(f'{len(entries)} markers, ID{min(entries)}–ID{max(entries)}; '
          f'{sum(row["geometry_status"] == "POSITION_CONFIRMED" for row in entries.values())} confirmed positions, '
          f'{len(data["confirmed_sections"])} confirmed sections')


if __name__ == '__main__':
    main()
