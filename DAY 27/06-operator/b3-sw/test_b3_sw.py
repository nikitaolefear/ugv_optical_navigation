import io
from unittest import TestCase

from pymavlink.dialects.v20 import ardupilotmega as mav
from decode import reconstruct
from owner_evidence import frame_fields, verify_stop
from usb_capture import Archive


def wire(stamp=32):
    return mav.MAVLink_vision_position_estimate_message(stamp,1,2,3,0,0,0).pack(
        mav.MAVLink(None, srcSystem=255,srcComponent=190))


def make(parts, end=None):
    rows=[dict(start=[0,100_000_000,10],mapping=dict(bus=1,device=2,endpoint=4))]
    for i,p in enumerate(parts):
        fields=dict(bus=1,dev=2,ep=4,kind=3,urb=i,captured=len(p),length=len(p),
                    payload=p.hex(),no_data=0,status=0)
        rows.extend([dict(fields,event='S',wall_ns=120_000_000+2*i),
                     dict(fields,event='C',wall_ns=120_000_001+2*i)])
    rows.append(dict(end=[6_000_000_000,6_100_000_000,6_000_000_010],
                     dropped=0,interrupted=False,undrained=0,failure=None))
    if end:
        rows[-1].update(end)
    return rows


class USBTests(TestCase):
    def test_one_split_and_coalesced_frames(self):
        frame=wire()
        for parts,count in (([frame],1),([frame[:5],frame[5:]],1),([wire()+wire(42)],2)):
            self.assertEqual(len(reconstruct(make(parts))),count)

    def test_crc(self):
        f=bytearray(wire()); f[12]^=1
        with self.assertRaisesRegex(ValueError,'CRC'):
            reconstruct(make([f]))

    def test_truncated_and_byte_tail(self):
        rows=make([wire()]); rows[1]['captured']-=1
        with self.assertRaises(ValueError):
            reconstruct(rows)
        with self.assertRaisesRegex(ValueError,'tail'):
            reconstruct(make([wire()[:-1]]))

    def test_capture_losses_and_clock(self):
        for changes in (dict(dropped=1),dict(undrained=1),dict(interrupted=True),
                        dict(end=[6_000_000_000,6_200_000_000,6_000_000_010])):
            with self.assertRaises(ValueError):
                reconstruct(make([wire()],changes))

    def test_completion_error_and_short_count(self):
        for change in (dict(status=-2),dict(length=1)):
            rows=make([wire()]); rows[2].update(change)
            with self.assertRaises(ValueError):
                reconstruct(rows)

    def test_filter_and_ambiguous_order(self):
        rows=make([wire(),wire()])
        rows[3]['ep']=rows[4]['ep']=2
        self.assertEqual(len(reconstruct(rows)),1)
        rows=make([wire(),wire()])
        order=[rows[1],rows[3],rows[4],rows[2]]
        for i,row in enumerate(order):
            row['wall_ns']=120_000_000+i
        rows[1:5]=order
        with self.assertRaisesRegex(ValueError,'order'):
            reconstruct(rows)

    def test_quota_reserves_footer(self):
        stream=io.BytesIO(); a=Archive(stream,8192)
        with self.assertRaises(OverflowError):
            a.write({'payload':'x'*8000})
        a.write(dict(end=[1,2,3], failure='CAPTURE_QUOTA'),final=True)
        self.assertLessEqual(len(stream.getvalue()),8192)

    def test_all_stop_cases_and_late_write(self):
        packet=wire(); rows=make([packet])
        trace=[frame_fields(packet,enter_ns=19_000_000,exit_ns=20_000_000,written=len(packet))]
        for s in ('revoke','client_exit','expiry'):
            timing=dict(scenario=s,lease_start_ns=1,trigger_ns=20_000_000,last_renew_ns=20_000_000,
                        gate_ns=21_000_000,barrier_ns=22_000_000,safety_lost=False,trace_drops=0)
            self.assertGreater(verify_stop(rows,reconstruct(rows),trace,timing),0)
            too_late=dict(timing,gate_ns=19_000_000,trigger_ns=18_000_000,last_renew_ns=18_000_000)
            with self.assertRaises(ValueError):
                verify_stop(rows,reconstruct(rows),trace,too_late)

    def test_trace_payload_mismatch(self):
        packet=wire(); rows=make([wire(99)])
        trace=[frame_fields(packet,enter_ns=19_000_000,exit_ns=20_000_000,written=len(packet))]
        timing=dict(scenario='revoke',lease_start_ns=1,trigger_ns=20_000_000,
                    gate_ns=21_000_000,barrier_ns=22_000_000,safety_lost=False,trace_drops=0)
        with self.assertRaisesRegex(ValueError,'sequences'):
            verify_stop(rows,reconstruct(rows),trace,timing)

    def test_no_out_traffic_is_inconclusive(self):
        with self.assertRaisesRegex(ValueError,"no complete OUT"):
            reconstruct(make([]))

    def test_trace_safety_loss_blocks_acceptance(self):
        packet=wire(); rows=make([packet])
        trace=[frame_fields(packet,enter_ns=19_000_000,exit_ns=20_000_000,written=len(packet))]
        evidence=dict(scenario='revoke',lease_start_ns=1,trigger_ns=20_000_000,
                      gate_ns=21_000_000,barrier_ns=22_000_000,trace_drops=0)
        for cause in ('heartbeat','pwm'):
            with self.subTest(cause=cause), self.assertRaisesRegex(ValueError,'unsafe'):
                verify_stop(rows,reconstruct(rows),trace,dict(evidence,safety_lost=True))
