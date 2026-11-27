import os
from pathlib import Path
import shlex
import tempfile
import time
import unittest
import uuid

from pymavlink.dialects.v20 import ardupilotmega as mav
from disarm_control.log_disarmed import GUARDS
from disarm_control.log_queue import LogQueue
from disarm_control.ipc import OwnerClient, OwnerServer, SocketPolicy
from stop_latch_test.hold_runner import parse_args
from profile import IDENTITIES, render, resolve_identities


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.work=tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root=Path(self.work.name)
        self.device=self.root/'device-test'
        self.device.symlink_to('/dev/null')
        self.fd=os.open('/dev/null',os.O_RDWR|os.O_NONBLOCK)
        self.addCleanup(os.close,self.fd)
        self.flags=dict.fromkeys(GUARDS,True)
        self.now=1.
        self.sent=[]
        self.peer=(os.getuid(),100,1)
        self.value=0
        self.marker=None
        self.epoch=1
        self.q=self.new_owner()

    def new_owner(self):
        q=LogQueue(lambda:self.now,self.root,self.root/'marker','1'*32,self.device,os.getuid(),
                   lambda _,expected:self.marker==expected,lambda peer:peer==self.peer,lambda:self.flags.copy(),7)
        m=mav.MAVLink_attitude_message(1000,0,0,0,0,0,0)
        q.observe(self.packet(m))
        q.tick(self.fd,self.sent.append,connected=True,epoch=self.epoch,priority_clear=True)
        return q

    @staticmethod
    def packet(message,source=1):
        return mav.MAVLink(None).parse_char(message.pack(mav.MAVLink(None,srcSystem=source,srcComponent=1)))

    def allow(self,action,tx='-'):
        i=self.q.identity()
        self.marker='B2-LOG '+' '.join(i[k] for k in ('hardware','pi_boot','fc_boot','owner_session'))+f' {action} {tx}'

    def dispatch(self,op,body=None):
        return self.q.dispatch(self.peer,op,body or {},other_job=None,logs_busy=False)

    def respond(self,msg):
        self.sent.append(msg)
        if msg.get_type()=='PARAM_SET':
            self.value=int(msg.param_value)
        else:
            data=mav.MAVLink_param_value_message(b'LOG_DISARMED',self.value,mav.MAV_PARAM_TYPE_INT8,10,7)
            self.reply=self.packet(data)

    def pump(self,until=None):
        self.reply=None
        for _ in range(250):
            time.sleep(.003)
            self.now+=.035
            self.q.tick(self.fd,self.respond,connected=True,epoch=self.epoch,priority_clear=True)
            if self.reply is not None:
                self.q.observe(self.reply)
                self.reply=None
            if (until and until()) or self.q.job is None:
                return
        self.fail('worker did not finish')

    def test_authorization_and_same_owner_rollback(self):
        self.dispatch('S3_LOG_BEGIN')
        self.pump()
        self.assertEqual(self.sent,[])
        self.allow('begin')
        self.dispatch('S3_LOG_BEGIN')
        self.pump()
        self.assertEqual([m.get_type() for m in self.sent],
                         ['PARAM_REQUEST_READ','PARAM_SET','PARAM_REQUEST_READ'])
        self.assertTrue(all(m.param_id=='LOG_DISARMED' for m in self.sent))
        s=self.dispatch('S3_LOG_STATUS')
        self.assertTrue(s['recovery_required'])
        self.assertEqual(s['dataflash'],'IDLE')
        tx=s['unfinished']['transaction']
        self.allow('restore',tx)
        self.dispatch('S3_LOG_RESTORE',{'transaction':tx})
        self.pump()
        self.assertEqual(self.value,0)
        self.assertIsNone(self.dispatch('S3_LOG_STATUS')['unfinished'])

    def test_cancelled_request_cannot_execute_late(self):
        self.allow('begin')
        self.dispatch('S3_LOG_BEGIN')
        for _ in range(100):
            if self.q.slot:break
            time.sleep(.002)
        self.assertEqual(self.dispatch('S3_LOG_STATUS')['queued'],1)
        self.now+=3
        self.pump()
        self.assertEqual(self.sent,[])

    def test_guard_rechecked_on_actual_send(self):
        self.allow('begin')
        self.dispatch('S3_LOG_BEGIN')
        for _ in range(100):
            if self.q.slot:break
            time.sleep(.002)
        self.flags['fresh_heartbeat']=False
        self.pump()
        self.assertEqual(self.sent,[])

    def test_echo_cannot_complete_readback_before_read(self):
        self.allow('begin')
        self.dispatch('S3_LOG_BEGIN')
        self.pump(lambda: any(m.get_type()=='PARAM_SET' for m in self.sent))
        echo=self.packet(mav.MAVLink_param_value_message(b'LOG_DISARMED',1,mav.MAV_PARAM_TYPE_INT8,10,65535))
        self.q.observe(echo)
        self.assertIsNotNone(self.q.job)
        self.pump()
        self.assertEqual(self.sent[-1].get_type(),'PARAM_REQUEST_READ')

    def test_recovery_after_owner_recreation_requires_new_marker(self):
        self.allow('begin')
        self.dispatch('S3_LOG_BEGIN')
        self.pump()
        tx=self.dispatch('S3_LOG_STATUS')['unfinished']['transaction']
        self.q=self.new_owner()
        self.dispatch('S3_LOG_RECOVER',{'transaction':tx})
        self.pump()
        self.assertEqual(self.value,1)
        self.allow('recover',tx)
        self.dispatch('S3_LOG_RECOVER',{'transaction':tx})
        self.pump()
        self.assertEqual(self.value,0)

    def test_boot_change_invalidates_marker(self):
        self.allow('begin')
        previous=self.q.identity()
        self.now+=1
        self.q.observe(self.packet(mav.MAVLink_attitude_message(5000,0,0,0,0,0,0)))
        self.q.observe(self.packet(mav.MAVLink_attitude_message(5,0,0,0,0,0,0)))
        self.q.tick(self.fd,self.sent.append,connected=True,epoch=self.epoch,priority_clear=True)
        self.assertNotEqual(previous,self.q.identity())
        self.dispatch('S3_LOG_BEGIN')
        self.pump()
        self.assertEqual(self.sent,[])

    def test_queue_reports_other_dataflash(self):
        s=self.q.dispatch(self.peer,'S3_LOG_STATUS',{},other_job='prefix',logs_busy=True)
        self.assertEqual((s['active'],s['queued'],s['dataflash']),('prefix',0,'ACTIVE_OR_END_OWED'))

    def test_real_b2_log_startup_parser(self):
        reference=Path(os.environ['B2_REFERENCE']).read_text()
        base=next(l.split('=',1)[1].removesuffix('/owner') for l in reference.splitlines() if l.startswith('WorkingDirectory='))
        config=render(reference,base,'/overlay','/marker','/journal','7')
        command=shlex.split(config.splitlines()[-1].removeprefix('ExecStart='))
        argv=resolve_identities(command,dict(zip(IDENTITIES.values(),(str(os.getuid()+1),'123'))))[4:]
        a=parse_args(argv)
        self.assertEqual((a.stage3_operator_mode,a.stage3_log_approval_file,a.stage3_log_journal_directory),('B2','/marker','/journal'))
        self.assertTrue(a.externalnav_observe_only)
        self.assertFalse(a.externalnav_tx or a.nav_bench_tx or a.nav_mission_tx)
        with self.assertRaises(SystemExit):
            parse_args([*argv,'--stage3-operator-mode','B1'])

    def test_uid_checked_socket_and_profile_allowlist(self):
        for enabled, uid, expected in ((False,os.getuid(),False),(True,os.getuid()+1,False),(True,os.getuid(),True)):
            with self.subTest(enabled=enabled,uid=uid),tempfile.TemporaryDirectory() as d:
                path=Path(d)/'socket'
                server=OwnerServer(object(),path,allowed_uid=uid,disarm_request=lambda *_:None,
                                   stage3_dispatch=lambda peer,*_:peer[:2],log_enabled=enabled)
                server.start()
                try:
                    c=OwnerClient(path,policy=SocketPolicy(os.getuid()))
                    try:
                        answer=c.call('S3_LOG_STATUS',{'id':1,'username':'test'},uuid.uuid4().hex,payload={})
                    except (OSError,ValueError):
                        self.assertFalse(expected)
                    else:
                        self.assertEqual(answer['ok'],expected)
                        if expected:
                            self.assertEqual(answer['command'],[os.getuid(),os.getpid()])
                finally:
                    server.close()
