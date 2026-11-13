"""Memory-only MAVLink wire decoding. No connection, send(), thread or serial open."""
import unittest
from unittest.mock import Mock
from pymavlink.dialects.v20 import ardupilotmega as mav
from .app import create_app
from .pixhawk_telemetry import PixhawkTelemetry, ESC_GROUPS, ESC_CACHE_LIMIT, TYPES
from .test_pixhawk_telemetry import Message


class EscDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.now=10.;self.wall=1700000000.
        self.opener=Mock(side_effect=AssertionError('serial must not be opened'))
        self.reader=PixhawkTelemetry(clock=lambda:self.now,wall_clock=lambda:self.wall,opener=self.opener)
        self.reader.state='READING'

    def tearDown(self):
        self.opener.assert_not_called();self.assertIsNone(self.reader.thread)

    def heartbeat(self):
        self.reader.ingest(Message(kind='HEARTBEAT',autopilot=3,type=10,base_mode=1,custom_mode=4))

    def wire_message(self,kind,system=1,component=1):
        cls=next(cls for cls in mav.mavlink_map.values() if cls.msgname==kind)
        msg=cls(temperature=[20]*4,voltage=[2345,0,65535,1200],current=[0]*4,
                totalcurrent=[0]*4,rpm=[0]*4,count=[1]*4)
        encoder=mav.MAVLink(None,srcSystem=system,srcComponent=component)
        packet=msg.pack(encoder)  # bytes only, never sent to a transport
        decoded=mav.MAVLink(None).parse_buffer(packet)
        self.assertEqual(len(decoded),1)
        return decoded[0]

    def test_wire_decode_all_eight_groups_centivolts_and_controller_numbers(self):
        self.heartbeat()
        for kind,first in ESC_GROUPS.items():
            self.assertNotIn(kind,TYPES)
            self.reader.ingest(self.wire_message(kind))
            group=self.reader.snapshot()['esc_diagnostics']['groups'][-1]
            self.assertEqual(group['system'],1);self.assertEqual(group['component'],1)
            self.assertEqual(group['source_filter_at_receive'],'MATCH')
            self.assertEqual(group['legacy_drop_reason'],'TYPE_NOT_IN_TYPES')
            first_esc=group['controllers'][0]
            self.assertEqual(first_esc['esc_number'],first)
            self.assertEqual(first_esc['esc_index'],first-1)
            self.assertEqual(first_esc['raw_voltage'],2345)
            self.assertEqual(first_esc['raw_unit'],'cV')
            self.assertEqual(first_esc['voltage_v'],23.45)
            self.assertEqual(group['controllers'][1]['voltage_v'],0.)
            self.assertFalse(group['controllers'][1]['positive_voltage'])
            # ESC telemetry has no documented UINT16_MAX sentinel; do not invent one.
            self.assertEqual(group['controllers'][2]['voltage_v'],655.35)
            self.assertEqual(group['received_unix_s'],self.wall)
            self.assertEqual(group['state'],'FRESH')
        s=self.reader.snapshot()
        self.assertEqual(len(s['esc_diagnostics']['groups']),8)
        self.assertTrue(all(c==1 for k,c in s['esc_diagnostics']['received_counts'].items() if k in ESC_GROUPS))
        self.assertEqual(s['voltage_v'],{'state':'UNKNOWN','value':None})
        self.assertFalse(s['esc_diagnostics']['used_for_hmi_voltage'])

    def test_observe_before_autopilot_selection_and_foreign_component_filter(self):
        self.reader.ingest(self.wire_message('ESC_TELEMETRY_1_TO_4'))
        self.reader.ingest(self.wire_message('ESC_TELEMETRY_1_TO_4',system=42,component=190))
        groups=self.reader.snapshot()['esc_diagnostics']['groups']
        self.assertEqual(groups[0]['source_filter_at_receive'],'AUTOPILOT_NOT_SELECTED')
        self.assertEqual(groups[1]['source_filter_at_receive'],'COMPONENT_NOT_1')
        self.assertIsNone(self.reader.source)
        self.heartbeat()
        groups=self.reader.snapshot()['esc_diagnostics']['groups']
        self.assertTrue(groups[0]['matches_current_autopilot'])
        self.assertFalse(groups[1]['matches_current_autopilot'])
        self.assertNotIn('ESC_TELEMETRY_1_TO_4',self.reader.messages)

    def test_freshness_monotonic_age_and_disconnection(self):
        self.reader.ingest(self.wire_message('ESC_TELEMETRY_1_TO_4'))
        self.wall-=10000;self.now+=3
        group=self.reader.snapshot()['esc_diagnostics']['groups'][0]
        self.assertEqual(group['received_unix_s'],1700000000.)
        self.assertEqual(group['age_s'],3.);self.assertEqual(group['state'],'FRESH')
        self.now+=.001
        self.assertEqual(self.reader.snapshot()['esc_diagnostics']['groups'][0]['state'],'STALE')
        self.now=10.;self.reader.state='DISCONNECTED'
        self.assertEqual(self.reader.snapshot()['esc_diagnostics']['groups'][0]['state'],'STALE')

    def test_bounded_cache_and_snapshot_does_not_mutate_cache(self):
        for system in range(80):
            self.reader.ingest(self.wire_message('ESC_TELEMETRY_1_TO_4',system=system))
        s=self.reader.snapshot()['esc_diagnostics']
        self.assertEqual(len(s['groups']),ESC_CACHE_LIMIT)
        self.assertEqual(s['received_counts']['ESC_TELEMETRY_1_TO_4'],80)
        self.assertEqual(s['evicted_group_source_entries'],80-ESC_CACHE_LIMIT)
        s['groups'][0]['controllers'][0]['raw_voltage']=123
        self.assertEqual(self.reader.snapshot()['esc_diagnostics']['groups'][0]['controllers'][0]['raw_voltage'],2345)

    def test_esc_status_unit_and_index_if_parser_event_is_available(self):
        # Current installed dialect lacks wire ESC_STATUS. Test the explicit ingest
        # representation without pretending the live parser can decode that type.
        self.reader.ingest(Message(kind='ESC_STATUS',index=4,voltage=[24.5,24.6,0,float('nan')]))
        entries=self.reader.snapshot()['esc_diagnostics']['groups'][0]['controllers']
        self.assertEqual(entries[0]['esc_number'],5)
        self.assertEqual(entries[0]['raw_unit'],'V');self.assertEqual(entries[0]['voltage_v'],24.5)
        self.assertIsNone(entries[3]['voltage_v'])

    def test_hmi_voltage_selection_unchanged_and_http_diagnostics_without_reader_start(self):
        self.heartbeat()
        self.reader.ingest(Message(kind='SYS_STATUS',voltage_battery=24000))
        self.reader.ingest(self.wire_message('ESC_TELEMETRY_1_TO_4'))
        response=create_app(telemetry=self.reader).test_client().get('/api/telemetry')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['voltage_v'],{'state':'FRESH','value':24.})
        self.assertEqual(response.json['voltage_diagnostics']['selected_source'],'SYS_STATUS')
        self.assertEqual(response.json['esc_diagnostics']['groups'][0]['controllers'][0]['voltage_v'],23.45)
