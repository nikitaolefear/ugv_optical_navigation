"""Opt-in, hardware-less DISARM/STATUS Unix gateway.

No listeners, databases, devices or child processes are opened on import.
The trusted frontend attests operator claims after its session/RBAC/CSRF
checks; peer credentials identify the service, not a human operator.
"""
import argparse
import copy
import logging
import os
from pathlib import Path
import re
import signal
from threading import Event
import time

from .engine import Rejected, UUID, finite
from .ipc import OwnerClient, OwnerServer, SocketPolicy
from .inhibit import InhibitLatch

LOG = logging.getLogger(__name__)
DELIVERY = frozenset(('QUEUED', 'SENT', 'UNKNOWN', 'REJECTED'))
OBSERVATION = frozenset(('DISARMED_OBSERVED', 'ARMED_OBSERVED', 'UNKNOWN'))
HB_FIELDS = ('heartbeat_age_s', 'heartbeat_rx_serial', 'heartbeat_observed_at', 'heartbeat_source')


def validate_actor(actor_id, actor, request_id):
    if (type(actor_id) is not int or actor_id <= 0 or type(actor) is not str or
            not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', actor) or
            type(request_id) is not str or not UUID.fullmatch(request_id)):
        raise Rejected('INVALID_REQUEST')


def unknown_observation(command, reason):
    """Only remove confidence. Delivery and UUID are never invented here."""
    command['observation_state'] = 'UNKNOWN'
    command['observation_reason'] = reason
    command['observation_pending'] = False
    for field in HB_FIELDS:
        command[field] = None
    return command


class CommandGateway:
    """One-hop relay with no retries, serial FD, force/target fields or API enable.

    Backend failure produces a bound IPC error; it does not manufacture an
    owner transaction or a healthy observation. Durable request idempotency
    stays at the owner. STATUS remains available after disabling new requests.
    """
    def __init__(self, backend, *, enabled=False, clock=time.monotonic,
                 inhibit_file=None):
        if type(enabled) is not bool:
            raise ValueError('enabled must be bool')
        self.backend, self._enabled, self.clock = backend, enabled, clock
        self.inhibit = InhibitLatch(inhibit_file) if inhibit_file is not None else None

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled must be bool')
        self._enabled = enabled

    def _inhibited(self):
        return self.inhibit is not None and self.inhibit.blocked()

    def request(self, actor_id, actor, request_id):
        validate_actor(actor_id, actor, request_id)
        if not self._enabled:
            raise Rejected('DISARM_DISABLED')
        if self._inhibited():
            raise Rejected('DISARM_INHIBITED')
        LOG.info('disarm_gateway intent request_id=%s actor_id=%s', request_id, actor_id)
        return self._forward('DISARM', actor_id, actor, request_id)

    def status(self, actor_id, request_id):
        # STATUS does not create commands. Actor
        # name is display/audit data at this boundary; owner scopes by actor ID.
        validate_actor(actor_id, 'status', request_id)
        result = self._forward('STATUS', actor_id, 'status', request_id)
        if not self._enabled or self._inhibited():
            unknown_observation(result, 'GATEWAY_DISABLED_OR_INHIBITED')
        return result

    def stop_operation(self, operation, actor_id, actor, request_id):
        validate_actor(actor_id, actor, request_id)
        if operation not in ('STOP_STATUS', 'STOP_RESET', 'MISSION_RESUME'):
            raise Rejected('UNKNOWN_OPERATION')
        if operation == 'STOP_RESET' and not getattr(self, 'stop_reset_enabled', False):
            raise Rejected('STOP_RESET_DISABLED')
        if operation == 'MISSION_RESUME' and not getattr(self, 'mission_resume_enabled', False):
            raise Rejected('MISSION_RESUME_DISABLED')
        reply = self.backend.call(operation, {'id': actor_id, 'username': actor}, request_id)
        if type(reply) is not dict or type(reply.get('ok')) is not bool:
            raise Rejected('INVALID_BACKEND_RESPONSE')
        if not reply['ok']:
            raise Rejected('STOP_OWNER_UNAVAILABLE')
        value = reply.get('command')
        if operation == 'MISSION_RESUME':
            if (type(value) is not dict or value.get('request_id') != request_id or
                    type(value.get('resume_accepted')) is not bool):
                raise Rejected('INVALID_MISSION_RESUME_RESPONSE')
            return value
        if (type(value) is not dict or type(value.get('stop_latch')) is not bool or
                type(value.get('reset_available')) is not bool or
                type(value.get('physical_drive_interlocked')) is not bool):
            raise Rejected('INVALID_STOP_RESPONSE')
        if operation == 'STOP_STATUS' and not getattr(self, 'stop_reset_enabled', False):
            return dict(value, reset_available=False)
        return value

    def arm_operation(self, operation, actor_id, actor, request_id):
        validate_actor(actor_id, actor, request_id)
        if operation not in ('ARM', 'ARM_READY', 'ARM_STATUS'):
            raise Rejected('UNKNOWN_OPERATION')
        if not getattr(self, 'arm_enabled', False) or not self._enabled or self._inhibited():
            if operation == 'ARM_READY':
                return {'ready': False, 'reason': 'ARM_DISABLED'}
            raise Rejected('ARM_DISABLED')
        reply = self.backend.call(operation, {'id': actor_id, 'username': actor}, request_id)
        if not isinstance(reply, dict) or reply.get('ok') is not True or not isinstance(reply.get('command'), dict):
            raise Rejected('ARM_OWNER_UNAVAILABLE')
        return reply['command']

    def _forward(self, operation, actor_id, actor, request_id):
        try:
            started = self.clock()
            if not finite(started):
                raise ValueError('Invalid clock')
            reply = self.backend.call(operation, {'id': actor_id, 'username': actor}, request_id)
            now = self.clock()
            if not finite(now) or now < started:
                raise ValueError('Invalid clock')
        except Exception:
            LOG.warning('disarm_gateway delivery_unknown request_id=%s actor_id=%s', request_id, actor_id)
            raise Rejected('BACKEND_RESULT_UNKNOWN') from None
        if type(reply) is not dict or type(reply.get('ok')) is not bool:
            raise Rejected('INVALID_BACKEND_RESPONSE')
        if not reply['ok']:
            error = reply.get('error')
            if type(error) is not str or not re.fullmatch(r'[A-Z0-9_]{1,96}', error):
                error = 'BACKEND_REJECTED'
            raise Rejected(error)
        result = reply.get('command')
        if (type(result) is not dict or result.get('id') != request_id or
                type(result.get('actor_id')) is not int or result['actor_id'] != actor_id or
                result.get('state') not in DELIVERY or result.get('delivery_state') != result['state'] or
                result.get('command_outcome') != 'UNKNOWN' or
                result.get('observation_state') not in OBSERVATION or
                type(result.get('observation_pending')) is not bool):
            raise Rejected('INVALID_BACKEND_RESPONSE')
        result = copy.deepcopy(result)
        freshness = result.get('heartbeat_freshness_s')
        if not finite(freshness) or not 0 < freshness <= 3.0:
            raise Rejected('INVALID_BACKEND_RESPONSE')
        if result['observation_state'] == 'UNKNOWN':
            if any(result.get(field) is not None for field in HB_FIELDS):
                raise Rejected('INVALID_BACKEND_RESPONSE')
        else:
            source = result.get('heartbeat_source')
            stamp, age = result.get('heartbeat_observed_at'), result.get('heartbeat_age_s')
            serial = result.get('heartbeat_rx_serial')
            if (result['delivery_state'] != 'SENT' or not finite(stamp) or not finite(age) or age < 0 or
                    type(serial) is not int or serial <= 0 or type(source) is not list or len(source) != 2 or
                    type(source[0]) is not int or not 1 <= source[0] <= 255 or type(source[1]) is not int or source[1] != 1):
                raise Rejected('INVALID_BACKEND_RESPONSE')
            # Both processes share the host monotonic clock. Also account for
            # all relay latency conservatively, without re-stamping evidence.
            current_age = max(age + now - started, now - stamp)
            if now < stamp or not finite(current_age) or current_age >= freshness:
                unknown_observation(result, 'STALE_DURING_GATEWAY_RELAY')
            else:
                result['heartbeat_age_s'] = current_age
        LOG.info('disarm_gateway result request_id=%s actor_id=%s delivery=%s observation=%s',
                 request_id, actor_id, result['delivery_state'], result['observation_state'])
        return result


def boot_arm_enabled(value):
    if not value:
        return False
    try:
        return value == Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', required=True, help='Private ingress Unix socket')
    parser.add_argument('--frontend-uid', required=True, type=int)
    parser.add_argument('--ingress-gid', required=True, type=int)
    parser.add_argument('--backend-socket', required=True)
    parser.add_argument('--owner-uid', required=True, type=int)
    parser.add_argument('--backend-gid', required=True, type=int)
    parser.add_argument('--enable-disarm', action='store_true', help='Explicit opt-in; default blocks new commands')
    parser.add_argument('--inhibit-file', default='/run/ugv-disarm-policy/inhibit',
                        help='Presence inhibits DISARM; parent must be root-owned/non-writable')
    parser.add_argument('--enable-stop-reset', action='store_true', help='Only after native STOP integration acceptance')
    parser.add_argument('--enable-mission-resume', action='store_true')
    parser.add_argument('--web-arm-boot-id', help='Current boot UUID for a transient ARM opt-in')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s %(message)s')
    uid = os.getuid()
    if os.geteuid() == 0 or uid == 0:
        parser.error('Gateway must run as its dedicated non-root service UID')
    if (args.frontend_uid <= 0 or args.owner_uid <= 0 or
            len({uid, args.frontend_uid, args.owner_uid}) != 3):
        parser.error('Frontend, gateway and owner require three distinct non-root UIDs')
    if min(args.ingress_gid, args.backend_gid) <= 0 or args.ingress_gid == args.backend_gid:
        parser.error('Ingress/backend require distinct non-root client groups')
    backend = OwnerClient(args.backend_socket,
        policy=SocketPolicy(args.owner_uid, args.backend_gid, 0o710, 0o660), timeout=.5)
    gateway = CommandGateway(backend, enabled=args.enable_disarm, inhibit_file=args.inhibit_file)
    gateway.stop_reset_enabled = args.enable_stop_reset
    gateway.mission_resume_enabled = args.enable_mission_resume and args.enable_stop_reset
    gateway.arm_enabled = boot_arm_enabled(args.web_arm_boot_id)
    server = OwnerServer(gateway, args.socket, allowed_uid=args.frontend_uid,
        arm_operations={op: (lambda aid, actor, rid, op=op: gateway.arm_operation(op, aid, actor, rid))
                        for op in ('ARM', 'ARM_READY', 'ARM_STATUS')},
        stop_operations={op: (lambda aid, actor, rid, op=op: gateway.stop_operation(op, aid, actor, rid))
                         for op in ('STOP_STATUS', 'STOP_RESET', 'MISSION_RESUME')},
        policy=SocketPolicy(os.getuid(), args.ingress_gid, 0o710, 0o660))
    stop = Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    server.start()
    try:
        stop.wait()
    finally:
        gateway.set_enabled(False)
        server.close()


if __name__ == '__main__':
    main()
