"""Drive an isolated Rover SITL process through UDP loopback only."""
import argparse
import socket
import subprocess
import time
import sys
from tempfile import TemporaryDirectory
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

from pymavlink.dialects.v20 import ardupilotmega as mav
sys.path.insert(0, str(Path(__file__).resolve().parent / 'owner_externalnav'))

from production_stop.mission_checkpoint import RouteSnapshot
from production_stop.mission_resume import ResumeAcceptance
from production_stop.rover_auto import RoverAutoAdapter, mission_current_sender, SET_CURRENT

def run(executable, model_params, workdir):
    clock = time.monotonic
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1', 0))
    sock.settimeout(.1)
    enc = mav.MAVLink(None, srcSystem=255, srcComponent=190)
    parser = mav.MAVLink(None)
    parser.robust_parsing = True
    reader = SimpleNamespace(lock=RLock(), messages={}, state='READING', clock=clock)
    ipc = Mock()
    ipc.resume.return_value = ResumeAcceptance.ACCEPTED
    route = None
    peer = None

    def send(msg):
        data = msg.pack(enc)
        enc.seq = (enc.seq + 1) % 256
        return sock.sendto(data, peer)

    def write(fd, packet):
        assert fd == sock.fileno()
        restore_writes.append(clock())
        return sock.sendto(packet, peer)

    adapter = RoverAutoAdapter(reader, ipc, interlock_ready=lambda: True,
                               read_route=lambda: route,
                               send_current=mission_current_sender(enc, write))
    last_hb = 0
    restore_writes = []

    def wait(predicate, timeout=10):
        nonlocal peer, last_hb
        end = clock() + timeout
        while clock() < end:
            if peer and clock() - last_hb > .8:
                send(mav.MAVLink_heartbeat_message(mav.MAV_TYPE_GCS, 8, 0, 0, 0, 3))
                last_hb = clock()
            try:
                data, addr = sock.recvfrom(65535)
            except TimeoutError:
                continue
            if addr[0] != '127.0.0.1':
                continue
            if peer is None:
                peer = addr
            if addr != peer:
                continue
            for msg in parser.parse_buffer(data) or []:
                kind = msg.get_type()
                if kind == 'BAD_DATA':
                    continue
                if kind == 'STATUSTEXT':
                    print('SITL:', msg.text, flush=True)
                if kind == 'HEARTBEAT':
                    reader.messages[kind] = clock(), msg
                adapter.observe(msg)
                if predicate(msg):
                    return msg
        raise TimeoutError('SITL observation timed out')

    def get(kind, predicate=lambda m: True, timeout=10):
        return wait(lambda m: m.get_type() == kind and predicate(m), timeout)

    def command(id, *params, result=None):
        send(mav.MAVLink_command_long_message(1, 1, id, 0, *(list(params) + [0] * (7 - len(params)))))
        ack = get('COMMAND_ACK', lambda m: m.command == id)
        if result is not None:
            assert ack.result == result, ack
        return ack

    def mode(number):
        command(mav.MAV_CMD_DO_SET_MODE, mav.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, number, result=0)
        return get('HEARTBEAT', lambda m: m.custom_mode == number)

    def download():
        send(mav.MAVLink_mission_request_list_message(1, 1, 0))
        count = get('MISSION_COUNT').count
        items = []
        for seq in range(count):
            send(mav.MAVLink_mission_request_int_message(1, 1, seq, 0))
            items.append(get('MISSION_ITEM_INT', lambda m: m.seq == seq))
        send(mav.MAVLink_mission_ack_message(1, 1, mav.MAV_MISSION_ACCEPTED, 0))
        return RouteSnapshot(clock(), tuple(items))

    def upload(delta=0):
        send(mav.MAVLink_mission_count_message(1, 1, 4, 0))
        for seq in range(4):
            wait(lambda m: m.get_type() in ('MISSION_REQUEST', 'MISSION_REQUEST_INT') and m.seq == seq)
            send(mav.MAVLink_mission_item_int_message(1, 1, seq, 6, 16, 0, 1, 0, 0, 0, 0,
                                                      -353632610 + seq * 30000, 1491652300 + (delta if seq == 2 else 0), 0))
        ack = get('MISSION_ACK')
        assert ack.type == mav.MAV_MISSION_ACCEPTED, ack

    def mission_current(seq):
        return get('MISSION_CURRENT', lambda m: m.seq == seq)

    def report(m):
        return (m.seq, m.mission_state, getattr(m, 'mission_id', None))

    defaults = workdir / 'defaults.parm'
    defaults.write_text(model_params.read_text() +
                        '\nARMING_CHECK 0\nFS_GCS_ENABLE 0\nMIS_RESTART 0\n'
                        'FS_EKF_THRESH 0\nFS_THR_ENABLE 0\n')
    port = sock.getsockname()[1]
    with (workdir / 'sitl.log').open('w') as log:
        proc = subprocess.Popen([str(executable), '-w', '--model', 'rover', '--home',
                                 '-35.363261,149.165230,584,353', '--base-port', '16970',
                                 '--serial0', f'udpclient:127.0.0.1:{port}',
                                 '--serial1', 'none', '--serial2', 'none',
                                 '--defaults', str(defaults)], cwd=workdir,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            get('HEARTBEAT', timeout=25)
            get('GPS_RAW_INT', lambda m: m.fix_type >= 3, timeout=45)
            get('GLOBAL_POSITION_INT', lambda m: abs(m.lat) > 1000, timeout=45)
            get('STATUSTEXT', lambda m: 'origin set' in m.text, timeout=45)
            print('Uploading route', flush=True)
            upload()
            print('Route count:', len(download().items), flush=True)
            print('Setting HOLD and ARM', flush=True)
            mode(4)
            command(mav.MAV_CMD_SET_MESSAGE_INTERVAL, mav.MAVLINK_MSG_ID_EKF_STATUS_REPORT, 200000)
            ekf = get('EKF_STATUS_REPORT', lambda m: bool(m.flags & 16) and not m.flags & 1024,
                      timeout=40)
            print('EKF flags:', ekf.flags, flush=True)
            try:
                mode(10)
            except AssertionError:
                send(mav.MAVLink_set_mode_message(1, 1, 10))
                get('HEARTBEAT', lambda m: m.custom_mode == 10, timeout=5)
            command(mav.MAV_CMD_COMPONENT_ARM_DISARM, 1, 21196, result=0)
            command(mav.MAV_CMD_SET_MESSAGE_INTERVAL, mav.MAVLINK_MSG_ID_MISSION_CURRENT, 100000, result=0)
            command(SET_CURRENT, 2, 0, result=0)
            route = download()
            msg = mission_current(2)
            print('AUTO mission:', report(msg), flush=True)
            get('HEARTBEAT')
            route = download()
            adapter.front_pressed(clock())
            assert adapter.checkpoint and adapter.checkpoint.seq == 2
            adapter.stop_latched()
            mode(4)
            command(mav.MAV_CMD_COMPONENT_ARM_DISARM, 0, 21196, result=0)
            # SITL transitions to seq 1 on fresh AUTO after ARM. Explicitly set it
            # while in HOLD here to make the checkpoint restore independently observable.
            command(SET_CURRENT, 1, 0, result=0)
            print('HOLD after DISARM:', report(mission_current(1)), flush=True)
            route = download()
            adapter.reset_accepted('reset-sitl')
            adapter.release_confirmed('reset-sitl')
            assert adapter.resume('resume-sitl', 'reset-sitl') == ResumeAcceptance.ACCEPTED
            assert not ipc.resume.called
            adapter.tick(sock.fileno())
            ack = get('COMMAND_ACK', lambda m: m.command == SET_CURRENT)
            msg = mission_current(2)
            print('Restore ACK:', ack.result, 'MISSION_CURRENT:', report(msg), flush=True)
            route = download()
            adapter.tick(sock.fileno())
            assert ack.result == mav.MAV_RESULT_ACCEPTED
            ipc.resume.assert_called_once_with('resume-sitl', 'reset-sitl')
            mode(10)
            command(mav.MAV_CMD_COMPONENT_ARM_DISARM, 1, 21196, result=0)
            get('HEARTBEAT', lambda m: m.base_mode & mav.MAV_MODE_FLAG_SAFETY_ARMED)
            active = mission_current(2)
            assert active.mission_state == mav.MISSION_STATE_ACTIVE
            assert adapter.status() == 'RUNNING'
            print('SITL restore PASS: seq 2 -> 1 -> 2 -> AUTO at seq 2', flush=True)

            route = download()
            adapter.front_pressed(clock())
            assert adapter.checkpoint and adapter.checkpoint.seq == 2
            adapter.stop_latched()
            mode(4)
            command(mav.MAV_CMD_COMPONENT_ARM_DISARM, 0, 21196, result=0)
            adapter.reset_accepted('reset-changed')
            adapter.release_confirmed('reset-changed')
            upload(delta=10000)
            route = download()
            assert adapter.resume('resume-changed', 'reset-changed') == ResumeAcceptance.REFUSED
            adapter.tick(sock.fileno())
            ipc.resume.assert_called_once()
            print('SITL changed route PASS: restore refused and no handoff', flush=True)

            adapter = RoverAutoAdapter(reader, ipc, interlock_ready=lambda: True,
                                       read_route=lambda: route,
                                       send_current=mission_current_sender(enc, write))
            upload()
            mode(10)
            command(mav.MAV_CMD_COMPONENT_ARM_DISARM, 1, 21196, result=0)
            command(SET_CURRENT, 2, 0, result=0)
            get('HEARTBEAT', lambda m: m.base_mode & mav.MAV_MODE_FLAG_SAFETY_ARMED)
            mission_current(2)
            route = download()
            # A new independent STOP can be captured after the route is restored.
            adapter.front_pressed(clock())
            assert adapter.checkpoint and adapter.checkpoint.seq == 2
            mode(4)
            command(mav.MAV_CMD_COMPONENT_ARM_DISARM, 0, 21196, result=0)
            route = download()
            adapter.reset_accepted('reset-pressed')
            adapter.release_confirmed('reset-pressed')
            assert adapter.resume('resume-pressed', 'reset-pressed') == ResumeAcceptance.ACCEPTED
            adapter.front_pressed(clock())
            adapter.tick(sock.fileno())
            assert adapter.status() == 'STOPPED'
            assert adapter.resume('resume-old', 'reset-pressed') == ResumeAcceptance.REFUSED
            ipc.resume.assert_called_once()
            assert len(restore_writes) == 1
            print('SITL new FRONT_PRESSED PASS: pending restore revoked', flush=True)

            mode(10)
            command(mav.MAV_CMD_COMPONENT_ARM_DISARM, 1, 21196, result=0)
            command(SET_CURRENT, 2, 0, result=0)
            get('HEARTBEAT', lambda m: m.base_mode & mav.MAV_MODE_FLAG_SAFETY_ARMED)
            mission_current(2)
            route = download()
            adapter.front_pressed(clock())
            assert adapter.checkpoint and adapter.checkpoint.seq == 2
            mode(4)
            command(mav.MAV_CMD_COMPONENT_ARM_DISARM, 0, 21196, result=0)
            route = download()
            adapter.reset_accepted('reset-no-feedback')
            adapter.release_confirmed('reset-no-feedback')
            assert adapter.resume('resume-no-feedback', 'reset-no-feedback') == ResumeAcceptance.ACCEPTED
            adapter.tick(sock.fileno())
            assert len(restore_writes) == 2
            # Consume no inbound traffic during the deadline to simulate lost feedback.
            time.sleep(2.05)
            adapter.tick(sock.fileno())
            assert adapter.status() == 'UNKNOWN'
            assert adapter.resume('resume-retry', 'reset-no-feedback') == ResumeAcceptance.REFUSED
            adapter.tick(sock.fileno())
            assert len(restore_writes) == 2
            ipc.resume.assert_called_once()
            print('SITL lost feedback PASS: UNKNOWN, no retry or handoff', flush=True)
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            sock.close()


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('ardurover', type=Path)
    cli.add_argument('rover_params', type=Path)
    args = cli.parse_args()
    with TemporaryDirectory(prefix='ugv-checkpoint-sitl-') as directory:
        run(args.ardurover.resolve(strict=True), args.rover_params.resolve(strict=True),
            Path(directory))
