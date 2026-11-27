"""Offline-testable Stage 3 orchestration. No hardware or service adapter."""
from dataclasses import asdict, dataclass
import json
import signal
from typing import Protocol

ORIGIN = (45, 15, 0)
PHASES = (('A', 0, 30), ('B', 30, 90), ('C', 90, 180))


@dataclass(frozen=True)
class Sample:
    heartbeat_age: float
    hold: bool
    armed: bool
    connected: bool
    isolated: bool
    guard_ok: bool
    origin: tuple
    source: int | None  # Independently observed, one-based. Never derived from ACK.
    log_id: int
    log_growing: bool  # Owner-side recording health, no concurrent catalog requests.
    sent: int
    tx_age: float
    pose_age: float
    flags: int | None = None
    local_age: float | None = None
    aiding: bool | None = None
    pixhawk_boot_ms: int | None = None


@dataclass(frozen=True)
class Baseline:
    log_disarmed: int
    source: int | None
    prior_log_sizes: dict
    profile: str


class Owner(Protocol):
    # A real adapter must enforce independent on-owner guards and bounded calls.
    def baseline(self) -> Baseline: ...
    def sample(self) -> Sample: ...
    def confirm_off(self) -> None: ...
    def read_log_disarmed(self) -> int: ...
    def set_log_disarmed(self, value: int) -> None: ...
    def prove_recording(self, prior_sizes: dict) -> int: ...
    def enable_tx_until(self, deadline: float) -> None: ...
    def revoke_tx(self) -> None: ...
    def request_src2(self) -> None: ...
    def src2_ack(self) -> str | None: ...
    def restore_source(self, source: int) -> None: ...
    def restore_profile(self, profile: str) -> None: ...
    def verify_restored(self, baseline: Baseline) -> None: ...


class Stop(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status


def check(ok, reason, status='FAIL'):
    if not ok:
        raise Stop(status, reason)


def safety(s):
    check(s.connected, 'OWNER_LINK_LOST')
    check(0 <= s.heartbeat_age < 1.5, 'HEARTBEAT_STALE')
    check(s.hold, 'HOLD_LOST')
    check(s.armed is False, 'ARMED_OR_UNKNOWN')
    check(s.isolated and s.guard_ok, 'ISOLATION_OR_GUARD_LOST')
    check(s.origin == ORIGIN, 'ORIGIN_MISMATCH')


def run(owner, clock, emit, *, isolation_confirmed=False, logging_approved=False,
        switch_approved=False):
    """clock exposes monotonic(), time(), sleep(); emit accepts structured records."""
    baseline = None
    log_dirty = source_dirty = False
    restore_errors = []
    status, reason = 'FAIL', 'UNFINISHED'
    completed = []
    ack = verified = None
    signals = {}

    def report(event, **fields):
        emit(dict(event=event, pi_monotonic=clock.monotonic(), pi_utc=clock.time(), **fields))

    def interrupted(sig, frame):
        raise Stop('FAIL', signal.Signals(sig).name)

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signals[sig] = signal.signal(sig, interrupted)
        check(isolation_confirmed, 'MANUAL_ISOLATION_UNCONFIRMED')
        baseline = owner.baseline()
        owner.confirm_off()
        first = owner.sample()
        safety(first)
        check(baseline.source in (1, 2, 3) and first.source is not None,
              'INITIAL_SRC_UNKNOWN', 'INCONCLUSIVE')
        check(first.source == baseline.source, 'BASELINE_SRC_MISMATCH')
        original = owner.read_log_disarmed()
        check(original == baseline.log_disarmed, 'LOG_BASELINE_MISMATCH')
        if original != 1:
            check(logging_approved, 'LOG_CHANGE_NOT_APPROVED')
            log_dirty = True  # A failed or interrupted write may have applied.
            owner.set_log_disarmed(1)
            check(owner.read_log_disarmed() == 1, 'LOG_CHANGE_READBACK_FAILED')
        log_id = owner.prove_recording(baseline.prior_log_sizes)
        check(type(log_id) is int and log_id >= 0, 'DATAFLASH_GROWTH_UNPROVEN')

        t0 = clock.monotonic()
        expected_source = baseline.source
        for phase, start, end in PHASES:
            report('phase', phase=phase, begin=t0 + start, end=t0 + end, log_id=log_id)
            start_sample = owner.sample()
            safety(start_sample)
            check(start_sample.source == expected_source, 'SRC_CHANGED_BEFORE_PHASE')
            old_sent = start_sample.sent
            previous_sent = old_sent
            last_progress = t0 + start
            if phase == 'B':
                owner.enable_tx_until(t0 + 180)
            if phase == 'C' and expected_source != 2:
                check(switch_approved, 'SRC2_SWITCH_NOT_APPROVED', 'INCONCLUSIVE')
                source_dirty = True
                owner.request_src2()
            while (now := clock.monotonic()) < t0 + end:
                s = owner.sample()
                now = clock.monotonic()
                safety(s)
                report('sample', phase=phase, **asdict(s))
                check(s.log_id == log_id and s.log_growing, 'DATAFLASH_RECORDING_LOST')
                if phase == 'A':
                    check(s.sent == old_sent, 'TX_IN_PHASE_A')
                else:
                    check(0 <= s.pose_age <= .2, 'POSE_STALE')
                    check(s.sent >= previous_sent, 'TX_COUNTER_RESET')
                    if s.sent > previous_sent:
                        last_progress = now
                    if now - t0 - start >= .5:
                        check(0 <= s.tx_age <= .5 and now - last_progress <= .5, 'TX_STALLED')
                previous_sent = s.sent
                if phase != 'C' or not source_dirty:
                    check(s.source is not None, 'SRC_OBSERVATION_LOST', 'INCONCLUSIVE')
                    check(s.source == expected_source, 'UNEXPECTED_SRC')
                else:
                    disposition = owner.src2_ack()
                    if disposition is not None and disposition != ack:
                        ack = disposition
                        report('src_ack', disposition=ack)
                    check(ack in (None, 'ACCEPTED'), 'SRC2_COMMAND_REJECTED')
                    if s.source == 2 and verified is None:
                        verified = now
                        report('src_verified', source=2)
                    if verified is not None:
                        check(s.source == 2, 'SRC2_LOST_AFTER_VERIFICATION')
                    check(s.source in (None, expected_source, 2), 'UNEXPECTED_SRC')
                    if now >= t0 + start + 5:
                        check(ack is not None, 'SRC2_ACK_MISSING', 'INCONCLUSIVE')
                        check(s.source in (None, 2), 'SRC2_REPORTED_INACTIVE')
                clock.sleep(max(0, min(.1, t0 + end - clock.monotonic())))
            if phase != 'A':
                check((previous_sent - old_sent) / (end - start) >= 8, 'TX_RATE_BELOW_8_HZ')
            completed.append(phase)

        if source_dirty and verified is None:
            status, reason = 'INCONCLUSIVE', 'SRC2_REQUIRES_DATAFLASH_VERIFICATION'
        else:
            status, reason = 'INCONCLUSIVE', 'AWAITING_ALIGNED_DATAFLASH'
    except Stop as exc:
        status, reason = exc.status, str(exc)
    except (Exception, KeyboardInterrupt) as exc:
        status, reason = 'FAIL', f'{type(exc).__name__}: {exc}'
    finally:
        # Preserve the first failure. Do not modify state until TX revoke succeeds.
        for sig in signals:
            signal.signal(sig, signal.SIG_IGN)

        def attempt(label, fn):
            try:
                fn()
                return True
            except Exception as exc:
                restore_errors.append(f'{label}: {type(exc).__name__}: {exc}')
                return False

        try:
            tx_off = attempt('TX_REVOKE', owner.revoke_tx)
            if tx_off:
                tx_off = attempt('TX_OFF_READBACK', owner.confirm_off)
            if tx_off and baseline is not None:
                if source_dirty:
                    attempt('SOURCE_RESTORE', lambda: owner.restore_source(baseline.source))
                if log_dirty:
                    attempt('LOG_RESTORE', lambda: owner.set_log_disarmed(baseline.log_disarmed))
                    attempt('LOG_READBACK', lambda: check(
                        owner.read_log_disarmed() == baseline.log_disarmed, 'LOG_RESTORE_MISMATCH'))
                attempt('PROFILE_RESTORE', lambda: owner.restore_profile(baseline.profile))
                attempt('OWNER_READBACK', lambda: owner.verify_restored(baseline))
        finally:
            for sig, handler in signals.items():
                signal.signal(sig, handler)

    result = dict(status='FAIL' if restore_errors else status, reason=reason,
                  phases=completed, src2_ack=ack, src2_verified_at=verified,
                  rollback='FAIL' if restore_errors else 'PASS', rollback_errors=restore_errors)
    report('result', **result)
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if not args.check or args.live:
        parser.exit(2, 'LIVE_DISABLED: reviewed owner adapter and explicit authorization required\n')
    print(json.dumps({'phases': PHASES, 'live': 'disabled'}))
