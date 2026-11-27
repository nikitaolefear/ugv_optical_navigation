from dataclasses import replace
import os
import signal
import unittest

from evidence import analyze
from runner import Baseline, ORIGIN, Sample, run


class FakeClock:
    t = 0.0

    def monotonic(self):
        return self.t

    def time(self):
        return 1_800_000_000 + self.t

    def sleep(self, duration):
        self.t += duration


class FakeOwner:
    def __init__(self, clock):
        self.clock = clock
        self.source = 1
        self.log = 0
        self.calls = []
        self.tx_start = None
        self.count = 0
        self.switch_at = None
        self.ack = 'ACCEPTED'
        self.effective_source = 2
        self.change_error = False
        self.restore_error = False
        self.revoke_error = False
        self.growth = True
        self.fault = lambda s: s

    def baseline(self):
        return Baseline(self.log, self.source, {70: 10}, 'normal')

    def confirm_off(self):
        self.calls.append('off')
        assert self.tx_start is None

    def read_log_disarmed(self):
        return self.log

    def set_log_disarmed(self, value):
        self.calls.append(('log', value))
        self.log = value
        if value == 1 and self.change_error:
            raise RuntimeError('parameter rejected after possible write')
        if value == 0 and self.restore_error:
            self.log = 1
            raise RuntimeError('restore rejected')

    def prove_recording(self, prior_sizes):
        return 71 if self.growth else None

    def enable_tx_until(self, deadline):
        self.calls.append(('tx', self.clock.t, deadline))
        self.tx_start = self.clock.t

    def revoke_tx(self):
        self.calls.append('revoke')
        if self.revoke_error:
            raise RuntimeError('cannot revoke')
        self.tx_start = None

    def request_src2(self):
        self.calls.append(('request_src2', self.clock.t))
        self.switch_at = self.clock.t

    def src2_ack(self):
        return self.ack

    def restore_source(self, source):
        self.calls.append(('source', source))
        self.source = source
        self.switch_at = None

    def restore_profile(self, profile):
        self.calls.append(('profile', profile))

    def verify_restored(self, baseline):
        self.calls.append('verified')
        s = self.sample()
        assert self.tx_start is None and s.hold and not s.armed
        assert self.log == baseline.log_disarmed and self.source == baseline.source

    def sample(self):
        now = self.clock.t
        if self.tx_start is not None:
            self.count = int((now - self.tx_start) * 10)
        src = self.source
        if self.switch_at is not None and now >= self.switch_at + 1:
            src = self.effective_source
        sample = Sample(.1, True, False, True, True, True, ORIGIN, src, 71,
                        True, self.count, .1, .1, 167, None, False)
        return self.fault(sample) if self.tx_start is not None or now else sample


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.owner = FakeOwner(self.clock)
        self.trace = []

    def execute(self, **options):
        args = dict(isolation_confirmed=True, logging_approved=True, switch_approved=True)
        args.update(options)
        return run(self.owner, self.clock, self.trace.append, **args)

    def test_three_phases_preserve_xy_failure_evidence_and_restore(self):
        result = self.execute()
        self.assertEqual(result['phases'], ['A', 'B', 'C'])
        self.assertEqual(self.clock.t, 180)
        self.assertIn(('tx', 30, 180), self.owner.calls)
        self.assertIn(('request_src2', 90), self.owner.calls)
        self.assertEqual(result['rollback'], 'PASS')
        self.assertEqual(result['status'], 'INCONCLUSIVE')  # Offline evidence still required.
        self.assertTrue(all(r['flags'] == 167 for r in self.trace if r['event'] == 'sample'))
        self.assertLess(self.owner.calls.index('revoke'), self.owner.calls.index(('source', 1)))

    def test_already_src2_never_switches(self):
        self.owner.source = 2
        self.assertEqual(self.execute()['phases'], ['A', 'B', 'C'])
        self.assertFalse(any(isinstance(c, tuple) and c[0] == 'request_src2'
                             for c in self.owner.calls))

    def test_unknown_initial_is_inconclusive_and_never_enables(self):
        self.owner.source = None
        self.assertEqual(self.execute()['reason'], 'INITIAL_SRC_UNKNOWN')
        self.assertEqual(self.clock.t, 0)

    def test_ack_is_not_source_verification(self):
        self.owner.effective_source = None
        result = self.execute()
        self.assertEqual(result['src2_ack'], 'ACCEPTED')
        self.assertIsNone(result['src2_verified_at'])
        self.assertEqual(result['reason'], 'SRC2_REQUIRES_DATAFLASH_VERIFICATION')

    def test_accepted_ack_with_known_wrong_src_fails(self):
        self.owner.effective_source = 1
        self.assertEqual(self.execute()['reason'], 'SRC2_REPORTED_INACTIVE')

    def test_static_counter_with_fresh_tx_age_fails(self):
        self.owner.fault = lambda s: replace(s, sent=0)
        self.assertEqual(self.execute()['reason'], 'TX_STALLED')

    def test_missing_ack(self):
        self.owner.ack = None
        self.assertEqual(self.execute()['reason'], 'SRC2_ACK_MISSING')
        self.assertEqual(self.owner.calls[-1], 'verified')

    def test_rejected_ack(self):
        self.owner.ack = 'DENIED'
        self.assertEqual(self.execute()['reason'], 'SRC2_COMMAND_REJECTED')

    def test_no_growth_and_parameter_error(self):
        self.owner.growth = False
        self.assertEqual(self.execute()['reason'], 'DATAFLASH_GROWTH_UNPROVEN')
        self.owner.change_error = True
        result = self.execute()
        self.assertIn('parameter rejected', result['reason'])
        self.assertEqual(result['rollback'], 'PASS')
        self.assertEqual(self.owner.log, 0)

    def test_restore_error_is_fail_and_profile_restore_still_attempted(self):
        self.owner.restore_error = True
        result = self.execute()
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['rollback'], 'FAIL')
        self.assertIn(('profile', 'normal'), self.owner.calls)

    def test_failed_revoke_blocks_further_mutation(self):
        self.owner.revoke_error = True
        result = self.execute()
        self.assertEqual(result['rollback'], 'FAIL')
        self.assertEqual(self.owner.calls[-1], 'revoke')

    def test_manual_isolation_and_approval(self):
        self.assertEqual(self.execute(isolation_confirmed=False)['reason'],
                         'MANUAL_ISOLATION_UNCONFIRMED')
        self.assertEqual(self.execute(logging_approved=False)['reason'], 'LOG_CHANGE_NOT_APPROVED')
        self.assertEqual(self.execute(switch_approved=False)['reason'], 'SRC2_SWITCH_NOT_APPROVED')

    def test_live_guards(self):
        for fields, reason in [
            ({'heartbeat_age': 1.5}, 'HEARTBEAT_STALE'), ({'hold': False}, 'HOLD_LOST'),
            ({'armed': True}, 'ARMED_OR_UNKNOWN'), ({'connected': False}, 'OWNER_LINK_LOST'),
            ({'tx_age': .6}, 'TX_STALLED'), ({'pose_age': .21}, 'POSE_STALE'),
            ({'origin': (50, 30, 0)}, 'ORIGIN_MISMATCH'), ({'source': 3}, 'UNEXPECTED_SRC'),
            ({'source': None}, 'SRC_OBSERVATION_LOST'),
            ({'isolated': False}, 'ISOLATION_OR_GUARD_LOST'),
            ({'log_growing': False}, 'DATAFLASH_RECORDING_LOST'),
        ]:
            with self.subTest(reason=reason):
                self.setUp()
                self.owner.fault = lambda s: replace(s, **fields) if self.clock.t >= 32 and self.owner.tx_start is not None else s
                self.assertEqual(self.execute()['reason'], reason)
                self.assertIn('revoke', self.owner.calls)

    def test_signals_and_unexpected_exception(self):
        for sig in (signal.SIGINT, signal.SIGTERM, None):
            with self.subTest(sig=sig):
                self.setUp()

                def fault(sample):
                    if self.owner.tx_start is not None:
                        if sig is None:
                            raise RuntimeError('synthetic exception')
                        os.kill(os.getpid(), sig)
                    return sample

                self.owner.fault = fault
                result = self.execute()
                self.assertEqual(result['status'], 'FAIL')
                self.assertEqual(result['reason'], sig.name if sig else 'RuntimeError: synthetic exception')
                self.assertEqual(result['rollback'], 'PASS')


class EvidenceTests(unittest.TestCase):
    def fixture(self):
        trace = [
            {'event': 'phase', 'phase': phase, 'begin': start, 'end': end, 'log_id': 71}
            for phase, start, end in [('A', 0, 30), ('B', 30, 90), ('C', 90, 180)]
        ]
        trace += [
            {'event': 'result', 'status': 'INCONCLUSIVE', 'rollback': 'PASS', 'phases': list('ABC')},
            {'event': 'dataflash_proof', 'id': 71, 'size_before': 20, 'size_after': 100,
             'prefix_parsed': True},
            {'event': 'sample', 'phase': 'A', 'source': 1},
        ]
        for t in (0, 180):
            trace.append({'event': 'clock_anchor', 'pi_monotonic': t, 'pi_utc': 1000 + t,
                          'boot_id': 'synthetic', 'uncertainty_s': .01,
                          'pixhawk_boot_us': (t + 200) * 1000000})
        msgs = []
        for i in range(1800):
            t = i / 10
            ts = (i + 2000) * 100000
            for core in (0, 1):
                for name in ('XKFS', 'XKF1', 'XKF2', 'XKF3', 'XKF4'):
                    msgs.append({'mavpackettype': name, 'TimeUS': ts, 'C': core, 'SS': int(t >= 91)})
            if t >= 30:
                trace.append({'event': 'tx', 'message': 'VISION_POSITION_ESTIMATE', 'write_ok': True,
                              'pi_monotonic': t, 'usec': i * 100000})
                msgs.append({'mavpackettype': 'VISP', 'TimeUS': ts, 'RTimeUS': i * 100000})
            if t >= 120:
                trace.append({'event': 'sample', 'phase': 'C', 'pi_monotonic': t,
                              'flags': 55, 'local_age': .1, 'aiding': True, 'aiding_ref': 'synthetic EKF evidence'})
        return trace, msgs

    def test_pass_and_xy_failure(self):
        trace, msgs = self.fixture()
        self.assertEqual(analyze(trace, msgs)['status'], 'PASS')
        for record in trace:
            if record.get('phase') == 'C' and record['event'] == 'sample':
                record['flags'] = 167
        self.assertEqual(analyze(trace, msgs)['reason'], 'XY_NOT_VALID')

    def test_inconclusive_aiding_missing_and_truncated_file(self):
        trace, msgs = self.fixture()
        self.assertEqual(analyze(trace, msgs, tail_ok=False)['status'], 'INCONCLUSIVE')
        for record in trace:
            if record.get('phase') == 'C' and record['event'] == 'sample':
                record['aiding'] = None
        self.assertEqual(analyze(trace, msgs)['reason'], 'EXTERNAL_NAV_AIDING_UNPROVEN')

    def test_clock_step_and_missing_ekf_rows(self):
        trace, msgs = self.fixture()
        broken = [dict(r, pi_utc=r['pi_utc'] + 2)
                  if r['event'] == 'clock_anchor' and r['pi_monotonic'] == 180 else r
                  for r in trace]
        self.assertEqual(analyze(broken, msgs)['reason'], 'CLOCK_DISCONTINUITY')
        msgs = [r for r in msgs if not (r['mavpackettype'] == 'XKF3' and r['C'] == 1)]
        self.assertEqual(analyze(trace, msgs)['reason'], 'XKF3_A_COVERAGE_GAP')

    def test_dataflash_overrules_ack_or_missing_live_src(self):
        trace, msgs = self.fixture()
        for row in msgs:
            if row['mavpackettype'] == 'XKFS' and row['C'] == 1:
                row['SS'] = 0
        self.assertEqual(analyze(trace, msgs)['reason'], 'SRC2_NOT_ACTIVE_IN_DATAFLASH')


if __name__ == '__main__':
    unittest.main()
