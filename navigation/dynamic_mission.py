"""Candidate single-leg missions. No upload, mode or serial transport."""

from dataclasses import dataclass
import math
from threading import RLock
import time

from .aruco_shuttle import finite, fresh
from .marker_catalog import section_covers, validate as validate_catalog
from .mavlink_shuttle import (
    MAV_CMD_DO_SET_REVERSE, MAV_CMD_NAV_WAYPOINT, MAV_FRAME_GLOBAL_INT,
    MAV_FRAME_MISSION, wire_location,
)


class Blocked(ValueError):
    pass


@dataclass(frozen=True)
class Pose:
    x_m: float
    y_m: float
    yaw_flu_deg: float
    acquired_at: float
    marker: int


@dataclass(frozen=True)
class OwnerEvidence:
    pose: Pose | None = None
    hold_at: float | None = None
    stationary_at: float | None = None
    home: dict | None = None
    origin: dict | None = None


def axis(angle):
    if not finite(angle):
        raise Blocked('Вісь маршруту не виміряна')
    angle = math.radians(angle)
    return math.cos(angle), math.sin(angle)


def project(x, y, direction):
    c, s = direction
    return x * c + y * s, -x * s + y * c


def route_reference(config):
    route_home = config['route_home_xy_m']
    if (type(route_home) is not list or len(route_home) != 2 or
            not all(finite(value) and value == 0 for value in route_home)):
        raise Blocked('HOME маршрутної системи має бути (0, 0)')
    home = config.get('home_map_xy_m')
    if not isinstance(home, list) or len(home) != 2 or not all(map(finite, home)):
        raise Blocked('Y_HOME UNKNOWN: потрібна виміряна позиція бази в HOME')
    direction = axis(config['axis_flu_deg'])
    return direction, project(*home, direction)


def route_coordinates(config, x, y):
    direction, (home_along, home_lateral) = route_reference(config)
    along, lateral = project(x, y, direction)
    return along - home_along, lateral - home_lateral


def map_geometry(marker_map, axis_flu_deg, *, home_along_m=0):
    if marker_map.get('coordinate_frame') != 'HOME_FRONT_DATUM' or marker_map.get('position_unit') != 'm':
        raise Blocked('Потрібна карта HOME_FRONT_DATUM у метрах')
    line = axis(axis_flu_deg)
    markers = {}
    for marker in marker_map['markers']:
        ident, point = marker['id'], marker['position']
        if (type(ident) is not int or ident < 0 or ident in markers or
                not all(finite(point.get(key)) for key in ('x', 'y'))):
            raise Blocked('Некоректна карта маркерів')
        along, lateral = project(point['x'], point['y'], line)
        markers[ident] = along - home_along_m, lateral
    if len(markers) < 2:
        raise Blocked('Потрібні принаймні дві виміряні мітки')
    ordered = sorted(markers, key=lambda ident: markers[ident][0])
    steps = [markers[b][0] - markers[a][0] for a, b in zip(ordered, ordered[1:])]
    return markers, ordered, steps


def route_geometry(marker_map, config):
    tolerances = ('lateral_tolerance_m', 'spacing_tolerance_m', 'heading_tolerance_deg',
                  'home_tolerance_m', 'minimum_leg_m', 'max_pose_age_s', 'max_stop_age_s')
    dwell = config['stop_at_endpoint_s']
    if (any(not finite(config[key]) or config[key] <= 0 for key in tolerances) or
            config['heading_tolerance_deg'] >= 90 or type(dwell) is not int or not 1 <= dwell <= 32767):
        raise Blocked('Некоректні межі профілю')
    _, (home_along, _) = route_reference(config)
    markers, ordered, steps = map_geometry(marker_map, config['axis_flu_deg'], home_along_m=home_along)
    crosses = [cross for _, cross in markers.values()]
    if (max(crosses) - min(crosses) > 2 * config['lateral_tolerance_m'] or
            any(abs(step - 2.5) > config['spacing_tolerance_m'] for step in steps)):
        raise Blocked('Фактична карта не підтверджує пряму з кроком 2,5 м')
    bounds = config['surveyed_bounds_m']
    if (not isinstance(bounds, list) or len(bounds) != 2 or not all(map(finite, bounds)) or
            not bounds[0] <= 0 <= bounds[1] or bounds[0] == bounds[1] or
            not all(bounds[0] <= value <= bounds[1] for value, _ in markers.values())):
        raise Blocked('Межі виміряної ділянки не підтверджені')
    return markers, ordered, bounds


def surveyed_markers(marker_map, config, catalog):
    if catalog is None:
        return marker_map
    entries = validate_catalog(catalog)
    limit = config['spacing_tolerance_m']
    direction, (home_along, _) = route_reference(config)
    matched = []
    for marker in marker_map['markers']:
        entry = entries.get(marker['id'])
        if not entry or entry['geometry_status'] != 'POSITION_CONFIRMED':
            continue
        measured, point = entry['measured_position'], marker['position']
        if (not all(finite(point.get(key)) for key in ('x', 'y')) or
                math.hypot(point['x'] - measured['x_m'], point['y'] - measured['y_m']) > limit or
                abs(project(point['x'], point['y'], direction)[0] - home_along - entry['nominal_m']) > limit):
            raise Blocked('Карта не збігається з виміряною позицією або розстановкою')
        matched.append(marker)
    return dict(marker_map, markers=matched)


def checked_pose(evidence, marker_ids, config, now):
    pose = evidence.pose
    if (pose is None or pose.marker not in marker_ids or
            not all(map(finite, (now, pose.acquired_at, pose.x_m, pose.y_m, pose.yaw_flu_deg))) or
            not fresh(now - pose.acquired_at, config['max_pose_age_s'])):
        raise Blocked('Немає свіжої підтвердженої ArUco-позиції')
    return pose


def check_stopped(evidence, config, now, after=None):
    for stamp in (evidence.hold_at, evidence.stationary_at):
        if (not finite(stamp) or not fresh(now - stamp, config['max_stop_age_s']) or
                (after is not None and stamp <= after)):
            raise Blocked('Немає свіжого підтвердження HOLD і фактичної зупинки')


def preview_route(request, config, marker_map, evidence, now, *, after=None, catalog=None):
    markers, _, (lo, hi) = route_geometry(surveyed_markers(marker_map, config, catalog), config)
    if not isinstance(request, dict):
        raise Blocked('Некоректний запит місії')
    check_stopped(evidence, config, now)
    pose = checked_pose(evidence, markers, config, now)
    if after is not None and pose.acquired_at <= after:
        raise Blocked('Потрібна нова локалізація після попередньої зупинки')
    (c, s), _ = route_reference(config)
    current, lateral = route_coordinates(config, pose.x_m, pose.y_m)
    if not lo <= current <= hi or abs(lateral) > config['lateral_tolerance_m']:
        raise Blocked('Позиція поза виміряною ділянкою')
    delta_yaw = (pose.yaw_flu_deg - config['axis_flu_deg'] + 180) % 360 - 180
    if min(abs(delta_yaw), 180 - abs(delta_yaw)) > config['heading_tolerance_deg']:
        raise Blocked('Напрямок корпусу відносно осі невідомий')
    body_sign = 1 if abs(delta_yaw) < 90 else -1
    start = request.get('start')
    round_trip = request.get('round_trip', False)
    if type(round_trip) is not bool or (round_trip and
            (start != 'home' or request.get('target_type') != 'marker')):
        raise Blocked('Повернення в HOME доступне для старту HOME і цілі за міткою')
    if start == 'home' and abs(current) <= config['home_tolerance_m']:
        initial = 0.0
    elif start == 'current':
        initial = current
    else:
        raise Blocked('Для старту HOME ровер має бути підтверджено в HOME')

    if request.get('target_type') == 'marker':
        ident = request.get('marker')
        if type(ident) is not int or ident not in markers:
            raise Blocked('Невідома мітка цілі')
        target = markers[ident][0]
    elif request.get('target_type') == 'distance':
        distance = request.get('distance_m')
        reference = request.get('distance_from', start)
        if not finite(distance) or distance < 0:
            raise Blocked('Вкажіть відстань у метрах')
        if reference == 'home':
            target = distance
        elif reference == 'current':
            direction = request.get('direction')
            if distance == 0 or direction not in ('forward', 'reverse'):
                raise Blocked('Вкажіть додатну відстань і напрямок від поточної позиції')
            target = current + distance * body_sign * (1 if direction == 'forward' else -1)
        else:
            raise Blocked('Невідома точка відліку відстані')
    else:
        raise Blocked('Невідомий спосіб вибору цілі')
    if not lo <= target <= hi or abs(target - current) <= config['minimum_leg_m']:
        raise Blocked('Ціль поза ділянкою або надто близька до поточної позиції')
    if catalog is not None and not section_covers(catalog, current, target):
        raise Blocked('Весь відрізок маршруту має бути підтверджений вимірюваннями')
    if round_trip and (not lo <= 0 <= hi or abs(target) <= config['minimum_leg_m'] or
                       (catalog is not None and not section_covers(catalog, 0, target))):
        raise Blocked('Зворотний відрізок до HOME не підтверджений')
    reverse = (target - current) * body_sign < 0
    home_x, home_y = config['home_map_xy_m']
    return {'start_m': initial, 'current_m': current, 'target_m': target, 'reverse': reverse,
            'round_trip': round_trip,
            'target_y_m': config['route_home_xy_m'][1], 'target_map_xy_m': [home_x + c * target, home_y + s * target]}


def ground_location(config, along):
    survey = config['survey']
    # In a local-only session the approved synthetic origin names HOME and
    # the route defines the estimator's +N. Global encoding is only for AUTO.
    local = survey.get('reference') == 'LOCAL_HOME_NORTH'
    zero = survey.get('expected_ekf_origin' if local else 'home_map_zero')
    bearing = 0 if local else survey.get('route_azimuth_deg')
    wire_location(zero)
    if not finite(bearing) or not 0 <= bearing < 360 or abs(along) > (172.5 if local else 100):
        raise Blocked('Азимут або відстань поза межами профілю')
    # Short-site WGS84 tangent plane. MSL is separately surveyed for this level route.
    latitude = math.radians(zero['latitude_deg'])
    if abs(zero['latitude_deg']) >= 80:
        raise Blocked('Коротка дотична апроксимація не підтримує полярний маршрут')
    heading = math.radians(bearing)
    ecc2, major = 0.00669437999014, 6378137.0
    d = math.sqrt(1 - ecc2 * math.sin(latitude) ** 2)
    meridian = major * (1 - ecc2) / d ** 3
    normal = major / d
    return wire_location(dict(
        latitude_deg=zero['latitude_deg'] + math.degrees(along * math.cos(heading) / meridian),
        longitude_deg=zero['longitude_deg'] + math.degrees(along * math.sin(heading) / (normal * math.cos(latitude))),
        altitude_msl_m=zero['altitude_msl_m'],
    ))


def compile_leg(route, config, evidence):
    survey = config['survey']
    try:
        if wire_location(evidence.origin) != wire_location(survey['expected_ekf_origin']):
            raise Blocked('EKF origin не збігається з підтвердженою прив’язкою')
        home = wire_location(evidence.home)
        if survey.get('reference') == 'LOCAL_HOME_NORTH' and home != ground_location(config, 0):
            raise Blocked('Pixhawk HOME не збігається з погодженим локальним origin')
        target = ground_location(config, route['target_m'])
        if ground_location(config, route['current_m']) == target:
            raise Blocked('Ціль не відрізняється від старту в точності MAVLink')
        return_point = ground_location(config, 0) if route.get('round_trip') else None
        if return_point == target:
            raise Blocked('Ціль не відрізняється від HOME в точності MAVLink')
    except ValueError as exc:
        if isinstance(exc, Blocked):
            raise
        raise Blocked('Немає перевірених HOME, origin або виміряної геоприв’язки') from exc

    base = dict(target_system=1, target_component=1, current=0, autocontinue=1,
                mission_type=0, param2=0, param3=0, param4=0)
    items = [
        dict(base, seq=0, frame=MAV_FRAME_GLOBAL_INT, command=MAV_CMD_NAV_WAYPOINT, param1=0, **home),
        dict(base, seq=1, frame=MAV_FRAME_MISSION, command=MAV_CMD_DO_SET_REVERSE,
             param1=int(route['reverse']), x=0, y=0, z=0),
        dict(base, seq=2, frame=MAV_FRAME_GLOBAL_INT, command=MAV_CMD_NAV_WAYPOINT,
             param1=config['stop_at_endpoint_s'], **target),
    ]
    if return_point is not None:
        items.extend([
            dict(items[1], seq=3, param1=int(not route['reverse'])),
            dict(items[2], seq=4, **return_point),
        ])
    return items


class MissionSession:
    """Candidate lifecycle driven by trusted events from the single owner."""
    def __init__(self, config, marker_map, owner_evidence, clock=time.monotonic, *, request_hold=None, catalog=None):
        self.config, self.marker_map, self.owner_evidence = config, marker_map, owner_evidence
        self.clock, self.lock = clock, RLock()
        self.request_hold = request_hold
        self.catalog = catalog
        self.state, self.stop, self.items = 'IDLE', None, None
        self.request = self.hold_since = None
        self.completed_passes = 0
        self.destination_m = None

    def prepare(self, request, *, create=False):
        with self.lock:
            if self.state in ('REQUESTED', 'EXECUTING', 'HOLD_REQUIRED'):
                raise Blocked('Попередня місія ще не завершена')
            now = self.clock()
            evidence = self.owner_evidence()
            route = self.preview(request, evidence, now)
            try:
                items = compile_leg(route, self.config, evidence)
            except Blocked as exc:
                if create:
                    raise
                return dict(route, state='PREVIEW', blocker=str(exc))
            if create:
                self.state, self.items, self.request = 'REQUESTED', items, dict(request)
                self.destination_m = 0.0 if route['round_trip'] else route['target_m']
            return dict(route, state='REQUESTED' if create else 'PREVIEW', blocker=None)

    def owner_started(self):
        with self.lock:
            if self.state != 'REQUESTED' or self.request_hold is None:
                raise Blocked('Немає запиту місії або інтегрованого owner HOLD')
            now = self.clock()
            try:
                evidence = self.owner_evidence()
                route = self.preview(self.request, evidence, now)
                if compile_leg(route, self.config, evidence) != self.items:
                    raise Blocked('Позиція змінилася після формування місії')
            except (Blocked, OSError):
                self.state, self.items = 'ABORTED', None
                raise
            self.state = 'EXECUTING'

    def owner_progress(self, *, navigation_valid, finished=False):
        with self.lock:
            if self.state == 'EXECUTING':
                now = self.clock()
                try:
                    checked_pose(self.owner_evidence(), self.marker_ids(), self.config, now)
                except (Blocked, OSError):
                    navigation_valid = False
                if finished or navigation_valid is not True:
                    self.state, self.hold_since = 'HOLD_REQUIRED', now
                    self.completed_route = finished and navigation_valid is True
                    try:
                        self.request_hold()
                    except OSError:
                        pass  # Only later observed HOLD and stop can clear this state.
            if self.state == 'HOLD_REQUIRED':
                now = self.clock()
                try:
                    evidence = self.owner_evidence()
                    check_stopped(evidence, self.config, now, after=self.hold_since)
                    pose = checked_pose(evidence, self.marker_ids(), self.config, now)
                    if pose.acquired_at < max(evidence.hold_at, evidence.stationary_at):
                        return self.state
                except (Blocked, OSError):
                    return self.state
                along, lateral = route_coordinates(self.config, pose.x_m, pose.y_m)
                self.stop = dict(acquired_at=pose.acquired_at, route_m=along, lateral_m=lateral,
                                 x_m=pose.x_m, y_m=pose.y_m, marker=pose.marker)
                complete = (self.completed_route and
                            abs(along - self.destination_m) <= self.config['home_tolerance_m'] and
                            abs(lateral) <= self.config['lateral_tolerance_m'])
                if complete:
                    self.completed_passes += (len(self.items) - 1) // 2
                self.items = None
                self.state = 'COMPLETED' if complete else 'ABORTED'
            return self.state

    def preview(self, request, evidence, now):
        return preview_route(request, self.config, self.marker_map, evidence, now,
                             after=self.stop['acquired_at'] if self.stop else None, catalog=self.catalog)

    def marker_ids(self):
        if self.catalog is None:
            return [marker['id'] for marker in self.marker_map['markers']]
        confirmed = validate_catalog(self.catalog)
        return [marker['id'] for marker in self.marker_map['markers']
                if marker['id'] in confirmed and
                confirmed[marker['id']]['geometry_status'] == 'POSITION_CONFIRMED']

    def snapshot(self):
        with self.lock:
            result = {'state': self.state, 'stop': dict(self.stop) if self.stop else None,
                      'marker_ids': self.marker_ids(), 'completed_passes': self.completed_passes}
            if self.catalog is not None:
                selectable = set(result['marker_ids'])
                result['marker_catalog'] = [
                    dict(id=row['id'], nominal_m=row['nominal_m'],
                         state=('CONFIRMED' if row['id'] in selectable else
                                'PLANNED' if row['status'] == 'PLANNED' else 'UNCONFIRMED'))
                    for row in self.catalog['markers']
                ]
            return result
