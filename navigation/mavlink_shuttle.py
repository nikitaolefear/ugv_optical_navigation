"""Compile surveyed shuttle legs to offline Rover MISSION_ITEM_INT fields."""

import argparse
import json
from pathlib import Path

from .aruco_shuttle import build_preview, finite

MAV_CMD_NAV_WAYPOINT = 16
MAV_CMD_DO_SET_REVERSE = 194
MAV_FRAME_GLOBAL_INT = 5
MAV_FRAME_MISSION = 2


def wire_location(point):
    if not isinstance(point, dict):
        raise ValueError('Expected a surveyed WGS84/MSL point')
    lat, lon, alt = (point.get(key) for key in ('latitude_deg', 'longitude_deg', 'altitude_msl_m'))
    if (not all(map(finite, (lat, lon, alt))) or not -90 <= lat <= 90 or
            not -180 <= lon <= 180 or not -83886 < alt < 83886):
        raise ValueError('Invalid WGS84/MSL point')
    x, y = round(lat * 1e7), round(lon * 1e7)
    if x == y == 0:
        raise ValueError('Zero Location is reserved by Rover')
    return {'x': x, 'y': y, 'z': alt}


def compile_mission(config, marker_map):
    cycles = config['cycles']
    if type(cycles) is int and 1 + 4 * cycles > 65535:
        raise ValueError('Mission exceeds the MAVLink count field')
    preview = build_preview(config, marker_map)
    survey = config['mavlink_survey']
    required = ('home', 'ekf_origin', 'map_zero', 'map_x_azimuth_deg', 'A', 'B')
    missing = [key for key in required if survey.get(key) is None]
    reasons = [f'LOCAL_{point}_NOT_SURVEYED' for point in preview.get('missing_survey_endpoints', [])]
    reasons.extend(f'SURVEY_{name.upper()}_MISSING' for name in missing)
    if reasons:
        return {'state': 'BLOCKED', 'reasons': reasons, 'items': []}

    if not finite(survey['map_x_azimuth_deg']) or not 0 <= survey['map_x_azimuth_deg'] < 360:
        raise ValueError('Map +X azimuth must be measured clockwise from true north')
    locations = {key: wire_location(survey[key]) for key in required if key != 'map_x_azimuth_deg'}
    if all(locations['A'][axis] == locations['B'][axis] for axis in ('x', 'y')):
        raise ValueError('A and B must have distinct horizontal coordinates at MAVLink precision')

    count = 1 + 4 * preview['cycles']
    items = []

    def append(command, frame, param1, location):
        items.append(dict(target_system=1, target_component=1, seq=len(items),
                          mission_type=0, frame=frame, command=command,
                          current=0, autocontinue=1, param1=param1, param2=0,
                          param3=0, param4=0, **location))

    # Rover reserves sequence zero for its established home.
    append(MAV_CMD_NAV_WAYPOINT, MAV_FRAME_GLOBAL_INT, 0, locations['home'])
    for leg in preview['legs']:
        append(MAV_CMD_DO_SET_REVERSE, MAV_FRAME_MISSION, leg['DO_SET_REVERSE_param1'], dict(x=0, y=0, z=0))
        append(MAV_CMD_NAV_WAYPOINT, MAV_FRAME_GLOBAL_INT, leg['NAV_WAYPOINT_param1_s'], locations[leg['to']])
    return {'state': 'OFFLINE_ONLY', 'count': count, 'cycles': preview['cycles'],
            'nominal_body_yaw_flu_deg': preview['nominal_body_yaw_flu_deg'], 'items': items}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    path = parser.parse_args().config
    config = json.loads(path.read_text())
    marker_map = json.loads((path.parent / config['map_file']).read_text())
    mission = compile_mission(config, marker_map)
    print(json.dumps(mission, indent=2))
    return 2 if mission['state'] == 'BLOCKED' else 0


if __name__ == '__main__':
    raise SystemExit(main())
