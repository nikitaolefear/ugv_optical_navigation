import multiprocessing
import os
from pathlib import Path
import shlex
import tempfile
import unittest

from pymavlink.dialects.v20 import ardupilotmega as mav
from dataflash_proof import core_source, growing_log
from disarm_control.log_disarmed import GUARDS, Journal, Refused, Transaction
from disarm_control.owner_port import LogDisarmedPort, TARGET
from stop_latch_test.hold_runner import parse_args

PEER = (42, 123, 1)
CURRENT = dict(hardware="a"*64, pi_boot="b"*32, fc_boot="c"*32, owner_session="d"*32)


class Mock:
    def __init__(self):
        self.current = dict(CURRENT)
        self.flags = dict.fromkeys(GUARDS, True)
        self.value = 0
        self.source = 1
        self.calls = []
        self.accept = {"begin", "restore", "recover"}
        self.crash = None
        self.bad = False
        self.timeout = False

    def identity(self):
        return self.current.copy()

    def safety(self):
        return self.flags.copy()

    def authorized(self, peer, action, transaction, current):
        return peer == PEER and action in self.accept and current == self.current

    def read_log_disarmed(self, check):
        check()
        self.calls.append("read")
        if self.timeout:
            raise TimeoutError()
        return self.value if not self.bad or self.value == 0 else 2

    def write_log_disarmed(self, value, check):
        check()
        if self.crash == "before":
            os._exit(51)
        self.calls.append(("write", value))
        self.value = value
        if self.crash == "after":
            os._exit(52)


def child_crash(directory, phase):
    mock = Mock()
    mock.crash = phase
    Transaction(Journal(directory), mock, gateway_uid=42,
                authorized=mock.authorized, profile="B2-LOG").execute(PEER, "begin", {})


class Tests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.store = Journal(self.work.name)
        self.mock = Mock()
        self.tx = Transaction(self.store, self.mock, gateway_uid=42,
                              authorized=self.mock.authorized, profile="B2-LOG")

    def test_begin_restore_and_guards(self):
        row = self.tx.execute(PEER,"begin",{})
        self.assertEqual(self.mock.calls, ["read",("write",1),"read"])
        restored = self.tx.execute(PEER,"restore",{"transaction":row["transaction"]})
        self.assertEqual(restored["phase"],"restored")
        self.assertEqual(self.mock.calls[-3:],["read",("write",0),"read"])
        self.assertEqual(self.mock.source,1)
        self.assertTrue(self.mock.flags["tx_zero"])

    def test_other_params_other_uid_and_b2_refused(self):
        with self.assertRaises(Refused):
            self.tx.execute(PEER,"begin",{"parameter":"OTHER"})
        with self.assertRaises(Refused):
            self.tx.execute((41,123,1),"begin",{})
        self.tx.profile="B2"
        with self.assertRaises(Refused):
            self.tx.execute(PEER,"begin",{})
        self.assertEqual(self.mock.calls, [])

    def test_every_guard(self):
        for name in GUARDS:
            with self.subTest(name=name):
                self.mock.flags[name]=False
                with self.assertRaises(Refused):
                    self.tx.execute(PEER,"begin",{})
                self.mock.flags[name]=True
        self.assertEqual(self.mock.calls,[])

    def test_real_process_crash_before_and_after_write(self):
        for phase, code, live in (("before",51,0),("after",52,1)):
            with self.subTest(phase=phase):
                proc=multiprocessing.get_context("fork").Process(target=child_crash,args=(self.work.name,phase))
                proc.start()
                proc.join(4)
                self.assertEqual(proc.exitcode,code)
                row=self.store.load()
                self.assertEqual((row["phase"],row["baseline"]),("enable_intent",0))
                self.mock.value=live
                self.mock.calls.clear()
                with self.assertRaises(Refused):
                    self.tx.execute(PEER,"begin",{})
                self.assertEqual(self.mock.calls,[])
                self.assertEqual(self.tx.execute(PEER,"recover",{"transaction":row["transaction"]})["phase"],"restored")
                self.assertEqual(self.mock.calls,["read",*( [("write",0)] if live else []),"read"])

    def test_explicit_recovery_and_device_mismatch(self):
        row=self.tx.execute(PEER,"begin",{})
        self.mock.current["fc_boot"]="e"*32
        with self.assertRaises(Refused):
            self.tx.execute(PEER,"restore",{"transaction":row["transaction"]})
        self.mock.accept.discard("recover")
        with self.assertRaises(Refused):
            self.tx.execute(PEER,"recover",{"transaction":row["transaction"]})
        self.mock.accept.add("recover")
        self.mock.current["hardware"]="f"*64
        with self.assertRaises(Refused):
            self.tx.execute(PEER,"recover",{"transaction":row["transaction"]})

    def test_timeout_and_incomplete_recovery(self):
        row=self.tx.execute(PEER,"begin",{})
        self.mock.timeout=True
        with self.assertRaises(TimeoutError):
            self.tx.execute(PEER,"recover",{"transaction":row["transaction"]})
        self.assertEqual(self.store.load()["phase"],"enabled")
        with self.assertRaises(Refused):
            self.tx.execute(PEER,"begin",{})

    def test_heartbeat_lost_after_durable_intent_before_send(self):
        original=self.store.save
        def save(row):
            original(row)
            self.mock.flags["fresh_heartbeat"]=False
        self.store.save=save
        with self.assertRaises(Refused):
            self.tx.execute(PEER,"begin",{})
        self.assertEqual(self.mock.calls,["read"])
        self.assertEqual(self.store.load()["phase"],"enable_intent")

    def test_bad_readback_retains_intent(self):
        self.mock.bad=True
        with self.assertRaisesRegex(Refused,"readback mismatch"):
            self.tx.execute(PEER,"begin",{})
        self.assertEqual(self.store.load()["phase"],"enable_intent")

    def test_failed_restore_readback_after_write(self):
        row=self.tx.execute(PEER,"begin",{})
        old=self.mock.write_log_disarmed
        def fail(value, check):
            old(value,check)
            self.mock.timeout=True
        self.mock.write_log_disarmed=fail
        with self.assertRaises(TimeoutError):
            self.tx.execute(PEER,"restore",{"transaction":row["transaction"]})
        self.assertEqual(self.store.load()["phase"],"restore_intent")

    def test_corrupt_journal_blocks_start(self):
        self.store.record.write_text("{}")
        self.store.record.chmod(0o600)
        with self.assertRaises((Refused, KeyError)):
            self.tx.execute(PEER,"begin",{})
        self.assertEqual(self.mock.calls,[])

    def test_real_mavlink_builder_fixed_name_and_source_filter(self):
        def rx(name, source):
            packet=mav.MAVLink_param_value_message(name,1,mav.MAV_PARAM_TYPE_INT8,10,7)
            return mav.MAVLink(None).parse_char(packet.pack(mav.MAVLink(None,srcSystem=source,srcComponent=1)))
        sent=[]
        def exchange(msg, check, timeout, accept):
            check()
            sent.append(msg)
            self.assertEqual((msg.target_system,msg.target_component),TARGET)
            self.assertEqual(msg.param_id,"LOG_DISARMED")
            if accept:
                self.assertFalse(accept(rx(b"OTHER",1)))
                self.assertFalse(accept(rx(b"LOG_DISARMED",2)))
                echo=mav.MAVLink_param_value_message(b"LOG_DISARMED",1,mav.MAV_PARAM_TYPE_INT8,10,65535)
                parsed=mav.MAVLink(None).parse_char(echo.pack(mav.MAVLink(None,srcSystem=1,srcComponent=1)))
                self.assertFalse(accept(parsed))
                return rx(b"LOG_DISARMED",1)
        port=LogDisarmedPort(exchange,self.mock.identity,self.mock.safety,7)
        port.write_log_disarmed(1,lambda: None)
        self.assertEqual(port.read_log_disarmed(lambda: None),1)
        self.assertEqual([m.get_type() for m in sent],["PARAM_SET","PARAM_REQUEST_READ"])

    def test_growth_and_independent_cores(self):
        captures=[(1,{10:5},True),(2,{10:5,11:6},True),(3,{10:5,11:8},True)]
        self.assertEqual(growing_log(captures),11)
        with self.assertRaises(ValueError):
            growing_log([*captures[:2],(3,{10:6,11:8},True)])
        records=[dict(mavpackettype="XKFS",TimeUS=120,C=c,SS=0) for c in (0,1)]
        self.assertEqual(core_source(records,(100,200),0),{0:1,1:1})
        with self.assertRaises(ValueError):
            core_source(records,(130,200),0)

    def test_installed_b2_parser_repeat(self):
        ref=os.environ.get("B2_REFERENCE")
        if not ref:
            self.skipTest("B2_REFERENCE unavailable")
        line=next(s.split("=",1)[1] for s in Path(ref).read_text().splitlines() if s.startswith("ExecStart=") and s!="ExecStart=")
        args=shlex.split(line)[4:]
        for flag,value in (("--web-disarm-client-uid",str(os.getuid()+1)),("--web-disarm-client-gid","123")):
            args[args.index(flag)+1]=value
        for _ in range(2):
            a=parse_args(args)
            self.assertEqual(a.stage3_operator_mode,"B2")
            self.assertTrue(a.externalnav_observe_only)
            self.assertFalse(a.externalnav_tx or a.nav_bench_tx or a.nav_mission_tx)
