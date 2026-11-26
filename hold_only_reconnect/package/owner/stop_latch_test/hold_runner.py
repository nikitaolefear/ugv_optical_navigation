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
from stop_latch_test.navigation_hold import NavigationHold
from stop_latch_test.stop_latch import monitor
from stop_latch_test.server import create_app


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', required=True)
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--externalnav-observe-only',action='store_true')
    group.add_argument('--externalnav-tx',action='store_true')
    parser.add_argument('--nav-bench-tx', action='store_true',
                        help='Opt in to bounded planar TX with HOLD-only and a verified alignment record')
    parser.add_argument('--nav-mission-tx', action='store_true',
                        help='Planar ExternalNav and mission storage for an isolated HOLD-only mission session')
    parser.add_argument('--local-ekf-bench-action', choices=('origin', 'src2', 'home'),
                        help='Separately approved one-shot local EKF action with VESC physically isolated')
    parser.add_argument('--externalnav-bench-profile',
                        help='Measured ID24 6DoF profile; OBSERVE_ONLY, never TX')
    parser.add_argument('--diagnostic-params',action='store_true')
    parser.add_argument('--ch6-mode-change', choices=('apply', 'rollback'))
    parser.add_argument('--dynamic-mission-profile',
                        help='Separately approved mission storage profile in HOLD-only')
    parser.add_argument('--navigation-readback', action='store_true',
                        help='Read EKF source parameters and GPS_GLOBAL_ORIGIN while DISARMED')
    parser.add_argument('--navigation-hold-watchdog', action='store_true',
                        help='Separately approved AUTO supervision on the existing owner')
    parser.add_argument('--diagnostic-brake-params', action='store_true')
    parser.add_argument('--export-pixhawk-params', action='store_true')
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
    if args.nav_mission_tx and (args.nav_bench_tx or not args.dynamic_mission_profile):
        parser.error('NAV_MISSION_TX requires mission storage and excludes the short bench TX profile')
    planar_tx = args.nav_bench_tx or args.nav_mission_tx
    pwm_enabled = args.observe_brake_pwm or args.brake_pwm_interval_us is not None
    if pwm_enabled and (not args.web_disarm_socket or args.enable_bench_hold or args.diagnostic_params or args.diagnostic_brake_params or args.export_pixhawk_params or
                        args.web_arm_boot_id):
        parser.error('PWM observation requires the sole owner in a dedicated diagnostic profile')
    if args.export_pixhawk_params and any((not args.web_disarm_socket, args.enable_bench_hold,
                                          args.functional_brake_boot_id, args.web_arm_boot_id,
                                          args.diagnostic_params, args.diagnostic_brake_params)):
        parser.error('Parameter export requires the sole owner and a dedicated diagnostic profile')
    if args.functional_brake_boot_id and (args.enable_bench_hold or not args.web_disarm_socket):
        parser.error('Functional brakes require the DISARM owner and exclude maintenance HOLD')
    if args.functional_brake_boot_id and args.brake_pwm_interval_us is not None:
        parser.error('Functional brakes allow only passive PWM observation')
    if args.functional_brake_boot_id and (args.web_arm_boot_id or args.externalnav_tx):
        parser.error('Functional brake bench excludes Web ARM and ExternalNav TX')
    if args.brake_hold_only and not (args.functional_brake_boot_id and
                                     args.observe_brake_pwm and args.externalnav_observe_only):
        parser.error('Brake HOLD-only requires the functional owner and passive observations')
    if planar_tx and (not (args.brake_hold_only and args.navigation_readback)
            or args.externalnav_bench_profile or args.ch6_mode_change
            or args.navigation_hold_watchdog or args.diagnostic_params or args.diagnostic_brake_params):
        parser.error('NAV_BENCH_TX requires a standalone HOLD-only navigation readback profile')
    if args.local_ekf_bench_action and (not (args.brake_hold_only and args.navigation_readback)
            or planar_tx or args.externalnav_bench_profile or args.dynamic_mission_profile
            or args.ch6_mode_change or args.navigation_hold_watchdog
            or args.diagnostic_params or args.diagnostic_brake_params):
        parser.error('Local EKF action requires a standalone, TX-off HOLD-only owner')
    if args.web_arm_boot_id and (args.enable_bench_hold or not args.web_disarm_socket):
        parser.error('Web ARM requires the DISARM owner and excludes maintenance HOLD')
    if args.externalnav_bench_profile and not args.externalnav_observe_only:
        parser.error('Bench geometry requires --externalnav-observe-only and forbids TX')
    if args.ch6_mode_change and not (args.navigation_readback and args.brake_hold_only):
        parser.error('CH6 mode change requires navigation readback and HOLD-only')
    if args.dynamic_mission_profile and not (args.brake_hold_only and args.navigation_readback and
                                             not args.externalnav_bench_profile and not args.ch6_mode_change):
        parser.error('Mission storage requires HOLD-only, navigation readback and surveyed map geometry')
    if args.navigation_readback and (not args.web_disarm_socket or args.export_pixhawk_params):
        parser.error('Navigation readback requires the existing owner and excludes full export')
    if args.navigation_hold_watchdog and (not args.externalnav_tx or args.web_arm_boot_id or
                                         args.enable_bench_hold or args.export_pixhawk_params):
        parser.error('Navigation watchdog requires the approved ExternalNav TX owner profile')
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


def main(*, interlock_io=None, native_interlock=None):
    if interlock_io is not None and native_interlock is not None:
        raise ValueError('Supply only one verified hardware interlock provider')
    args = parse_args()
    planar_tx = args.nav_bench_tx or args.nav_mission_tx
    if args.brake_hold_only and not os.path.isfile(args.inhibit_file):
        raise RuntimeError('Brake HOLD-only requires the DISARM inhibit marker')
    reader = PixhawkTelemetry(opener=partial(open_with_owner_helper,
        bench_write=bool(args.web_disarm_socket or args.enable_bench_hold)))
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
                            pwm_observe=(args.observe_brake_pwm or args.brake_pwm_interval_us is not None),
                            pwm_interval=args.brake_pwm_interval_us,
                            manual_readback=args.brake_hold_only,
                            ch8_readback=bool(args.functional_brake_boot_id and not args.brake_hold_only),
                            navigation_readback=args.navigation_readback)
    nav=None
    if args.externalnav_observe_only or args.externalnav_tx:
        from stop_latch_test.externalnav import ExternalNav
        nav=ExternalNav(reader,tx=args.externalnav_tx,bench_profile=args.externalnav_bench_profile)
        reader.tick_interval=.02
        atexit.register(nav.close)

    navigation_hold = None
    if args.navigation_hold_watchdog:
        navigation_hold = NavigationHold(reader, nav.snapshot, raw_hold_sender)

    def maintenance_hold(fd, target):
        if not args.enable_bench_hold:
            raise OSError('Bench HOLD disabled')
        return raw_hold_sender(fd, target)

    bench = HoldBench(args.journal, reader, maintenance_hold)
    reader.bench_tick = bench.tick
    production = None
    mode_change = None
    mission = None
    arm = None
    if args.web_disarm_socket:
        from disarm_control.engine import DisarmEngine, disarm_sender
        from disarm_control.ipc import BRAKE_OPERATIONS, MISSION_OPERATIONS, OwnerServer, SocketPolicy
        from disarm_control.inhibit import InhibitedControl, InhibitLatch
        control = DisarmEngine(reader, bench, disarm_sender(encoder=encoder), args.web_disarm_db,
                               clock=reader.clock)
        bench.disarm_pending = lambda: control.active is not None or control.storage_fault or control.clock_fault
        guarded = InhibitedControl(control, InhibitLatch(args.inhibit_file))
        def observe(message):
            guarded.observe(message)
            diagnostics.observe(message)
            if mission is not None:
                mission.observe(message)
        reader.command_observer = observe

        from production_stop.owner_bridge import OwnerProductionStop
        native = native_interlock
        if interlock_io is not None:
            from production_stop.hardwired_interlock import HardwiredInterlock
            native = HardwiredInterlock(interlock_io, reader)
        production = OwnerProductionStop(reader, bench, args.web_disarm_db + '.stop-reset.sqlite3',
                                         native=native)
        atexit.register(production.close)

        from pathlib import Path
        from production_stop.web_arm import WebArm, arm_sender
        brake = None
        if args.functional_brake_boot_id:
            from production_stop.brake_link import BrakeLink
            try:
                if args.functional_brake_boot_id == Path('/proc/sys/kernel/random/boot_id').read_text().strip():
                    brake = BrakeLink(reader, control, production, encoder,
                                      lambda: diagnostics.rc, hold_only=args.brake_hold_only,
                                      rc_health=lambda: diagnostics.sys,
                                      parameters=lambda: diagnostics.params,
                                      release_inhibited=guarded.policy.blocked)
                    production.brake = brake
                    reader.owner_lost = brake.owner_lost
                elif args.brake_hold_only:
                    raise RuntimeError('Brake HOLD-only requires the current Pi boot id')
            except OSError:
                if args.brake_hold_only:
                    raise
                pass  # Without an owner lease, configured Pixhawk brakes hold.
        if args.dynamic_mission_profile:
            from navigation.mission_owner import MissionOwner
            mission = MissionOwner.from_profile(args.dynamic_mission_profile,
                Path(__file__).resolve().parents[2] / 'optical' / 'marker_map.json',
                reader, nav, diagnostics, encoder, directory=Path(args.web_disarm_db).parent,
                safety_ready=lambda: (not args.nav_mission_tx and brake is not None and brake.hold_only and
                                      os.path.isfile(args.inhibit_file) and production.core.snapshot().stop_latch),
                send_hold=raw_hold_sender, require_live_tx=True, static_upload=not planar_tx,
                traction_readiness=production.traction_readiness, latch_stop=production.latch_for_brake,
                brake_command=lambda: brake.phase if brake is not None else None)
            def mission_owner_lost():
                brake.owner_lost()
                mission.owner_lost()
            reader.owner_lost = mission_owner_lost
        if args.local_ekf_bench_action:
            def local_ekf_safe(now):
                hb = reader.messages.get('HEARTBEAT')
                stop_state = production.core.snapshot()
                if args.local_ekf_bench_action == 'src2':
                    expected = {'AHRS_EKF_TYPE': 3, 'EK3_ENABLE': 1, 'EK3_SRC2_POSXY': 6, 'EK3_SRC2_YAW': 6}
                    if any(diagnostics.params.get(key, {}).get('value') != value
                           for key, value in expected.items()):
                        return False
                if args.local_ekf_bench_action == 'home':
                    if nav is None or not nav.alignment_check() or nav.tx or nav.sent:
                        return False
                    pose = nav.route_pose()
                    if pose is None or pose['x'] ** 2 + pose['y'] ** 2 > .15 ** 2:
                        return False
                return (reader.state == 'READING' and reader.source == (1, 1) and hb is not None
                        and 0 <= now - hb[0] < 1 and (hb[1].autopilot, hb[1].type) == (3, 10)
                        and hb[1].custom_mode == 4 and not hb[1].base_mode & 128
                        and brake is not None and brake.hold_only
                        and os.path.isfile(args.inhibit_file) and guarded.policy.blocked()
                        and not production.storage_fault and stop_state.stop_latch and not stop_state.reasons)

            def claim_local_ekf_action():
                directory = Path(args.web_disarm_db).parent
                claim = directory / f'local-ekf-{args.functional_brake_boot_id}-{args.local_ekf_bench_action}'
                fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)

            diagnostics.configure_local_ekf(args.local_ekf_bench_action, local_ekf_safe, claim_local_ekf_action)
        if planar_tx:
            from stop_latch_test.externalnav import alignment_record, static_planar_message
            initial_alignment = None

            def static_writer(pose, now):
                nonlocal initial_alignment
                hb = reader.messages.get('HEARTBEAT')
                stop_state = production.core.snapshot()
                if (reader.state != 'READING' or reader.source != (1, 1) or hb is None
                        or not 0 <= now-hb[0] <= 1 or hb[1].autopilot != 3 or hb[1].type != 10
                        or hb[1].custom_mode != 4 or hb[1].base_mode & 128
                        or brake is None or not brake.hold_only
                        or not os.path.isfile(args.inhibit_file) or not guarded.policy.blocked()
                        or production.storage_fault or stop_state.reasons or not stop_state.stop_latch
                        or diagnostics.readback_epoch != reader.heartbeat_safety_serial):
                    return None
                record = alignment_record()
                if not record or (initial_alignment is not None and record != initial_alignment):
                    return None
                packet = static_planar_message(pose, now, diagnostics.origin, diagnostics.params, record,
                                               mission_session=args.nav_mission_tx)
                if packet is not None and initial_alignment is None:
                    initial_alignment = record
                return packet

            nav.static_writer = static_writer
            stream_hold = (NavigationHold(reader, nav.snapshot, raw_hold_sender, monitor_stream=True)
                           if args.nav_mission_tx else None)

            if stream_hold is not None and mission is not None:
                mission.navigation_hold_status = stream_hold.snapshot

            def static_owner_lost():
                brake.owner_lost()
                nav.static_revoked = True
                if stream_hold is not None:
                    stream_hold.owner_lost()
                if mission is not None:
                    mission.owner_lost()

            reader.owner_lost = static_owner_lost
        if args.ch6_mode_change:
            from stop_latch_test.ch6_mode_change import ModeChange
            mode_change = ModeChange(reader, diagnostics, encoder,
                lambda: (brake is not None and brake.hold_only and
                         os.path.isfile(args.inhibit_file) and production.core.snapshot().stop_latch),
                args.ch6_mode_change, Path(args.web_disarm_db).parent / 'ch6-mode-change.jsonl')
        if navigation_hold is not None:
            reader.owner_lost = navigation_hold.owner_lost
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
            production.tick()
            if arm is not None:
                arm.tick(fd)
            bench.tick(fd)
            if navigation_hold is not None:
                navigation_hold.tick(fd, command_pending=bench.disarm_pending())
            if mode_change is not None:
                mode_change.tick(fd, command_pending=bench.disarm_pending())
            diagnostics.tick(fd,command_pending=bench.disarm_pending() or
                             (arm is not None and arm.active is not None) or
                             (mode_change is not None and mode_change.writing))
            if nav is not None:
                nav.tick(fd,command_pending=bench.disarm_pending())
                if args.nav_mission_tx:
                    stream_hold.tick(fd, command_pending=bench.disarm_pending())
            if mission is not None:
                mission.tick(fd, command_pending=bench.disarm_pending())

        reader.bench_tick = owner_tick
        stop_operations = {'STOP_STATUS': production.status}
        if not args.brake_hold_only:
            stop_operations['STOP_RESET'] = production.reset
        channel = OwnerServer(guarded, args.web_disarm_socket,
            stop_operations=stop_operations,
            brake_operations={op: partial(production.web_brake, op) for op in BRAKE_OPERATIONS
                              if not (planar_tx or args.local_ekf_bench_action) or op == 'BRAKE_APPLY'},
            mission_operations={op: partial(mission.command, op) for op in MISSION_OPERATIONS}
            if mission is not None and not args.nav_mission_tx else
            ({'MISSION_STATUS': partial(mission.command, 'MISSION_STATUS')} if mission is not None else None),
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
    if mode_change is not None:
        page_app.add_url_rule('/api/ch6-mode-change', 'ch6_mode_change',
                              lambda: jsonify(mode_change.snapshot()), methods=['GET'])
    if navigation_hold is not None:
        page_app.add_url_rule('/api/navigation-hold', 'navigation_hold',
                              lambda: jsonify(navigation_hold.snapshot()), methods=['GET'])
    page = make_server('127.0.0.1', 8084, page_app, threaded=True)
    threading.Thread(target=page.serve_forever, daemon=True).start()
    gpio.start()
    atexit.register(stop.set)
    atexit.register(reader.close)
    atexit.register(page.shutdown)
    reader.start()
    telemetry_app(telemetry=reader, servo_outputs=diagnostics.servo_outputs).run(
        host='127.0.0.1', port=8082, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
