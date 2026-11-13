import copy
import os
import unittest
from unittest.mock import patch
from .telemetry_bridge import TelemetryBridge, select_voltage, URL, TIMEOUT
from .test_support import TestEnvironment


def sample(age=0.9):
    return dict(connection='CONNECTED', armed=dict(state='FRESH', value='DISARMED'),
                flight_mode=dict(state='FRESH', value={}), yaw_deg=dict(state='FRESH', value=0),
                ekf={}, messages={}, voltage_v=dict(state='UNKNOWN', value=None),
                voltage_diagnostics=dict(selected_source=None),
                esc_diagnostics=dict(groups=[dict(state='FRESH', age_s=age,
                    matches_current_autopilot=True, system=1, component=1,
                    controllers=[dict(esc_number=3, raw_voltage=6368, voltage_v=63.68)])]))

class BridgeTests(unittest.TestCase):
    def test_units_and_source(self):
        d=select_voltage(sample())
        self.assertEqual(d['voltage_v'], dict(state='FRESH', value=63.68))
        self.assertEqual(d['voltage_detail'], dict(source='Вхід ESC №3', age_s=0.9))

    def test_battery_priority(self):
        for source in ['BATTERY_STATUS', 'SYS_STATUS']:
            d=sample();d['voltage_v']=dict(state='FRESH',value=62.1)
            d['voltage_diagnostics']=dict(selected_source=source, **{source:dict(age_s=0.2)})
            self.assertEqual(select_voltage(d)['voltage_detail']['source'],source)
            d['voltage_diagnostics'][source]['age_s']=4
            self.assertEqual(select_voltage(d)['voltage_detail']['source'],'Вхід ESC №3')

    def test_stale_boundary_and_no_reset(self):
        for age,state in [(2.99,'FRESH'),(3,'STALE'),(8,'STALE')]:
            d=sample(age)
            a=TelemetryBridge(fetch=lambda:d).snapshot();b=TelemetryBridge(fetch=lambda:d).snapshot()
            self.assertEqual(a['voltage_v']['state'],state)
            self.assertGreaterEqual(b['voltage_detail']['age_s'],age)
            self.assertEqual(d['esc_diagnostics']['groups'][0]['age_s'],age)

    def test_invalid_or_foreign(self):
        for key,val in [('voltage_v',0),('voltage_v',-1),('voltage_v',float('nan')),('voltage_v',True),('esc_number',2)]:
            d=sample();d['esc_diagnostics']['groups'][0]['controllers'][0][key]=val
            self.assertEqual(select_voltage(d)['voltage_v']['state'],'UNKNOWN')
        d=sample();d['esc_diagnostics']['groups'][0]['matches_current_autopilot']=False
        self.assertEqual(select_voltage(d)['voltage_v']['state'],'UNKNOWN')

    def test_missing_and_disconnected(self):
        d=sample();d['esc_diagnostics']['groups']=[]
        self.assertEqual(select_voltage(d)['voltage_v']['state'],'UNKNOWN')
        d=sample();d['connection']='DISCONNECTED'
        self.assertNotEqual(select_voltage(d)['voltage_v']['state'],'FRESH')

    def test_failure_clears_previous_value(self):
        bridge=TelemetryBridge(fetch=lambda:sample())
        self.assertEqual(bridge.snapshot()['voltage_v']['state'],'FRESH')
        def fail():raise TimeoutError()
        bridge.fetch=fail
        d=bridge.snapshot()
        self.assertEqual(d['telemetry_bridge']['state'],'UNAVAILABLE')
        self.assertIsNone(d['voltage_v']['value'])
        bridge.fetch=lambda:{'broken':True}
        self.assertEqual(bridge.snapshot()['telemetry_bridge']['state'],'UNAVAILABLE')

    def test_fixed_transport(self):
        self.assertEqual(URL,'http://127.0.0.1:8082/api/telemetry')
        self.assertLess(TIMEOUT,1)

    def test_permission_and_disabled_local_reader(self):
        env=TestEnvironment()
        try:
            with patch.dict(os.environ,{'UGV_HMI_TELEMETRY':'0'}), patch.object(TelemetryBridge,'_fetch',return_value=sample()) as fetch:
                app=env.app()
                self.assertEqual(app.test_client().get('/api/telemetry').status_code,401)
                fetch.assert_not_called()
                client=env.client()
                response=client.get('/api/telemetry')
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json['voltage_v']['value'],63.68)
                with env.store.transaction() as c:
                    c.execute("DELETE FROM role_permissions WHERE role='admin' AND permission='telemetry.view'")
                self.assertEqual(client.get('/api/telemetry').status_code,403)
        finally:env.close()

if __name__=='__main__':unittest.main()
