import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from disarm_control.stage3_baseline import BaselineStore
from disarm_control.stage3_log_transport import Job, LogTransport
from disarm_control.stage3_observers import ClockProbe, SourceObserver
from stage3_tools.recover_stage3 import recover


class Message:
    def __init__(self, kind, source=(1, 1), **fields):
        self.kind, self.source = kind, source
        vars(self).update(fields)
    def get_type(self): return self.kind
    def get_srcSystem(self): return self.source[0]
    def get_srcComponent(self): return self.source[1]


class ClockTests(unittest.TestCase):
    def test_nonce_target_delay_and_source_replay(self):
        mono = [1_000_000_000]
        utc = lambda: 1_800_000_000_000_000_000 + mono[0]
        probe = ClockProbe(time_ns=utc, mono_ns=lambda:mono[0])
        requests=[]
        probe.poll(requests.append)
        self.assertFalse(probe.poll(requests.append))
        mono[0] += 20_000_000
        probe.observe(Message('TIMESYNC',source=(2,1),tc1=5_000_000_000,ts1=requests[0]))
        self.assertIsNone(probe.anchor)
        probe.observe(Message('TIMESYNC',tc1=5_000_000_000,ts1=requests[0]+1))
        self.assertIsNone(probe.anchor)
        probe.observe(Message('TIMESYNC',tc1=5_000_000_000,ts1=requests[0]))
        self.assertEqual(probe.anchor['error_ns'],15_000_000)
        parser=SourceObserver(probe,provenance=lambda:True)
        parser.observe(Message('STATUSTEXT',text='S3SRC1 1 5000'))
        parser.observe(Message('STATUSTEXT',text='S3SRC1 0 5000'))
        parser.observe(Message('STATUSTEXT',text='S3SRC1 2 5001',source=(3,1)))
        self.assertEqual(parser.read(),2)
        mono[0] += 1_000_000_000
        self.assertIsNone(parser.read())

    def test_utc_step_and_fc_boot_reset_fail_closed(self):
        mono=[1_000_000_000]
        offset=[0]
        probe=ClockProbe(time_ns=lambda:mono[0]+offset[0],mono_ns=lambda:mono[0])
        ids=[]
        probe.poll(ids.append)
        offset[0]=1_000_000_000
        mono[0]+=10_000_000
        probe.observe(Message('TIMESYNC',tc1=15_000_000_000,ts1=ids[0]))
        self.assertTrue(probe.fault)
        probe=ClockProbe()
        probe.observe(Message('SYSTEM_TIME',time_boot_ms=6000))
        probe.observe(Message('SYSTEM_TIME',time_boot_ms=30))
        self.assertTrue(probe.fault)

    def test_lua_on_mock_firmware_api(self):
        script=Path(__file__).parent/'candidate/stage3_tools/stage3_src_observer.lua'
        lua="""ahrs={get_posvelyaw_source_set=function() return 1 end}
gcs={send_text=function(_,lv,s) assert(lv==6 and s=='S3SRC1 1 1234') end}
function millis() return {toint=function() return 1234 end} end
local update,ms=dofile(arg[1]); assert(ms==500)
local again,period=update(); assert(again==update and period==500)
"""
        subprocess.run(['lua','-',str(script)],input=lua,text=True,check=True)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.now=0
        self.transport=LogTransport(lambda:self.now)
        self.calls=[]

    def tick(self, **options):
        self.transport.tick(lambda *x:self.calls.append(x),
                            **dict(dict(safe=True,connected=True,priority_clear=True),**options))

    def test_catalog_and_end_and_explicit_prefix(self):
        self.transport.start(Job(),permitted=True)
        self.tick()
        self.transport.observe(Message('LOG_ENTRY',num_logs=1,id=74,size=1500,time_utc=315532800))
        self.tick()
        self.assertEqual(self.calls,[('list',),('end',)])
        self.assertEqual(self.transport.result['catalog'][74][0],1500)
        self.transport.start(Job(74,100),permitted=True)
        self.tick()
        self.transport.observe(Message('LOG_DATA',id=74,ofs=90,count=10,data=b'x'*10))
        self.assertEqual(len(self.transport.data),0)
        for offset,count in [(0,90),(90,10)]:
            self.transport.observe(Message('LOG_DATA',id=74,ofs=offset,count=count,data=b'x'*count))
        self.tick()
        self.assertEqual(self.calls[-2:],[('data',74,0,100),('end',)])
        self.assertEqual(self.transport.result['bytes_received'],100)

    def test_conflict_guard_loss_owes_end_before_reuse(self):
        self.transport.start(Job(),permitted=True)
        self.tick()
        with self.assertRaises(RuntimeError):
            self.transport.start(Job(74,10),permitted=True)
        self.tick(safe=False,connected=False)
        self.assertTrue(self.transport.busy)
        self.tick(safe=False,priority_clear=False)
        self.assertEqual(self.calls,[('list',)])
        self.tick(safe=False)
        self.assertEqual(self.calls[-1],('end',))
        self.assertFalse(self.transport.busy)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/'baseline'
        self.values={'source':2,'log_disarmed':1}
        self.store=BaselineStore.provision(self.path,'normal',dict(source=1,log_disarmed=0))
        self.store.start('session',dict(source=1,log_disarmed=0))
        self.events=[]
        self.safety_ok=True
        self.profile_ok=True

    def safety(self):
        return dict.fromkeys(('isolated','hold','disarmed','inhibit','stop_latch','tx_barrier','driver_empty','no_new_lease'),self.safety_ok)

    def read(self,name): return self.values[name]
    def restore(self,name,value): self.values[name]=value
    def restore_and_verify_profile(self,profile): return self.profile_ok

    def run_recovery(self, approval=True):
        return recover(self.path,'normal',self,authorize=lambda *a:approval,
                       audit=lambda k,**kw:self.events.append((k,kw)))

    def test_authorized_order_and_durable_completion(self):
        self.assertTrue(self.run_recovery())
        self.assertEqual([x[1]['name'] for x in self.events if x[0]=='restore_intent'],
                         ['source','log_disarmed'])
        self.assertFalse(BaselineStore(self.path,'normal').blocked)

    def test_unauthorized_unsafe_unknown_src(self):
        with self.assertRaises(PermissionError): self.run_recovery(False)
        self.safety_ok=False
        with self.assertRaises(RuntimeError): self.run_recovery()
        self.safety_ok=True
        self.values['source']=None
        with self.assertRaisesRegex(RuntimeError,'readback unavailable'): self.run_recovery()
        self.assertFalse(any(k=='restore_intent' for k,_ in self.events))
        self.assertTrue(BaselineStore(self.path,'normal').blocked)

    def test_failed_profile_stays_unfinished(self):
        self.profile_ok=False
        with self.assertRaisesRegex(RuntimeError,'profile restore'): self.run_recovery()
        self.assertTrue(BaselineStore(self.path,'normal').blocked)
