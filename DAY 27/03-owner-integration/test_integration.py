import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from disarm_control.ipc import OwnerClient, OwnerServer
from disarm_control.stage3 import Refused, Stage3, Trace
from dataflash_checks import join_vpe_visp, prove_prefix
from integrated_runner import Stage3Client, exercise


class Clock:
    t = 0.0
    step = lambda self: None
    def monotonic(self):
        return self.t
    def time_ns(self):
        return int(self.t * 1e9)
    def sleep(self, seconds):
        self.t += seconds
        self.step()


class Message:
    def __init__(self, kind, **attrs):
        self.kind = kind
        self.__dict__.update(attrs)
    def get_type(self): return self.kind
    def get_msgId(self): return 22 if self.kind == 'PARAM_VALUE' else 77
    def get_srcSystem(self): return 1
    def get_srcComponent(self): return 1
    def get_msgbuf(self): return b'fixture'
    def to_dict(self): return vars(self)


class Fixture:
    def __init__(self, trace, clock, *, log=0, source=1):
        self.clock = clock
        self.trace = trace
        self.live = dict(log_disarmed=log, source=source)
        self.safe = True
        self.actions = []
        self.packets = []
        self.replies = []
        self.drain = True
        self.drop_param = False
        self.bad_ack = False
        self.apply_src = True
        self.stage = Stage3(clock=clock.monotonic, trace=trace, baseline=lambda: dict(self.live),
                            guard=lambda: self.safe, action=self.action, restore_eligible=lambda: self.safe,
                            recording_proven=lambda: True, driver_queue_empty=lambda fd: self.drain,
                            source_reader=lambda: self.live['source'])
        clock.step = self.step

    def action(self, fd, kind, value):
        self.actions.append((kind, value))
        if kind == 'log':
            self.live['log_disarmed'] = value
        if kind == 'log_read' and not self.drop_param:
            self.replies.append(Message('PARAM_VALUE', param_id='LOG_DISARMED', param_value=self.live['log_disarmed']))
        if kind == 'src':
            if not self.bad_ack and self.apply_src:
                self.live['source'] = value
            self.replies.append(Message('COMMAND_ACK', command=42007, result=2 if self.bad_ack else 0,
                                        target_system=255, target_component=190))

    def writer(self, fd, packet):
        self.packets.append(self.clock.monotonic())
        return len(packet)

    def step(self):
        self.stage.tick(0)
        for rx in self.replies:
            self.stage.observe(rx)
        self.replies.clear()
        if self.stage.can_tick():
            self.stage.write_tx(self.writer, 0, bytes(12))


class Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.trace = Trace(self.root/'trace.jsonl')
        self.addCleanup(self.trace.close)
        self.clock = Clock()
        self.f = Fixture(self.trace, self.clock)

    def server(self, *, uid=None, dispatch=True):
        engine = SimpleNamespace(request=lambda *a: 'disarm', status=lambda *a: 'regular-status')
        path = self.root / 'owner.sock'
        s = OwnerServer(engine, path, allowed_uid=uid,
                        stage3_dispatch=self.f.stage.dispatch if dispatch else None)
        s.start()
        self.last_server = s
        self.addCleanup(lambda: s.close() if s.path.exists() else None)
        return OwnerClient(path, timeout=.5)

    def direct(self, op, payload=None, who=None):
        payload = dict(payload or {})
        if op == 'S3_RENEW':
            payload.setdefault('seq', self.f.stage.renew_seq + 1)
        return self.f.stage.dispatch(who or (1000, 10), op, payload)

    def prepared(self):
        self.f.live['log_disarmed'] = 1
        token = self.direct('S3_OPEN')['token']
        self.direct('S3_START', {'token': token})
        for i in range(60):
            self.clock.t = (i + 1) / 2
            self.direct('S3_RENEW', {'token': token, 'phase': 'A'})
        self.direct('S3_RENEW', {'token': token, 'phase': 'B'})
        return token

    def test_actual_unix_ipc_all_phases_restore_and_original_status(self):
        channel = self.server()
        actor = {'id': 1, 'username': 'offline'}
        self.assertEqual(channel.call('STATUS', actor, 'old')['command'], 'regular-status')
        client = Stage3Client(channel, actor)
        def preflight(c, old):
            c.call('S3_LOG_ON')
            self.clock.sleep(.05)
            self.clock.sleep(.05)
            self.assertEqual(c.call('S3_STATUS')['action']['readback'], 'PARAM_VALUE')
        phases = []
        result = exercise(client, self.clock, preflight, lambda phase, snapshot: phases.append(phase),
                          switch_approved=True)
        self.assertEqual(result['phases'], ['A','B','C'], result)
        self.assertEqual(result['rollback'], 'PASS', result)
        self.assertEqual(self.f.live, {'source': 1, 'log_disarmed': 0})
        self.assertEqual(self.f.actions, [('log',1),('log_read',1),('src',2),('src',1),('log',0),('log_read',0)])
        self.assertTrue(self.f.packets)
        self.assertGreaterEqual(min(self.f.packets),30)
        self.assertLess(max(self.f.packets),181)

    def test_uid_refusal_and_existing_profile_has_no_stage3(self):
        channel = self.server(uid=os.getuid()+1)
        with self.assertRaises((Refused, ValueError, OSError)):
            Stage3Client(channel, {'id':1,'username':'offline'}).open()
        self.assertIsNone(self.f.stage.token)

    def test_concurrent_open_different_peer_and_replay(self):
        token = self.prepared()
        for op, who, payload in [('S3_OPEN',(1000,11),{}),
                                 ('S3_RENEW',(1000,11),{'token':token,'phase':'B'})]:
            with self.assertRaises(Refused):
                self.direct(op, payload, who)
        with self.assertRaises(Refused):
            self.direct('S3_RENEW', {'token':token,'phase':'B', 'seq': self.f.stage.renew_seq})
        self.clock.t += .75
        self.f.step()
        with self.assertRaises(Refused):
            self.direct('S3_RENEW', {'token':token,'phase':'B'})
        with self.assertRaises(Refused):
            self.direct('S3_OPEN')

    def test_hard_deadline_overrules_recent_renewal(self):
        token = self.prepared()
        self.clock.t = 30.6
        self.f.stage.hard = 30.65
        self.direct('S3_RENEW', {'token':token,'phase':'B'})
        self.clock.t = 30.65
        self.assertFalse(self.f.stage.can_tick())

    def test_owner_without_diagnostic_dispatch_preserves_status(self):
        channel = self.server(dispatch=False)
        actor = {'id':1,'username':'old'}
        self.assertEqual(channel.call('STATUS', actor, 'req')['command'], 'regular-status')
        with self.assertRaises(Refused):
            Stage3Client(channel, actor).open()
        self.assertIsNone(self.f.stage.token)

    def test_synthetic_owner_restart_rejects_old_token(self):
        token = self.prepared()
        fresh = Fixture(self.trace, self.clock)
        with self.assertRaises(Refused):
            fresh.stage.dispatch((1000,10), 'S3_RENEW', {'token':token,'phase':'A'})
        self.assertNotEqual(fresh.stage.dispatch((1000,10),'S3_OPEN',{})['token'], token)

    def test_heartbeat_arm_hold_stop_guard_all_close_serial(self):
        for cause in ('heartbeat', 'ARM', 'HOLD', 'STOP latch'):
            with self.subTest(cause=cause):
                self.clock.t = 0
                self.f = Fixture(self.trace, self.clock)
                self.prepared()
                self.f.step()
                self.f.safe = False
                with self.assertRaises(OSError): self.f.stage.write_tx(self.f.writer, 0, b'x')
                self.assertTrue(self.f.stage.status()['stopped'])

    def test_stop_and_write_barrier(self):
        token = self.prepared()
        entered, release, responded = threading.Event(), threading.Event(), threading.Event()
        def writer(fd, packet):
            entered.set()
            release.wait(1)
            return len(packet)
        w = threading.Thread(target=lambda: self.f.stage.write_tx(writer,0,b'frame'))
        w.start()
        self.assertTrue(entered.wait(1))
        def stop():
            self.direct('S3_STOP', {'token':token})
            responded.set()
        ending = threading.Thread(target=stop)
        ending.start()
        self.assertFalse(responded.wait(.03))
        release.set()
        w.join(1)
        ending.join(1)
        self.assertTrue(responded.is_set())
        self.assertTrue(self.f.stage.status()['tx_barrier'])
        self.assertFalse(self.f.stage.status()['driver_empty'])
        self.f.drain = False
        self.f.step()
        self.assertFalse(self.f.stage.status()['driver_empty'])
        self.f.drain = True
        self.f.step()
        self.assertTrue(self.f.stage.status()['driver_empty'])
        with self.assertRaises(OSError): self.f.stage.write_tx(self.f.writer,0,b'after')

    def test_wrong_ack_unconfirmed_src_and_missing_parameter_readback(self):
        token = self.direct('S3_OPEN')['token']
        self.direct('S3_LOG_ON',{'token':token})
        self.f.drop_param = True
        self.clock.sleep(.1)
        self.clock.sleep(.1)
        for _ in range(5):
            self.clock.sleep(.4)
            if not self.f.stage.status()['stopped']:
                self.direct('S3_RENEW',{'token':token,'phase':'A'})
        self.assertIsNone(self.f.stage.status()['action']['readback'])
        self.assertTrue(self.f.stage.status()['stopped'])

    def test_src_ack_rejection_and_accepted_without_source_change(self):
        for rejected in (True,False):
            with self.subTest(rejected=rejected):
                self.clock.t = 0
                self.f = Fixture(self.trace,self.clock)
                token = self.prepared()
                for i in range(120):
                    self.clock.t = 30+(i+1)*.5
                    self.direct('S3_RENEW',{'token':token,'phase':'B'})
                self.direct('S3_RENEW',{'token':token,'phase':'C'})
                self.f.bad_ack = rejected
                self.f.apply_src = False
                self.direct('S3_SRC2',{'token':token})
                self.f.step()
                p = self.f.stage.pending
                self.assertEqual(p.ack,'REJECTED' if rejected else 'ACCEPTED')
                self.assertIsNone(self.f.stage.status()['action'])
                self.assertEqual(self.f.live['source'],1)

    def test_trace_overflow_aborts_and_does_not_pass(self):
        token = self.prepared()
        self.trace.limit = 0
        self.trace.push('intent')
        until = time.monotonic()+1
        while not self.trace.failed and time.monotonic() < until:
            time.sleep(.005)
        self.assertTrue(self.trace.failed)
        with self.assertRaises(OSError): self.f.stage.write_tx(self.f.writer,0,b'x')
        self.assertGreater(self.trace.dropped,0)
        self.assertTrue(self.f.stage.status()['stopped'])

    def test_rollback_readback_failure(self):
        token = self.direct('S3_OPEN')['token']
        self.direct('S3_LOG_ON', {'token':token})
        for i in range(2): self.clock.sleep(.1)
        self.direct('S3_STOP', {'token':token})
        self.f.drop_param = True
        for i in range(24): self.clock.sleep(.1)
        self.assertTrue(self.f.stage.status()['restore_failed'])
        self.assertFalse(self.f.stage.status()['restored'])

    def test_killed_client_idle_deadline_no_late_serial_writes(self):
        delays = []
        for label, sig in [('runner', signal.SIGKILL), ('adapter', signal.SIGKILL), ('stall', signal.SIGSTOP)]:
            with self.subTest(label=label):
                self.clock.t = 0
                self.f = Fixture(self.trace, self.clock)
                self.f.live['log_disarmed'] = 1
                if (self.root/'owner.sock').exists():
                    server.close()
                channel = self.server()
                server = self.last_server
                code = '''
import sys
from disarm_control.ipc import OwnerClient
from integrated_runner import Stage3Client
c=Stage3Client(OwnerClient(sys.argv[1]), {'id':1,'username':'child'})
c.open()
c.call('S3_START')
print('ready',flush=True)
for line in sys.stdin:
    c.call('S3_RENEW',phase=line.strip())
    print('renewed',flush=True)
'''
                child = subprocess.Popen([sys.executable,'-u','-c',code,channel.path],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                try:
                    self.assertEqual(child.stdout.readline().strip(),'ready')
                    for i in range(60):
                        self.clock.t = (i+1)/2
                        child.stdin.write('A\n')
                        child.stdin.flush()
                        self.assertEqual(child.stdout.readline().strip(),'renewed')
                    child.stdin.write('B\n')
                    child.stdin.flush()
                    self.assertEqual(child.stdout.readline().strip(),'renewed')
                    self.f.step()
                    start = time.monotonic()
                    os.kill(child.pid,sig)
                    while not self.f.stage.status()['stopped']:
                        elapsed = time.monotonic()-start
                        self.clock.t = 30+elapsed
                        self.f.step()
                        time.sleep(.005)
                    delay = time.monotonic()-start
                    delays.append(delay)
                    writes = len(self.f.packets)
                    self.f.step()
                    self.assertEqual(writes,len(self.f.packets))
                    self.assertLess(delay,.9)
                finally:
                    child.kill()
                    child.wait()
                    child.stdin.close()
                    child.stdout.close()
                    server.close()
        print('MAX_TESTED_STOP_SECONDS=%.6f' % max(delays), flush=True)

    def test_exact_vpe_visp_join_reports_ambiguity(self):
        rows = [dict(direction='TX', message_id=102, usec=u, monotonic=35) for u in (1, 2, 3)]
        visp = [dict(mavpackettype='VISP', RTimeUS=u) for u in (1, 2, 2)]
        self.assertEqual(join_vpe_visp(rows, visp, t0=30, t1=180),
                         dict(tx=3, matched=1, unmatched=1, ambiguous=1))

    def test_preflight_proof_and_live_gate(self):
        prefix = [{'mavpackettype':'FMT'},*({'mavpackettype':'XKFS','C':c} for c in (0,1))]
        self.assertEqual(prove_prefix({71:100},{71:200},71,prefix,session_ended=True)['id'],71)
        with self.assertRaises(ValueError): prove_prefix({71:100},{71:100},71,prefix,session_ended=True)
        with self.assertRaises(ValueError): prove_prefix({71:100},{71:200},71,prefix,session_ended=False)
        for mode in ('preflight','retest'):
            result=subprocess.run([sys.executable,'integrated_runner.py',mode],capture_output=True)
            self.assertEqual(result.returncode,2)
            self.assertIn(b'LIVE_DISABLED',result.stderr)
