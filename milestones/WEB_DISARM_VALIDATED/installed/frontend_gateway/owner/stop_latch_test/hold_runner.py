"""Explicit bench replacement entry point for 8082's sole serial owner.
No hardware access at import. Do not run alongside ugv-hmi.service.
"""
import argparse
import atexit
import os
import threading
from functools import partial
from werkzeug.serving import make_server
from web_hmi.app import create_app as telemetry_app
from web_hmi.pixhawk_telemetry import PixhawkTelemetry
from web_hmi.pixhawk_owner_client import open_with_owner_helper
from stop_latch_test.hold_bench import HoldBench, hold_sender
from stop_latch_test.stop_latch import monitor
from stop_latch_test.server import create_app


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', required=True)
    parser.add_argument('--enable-bench-hold', action='store_true',
                        help='Maintenance only; legacy HTTP mutations remain disabled')
    parser.add_argument('--web-disarm-socket', help='Explicit opt-in; private Unix socket path')
    parser.add_argument('--web-disarm-db', help='Durable DISARM audit database path')
    parser.add_argument('--web-disarm-client-uid', type=int, help='Dedicated gateway UID')
    parser.add_argument('--web-disarm-client-gid', type=int, help='Private backend client GID')
    parser.add_argument('--inhibit-file', default='/run/ugv-disarm-policy/inhibit')
    args = parser.parse_args(argv)
    if bool(args.web_disarm_socket) != bool(args.web_disarm_db):
        parser.error('DISARM requires both socket and database')
    if args.web_disarm_socket:
        if (args.web_disarm_client_uid is None or args.web_disarm_client_uid <= 0 or
                args.web_disarm_client_uid == os.getuid() or
                args.web_disarm_client_gid is None or args.web_disarm_client_gid <= 0):
            parser.error('DISARM requires a distinct nonroot gateway UID and private client GID')
    elif args.web_disarm_client_uid is not None or args.web_disarm_client_gid is not None:
        parser.error('Gateway credentials require the explicit DISARM socket/database')
    return args


def main():
    args = parse_args()
    reader = PixhawkTelemetry(opener=partial(open_with_owner_helper,
        bench_write=bool(args.web_disarm_socket or args.enable_bench_hold)))
    encoder = None
    if args.web_disarm_socket:
        from pymavlink.dialects.v20 import ardupilotmega as mav
        encoder = mav.MAVLink(None, srcSystem=255, srcComponent=190)
    raw_hold_sender = hold_sender(encoder=encoder)

    def maintenance_hold(fd, target):
        if not args.enable_bench_hold:
            raise OSError('Bench HOLD disabled')
        return raw_hold_sender(fd, target)

    bench = HoldBench(args.journal, reader, maintenance_hold)
    reader.bench_tick = bench.tick
    if args.web_disarm_socket:
        from disarm_control.engine import DisarmEngine, disarm_sender
        from disarm_control.ipc import OwnerServer, SocketPolicy
        from disarm_control.inhibit import InhibitedControl, InhibitLatch
        control = DisarmEngine(reader, bench, disarm_sender(encoder=encoder), args.web_disarm_db,
                               clock=reader.clock)
        bench.disarm_pending = lambda: control.active is not None or control.storage_fault or control.clock_fault
        guarded = InhibitedControl(control, InhibitLatch(args.inhibit_file))
        reader.command_observer = guarded.observe

        def owner_tick(fd):
            guarded.tick(fd)
            bench.tick(fd)

        reader.bench_tick = owner_tick
        channel = OwnerServer(guarded, args.web_disarm_socket,
            allowed_uid=args.web_disarm_client_uid,
            policy=SocketPolicy(os.getuid(), args.web_disarm_client_gid, 0o710, 0o660))
        channel.start()
        atexit.register(control.close)
        atexit.register(channel.close)
    stop = threading.Event()
    gpio = threading.Thread(target=monitor, args=(bench, stop), daemon=True)
    page = make_server('127.0.0.1', 8084, create_app(bench, hold=True), threaded=True)
    threading.Thread(target=page.serve_forever, daemon=True).start()
    gpio.start()
    atexit.register(stop.set)
    atexit.register(reader.close)
    atexit.register(page.shutdown)
    reader.start()
    telemetry_app(telemetry=reader).run(host='127.0.0.1', port=8082, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
