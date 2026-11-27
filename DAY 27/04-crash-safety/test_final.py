import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest

from disarm_control.stage3 import Stage3, Trace
from disarm_control.stage3_baseline import BaselineStore
from disarm_control.ipc import peer_generation
from stage3_tools.clock_mapping import Anchor, map_window
from stage3_tools.dataflash_checks import observe_catalog
from test_integration import Clock


class FinalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root/'baseline'
        self.old = dict(source=1, log_disarmed=0)

    def test_missing_and_corrupt_baseline_block(self):
        self.assertTrue(BaselineStore(self.path,'original').blocked)
        store = BaselineStore.provision(self.path,'original',self.old)
        self.assertFalse(store.blocked)
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
        envelope = json.loads(self.path.read_text())
        envelope['record']['baseline']['source'] = 2
        self.path.write_text(json.dumps(envelope))
        self.assertTrue(BaselineStore(self.path,'original').blocked)

    def test_crash_between_write_and_readback_and_during_rollback(self):
        for state in ('PENDING', 'RESTORING'):
            with self.subTest(state=state):
                self.path = self.root/state
                BaselineStore.provision(self.path,'original',self.old)
                code = '''
import sys, time
from disarm_control.stage3_baseline import BaselineStore
s=BaselineStore(sys.argv[1],'original')
s.start('killed-owner',dict(log_disarmed=0,source=1))
if sys.argv[2] == 'RESTORING':
    s.checkpoint([dict(kind='log',value=1)],'RESTORING')
print('durable',flush=True)
time.sleep(30)
'''
                with subprocess.Popen([sys.executable,'-u','-c',code,str(self.path),state],
                                      stdout=subprocess.PIPE) as child:
                    try:
                        self.assertEqual(child.stdout.readline(),b'durable\n')
                    finally:
                        os.kill(child.pid,signal.SIGKILL)
                    self.assertEqual(child.wait(timeout=2),-signal.SIGKILL)
                reopened = BaselineStore(self.path,'original')
                self.assertTrue(reopened.blocked)
                self.assertEqual(reopened.record['baseline'],self.old)
                self.assertEqual(reopened.record['rollback'],state)
                with self.assertRaises(RuntimeError): reopened.start('new',self.old)

    def test_two_stores_cannot_open_same_baseline(self):
        first = BaselineStore.provision(self.path, 'initial', self.old)
        stale = BaselineStore(self.path, 'initial')
        first.start('first', self.old)
        with self.assertRaises(RuntimeError):
            stale.start('stale', self.old)
        self.assertEqual(BaselineStore(self.path, 'initial').record['session'], 'first')

    def test_durable_completion_requires_independent_profile_proof(self):
        store=BaselineStore.provision(self.path,'original',self.old)
        store.start('session',self.old)
        store.note([{'kind':'log','value':0}],'COMPLETED')
        store.updates.join()
        self.assertFalse(BaselineStore(self.path,'original').blocked)
        self.assertTrue(self.path.exists())

    def test_stuck_write_stop_closes_without_claiming_barrier(self):
        clock=Clock()
        trace=Trace(self.root/'trace')
        self.addCleanup(trace.close)
        owner=Stage3(clock=clock.monotonic,trace=trace,baseline=lambda:dict(source=2,log_disarmed=1),
                     guard=lambda:True,action=lambda *a:None,restore_eligible=lambda:True,
                     recording_proven=lambda:True)
        token=owner.dispatch((1,2,3),'S3_OPEN',{})['token']
        owner.dispatch((1,2,3),'S3_START',{'token':token})
        for n in range(1,61):
            clock.t=n/2
            owner.dispatch((1,2,3),'S3_RENEW',{'token':token,'phase':'B' if n==60 else 'A','seq':n})
        entered, release = threading.Event(),threading.Event()
        def writer(fd,data):
            entered.set()
            release.wait(2)
            return len(data)
        t=threading.Thread(target=lambda:owner.write_tx(writer,0,b'packet'))
        t.start()
        try:
            self.assertTrue(entered.wait(1))
            receipt=owner.dispatch((1,2,3),'S3_STOP',{'token':token})
            self.assertTrue(receipt['stopped'])
            self.assertFalse(receipt['tx_barrier'])
            with self.assertRaises(OSError): owner.write_tx(writer,0,b'again')
        finally:
            release.set()
            t.join(2)
        self.assertTrue(owner.status()['tx_barrier'])
        self.assertFalse(owner.status()['driver_empty'])

    def test_reused_pid_generation_and_unknown_src(self):
        trace=Trace(self.root/'trace')
        self.addCleanup(trace.close)
        clock=Clock()
        old=self.old.copy()
        owner=Stage3(clock=clock.monotonic,trace=trace,baseline=lambda:old,guard=lambda:True,
                     action=lambda *a:None,restore_eligible=lambda:True)
        generation=peer_generation(os.getpid())
        token=owner.dispatch((1000,123,generation),'S3_OPEN',{})['token']
        with self.assertRaisesRegex(RuntimeError,'SESSION_REFUSED'):
            owner.dispatch((1000,123,generation+1),'S3_RENEW',{'token':token,'phase':'A','seq':1})
        other=Stage3(clock=clock.monotonic,trace=trace,baseline=lambda:dict(source=None,log_disarmed=0),
                     guard=lambda:True,action=lambda *a:None,restore_eligible=lambda:True)
        with self.assertRaisesRegex(RuntimeError,'UNVERIFIED_BASELINE'): other.dispatch((1,2,3),'S3_OPEN',{})

    def test_clock_error_and_resets(self):
        a=Anchor('pi1','fc1',0,.02,100.01,10010000)
        b=Anchor('pi1','fc1',181,181.02,281.01,191010000)
        result=map_window(1,180,[a,b])
        self.assertAlmostEqual(result['start']['error_s'],.011099)
        for other in (
            Anchor('pi1','fc2',181,181.02,281.01,191010000),
            Anchor('pi2','fc1',181,181.02,281.01,191010000),
            Anchor('pi1','fc1',181,181.02,290,191010000),
            Anchor('pi1','fc1',181,181.02,281.01,1000),
            Anchor('pi1','fc1',181,182,281.5,191010000),
        ):
            with self.assertRaises(ValueError): map_window(1,180,[a,other])

    def test_dataflash_evidence_loss_always_attempts_end(self):
        ops=[]
        def exchange(op):
            ops.append(op)
            return None if op.endswith('LIST') else True
        with self.assertRaises(ValueError): observe_catalog(exchange,lambda:True,test_active=False)
        self.assertEqual(ops,['LOG_REQUEST_LIST','LOG_REQUEST_END'])
        with self.assertRaises(ValueError): observe_catalog(exchange,lambda:True,test_active=True)
        self.assertEqual(len(ops),2)

    def test_trace_enospc_is_sticky(self):
        from unittest.mock import patch
        trace=Trace(self.root/'trace')
        self.addCleanup(trace.close)
        with patch('disarm_control.stage3.os.write',side_effect=OSError(28,'ENOSPC')):
            trace.push('synthetic')
            trace.worker.join(1)
        self.assertTrue(trace.failed)
        self.assertFalse(trace.push('late'))
