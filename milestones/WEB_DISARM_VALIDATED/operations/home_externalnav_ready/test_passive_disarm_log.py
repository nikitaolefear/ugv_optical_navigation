"""Focused offline observer contracts; temporary DB and mocked fixed HTTP GETs."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('passive_log',Path(__file__).with_name('passive_disarm_log.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class PassiveLogTests(unittest.TestCase):
    def test_query_only_snapshot_accepts_pending_and_preserves_db(self):
        with tempfile.TemporaryDirectory() as name:
            db=Path(name)/'ledger.sqlite'
            with sqlite3.connect(db) as conn:
                conn.execute('CREATE TABLE commands(id TEXT PRIMARY KEY,state TEXT)')
                conn.execute('CREATE TABLE delivery_evidence(id TEXT PRIMARY KEY,write_state TEXT)')
                conn.execute("INSERT INTO commands VALUES('a','QUEUED')")
                conn.execute("INSERT INTO delivery_evidence VALUES('a','NOT_ATTEMPTED')")
            before=hashlib.sha256(db.read_bytes()).hexdigest()
            result=m.ledger_snapshot(db)
            self.assertEqual(result['commands'][0]['state'],'QUEUED')
            self.assertEqual(before,hashlib.sha256(db.read_bytes()).hexdigest())
            missing=Path(name)/'missing.sqlite'
            with self.assertRaises(sqlite3.OperationalError):m.ledger_snapshot(missing)
            self.assertFalse(missing.exists())

    def test_mock_http_and_fresh_transitions_and_loss_not_stopped(self):
        requests=[]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self):return b'{}'
        class Opener:
            def open(self,request,timeout):
                requests.append((request.full_url,request.get_method()))
                return Response()
        with patch.object(m.urllib.request,'build_opener',return_value=Opener()):
            values,errors=m.collect_http()
        self.assertEqual(errors,{})
        self.assertEqual(len(requests),4)
        self.assertTrue(all(url in m.ENDPOINTS.values() and method=='GET' for url,method in requests))
        with self.assertRaises(ValueError):m.get_json('http://127.0.0.1:8084/api/reset')
        tracker=m.Events()
        def row(armed,link='CONNECTED'):
            return dict(utc='2026-11-17T00:00:00Z',monotonic_s=1.,errors={},
                        http={'telemetry':dict(connection=link,armed=dict(state='FRESH',value=armed),
                                               messages={'HEARTBEAT':dict(state='FRESH')})})
        tracker.observe(row('DISARMED'));tracker.observe(row('ARMED'));tracker.observe(row('DISARMED'))
        self.assertEqual(len(tracker.armed_to_disarmed),1)
        tracker.observe(row('ARMED'));tracker.observe(row('DISARMED','STALE'));tracker.observe(row('DISARMED'))
        self.assertEqual(len(tracker.armed_to_disarmed),1)
        self.assertEqual(tracker.arm_transitions[-2]['current'],'UNKNOWN')
        malformed=dict(utc='fixture',monotonic_s=2.,errors={},
                       http={'telemetry':{'armed':None,'messages':None},
                             'diagnostics':{'attitude':None,'statustext':None}})
        events=tracker.observe(malformed)
        self.assertEqual(tracker.armed,'UNKNOWN')
        self.assertTrue(any(e['kind']=='OBSERVATION_SOURCE_LOST' for e in events))
        values,errors=m.collect_http(lambda url:{'invalid':float('nan')})
        self.assertEqual(values,{})
        self.assertEqual(set(errors),set(m.ENDPOINTS))

    def test_duration_bounds_and_short_complete_artifact_without_live_access(self):
        for value in ('0','-1','601','nan','inf','no'):
            with self.subTest(value=value),self.assertRaises(argparse.ArgumentTypeError):m.duration_value(value)
        self.assertEqual(m.duration_value('600'),600.)
        with tempfile.TemporaryDirectory() as name:
            base=Path(name);db=base/'ledger'
            with sqlite3.connect(db) as conn:
                conn.execute('CREATE TABLE commands(id TEXT PRIMARY KEY)')
                conn.execute('CREATE TABLE delivery_evidence(id TEXT PRIMARY KEY)')
            out=base/'capture'
            result=m.record(.01,out,db,getter=lambda url:{})
            self.assertFalse(result['control_actions'])
            self.assertFalse(result['physical_wheel_stop_measured'])
            self.assertGreaterEqual(result['rows'],1)
            for line in (out/'SHA256SUMS').read_text().splitlines():
                h,p=line.split('  ',1)
                self.assertEqual(h,hashlib.sha256((out/p).read_bytes()).hexdigest())
            records=[json.loads(line) for line in (out/'raw.jsonl').read_text().splitlines()]
            self.assertEqual(records[0]['kind'],'HEADER')
            self.assertIn('+02:00',records[0]['kyiv'])
            self.assertEqual(records[-1]['kind'],'END')


if __name__=='__main__':unittest.main(verbosity=2)
