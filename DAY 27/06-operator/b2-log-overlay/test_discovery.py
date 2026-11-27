import time
import unittest

from pymavlink.dialects.v20 import ardupilotmega as mav
from disarm_control.log_discovery import Inventory
from disarm_control.log_disarmed import Refused

# Reuse the integration harness without importing its test cases into this module.
import test_integration as integration


class DiscoveryTests(integration.OwnerTests):
    def setUp(self):
        super().setUp()
        self.q.discovery = None
        self.index_reply = self.make(1, b'LOG_DISARMED')
        self.list_replies = [self.make(0, b'OTHER', 6), self.make(1, b'LOG_DISARMED')]
        self.responses = []

    def make(self, index, name, value=0, kind=mav.MAV_PARAM_TYPE_INT8, source=1, count=2):
        return self.packet(mav.MAVLink_param_value_message(name,value,kind,count,index),source)

    def respond(self, message):
        self.sent.append(message)
        if message.get_type() == 'PARAM_REQUEST_LIST':
            self.responses.extend(self.list_replies)
        elif message.get_type() == 'PARAM_REQUEST_READ':
            self.assertEqual(message.param_index,1)
            self.responses.append(self.index_reply)
        else:
            self.fail('unexpected mutation')

    def pump_discovery(self, abort=None):
        for _ in range(250):
            time.sleep(.002)
            self.now += .035
            self.q.tick(self.fd,self.respond,connected=True,epoch=self.epoch,priority_clear=True)
            while self.responses:
                self.q.observe(self.responses.pop(0))
            if abort and self.sent:
                abort()
                abort=None
            if self.q.job is None:
                return
        self.fail('discovery did not finish')

    def start(self):
        self.allow('discover')
        self.dispatch('S3_LOG_DISCOVER')

    def test_unknown_index_then_separate_indexed_read_and_authorization(self):
        self.allow('begin')
        with self.assertRaises(Refused):
            self.dispatch('S3_LOG_BEGIN')
        self.start()
        self.pump_discovery()
        proof=self.dispatch('S3_LOG_STATUS')['discovery']
        self.assertEqual({k:proof[k] for k in ('index','count','value','param_type')},
                         dict(index=1,count=2,value=0,param_type=mav.MAV_PARAM_TYPE_INT8))
        self.assertEqual([m.get_type() for m in self.sent],['PARAM_REQUEST_LIST','PARAM_REQUEST_READ'])
        self.dispatch('S3_LOG_BEGIN')
        self.pump_discovery()
        self.assertEqual(len(self.sent),2)

    def test_duplicate_sentinel_and_foreign_frames(self):
        self.list_replies.insert(0,self.make(65535,b'LOG_DISARMED'))
        self.list_replies.insert(0,self.make(1,b'FALSE',source=8))
        self.list_replies.insert(0,self.list_replies[-1])
        self.start()
        self.pump_discovery()
        self.assertEqual(self.dispatch('S3_LOG_STATUS')['discovery']['index'],1)

    def test_incomplete_list_times_out_without_indexed_read(self):
        self.list_replies.pop(0)
        self.start()
        self.pump_discovery(lambda: setattr(self,'now',self.now+61))
        self.assertIsNone(self.dispatch('S3_LOG_STATUS')['discovery'])
        self.assertEqual([m.get_type() for m in self.sent],['PARAM_REQUEST_LIST'])

    def test_mismatched_confirmation_name_type_value_or_count(self):
        for reply in (self.make(1,b'OTHER'),self.make(1,b'LOG_DISARMED',kind=6),
                      self.make(1,b'LOG_DISARMED',1),self.make(1,b'LOG_DISARMED',count=3)):
            with self.subTest(reply=reply):
                self.q=self.new_owner()
                self.q.discovery=None
                self.index_reply=reply
                self.start()
                self.pump_discovery()
                self.assertIsNone(self.dispatch('S3_LOG_STATUS')['discovery'])

    def test_heartbeat_loss_and_cancel(self):
        self.start()
        self.pump_discovery(lambda: self.flags.update(fresh_heartbeat=False))
        self.assertIsNone(self.dispatch('S3_LOG_STATUS')['discovery'])
        self.assertEqual([m.get_type() for m in self.sent],['PARAM_REQUEST_LIST'])
        self.q=self.new_owner()
        self.q.discovery=None
        self.flags.update(fresh_heartbeat=True)
        self.start()
        self.marker=None
        self.pump_discovery()
        self.assertEqual([m.get_type() for m in self.sent],['PARAM_REQUEST_LIST'])

    def test_conflicting_duplicate_rejected(self):
        inventory=Inventory()
        inventory.add(self.make(0,b'OTHER'))
        with self.assertRaises(Refused):
            inventory.add(self.make(0,b'RENAMED'))

    def test_completed_proof_invalidated_by_epoch_and_restart(self):
        self.start()
        self.pump_discovery()
        self.assertIsNotNone(self.q.verified())
        self.q.tick(self.fd,self.sent.append,connected=True,epoch=2,priority_clear=True)
        self.assertIsNone(self.q.verified())
        self.q=self.new_owner()
        self.q.discovery=None
        self.assertIsNone(self.q.verified())


# Only collect discovery-specific cases. The original integration cases are run separately.
for name in vars(integration.OwnerTests):
    if name.startswith('test_'):
        setattr(DiscoveryTests,name,None)
