"""Current owner HTTP reader. Diagnostic writes require interfaces absent today."""
from dataclasses import dataclass
import json
from http.client import HTTPException
import math
import time
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class OwnerUnavailable(RuntimeError):
    pass


class UnsupportedOperation(RuntimeError):
    pass


ENDPOINTS = {
    'telemetry': 'http://127.0.0.1:8082/api/telemetry',
    'externalnav': 'http://127.0.0.1:8084/api/externalnav',
    'production-stop': 'http://127.0.0.1:8084/api/production-stop',
    'owner-diagnostics': 'http://127.0.0.1:8084/api/owner-diagnostics',
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class HTTPReader:
    def __init__(self, opener=None, timeout=.35):
        if not 0 < timeout <= 1:
            raise ValueError('invalid timeout')
        self.opener = opener if opener is not None else build_opener(ProxyHandler({}), NoRedirect())
        self.timeout = timeout

    def get(self, endpoint):
        request = Request(ENDPOINTS[endpoint], method='GET')
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('response too large')
            value = json.loads(raw, parse_constant=_reject_constant)
            if type(value) is not dict:
                raise ValueError('expected JSON object')
            return value
        except (URLError, OSError, ValueError, EOFError, HTTPException) as exc:
            raise OwnerUnavailable(f'{endpoint}: {exc}') from exc


def _reject_constant(value):
    raise ValueError(f'invalid JSON constant: {value}')


def _age(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise OwnerUnavailable('missing or invalid age')
    return value


@dataclass(frozen=True)
class Observation:
    pi_monotonic: float
    pi_utc: float
    poll_span: float
    heartbeat_age: float
    tx_enabled: bool
    sent: int
    pose_age: float | None
    origin: dict | None
    local_position: dict | None
    pixhawk_boot_ms: int | None
    flags: int | None
    source: int | None
    source_ack_state: str | None
    raw: dict


class OwnerAdapter:
    def __init__(self, transport, clock=time, *, isolation_confirmed=False):
        self.transport = transport
        self.clock = clock
        self.isolation_confirmed = isolation_confirmed

    def observe(self):
        start = self.clock.monotonic()
        raw = {name: self.transport.get(name) for name in ENDPOINTS}
        now = self.clock.monotonic()
        span = _age(now - start)
        try:
            t, n, s, d = (raw[name] for name in ENDPOINTS)
            mode = t['flight_mode']
            hb = t['messages']['HEARTBEAT']
            hb_age = _age(hb['age_s']) + span
            if (t['connection'] != 'CONNECTED' or hb['state'] != 'FRESH' or hb_age >= 1
                    or mode['state'] != 'FRESH' or mode['value']['custom_mode'] != 4
                    or mode['value']['autopilot'] != 3 or mode['value']['vehicle_type'] != 10
                    or t['armed'] != {'state': 'FRESH', 'value': 'DISARMED'}):
                raise OwnerUnavailable('fresh Rover HOLD/DISARMED not confirmed')
            if (s['stop_latch'] is not True or s['brake_release'] != 'HOLD'
                    or 'BRAKE_HOLD_ONLY' not in s['blocking_reasons'] or s['reasons'] != []):
                raise OwnerUnavailable('STOP latch / HOLD-only not confirmed')
            if type(n['tx_enabled']) is not bool or type(n['sent']) is not int or n['sent'] < 0:
                raise OwnerUnavailable('invalid TX state')
            ekf = t['ekf'].get('EKF_STATUS_REPORT') or {}
            flags = (ekf['value']['flags'] if ekf.get('state') == 'FRESH' else None)
            if flags is not None and (type(flags) is not int or not 0 <= flags <= 65535):
                raise OwnerUnavailable('invalid EKF flags')
            local = d.get('local_position_ned')
            if local is not None and _age(local['age_s']) + span >= 1:
                local = None
            boot_ms = local.get('time_boot_ms') if local else None
            if boot_ms is not None and (type(boot_ms) is not int or not 0 <= boot_ms < 2**32):
                raise OwnerUnavailable('invalid Pixhawk boot timestamp')
            pose_age = n.get('pose_age_s')
            if pose_age is not None:
                pose_age = _age(pose_age) + span
            return Observation(now, self.clock.time(), span, hb_age, n['tx_enabled'], n['sent'],
                               pose_age, d.get('ekf_origin'), local, boot_ms, flags, None,
                               d.get('local_ekf_setup'), raw)
        except (KeyError, AttributeError, IndexError, TypeError) as exc:
            raise OwnerUnavailable(f'invalid owner response: {exc}') from exc

    def confirm_off(self):
        obs = self.observe()
        if obs.tx_enabled or obs.sent != 0 or obs.raw['externalnav'].get('nav_bench_tx') is not False:
            raise OwnerUnavailable('observe-only TX=0 not confirmed')

    def read_log_disarmed(self):
        raise UnsupportedOperation('LOG_DISARMED absent from current navigation readback allowlist')

    def src2_ack(self):
        raise UnsupportedOperation('no attempt-correlated ACK on current API; use raw historical state only')

    def baseline(self):
        raise UnsupportedOperation('no baseline with fresh LOG_DISARMED, active SRC and catalog')

    def sample(self):
        raise UnsupportedOperation('no runner Sample with active SRC, inhibit, logger and lease proof; use observe()')

    def set_log_disarmed(self, value):
        raise UnsupportedOperation('no allowed parameter write interface')

    def prove_recording(self, prior_sizes):
        raise UnsupportedOperation('no active logger counter or compatible catalog interface')

    def enable_tx_until(self, deadline):
        raise UnsupportedOperation('no client-bound TX lease interface')

    def revoke_tx(self):
        raise UnsupportedOperation('revoke_static is owner-local; no client RPC')

    def request_src2(self):
        raise UnsupportedOperation('one-shot startup action only; no in-session SRC2 RPC')

    def restore_source(self, source):
        raise UnsupportedOperation('no independently verified source restore interface')

    def restore_profile(self, profile):
        raise UnsupportedOperation('profile rollback belongs to privileged lifecycle procedure')

    def verify_restored(self, baseline):
        self.confirm_off()
        raise UnsupportedOperation('TX off observed; full restore requires independent SRC/parameter/profile readback')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'preflight', 'retest'))
    args = parser.parse_args()
    if args.action != 'check':
        parser.exit(2, 'LIVE_DISABLED: missing reviewed owner integration and explicit authorization\n')
    print(json.dumps({'endpoints': ENDPOINTS, 'live': 'disabled'}))
