import json
from pathlib import Path
import unittest

from navigation.dynamic_mission import Blocked, MissionSession, OwnerEvidence, Pose
from navigation.marker_catalog import confirm_position, validate


ROOT = Path(__file__).resolve().parents[2]


class CatalogTest(unittest.TestCase):
    def test_beta_catalog_does_not_admit_planned_stations(self):
        directory = ROOT / 'navigation'
        catalog = json.loads((directory / 'aruco_69_marker_catalog.json').read_text())
        entries = validate(catalog)
        self.assertEqual(list(entries), list(range(23, 92)))
        self.assertEqual(entries[91]['nominal_m'], 172.5)
        self.assertTrue(all(entries[i]['status'] == 'PLANNED' for i in range(26, 92)))
        config = json.loads((directory / 'dynamic_mission_beta.json').read_text())
        marker_map = json.loads((directory / 'nav_bench/marker_map.json').read_text())
        session = MissionSession(config, marker_map, lambda: OwnerEvidence(), catalog=catalog)
        snapshot = session.snapshot()
        self.assertEqual(snapshot['marker_ids'], [])
        self.assertLess(len(json.dumps(dict(ok=True, command=snapshot)).encode()), 3900)
        with self.assertRaises(Blocked):
            session.prepare(dict(start='home', target_type='marker', marker=91), create=True)

    def test_100m_catalog_and_measured_route_admission(self):
        catalogue = json.loads((ROOT / 'navigation/aruco_100m_catalog.json').read_text())
        entries = validate(catalogue)
        self.assertEqual(len(entries), 40)
        self.assertEqual({key: entries[key]['nominal_m'] for key in (23, 25, 42, 62)},
                         {23: 2.5, 25: 7.5, 42: 50, 62: 100})
        self.assertEqual([key for key, row in entries.items() if row['status'] == 'INSTALLED'], [23,24,25])
        self.assertTrue(all(row['measured_position'] is None for row in entries.values()))
        config = json.loads((ROOT / 'navigation/dynamic_mission.json').read_text())
        config.update(axis_flu_deg=0, home_map_xy_m=[0, 0], surveyed_bounds_m=[0, 10])
        marker_map = dict(coordinate_frame='HOME_FRONT_DATUM', position_unit='m',
                          markers=[dict(id=i, position=dict(x=entries[i]['nominal_m'], y=0))
                                   for i in (23,24,25,26)])
        evidence = OwnerEvidence(Pose(7.5, 0, 0, 10., 25), 10., 10.)
        session = MissionSession(config, marker_map, lambda: evidence, lambda: 10.1, catalog=catalogue)
        result = session.snapshot()
        self.assertEqual(len(result['marker_catalog']), 40)
        self.assertLess(len(json.dumps(dict(result, id='x'*36, actor_id=1)).encode()), 3800)
        home_distance = dict(start='current',target_type='distance',distance_from='home',distance_m=5.)
        with self.assertRaises(Blocked):
            session.prepare(home_distance)
        for ident in (23,24,25):
            confirm_position(catalogue, ident, entries[ident]['nominal_m'], 0., 'test snapshot')
        with self.assertRaisesRegex(Blocked, 'Весь відрізок'):
            session.prepare(home_distance)
        catalogue['confirmed_sections'].append(dict(from_m=0,to_m=10,evidence='test coverage'))
        route = session.prepare(home_distance)
        self.assertEqual((route['target_m'], route['reverse']), (5., True))
        relative = dict(start='current',target_type='distance',distance_from='current',
                        distance_m=2.5,direction='forward')
        self.assertEqual(session.prepare(relative)['target_m'], 10.)
        with self.assertRaisesRegex(Blocked, 'Невідома мітка'):
            session.prepare(dict(start='current',target_type='marker',marker=26))
        with self.assertRaises(Blocked):
            session.prepare(dict(relative,distance_m=3))
        with self.assertRaises(Blocked):
            session.prepare(home_distance,create=True)  # No measured geographic origin.
        validate(catalogue)
        catalogue['markers'].append(catalogue['markers'][0])
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            validate(catalogue)
