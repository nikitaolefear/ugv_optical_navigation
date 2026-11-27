import os
from pathlib import Path
import pty
import select
import subprocess
import tempfile
import threading
import time
import tty
import unittest
import uuid

from pymavlink.dialects.v20 import ardupilotmega as mav
from disarm_control.b3_lease import BenchLease, watch_pid
from disarm_control.b3_probe import probe_packet, send_guard
from disarm_control.ipc import OwnerClient, OwnerServer, SocketPolicy, peer_generation
from disarm_control.log_disarmed import GUARDS
from disarm_control.stage3 import Refused
from owner_evidence import Failed, Inconclusive, frame_fields, verify_stop
from decode import reconstruct
from test_b3_sw import make, wire
import test_active as harness


class AuditTests(unittest.TestCase):
    def test_false_pass_and_missing_external_trigger(self):
        for actual in ('pose_stale','guard_lost'):
            with self.assertRaisesRegex(Failed,'unexpected'):
                verify_stop([],[],[],dict(scenario='revoke',reason=actual))
        with self.assertRaises(Inconclusive):
            verify_stop([],[],[],dict(scenario='revoke',reason='revoke'))

    def test_independent_revoke_chronology_and_client_exit_delay(self):
        packet=wire()
        rows=make([packet])
        # Shift capture timestamps so the artificial case includes the 10s baseline.
        rows[-1]['end']=[26_000_000_000,26_100_000_000,26_000_000_010]
        rows[1]['wall_ns']+=12_000_000_000
        rows[2]['wall_ns']+=12_000_000_000
        trace=[dict(frame_fields(packet,enter_ns=12_019_000_000,
                                 exit_ns=12_020_000_000,written=len(packet)),lease='test')]
        start=11_700_000_000
        stop=dict(scenario='revoke',reason='revoke',lease='test',peer=[1,2,3],
                  lease_start_ns=start,last_syscall_ns=12_020_000_000,
                  gate_ns=12_021_000_000,barrier_ns=12_022_000_000,
                  safety_lost=False,trace_drops=0,
                  ipc=dict(received_ns=12_020_100_000,lock_wait_ns=12_020_200_000,
                           lock_acquired_ns=12_020_900_000))
        external=dict(event='client_revoke',lease='test',peer=[1,2,3],
                      before_ns=12_020_000_000,after_ns=12_025_000_000)
        verify_stop(rows,reconstruct(rows),trace,stop,external)
        with self.assertRaises(Inconclusive):
            verify_stop(rows,reconstruct(rows),trace,stop,dict(external,before_ns=12_024_000_000))
        # 700 ms between independent trigger and the Owner's observation must count.
        stop.update(scenario='client_exit',reason='client_exit',lease_start_ns=11_000_000_000)
        external=dict(event='supervisor_pidfd_exit',lease='test',peer=[1,2,3],
                      before_ns=11_321_000_000,exit_observed_ns=11_350_000_000,signal_sent=True)
        with self.assertRaisesRegex(Failed,'budget'):
            verify_stop(rows,reconstruct(rows),trace,stop,external)
        with self.assertRaises(Inconclusive):
            verify_stop(rows,reconstruct(rows),trace,stop,dict(external,signal_sent=False))

    def test_guard_independent_predicates(self):
        good=dict.fromkeys(GUARDS,True)
        pwm={str(i):dict(state='FRESH',pwm_us=999) for i in (5,6)}
        cfg=dict(heartbeat_age=.3,src=1,transport_fault=False,conflicting=False,
                 disarm_pending=False,observe_only=True)
        self.assertTrue(send_guard(good,pwm,**cfg))
        for key in GUARDS:
            with self.subTest(key=key):
                self.assertFalse(send_guard(dict(good,**{key:False}),pwm,**cfg))
        for key,val in dict(heartbeat_age=1.01,src=2,transport_fault=True,conflicting=True,
                            disarm_pending=True,observe_only=False).items():
            with self.subTest(key=key):
                self.assertFalse(send_guard(good,pwm,**dict(cfg,**{key:val})))
        for n in ('5','6'):
            for sample in (dict(state='STALE',pwm_us=999),dict(state='FRESH',pwm_us=2000)):
                with self.subTest(n=n,sample=sample):
                    self.assertFalse(send_guard(good,dict(pwm,**{n:sample}),**cfg))

    def test_exception_each_callback_and_cleanup(self):
        def fail(*_):
            raise OSError('synthetic')
        for callback in ('clock','identity','alive','approved','guard','pose','encode','qualified','write','ns','drop_watch'):
            with self.subTest(callback=callback):
                h=harness.LeaseTests('test_default_closed_and_separate_approval')
                h.setUp()
                h.start()
                setattr(h.l,callback,fail if callback!='guard' else lambda: {}['missing'])
                h.now=1.15
                h.pose.update(sequence=2,acquired_at=1.15)
                if callback=='drop_watch':
                    h.approved=False
                if callback=='write':
                    # OSError is an explicit serial write failure; exercise an internal exception.
                    h.l.write=lambda *_: {}['missing']
                h.l.tick(5)
                self.assertFalse(h.l.active())
                self.assertTrue(h.l.fault)
                self.assertEqual(h.l.failure['reason'],'internal_fault')
                with self.assertRaises(Refused):
                    h.l.dispatch(h.peer,'S3_B3_OPEN',dict(scenario='revoke',nonce='b'*32))

    def test_trace_failure_latches_after_write(self):
        class Broken(list):
            def append(self,row):
                raise OSError('trace fault')
        h=harness.LeaseTests('test_default_closed_and_separate_approval')
        h.setUp();h.l.rows=Broken();h.start()
        self.assertFalse(h.l.active())
        self.assertEqual(h.l.trace_drops,1)
        self.assertEqual(h.l.failure['callback'],'trace')

    def test_pidfd_observes_exited_unreaped_child(self):
        go=os.pipe()
        pid=os.fork()
        if pid==0:
            os.close(go[1])
            os.read(go[0],1)
            os._exit(0)
        os.close(go[0])
        fd,poll=watch_pid((os.getuid(),pid,0))
        try:
            os.close(go[1])
            self.assertTrue(poll.poll(1000))
            self.assertEqual(Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[0],'Z')
        finally:
            os.close(fd)
            os.waitpid(pid,0)

    def test_pty_owner_ipc_real_creds_reentrant_lock_probe(self):
        # PTY is the only opened device. No production Owner, tty, or vehicle.
        master,slave=pty.openpty()
        self.addCleanup(os.close,master);self.addCleanup(os.close,slave)
        tty.setraw(slave)
        now=time.monotonic()
        peer=(os.getuid(),os.getpid(),peer_generation(os.getpid()))
        owner_lock=threading.RLock()
        enc=mav.MAVLink(None,srcSystem=1,srcComponent=191)
        def guard():
            with owner_lock:
                return send_guard(dict.fromkeys(GUARDS,True),
                                  {str(i):dict(state='FRESH',pwm_us=999) for i in (5,6)},
                                  heartbeat_age=.1,src=1,transport_fault=False,conflicting=False,
                                  disarm_pending=False,observe_only=True)
        lease=BenchLease(time.monotonic,lambda:{'owner_session':'fixture'},lambda p:p==peer,
                         lambda l:l['peer']==peer,guard,lambda:dict(sequence=1,acquired_at=now),
                         lambda p:probe_packet(p,enc),os.write)
        def dispatch(peer,op,payload,received):
            waiting=time.monotonic_ns()
            with owner_lock:
                return lease.dispatch(peer,op,payload,dict(received_ns=received,lock_wait_ns=waiting,
                                                         lock_acquired_ns=time.monotonic_ns()))
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'socket'
            server=OwnerServer(object(),path,allowed_uid=os.getuid(),disarm_request=lambda *_:None,
                               stage3_dispatch=dispatch,log_enabled=True,b3_enabled=True)
            server.start()
            try:
                c=OwnerClient(path,policy=SocketPolicy(os.getuid()))
                def call(op,payload):
                    return c.call('S3_B3_'+op,{'id':1,'username':'fixture'},uuid.uuid4().hex,payload=payload)
                lease.tick(slave)
                self.assertFalse(select.select([master],[],[],0)[0])
                self.assertTrue(call('OPEN',dict(scenario='revoke',nonce='e'*32))['ok'])
                self.assertFalse(call('RENEW',{})['ok'])
                with owner_lock:
                    lease.tick(slave)
                self.assertTrue(select.select([master],[],[],.1)[0])
                m=mav.MAVLink(None).parse_char(os.read(master,512))
                self.assertEqual((m.get_msgId(),m.frame_id,m.child_frame_id,m.quality),
                                 (331,mav.MAV_FRAME_LOCAL_NED,mav.MAV_FRAME_BODY_FRD,-1))
                self.assertTrue(call('REVOKE',{})['ok'])
                stop=call('STATUS',{})['command']['stop']
                self.assertLessEqual(stop['ipc']['received_ns'],stop['ipc']['lock_acquired_ns'])
                self.assertLessEqual(stop['last_syscall_ns'],stop['barrier_ns'])
                self.assertFalse(stop['safety_lost'])
                lease.tick(slave)
                self.assertFalse(select.select([master],[],[],0)[0])
            finally:
                server.close()

    def test_rollback_refuses_unfinished_recovery(self):
        from b3_profile import rollback_allowed
        safe=dict(active=None,queued=0,dataflash='IDLE',b3_active=False,
                  unfinished=None,recovery_required=False)
        self.assertTrue(rollback_allowed(safe))
        for key,value in dict(unfinished={'phase':'enable_intent'},recovery_required=True,
                              active='catalog',queued=1,dataflash='ACTIVE',b3_active=True).items():
            self.assertFalse(rollback_allowed(dict(safe,**{key:value})))

    def test_revoke_before_activation_keeps_stop_record(self):
        h=harness.LeaseTests('test_default_closed_and_separate_approval')
        h.setUp();h.approved=True
        h.l.dispatch(h.peer,'S3_B3_OPEN',dict(scenario='revoke',nonce='a'*32))
        h.l.dispatch(h.peer,'S3_B3_REVOKE',{})
        self.assertEqual(h.l.stop['reason'],'revoke')
        self.assertIsNone(h.l.stop['lease_start_ns'])
        self.assertFalse(h.l.active())
        self.assertFalse(h.writes)

    def test_marker_revocation_during_alignment_or_log_conflict(self):
        h=harness.LeaseTests('test_default_closed_and_separate_approval')
        h.setUp()
        def qualified():
            h.approved=False
            return True
        h.l.qualified=qualified;h.start()
        self.assertFalse(h.writes)
        self.assertEqual(h.l.stop['reason'],'guard_lost')
        state=dict.fromkeys(GUARDS,True)
        pwm={str(i):dict(state='FRESH',pwm_us=999) for i in (5,6)}
        self.assertFalse(send_guard(state,pwm,heartbeat_age=.2,src=1,transport_fault=False,
                                    conflicting=True,disarm_pending=False,observe_only=True))

    def test_common_uid_cannot_reuse_other_process_marker(self):
        from disarm_control.b3_probe import approval_record
        lease=dict(identity=dict(hardware='a',pi_boot='b',fc_boot='c',owner_session='d'),
                   peer=(42,99,500),scenario='revoke',nonce='a'*32)
        self.assertNotEqual(approval_record(lease),approval_record(dict(lease,peer=(42,100,500))))
        self.assertNotEqual(approval_record(lease),approval_record(dict(lease,peer=(42,99,501))))
