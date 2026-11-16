import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from .technical_telemetry_log import TechnicalLog


class TechnicalLogTests(unittest.TestCase):
    def test_closed_page_absence_changes_fresh_sampling_and_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [0]
            data = dict(telemetry_bridge={'state':'OK'}, messages={}, ekf={}, voltage_diagnostics={})
            bridge = Mock();bridge.snapshot.side_effect=lambda:data
            path = Path(directory)/'technical.jsonl'
            log = TechnicalLog(path,bridge,clock=lambda:now[0],max_bytes=100000)
            self.addCleanup(log.close)
            for t in range(600):
                now[0]=t;log.tick();log.tick()
            self.assertEqual(bridge.snapshot.call_count,600)
            lines=[json.loads(x) for x in path.read_text().splitlines()]
            self.assertEqual(len(lines),4)  # ten minutes absent: one record per each of four indicators
            self.assertTrue(all(x['time'] and x['reason'] for x in lines))
            self.assertIsNone(lines[0]['source'])
            self.assertIsNone(lines[0]['unit'])
            self.assertEqual(lines[0]['reason'],'NOT_RECEIVED')
            data['ekf']={'ESTIMATOR_STATUS':{'state':'FRESH','value':{'flags':167}}}
            data['messages']={'ESTIMATOR_STATUS':{'state':'FRESH','age_s':.2}}
            for t in range(600,661):now[0]=t;log.tick()
            lines=[json.loads(x) for x in path.read_text().splitlines()]
            self.assertEqual(len(lines),7)  # immediate recovery, then 30s samples
            self.assertEqual(lines[-1]['value'],{'flags':167})
            data['telemetry_bridge']['state']='UNAVAILABLE';now[0]=661;log.tick()
            self.assertTrue(all(x['reason']=='UPSTREAM_UNAVAILABLE' for x in
                                [json.loads(v) for v in path.read_text().splitlines()][-4:]))
            log.handler.maxBytes=1200
            for t in range(662,900):
                now[0]=t;data['telemetry_bridge']['state']='OK' if t%2 else 'UNAVAILABLE';log.tick()
            files=list(Path(directory).glob('technical.jsonl*'))
            self.assertLessEqual(len(files),4)
            self.assertTrue(all(p.stat().st_size<1200 and p.stat().st_mode&0o777==0o600 for p in files))
            self.assertEqual(data['ekf']['ESTIMATOR_STATUS']['value'],{'flags':167})

    def test_fresh_ekf_is_logged_without_browser_and_api_is_unchanged(self):
        import copy
        with tempfile.TemporaryDirectory() as directory:
            now=[0]
            data={'telemetry_bridge':{'state':'OK'}, 'ekf':{'EKF_STATUS_REPORT':{'state':'FRESH','value':{'flags':167}}},
                  'messages':{'EKF_STATUS_REPORT':{'state':'FRESH','age_s':0.2}}}
            original=copy.deepcopy(data)
            bridge=Mock();bridge.snapshot.return_value=data
            path=Path(directory)/'technical.jsonl'
            log=TechnicalLog(path,bridge,clock=lambda:now[0]);self.addCleanup(log.close)
            for t in range(61):now[0]=t;log.tick()
            rows=[json.loads(x) for x in path.read_text().splitlines()]
            rows=[x for x in rows if x['indicator']=='EKF_STATUS_REPORT']
            self.assertEqual(len(rows),3)
            self.assertTrue(all(x['state']=='FRESH' and x['value']=={'flags':167} and x['age_s']==0.2 for x in rows))
            self.assertEqual(data,original)
