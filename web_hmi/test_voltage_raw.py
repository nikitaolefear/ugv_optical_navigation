"""Synthetic messages only: no reader thread, serial open, or MAVLink transmission."""
import math
import unittest
from unittest.mock import Mock
from .app import create_app
from .pixhawk_telemetry import PixhawkTelemetry
from .test_pixhawk_telemetry import Message


class RawVoltageTest(unittest.TestCase):
    def setUp(self):
        self.now=10.0;self.wall=1700000000.0
        self.opener=Mock(side_effect=AssertionError('serial must not be opened'))
        self.reader=PixhawkTelemetry(clock=lambda:self.now,wall_clock=lambda:self.wall,opener=self.opener)
        self.reader.state='READING'
        self.ingest('HEARTBEAT',autopilot=3,type=10,base_mode=1,custom_mode=4)

    def tearDown(self):
        self.opener.assert_not_called()
        self.assertIsNone(self.reader.thread)

    def ingest(self,kind,**fields):
        self.reader.ingest(Message(kind=kind,**fields))

    def diagnostic(self,kind):
        return self.reader.snapshot()['voltage_diagnostics'][kind]

    def test_not_received_and_scope_are_explicit(self):
        s=self.reader.snapshot()
        self.assertEqual(s['voltage_v'],{'state':'UNKNOWN','value':None})
        for kind in ('SYS_STATUS','BATTERY_STATUS'):
            d=s['voltage_diagnostics'][kind]
            self.assertFalse(d['received']);self.assertIsNone(d['received_unix_s'])
            self.assertIsNone(d['age_s']);self.assertEqual(d['reason'],'NOT_RECEIVED')
        self.assertEqual(s['voltage_diagnostics']['scope'],'accepted_autopilot_messages')
        self.assertEqual(s['voltage_diagnostics']['autopilot_source'],[1,1])

    def test_raw_sys_values_and_exact_rejection_reasons(self):
        for raw,reason,state in [(65535,'UINT16_MAX_NOT_SENT','UNKNOWN'),(0,'NON_POSITIVE_VOLTAGE','UNKNOWN'),
                                 (-1,'NON_POSITIVE_VOLTAGE','UNKNOWN'),(65536,'OUT_OF_RANGE','UNKNOWN'),
                                 (24500,'VALID','FRESH')]:
            with self.subTest(raw=raw):
                self.ingest('SYS_STATUS',voltage_battery=raw)
                d=self.diagnostic('SYS_STATUS')
                self.assertEqual(d['voltage_battery_mv'],raw)
                self.assertEqual(d['reason'],reason)
                self.assertEqual(self.reader.snapshot()['voltage_v']['state'],state)
                self.assertEqual(d['received_unix_s'],self.wall)
                self.assertEqual(d['age_s'],0)
        s=self.reader.snapshot();self.assertEqual(s['voltage_v']['value'],24.5)
        self.assertEqual(s['voltage_diagnostics']['selected_source'],'SYS_STATUS')

    def test_host_timestamp_is_separate_from_monotonic_age(self):
        self.ingest('SYS_STATUS',voltage_battery=24500)
        self.now+=1.25;self.wall-=10000
        d=self.diagnostic('SYS_STATUS')
        self.assertEqual(d['received_unix_s'],1700000000.0)
        self.assertEqual(d['age_s'],1.25)
        self.assertEqual(d['reason'],'VALID')

    def test_timeout_boundary_and_reader_state_keep_old_raw_but_no_live_value(self):
        self.ingest('SYS_STATUS',voltage_battery=24500)
        self.now+=3.0
        self.assertEqual(self.reader.snapshot()['voltage_v']['state'],'FRESH')
        self.now+=.001
        d=self.diagnostic('SYS_STATUS')
        self.assertEqual(d['reason'],'MESSAGE_STALE')
        self.assertEqual(d['raw_validation_reason'],'VALID')
        self.assertEqual(d['voltage_battery_mv'],24500)
        self.assertEqual(self.reader.snapshot()['voltage_v'],{'state':'STALE','value':None})
        self.reader.state='DISCONNECTED'
        self.assertEqual(self.diagnostic('SYS_STATUS')['reason'],'READER_NOT_READING')
        self.assertIsNone(self.reader.snapshot()['voltage_diagnostics']['selected_source'])

    def test_battery_id_arrays_and_sentinel_causes(self):
        cases=[(1,[24000,65535],[],'NON_PRIMARY_BATTERY_ID'),
               (0,[],[],'EMPTY_VOLTAGES'),
               (0,[65535,4100],[4100],'FIRST_VOLTAGE_UINT16_MAX'),
               (0,[0,0],[],'NON_POSITIVE_TOTAL'),
               (0,[4100,4100,65535],[4100,0],'VALID')]
        for battery_id,raw,ext,reason in cases:
            with self.subTest(reason=reason):
                self.ingest('BATTERY_STATUS',id=battery_id,voltages=raw,voltages_ext=ext)
                d=self.diagnostic('BATTERY_STATUS')
                self.assertTrue(d['received']);self.assertEqual(d['id'],battery_id)
                self.assertEqual(d['voltages_mv'],raw);self.assertEqual(d['voltages_ext_mv'],ext)
                self.assertEqual(d['reason'],reason)
        s=self.reader.snapshot()
        self.assertEqual(s['voltage_v'],{'state':'FRESH','value':12.3})
        self.assertEqual(s['voltage_diagnostics']['selected_source'],'BATTERY_STATUS')

    def test_multiple_battery_ids_still_overwrite_one_cache_slot_as_before(self):
        self.ingest('SYS_STATUS',voltage_battery=24000)
        self.ingest('BATTERY_STATUS',id=0,voltages=[25000,65535])
        self.assertEqual(self.reader.snapshot()['voltage_v']['value'],25.0)
        self.ingest('BATTERY_STATUS',id=1,voltages=[12000,65535])
        s=self.reader.snapshot()
        self.assertEqual(s['voltage_v']['value'],24.0)
        self.assertEqual(s['voltage_diagnostics']['BATTERY_STATUS']['id'],1)
        self.assertEqual(s['voltage_diagnostics']['selected_source'],'SYS_STATUS')

    def test_stale_primary_not_replaced_by_unknown_fallback(self):
        self.ingest('BATTERY_STATUS',id=0,voltages=[24000,65535])
        self.now+=4
        self.ingest('SYS_STATUS',voltage_battery=65535)
        s=self.reader.snapshot()
        self.assertEqual(s['voltage_v'],{'state':'STALE','value':None})
        self.assertEqual(s['voltage_diagnostics']['state_source'],'BATTERY_STATUS')
        self.assertEqual(s['voltage_diagnostics']['SYS_STATUS']['reason'],'UINT16_MAX_NOT_SENT')

    def test_invalid_primary_can_fall_back_to_stale_sys(self):
        self.ingest('SYS_STATUS',voltage_battery=24000)
        self.now+=4
        self.ingest('BATTERY_STATUS',id=0,voltages=[65535])
        s=self.reader.snapshot()
        self.assertEqual(s['voltage_v'],{'state':'STALE','value':None})
        self.assertEqual(s['voltage_diagnostics']['state_source'],'SYS_STATUS')

    def test_missing_or_malformed_raw_values_are_not_stringified(self):
        self.ingest('SYS_STATUS')
        self.assertEqual(self.diagnostic('SYS_STATUS')['reason'],'DECODE_ERROR')
        self.ingest('SYS_STATUS',voltage_battery='invalid-untrusted-text')
        d=self.diagnostic('SYS_STATUS')
        self.assertEqual(d['reason'],'DECODE_ERROR');self.assertIsNone(d['voltage_battery_mv'])
        self.ingest('SYS_STATUS',voltage_battery=float('nan'))
        self.assertIsNone(self.diagnostic('SYS_STATUS')['voltage_battery_mv'])
        self.assertEqual(self.diagnostic('SYS_STATUS')['reason'],'INVALID_VALUE')
        self.ingest('SYS_STATUS',voltage_battery=10**400)
        self.assertIsNone(self.diagnostic('SYS_STATUS')['voltage_battery_mv'])
        self.assertEqual(self.diagnostic('SYS_STATUS')['reason'],'OUT_OF_RANGE')

    def test_messages_before_source_selection_or_from_other_components_are_not_received(self):
        reader=PixhawkTelemetry(opener=self.opener)
        reader.ingest(Message(kind='SYS_STATUS',voltage_battery=24000))
        self.assertFalse(reader.snapshot()['voltage_diagnostics']['SYS_STATUS']['received'])
        self.ingest('SYS_STATUS',component=190,voltage_battery=24000)
        self.ingest('BATTERY_STATUS',system=2,id=0,voltages=[24000])
        self.assertFalse(self.diagnostic('SYS_STATUS')['received'])
        self.assertFalse(self.diagnostic('BATTERY_STATUS')['received'])

    def test_existing_http_snapshot_exposes_diagnostics_without_starting_reader(self):
        self.ingest('SYS_STATUS',voltage_battery=65535)
        response=create_app(telemetry=self.reader).test_client().get('/api/telemetry')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['voltage_v'],{'state':'UNKNOWN','value':None})
        self.assertEqual(response.json['voltage_diagnostics']['SYS_STATUS']['voltage_battery_mv'],65535)
        self.assertEqual(response.json['voltage_diagnostics']['SYS_STATUS']['reason'],'UINT16_MAX_NOT_SENT')
