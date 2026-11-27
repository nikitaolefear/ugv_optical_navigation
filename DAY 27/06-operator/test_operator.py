import base64
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from disarm_control.ipc import OwnerClient, OwnerServer
from disarm_control.stage3 import Refused
from disarm_control.stage3_operator import BenchOperator
class Message:
    def __init__(self, kind, source=(1, 1), **fields):
        self.kind, self.source = kind, source
        vars(self).update(fields)

    def get_type(self):
        return self.kind

    def get_srcSystem(self):
        return self.source[0]

    def get_srcComponent(self):
        return self.source[1]


class OperatorTests(unittest.TestCase):
    peer=(1,2,3)
    def setUp(self):
        self.now=0
        self.safe=True
        self.sent=[]
        self.o=self.make('B2')

    def make(self,mode):
        return BenchOperator(mode,lambda:self.now,lambda:self.safe,
                             lambda:{'tx_enabled':False,'hold':True},pi_boot='mock')

    def tick(self,**values):
        self.o.tick(lambda *x:self.sent.append(x),
                    **dict(dict(connected=True,priority_clear=True),**values))

    def ask(self,kind,**fields):
        return self.o.dispatch(self.peer,'S3_BENCH_REQUEST',dict(kind=kind,**fields))

    def test_b1_no_writes_or_reads_except_snapshot(self):
        self.o=self.make('B1')
        self.assertFalse(self.o.dispatch(self.peer,'S3_BENCH_STATUS',{})['tx_enabled'])
        for name in ('catalog','timesync','log_disarmed'):
            with self.assertRaises(Refused): self.ask(name)
        self.tick()
        self.assertEqual(self.sent,[])

    def test_b2_does_not_expose_mutation_and_validates_readback(self):
        for name in ('S3_OPEN','S3_LOG_ON','S3_SRC2','S3_RESTORE'):
            with self.assertRaises(Refused): self.o.dispatch(self.peer,name,{})
        self.ask('log_disarmed')
        self.tick()
        self.o.observe(Message('PARAM_VALUE',source=(2,1),param_id='LOG_DISARMED',param_value=1))
        self.o.observe(Message('PARAM_VALUE',param_id='OTHER',param_value=1))
        self.assertIsNone(self.o.result)
        self.o.observe(Message('PARAM_VALUE',param_id='LOG_DISARMED',param_value=1.0))
        self.assertEqual(self.o.result['value'],1)
        self.assertIsNone(self.o.source.read())
        self.assertEqual(self.sent,[('log_disarmed',)])

    def test_catalog_and_prefix_are_peer_bound_and_end_is_required(self):
        self.ask('catalog')
        self.tick()
        self.tick()
        self.o.observe(Message('LOG_ENTRY',num_logs=1,id=74,size=832,time_utc=315532800))
        self.tick()
        self.assertEqual(self.sent,[('list',),('end',)])
        with self.assertRaises(Refused):
            self.o.dispatch((1,2,4),'S3_BENCH_RESULT',{})
        self.ask('prefix',id=74,size=4)
        self.tick()
        self.tick()
        self.o.observe(Message('LOG_DATA',id=74,ofs=0,count=4,data=b'abcd'))
        self.tick()
        reply=self.o.dispatch(self.peer,'S3_BENCH_CHUNK',{'offset':0})
        self.assertEqual(base64.b64decode(reply['base64']),b'abcd')
        self.assertTrue(self.o.result['end_sent'])

    def test_real_unix_uid_check_and_normal_status(self):
        tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(tmp.cleanup)
        root=Path(tmp.name)
        engine=SimpleNamespace(request=lambda *a:'old',status=lambda *a:'old')
        actor=dict(id=1,username='test')
        for index,uid in enumerate((os.getuid(),os.getuid()+1)):
            server=OwnerServer(engine,root/('socket'+str(index)),allowed_uid=uid,
                               stage3_dispatch=self.o.dispatch)
            server.start()
            try:
                client=OwnerClient(server.path)
                if uid!=os.getuid():
                    with self.assertRaises((ValueError,OSError)):
                        client.call('S3_BENCH_STATUS',actor,'1',payload={})
                else:
                    self.assertEqual(client.call('STATUS',actor,'old')['command'],'old')
                    self.assertTrue(client.call('S3_BENCH_STATUS',actor,'1',payload={})['command']['readonly'])
                    try:
                        denied = client.call('S3_LOG_ON',actor,'2',payload={})
                    except ValueError:
                        pass  # Minimal overlay refuses this opcode even client-side.
                    else:
                        self.assertFalse(denied['ok'])
            finally:
                server.close()
