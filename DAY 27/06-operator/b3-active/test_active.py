import threading
import time
import unittest

from disarm_control.b3_lease import BenchLease
from disarm_control.stage3 import Refused


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.now=1.
        self.writes=[]
        self.healthy=True
        self.alive=True
        self.approved=False
        self.id={'owner_session':'session'}
        self.pose=dict(acquired_at=1.,sequence=1)
        self.peer=(100,200,300)
        self.l=BenchLease(lambda:self.now,lambda:self.id.copy(),lambda p:self.alive,
                          lambda _:self.approved,lambda:self.healthy,lambda:self.pose,
                          self.encode,self.write,lambda:int(self.now*1e9))

    @staticmethod
    def encode(_):
        return b'abc',dict(id=331,sys=1,comp=191,seq=5,usec=1000000)

    def write(self, fd, raw):
        self.writes.append(raw)
        return len(raw)

    def start(self, scenario='revoke'):
        self.approved=True
        self.l.dispatch(self.peer,'S3_B3_OPEN',dict(scenario=scenario,nonce='a'*32))
        self.l.tick(5)

    def test_default_closed_and_separate_approval(self):
        self.l.tick(5)
        with self.assertRaises(Refused):
            self.l.dispatch(self.peer,'S3_B3_OPEN',dict(scenario='revoke',nonce='a'*32))
        self.assertEqual(self.writes,[])

    def test_manual_expiry_peer_exit(self):
        for scenario in ('revoke','client_exit','expiry'):
            with self.subTest(scenario=scenario):
                self.setUp()
                self.start(scenario)
                if scenario=='revoke':
                    self.l.dispatch(self.peer,'S3_B3_REVOKE',{})
                elif scenario=='client_exit':
                    self.alive=False
                else:
                    self.now=1.75
                self.l.tick(5)
                self.assertEqual(self.l.stop['reason'],scenario)
                self.assertEqual(len(self.writes),1)
                self.assertFalse(self.l.active())

    def test_identity_and_send_time_guard(self):
        for change in ('session','heartbeat','pwm','transport','stale'):
            with self.subTest(change=change):
                self.setUp()
                self.start()
                self.now=1.15
                self.pose=dict(acquired_at=1.15,sequence=2)
                if change=='session': self.id={'owner_session':'different'}
                elif change=='stale': self.pose['acquired_at']=0
                else: self.healthy=False
                self.l.tick(5)
                self.assertEqual(len(self.writes),1)
                self.assertFalse(self.l.active())

    def test_guard_rechecked_after_encoding(self):
        self.approved=True
        def fail(_):
            self.healthy=False
            return self.encode(None)
        self.l.encode=fail
        self.start()
        self.assertFalse(self.writes)
        self.assertEqual(self.l.stop['reason'],'guard_lost')

    def test_simultaneous_revoke_and_syscall_serialized(self):
        enter, release=threading.Event(),threading.Event()
        def write(fd, raw):
            enter.set()
            self.assertTrue(release.wait(1))
            return self.write(fd,raw)
        self.l.write=write
        self.approved=True
        self.l.dispatch(self.peer,'S3_B3_OPEN',dict(scenario='revoke',nonce='a'*32))
        a=threading.Thread(target=self.l.tick,args=(5,)); a.start()
        self.assertTrue(enter.wait(1))
        b=threading.Thread(target=self.l.dispatch,args=(self.peer,'S3_B3_REVOKE',{})); b.start()
        time.sleep(.015); self.assertTrue(b.is_alive())
        release.set(); a.join(1); b.join(1)
        self.l.tick(5)
        self.assertEqual(len(self.writes),1)
        self.assertFalse(self.l.active())

    def test_partial_and_io_failure_no_retry_trace_retained(self):
        for result in (1,None):
            self.setUp()
            def write(*args):
                if result is None: raise OSError(5,'synthetic')
                return result
            self.l.write=write
            self.start()
            self.l.tick(5)
            self.assertTrue(self.l.fault)
            row=self.l.dispatch(self.peer,'S3_B3_ROW',{'index':0})
            self.assertEqual(row['written'], -1 if result is None else result)
            self.assertEqual(self.l.stop['reason'],'serial_write_fault')

    def test_hard_deadline_and_trace_hash(self):
        self.start()
        row=self.l.rows[0]
        self.assertEqual(row['sha256'],'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
        self.now=5.95
        self.l.lease['deadline']=100
        self.now=6
        self.l.tick(5)
        self.assertFalse(self.l.active())
        self.assertEqual(self.l.stop['reason'],'hard_limit')

    def test_alignment_not_relaxed(self):
        self.l.qualified=lambda:False
        self.start()
        self.assertEqual(self.writes,[])
        self.assertEqual(self.l.stop['reason'],'alignment_unverified')

    def test_existing_full_command_accepts_combined_profile(self):
        import os
        from pathlib import Path
        import shlex
        from b3_profile import IDENTITIES,render,resolve_identities
        from stop_latch_test.hold_runner import parse_args

        ref=Path(os.environ['B2_REFERENCE']).read_text()
        base=next(l.split('=',1)[1].removesuffix('/owner') for l in ref.splitlines()
                  if l.startswith('WorkingDirectory='))
        rendered=render(ref,base,'/overlay','/log-marker','/journal','/b3-marker')
        args=shlex.split(rendered.splitlines()[-1].removeprefix('ExecStart='))
        args=resolve_identities(args,dict(zip(IDENTITIES.values(),(str(os.getuid()+1),'123'))))
        parsed=parse_args(args[4:])
        self.assertTrue(parsed.stage3_log_approval_file and parsed.stage3_b3_approval_file)
        self.assertTrue(parsed.externalnav_observe_only)
        self.assertFalse(parsed.externalnav_tx or parsed.nav_bench_tx or parsed.nav_mission_tx)
