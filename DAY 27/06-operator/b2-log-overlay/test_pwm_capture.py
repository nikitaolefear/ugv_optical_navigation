import time
from unittest import TestCase

from pymavlink.dialects.v20 import ardupilotmega as mav

import test_discovery as harness


class CaptureCases(TestCase):
    def fixture(self):
        t = harness.DiscoveryTests('test_unknown_index_then_separate_indexed_read_and_authorization')
        t.setUp()
        self.addCleanup(t.doCleanups)
        return t

    def test_stale_pwm_captured_without_widening_threshold(self):
        t=self.fixture()
        t.start()
        t.pump_discovery(lambda: t.pwm.update(pwm_age=3.0))
        s=t.dispatch('S3_LOG_STATUS')
        self.assertEqual(s['failure_capture']['reason'],'LOG_DISCOVERY_PWM_NOT_FRESH')
        self.assertEqual(s['failure_capture']['pwm_age'],3.0)
        self.assertEqual(s['failure_capture']['servo5'],999)
        self.assertEqual(s['failure_capture']['tx_sent'],0)
        self.assertIsNotNone(s['failure_capture']['last_param_at'])

    def test_pwm_change_captured_and_next_read_blocked(self):
        t=self.fixture()
        t.start()
        t.pump_discovery(lambda: t.pwm.update(servo6=2000))
        s=t.dispatch('S3_LOG_STATUS')['failure_capture']
        self.assertEqual((s['reason'],s['servo6'],s['unique']),('LOG_DISCOVERY_PWM_NOT_HOLD',2000,1))
        self.assertEqual(len(t.sent),1)

    def test_flood_count_bounded_and_critical_message_observed(self):
        t=self.fixture()
        t.list_replies.clear()
        t.start()
        for _ in range(200):
            if t.q.slot: break
            time.sleep(.002)
        t.q.tick(t.fd,t.respond,connected=True,epoch=1,priority_clear=False)
        self.assertFalse(t.sent)
        t.q.tick(t.fd,t.respond,connected=True,epoch=1,priority_clear=True)
        frame=t.make(65535,b'LOG_DISARMED')
        for _ in range(7000):
            t.q.observe(frame)
        t.q.observe(t.packet(mav.MAVLink_attitude_message(1234,0,0,0,0,0,0)))
        self.assertEqual(t.q.fc_time,1234)
        for _ in range(5300):
            t.q.observe(frame)
        t.pump_discovery()
        capture=t.dispatch('S3_LOG_STATUS')['failure_capture']
        self.assertEqual(capture['reason'],'LOG_DISCOVERY_FRAME_LIMIT')
        self.assertEqual(len(t.sent),1)

    def test_scan_paces_each_wire_request_and_confirms_separately(self):
        t=self.fixture()
        original=t.respond
        sent_at=[]
        def respond(message):
            sent_at.append(t.now)
            original(message)
        t.respond=respond
        t.start()
        t.pump_discovery()
        self.assertEqual([m.param_index for m in t.sent],[0,1,1])
        self.assertGreaterEqual(sent_at[1]-sent_at[0],.1)
        self.assertGreaterEqual(sent_at[2]-sent_at[1],.3)

    def test_inflight_safety_abort_fault_and_no_retry(self):
        t=self.fixture()
        t.list_replies.clear()
        t.start()
        t.pump_discovery(lambda: t.flags.update(fresh_heartbeat=False))
        s=t.dispatch('S3_LOG_STATUS')
        self.assertTrue(s['transport_fault'])
        self.assertEqual(s['failure_capture']['reason'],'LOG_DISCOVERY_FRESH_HEARTBEAT')
        self.assertEqual([m.get_type() for m in t.sent],['PARAM_REQUEST_READ'])
