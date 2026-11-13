import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from stop_latch import StopLatch, Rejected, read_contact, monitor
from server import create_app

class LatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'transitions.jsonl'
        self.latch=StopLatch(self.path,clock=lambda:'2026-11-13T19:00:00+02:00')
    def tearDown(self):self.tmp.cleanup()
    def ready(self):
        self.latch.input('RELEASED');self.latch.command('reset')
    def test_two_physical_cycles_release_does_not_reset(self):
        self.ready()
        for _ in range(2):
            self.latch.command('start')
            self.latch.input('PRESSED')
            self.assertTrue(self.latch.latched)
            self.assertEqual(self.latch.run,'STOPPED')
            self.latch.input('RELEASED')
            self.assertTrue(self.latch.latched)
            for action in ('start','resume'):
                with self.assertRaises(Rejected):self.latch.command(action)
            self.latch.command('reset')
            self.assertFalse(self.latch.latched)
            self.assertEqual(self.latch.run,'IDLE')
            self.assertFalse(self.latch.snapshot()['movement_allowed'])
    def test_pressed_reset_rejected_on_server(self):
        self.ready();self.latch.input('PRESSED')
        client=create_app(self.latch).test_client()
        token=client.get('/api/state').json['csrf']
        for action in ('reset','start','resume'):
            r=client.post('/api/test-action',json={'action':action},headers={'X-Test-CSRF':token})
            self.assertEqual(r.status_code,409)
            self.assertTrue(r.json['stop_latch'])
            self.assertEqual(r.json['contact'],'PRESSED')
        self.assertIn('RESET_REJECTED',[r['event'] for r in self.latch.events])
    def test_reset_probe_records_real_pressed_input(self):
        self.ready();self.latch.command('arm_reset_probe');self.latch.input('PRESSED')
        self.latch.input('RELEASED')
        rows=[json.loads(line) for line in self.path.read_text().splitlines()]
        reject=next(r for r in rows if r['event']=='RESET_REJECTED')
        self.assertEqual(reject['contact'],'PRESSED')
        self.assertTrue(reject['stop_latch'])
        self.assertIn('AUTO_RESET_PROBE',reject['detail'])
        self.assertTrue(all(r['time'] and r['test_only'] and not r['movement_allowed'] for r in rows))
        self.assertTrue(self.latch.snapshot()['stop_latch'])
        self.assertFalse(self.latch.probe)
    def test_unknown_and_restart_fail_closed(self):
        with self.assertRaises(Rejected):self.latch.command('reset')
        self.ready();self.latch.input('UNKNOWN')
        for action in ('reset','start','resume'):
            with self.assertRaises(Rejected):self.latch.command(action)
        restarted=StopLatch(self.path)
        self.assertTrue(restarted.latched)
        self.assertEqual(restarted.contact,'UNKNOWN')
    def test_pause_resume_only_without_latch(self):
        self.ready();self.latch.command('start');self.latch.command('pause');self.latch.command('resume')
        self.assertEqual(self.latch.run,'TEST_RUNNING')
        self.latch.input('PRESSED');self.latch.input('RELEASED');self.latch.command('reset')
        with self.assertRaises(Rejected):self.latch.command('resume')
        self.assertEqual(self.latch.run,'IDLE')
    def test_repeated_samples_no_duplicate_transitions(self):
        self.ready();self.latch.input('PRESSED');count=len(self.latch.events)
        self.latch.input('PRESSED');self.assertEqual(len(self.latch.events),count)
    def test_web_csrf_and_no_gpio_injection(self):
        c=create_app(self.latch).test_client()
        self.assertEqual(c.post('/api/test-action',json={'action':'reset'}).status_code,403)
        token=c.get('/api/state').json['csrf']
        h={'X-Test-CSRF':token}
        self.assertEqual(c.post('/api/test-action',json={'action':'reset'},headers={**h,'Origin':'http://other.example'}).status_code,403)
        self.assertEqual(c.post('/api/test-action',json={'action':'set_gpio'},headers=h).status_code,409)
    def test_read_only_gpio_mapping_and_mode_validation(self):
        for raw,expected in [('17: ip pu | lo // GPIO17 = input','PRESSED'),('17: ip pu | hi // GPIO17 = input','RELEASED')]:
            with patch('stop_latch.subprocess.run',return_value=subprocess.CompletedProcess([],0,raw,'')) as run:
                self.assertEqual(read_contact(),expected)
                self.assertEqual(run.call_args.args[0],['/usr/bin/pinctrl','get','17'])
        with patch('stop_latch.subprocess.run',return_value=subprocess.CompletedProcess([],0,'17: op pn | hi','')):
            with self.assertRaises(ValueError):read_contact()
    def test_gpio_failure_stops_and_latches(self):
        import threading
        self.ready();self.latch.command('start');stop=threading.Event()
        def fail():
            stop.set();raise subprocess.TimeoutExpired('pinctrl',0.5)
        monitor(self.latch,stop,fail)
        self.assertEqual(self.latch.contact,'UNKNOWN');self.assertTrue(self.latch.latched)
        self.assertEqual(self.latch.run,'STOPPED')
    def test_stale_reader_cannot_allow_reset_or_start(self):
        self.ready()
        self.latch.last_sample -= 2
        with self.assertRaises(Rejected):self.latch.command('start')
        with self.assertRaises(Rejected):self.latch.command('reset')
        self.assertEqual(self.latch.snapshot()['contact'],'UNKNOWN')
        self.assertTrue(self.latch.latched)

    def test_journal_failure_is_fail_closed(self):
        self.ready()
        with patch.object(Path,'open',side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):self.latch.command('start')
        self.assertTrue(self.latch.latched);self.assertEqual(self.latch.contact,'UNKNOWN')
        self.assertEqual(self.latch.run,'STOPPED')

if __name__=='__main__':unittest.main()
