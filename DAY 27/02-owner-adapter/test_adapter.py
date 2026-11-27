import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest

from owner_adapter import ENDPOINTS, HTTPReader, OwnerAdapter, OwnerUnavailable, UnsupportedOperation
from stage3_lease import Stage3Lease


class Clock:
    t = 0.0
    def monotonic(self):
        return self.t
    def time(self):
        return 1800000000 + self.t


def responses():
    return {
        'telemetry': dict(connection='CONNECTED',
                          flight_mode={'state': 'FRESH', 'value': {'custom_mode': 4, 'autopilot': 3, 'vehicle_type': 10}},
                          armed={'state': 'FRESH', 'value': 'DISARMED'},
                          messages={'HEARTBEAT': {'state': 'FRESH', 'age_s': .1}},
                          ekf={'EKF_STATUS_REPORT': {'state': 'FRESH', 'value': {'flags': 167}}}),
        'externalnav': dict(tx_enabled=False, sent=0, pose_age_s=.1, nav_bench_tx=False),
        'production-stop': dict(stop_latch=True, brake_release='HOLD',
                                blocking_reasons=['BRAKE_HOLD_ONLY'], reasons=[]),
        'owner-diagnostics': dict(ekf_origin={'latitude_deg': 45, 'longitude_deg': 15, 'altitude_msl_m': 0},
                                  local_position_ned={'age_s': .1, 'time_boot_ms': 1234},
                                  local_ekf_setup='SRC2_ACK_ACCEPTED_SOURCE_UNVERIFIED'),
    }


class FakeOpener:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request.full_url, request.method, timeout))
        endpoint = next(k for k, url in ENDPOINTS.items() if url == request.full_url)
        value = self.payloads[endpoint]
        if isinstance(value, Exception):
            raise value
        return io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode())


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.payloads = responses()
        self.opener = FakeOpener(self.payloads)
        self.adapter = OwnerAdapter(HTTPReader(self.opener), Clock())

    def test_actual_routes_and_snapshot_fields(self):
        value = self.adapter.observe()
        self.assertEqual(value.flags, 167)
        self.assertEqual(value.pixhawk_boot_ms, 1234)
        self.assertEqual(value.origin['latitude_deg'], 45)
        self.assertIsNone(value.source)
        self.assertEqual(self.opener.calls, [(url,'GET',.35) for url in ENDPOINTS.values()])
        self.adapter.confirm_off()

    def test_bad_json_and_invalid_schema(self):
        for payload in (b'[]', b'{', b'{"age":NaN}', {'connection': 'CONNECTED'}):
            with self.subTest(payload=payload):
                self.payloads['telemetry'] = payload
                with self.assertRaises(OwnerUnavailable):
                    self.adapter.observe()

    def test_transport_errors_never_become_success(self):
        for error in (TimeoutError('timeout'), ConnectionResetError('broken')):
            with self.subTest(error=error):
                self.payloads['owner-diagnostics'] = error
                with self.assertRaises(OwnerUnavailable):
                    self.adapter.observe()

    def test_stale_data_and_tx_on(self):
        self.payloads['telemetry']['messages']['HEARTBEAT']['age_s'] = 1
        with self.assertRaises(OwnerUnavailable):
            self.adapter.observe()
        self.payloads['telemetry']['messages']['HEARTBEAT']['age_s'] = .1
        self.payloads['owner-diagnostics']['local_position_ned']['age_s'] = 1
        self.assertIsNone(self.adapter.observe().local_position)
        self.payloads['externalnav']['tx_enabled'] = True
        with self.assertRaises(OwnerUnavailable):
            self.adapter.confirm_off()

    def test_repeated_ack_and_unavailable_source_never_confirm_transition(self):
        for _ in range(2):
            self.assertIsNone(self.adapter.observe().source)
        with self.assertRaises(UnsupportedOperation):
            self.adapter.src2_ack()

    def test_actual_unsupported_mutations_and_readback(self):
        for method, args in [('set_log_disarmed', (1,)), ('request_src2', ()),
                             ('restore_source', (1,)), ('restore_profile', ('normal',)),
                             ('enable_tx_until', (1,)), ('revoke_tx', ()),
                             ('read_log_disarmed', ()), ('sample', ()), ('baseline', ()),
                             ('prove_recording', ({},))]:
            with self.subTest(method=method), self.assertRaises(UnsupportedOperation):
                getattr(self.adapter, method)(*args)
        self.assertFalse(self.opener.calls)
        with self.assertRaises(UnsupportedOperation):
            self.adapter.verify_restored(None)  # observe-only proof is partial

    def test_live_cli_refused(self):
        entry = Path(__file__).with_name('owner_adapter.py')
        for mode in ('preflight', 'retest'):
            outcome = subprocess.run([sys.executable, str(entry), mode], capture_output=True)
            self.assertEqual(outcome.returncode, 2)
            self.assertIn(b'LIVE_DISABLED', outcome.stderr)


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.lease = Stage3Lease(self.clock.monotonic)

    def test_idle_expiry_on_stall_or_ipc_loss_and_late_renewal(self):
        self.assertFalse(self.lease.permits())
        self.assertFalse(self.lease.expired_or_revoked())
        self.assertTrue(self.lease.open('runner', 150))
        self.assertTrue(self.lease.permits())
        self.clock.t = .74
        self.assertFalse(self.lease.renew('other'))
        self.clock.t = .75
        self.assertFalse(self.lease.permits())
        self.assertTrue(self.lease.expired_or_revoked())
        self.assertFalse(self.lease.renew('runner'))
        self.assertFalse(self.lease.open('runner', 150))

    def test_hard_expiry_and_explicit_revoke(self):
        self.assertTrue(self.lease.open('runner', 1))
        self.clock.t = .7
        self.assertTrue(self.lease.renew('runner'))
        self.clock.t = 1
        self.assertFalse(self.lease.permits())
        lease = Stage3Lease(self.clock.monotonic)
        self.assertTrue(lease.open('runner', 2))
        lease.revoke()
        self.assertFalse(lease.permits())

    def test_real_client_sigkill_owner_model_expires_without_callback(self):
        child = subprocess.Popen([sys.executable, '-u', '-c',
                                  'import time; print("renewal"); time.sleep(30)'],
                                 stdout=subprocess.PIPE)
        try:
            self.assertEqual(child.stdout.readline(), b'renewal\n')
            self.assertTrue(self.lease.open('runner', 150))
            os.kill(child.pid, signal.SIGKILL)
            self.assertEqual(child.wait(timeout=2), -signal.SIGKILL)
            self.clock.t = .75
            self.assertFalse(self.lease.permits())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdout.close()
