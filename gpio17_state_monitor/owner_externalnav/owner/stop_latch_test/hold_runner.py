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
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--externalnav-observe-only',action='store_true')
    group.add_argument('--externalnav-tx',action='store_true')
    parser.add_argument('--externalnav-bench-profile',
                        help='Measured ID24 6DoF profile; OBSERVE_ONLY, never TX')
    parser.add_argument('--diagnostic-params',action='store_true')
    parser.add_argument('--diagnostic-brake-params', action='store_true')
    parser.add_argument('--export-pixhawk-params', action='store_true')
    parser.add_argument('--observe-auto-route', action='store_true',
                        help='Download mission items read-only through this owner')
    parser.add_argument('--enable-bench-hold', action='store_true',
                        help='Maintenance only; legacy HTTP mutations remain disabled')
    parser.add_argument('--web-disarm-socket', help='Explicit opt-in; private Unix socket path')
    parser.add_argument('--web-disarm-db', help='Durable DISARM audit database path')
    parser.add_argument('--web-disarm-client-uid', type=int, help='Dedicated gateway UID')
    parser.add_argument('--web-disarm-client-gid', type=int, help='Private backend client GID')
    parser.add_argument('--inhibit-file', default='/run/ugv-disarm-policy/inhibit')
    parser.add_argument('--web-arm-boot-id', help='Current boot UUID for a transient ARM opt-in')
    parser.add_argument('--functional-brake-boot-id', help='Bench-approved functional brake lease for this boot')
    parser.add_argument('--brake-hold-only', action='store_true',
                        help='Bench GPIO17 verification with HOLD as the only brake message')
    pwm = parser.add_mutually_exclusive_group()
    pwm.add_argument('--observe-brake-pwm', action='store_true')
    pwm.add_argument('--brake-pwm-interval-us', type=int, choices=(500000, 0, -1),
                     help='Observe outputs and opt into one telemetry-only interval request')
    args = parser.parse_args(argv)
    pwm_enabled = args.observe_brake_pwm or args.brake_pwm_interval_us is not None
    if pwm_enabled and (not args.web_disarm_socket or args.enable_bench_hold or args.diagnostic_params or args.diagnostic_brake_params or args.export_pixhawk_params or
                        args.web_arm_boot_id):
        parser.error('PWM observation requires the sole owner in a dedicated diagnostic profile')
    if args.export_pixhawk_params and any((not args.web_disarm_socket, args.enable_bench_hold,
                                          args.functional_brake_boot_id, args.web_arm_boot_id,
                                          args.diagnostic_params, args.diagnostic_brake_params)):
        parser.error('Parameter export requires the sole owner and a dedicated diagnostic profile')
    if args.observe_auto_route and (not args.web_disarm_socket or args.export_pixhawk_params):
        parser.error('Mission readback requires the sole owner without concurrent parameter export')
    if args.functional_brake_boot_id and (args.enable_bench_hold or not args.web_disarm_socket):
        parser.error('Functional brakes require the DISARM owner and exclude maintenance HOLD')
    if args.functional_brake_boot_id and args.brake_pwm_interval_us is not None:
        parser.error('Functional brakes allow only passive PWM observation')
    if args.functional_brake_boot_id and (args.web_arm_boot_id or args.externalnav_tx):
        parser.error('Functional brake bench excludes Web ARM and ExternalNav TX')
    if args.brake_hold_only and not (args.functional_brake_boot_id and
                                     args.observe_brake_pwm and args.externalnav_observe_only):
        parser.error('Brake HOLD-only requires the functional owner and passive observations')
    if args.web_arm_boot_id and (args.enable_bench_hold or not args.web_disarm_socket):
        parser.error('Web ARM requires the DISARM owner and excludes maintenance HOLD')
    if args.externalnav_bench_profile and not args.externalnav_observe_only:
        parser.error('Bench geometry requires --externalnav-observe-only and forbids TX')
    if bool(args.web_disarm_socket) != bool(args.web_disarm_db):
        parser.error('DISARM requires both socket and database')
    if args.web_disarm_socket:
        if (args.web_disarm_client_uid is None or args.web_disarm_client_uid <= 0 or
                args.web_disarm_client_uid == os.getuid() or
                args.web_disarm_client_gid is None or args.web_disarm_client_gid <= 0):
            parser.error('DISARM requires a distinct nonroot gateway UID and private client GID')
    elif args.web_disarm_client_uid is not None or args.web_disarm_client_gid is not None:
        parser.error('Gateway credentials require the explicit DISARM socket/database')
    if (args.externalnav_observe_only or args.externalnav_tx or args.diagnostic_params or args.diagnostic_brake_params) and not args.web_disarm_socket:
        parser.error('ExternalNav/diagnostics require the existing DISARM owner profile')
    return args


def main(*, interlock_io=None, mission_supervisor=None, rover_auto_factory=None):
    args = parse_args()
    if args.brake_hold_only and not os.path.isfile(args.inhibit_file):
        raise RuntimeError('Brake HOLD-only requires the DISARM inhibit marker')
    reader = PixhawkTelemetry(opener=partial(open_with_owner_helper,
        bench_write=bool(args.web_disarm_socket or args.enable_bench_hold)))
    if rover_auto_factory is not None:
        if mission_supervisor is not None:
            raise ValueError('Specify only one mission supervisor')
        mission_supervisor = rover_auto_factory(reader)
    encoder = None
    if args.web_disarm_socket:
        from pymavlink.dialects.v20 import ardupilotmega as mav
        encoder = mav.MAVLink(None, srcSystem=255, srcComponent=190)
    raw_hold_sender = hold_sender(encoder=encoder)
    from stop_latch_test.diagnostics import Diagnostics
    if encoder is None:
        from pymavlink.dialects.v20 import ardupilotmega as mav
        encoder=mav.MAVLink(None,srcSystem=255,srcComponent=190)
    diagnostics=Diagnostics(reader,encoder,query=args.diagnostic_params,
                            brake_query=args.diagnostic_brake_params, export_params=args.export_pixhawk_params,
                            pwm_observe=bool(args.web_disarm_socket or args.observe_brake_pwm or
                                             args.brake_pwm_interval_us is not None),
                            pwm_interval=args.brake_pwm_interval_us)
    nav=None
    if args.externalnav_observe_only or args.externalnav_tx:
        from stop_latch_test.externalnav import ExternalNav
        nav=ExternalNav(reader,tx=args.externalnav_tx,bench_profile=args.externalnav_bench_profile)
        reader.tick_interval=.02
        atexit.register(nav.close)

    def maintenance_hold(fd, target):
        if not args.enable_bench_hold:
            raise OSError('Bench HOLD disabled')
        return raw_hold_sender(fd, target)

    bench = HoldBench(args.journal, reader, maintenance_hold)
    reader.bench_tick = bench.tick
    production = None
    arm = None
    if args.web_disarm_socket:
        from disarm_control.engine import DisarmEngine, disarm_sender
        from disarm_control.ipc import OwnerServer, SocketPolicy
        from disarm_control.inhibit import InhibitedControl, InhibitLatch
        control = DisarmEngine(reader, bench, disarm_sender(encoder=encoder), args.web_disarm_db,
                               clock=reader.clock)
        bench.disarm_pending = lambda: control.active is not None or control.storage_fault or control.clock_fault
        guarded = InhibitedControl(control, InhibitLatch(args.inhibit_file))
        from production_stop.rover_auto import RoverAutoAdapter
        mission_reader = None
        if args.observe_auto_route:
            from production_stop.mission_reader import OwnerMissionReader
            mission_reader = OwnerMissionReader(reader, encoder)
            reader.tick_interval = .02
            if mission_supervisor is None:
                mission_supervisor = RoverAutoAdapter(reader, None, read_route=mission_reader.snapshot)
            elif isinstance(mission_supervisor, RoverAutoAdapter):
                mission_supervisor.read_route = mission_reader.snapshot
            else:
                raise ValueError('Mission readback requires the Rover AUTO adapter')

        def observe(message):
            guarded.observe(message)
            diagnostics.observe(message)
            if mission_reader is not None:
                mission_reader.observe(message)
            if isinstance(mission_supervisor, RoverAutoAdapter):
                mission_supervisor.observe(message)
        reader.command_observer = observe

        from production_stop.owner_bridge import OwnerProductionStop
        from production_stop.state_monitor import StateMonitor
        native = None
        if interlock_io is not None:
            from production_stop.hardwired_interlock import HardwiredInterlock
            native = HardwiredInterlock(interlock_io, reader)
        production = OwnerProductionStop(
            reader, bench, args.web_disarm_db + '.stop-reset.sqlite3',
            native=native, mission_supervisor=mission_supervisor,
            state_monitor=StateMonitor(read_brake_pwm=diagnostics.brake_pwm_snapshot),
        )
        atexit.register(production.close)
        if mission_reader is not None:
            def mission_link_lost():
                mission_reader.link_lost()
                production.mavlink_lost()

            reader.link_lost_observer = mission_link_lost

        from pathlib import Path
        from production_stop.web_arm import WebArm, arm_sender
        brake = None
        if args.functional_brake_boot_id:
            from production_stop.brake_link import BrakeLink
            try:
                if args.functional_brake_boot_id == Path('/proc/sys/kernel/random/boot_id').read_text().strip():
                    brake = BrakeLink(reader, control, production, encoder,
                                      lambda: diagnostics.rc, hold_only=args.brake_hold_only)
                    production.brake = diagnostics.brake = brake
                elif args.brake_hold_only:
                    raise RuntimeError('Brake HOLD-only requires the current Pi boot id')
            except OSError:
                if args.brake_hold_only:
                    raise
                pass  # Without an owner lease, configured Pixhawk brakes hold.
        if args.web_arm_boot_id:
            try:
                if args.web_arm_boot_id != Path('/proc/sys/kernel/random/boot_id').read_text().strip():
                    raise ValueError('ARM opt-in belongs to a different boot')
                arm = WebArm(reader, control, production, args.web_disarm_db + '.arm.sqlite3',
                             arm_sender(encoder))
                atexit.register(arm.close)
            except Exception:
                import logging
                logging.exception('Web ARM unavailable')

        def priority_disarm(actor_id, actor, request_id):
            with reader.lock:
                if arm is not None:
                    arm.preempt()
                previous = control.active
                reply = guarded.request(actor_id, actor, request_id)
                if brake is not None:
                    brake.on_disarm(request_id, previous)
                return reply

        def owner_tick(fd):
            guarded.tick(fd)
            if brake is not None:
                brake.tick(fd)
            production.tick(fd)
            if arm is not None:
                arm.tick(fd)
            bench.tick(fd)
            diagnostics.tick(fd,command_pending=bench.disarm_pending() or (arm is not None and arm.active is not None))
            if nav is not None:
                nav.tick(fd,command_pending=bench.disarm_pending())
            if mission_reader is not None:
                with reader.lock:
                    restore_pending = (isinstance(mission_supervisor, RoverAutoAdapter) and
                                       mission_supervisor.phase in ('RESTORE_QUEUED', 'RESTORING'))
                    mission_reader.tick(fd, command_pending=(
                        restore_pending or bench.disarm_pending() or
                        (brake is not None and brake.phase == 'PENDING') or
                        (arm is not None and arm.active is not None)
                    ))

        reader.bench_tick = owner_tick
        channel = OwnerServer(guarded, args.web_disarm_socket,
            stop_operations=production.rpc_operations(),
            arm_operations={'ARM': arm.request, 'ARM_READY': arm.ready, 'ARM_STATUS': arm.status} if arm else None,
            disarm_request=priority_disarm,
            allowed_uid=args.web_disarm_client_uid,
            policy=SocketPolicy(os.getuid(), args.web_disarm_client_gid, 0o710, 0o660))
        channel.start()
        atexit.register(control.close)
        atexit.register(channel.close)
    stop = threading.Event()
    gpio = threading.Thread(target=monitor, args=(bench, stop),
                            kwargs={'observer': production.gpio_observed if production else None},
                            daemon=True)
    page_app=create_app(bench,hold=True)
    from flask import jsonify
    page_app.add_url_rule('/api/owner-diagnostics','owner_diagnostics',lambda:jsonify(diagnostics.snapshot()),methods=['GET'])
    if args.export_pixhawk_params:
        page_app.add_url_rule('/api/pixhawk-params-export', 'parameter_export',
                              lambda: jsonify(diagnostics.export_snapshot()), methods=['GET'])
    if production is not None:
        page_app.add_url_rule('/api/production-stop', 'production_stop',
            lambda: jsonify(production.status(0, 'diagnostic', '')), methods=['GET'])
    if nav is not None:
        page_app.add_url_rule('/api/externalnav','externalnav',lambda:jsonify(nav.snapshot()),methods=['GET'])
        nav.start()
    page = make_server('127.0.0.1', 8084, page_app, threaded=True)
    threading.Thread(target=page.serve_forever, daemon=True).start()
    gpio.start()
    atexit.register(stop.set)
    atexit.register(reader.close)
    atexit.register(page.shutdown)
    reader.start()
    telemetry_app(telemetry=reader).run(host='127.0.0.1', port=8082, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
