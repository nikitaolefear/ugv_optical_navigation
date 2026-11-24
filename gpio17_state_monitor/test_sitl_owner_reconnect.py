"""Run the full isolated serial-owner with Rover SITL over a disposable PTY."""
import argparse
from copy import copy
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import pty
import select
import shutil
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import tty
import unittest
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

from pymavlink.dialects.v20 import ardupilotmega as mav

ROOT = Path(__file__).resolve().parent
BASE = Path('/opt/ugv-tilted-observe/20261117/package')


def isolated_owner(work):
    """Replace only hardware and listener bindings in the normal owner entry point."""
    package = work / 'package'
    sys.path[:0] = [str(package / 'owner'), str(package)]
    from flask import Flask
    from werkzeug.serving import make_server
    from production_stop import mission_reader, rover_auto
    from stop_latch_test import hold_runner

    def trace(kind, **details):
        with (work / 'trace.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(kind=kind, **details)) + '\n')

    class TracedReader(mission_reader.OwnerMissionReader):
        def _finish(self, route, next_read):
            super()._finish(route, next_read)
            if route is not None:
                trace('ROUTE', count=len(route.items), items=[m.seq for m in route.items],
                      sha256=route.fingerprint(), recipient=self.recipient[1])

        def link_lost(self):
            partial = [m.seq for m in self.items]
            super().link_lost()
            trace('DOWNLOAD_LOST', partial=partial, discarded=self.route is None and not self.items)

        def _abort(self, now):
            timeout = self.started_at is not None and self._expired(now)
            if timeout:
                pending = self.expected
                recipient = self.recipient[1]
                partial = [m.seq for m in self.items]
            super()._abort(now)
            if timeout:
                trace('DOWNLOAD_TIMEOUT', recipient=recipient, pending=pending, partial=partial,
                      discarded=self.route is None and not self.items)

    class TracedAdapter(rover_auto.RoverAutoAdapter):
        def observe(self, message):
            super().observe(message)
            if message.get_type() == 'MISSION_CURRENT' and self.current is not None:
                trace('CURRENT', seq=self.current[1])

        def link_lost(self):
            super().link_lost()
            trace('ADAPTER_LOST', revoked=self.checkpoint is None and self.reset_id is None
                  and self.pending is None and self.current is None)

        def front_pressed(self, observed_at=None):
            age = self.reader.clock() - self.current[0] if self.current is not None else None
            route_available = self.read_route() is not None
            super().front_pressed(observed_at)
            trace('FRONT_CHECKPOINT', route_available=route_available, current_age=age,
                  captured=self.checkpoint is not None)

    def test_open(*, bench_write):
        assert bench_write
        fd = os.open(work / 'port', os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        trace('OPEN')
        return fd

    def test_gpio(bench, stop, *, observer):
        while not stop.is_set():
            contact = (work / 'gpio').read_text().strip()
            if contact in ('RELEASED', 'PRESSED'):
                bench.input(contact)
                observer(contact, monotonic())
            stop.wait(.03)

    def local_server(host, requested_port, app, **kwargs):
        assert host == '127.0.0.1'
        server = make_server(host, 0, app, **kwargs)
        (work / f'http-{requested_port}').write_text(str(server.server_port))
        return server

    def run_http(app, *, host, port, **kwargs):
        local_server(host, port, app, threaded=True).serve_forever()

    parse_args = hold_runner.parse_args

    def test_args():
        args = parse_args([
            '--journal', str(work / 'bench.jsonl'), '--observe-auto-route',
            '--web-disarm-socket', str(work / 'backend.sock'),
            '--web-disarm-db', str(work / 'state/disarm.db'),
            '--web-disarm-client-uid', str(os.getuid() + 1),
            '--web-disarm-client-gid', str(os.getgid()),
        ])
        # The harness is in the same UID. The real peer-credential check stays in place.
        args.web_disarm_client_uid = os.getuid()
        return args

    mission_reader.OwnerMissionReader = TracedReader
    rover_auto.RoverAutoAdapter = TracedAdapter
    # owner_bridge uses the same subclass in its adapter checks.
    hold_runner.parse_args = test_args
    hold_runner.open_with_owner_helper = test_open
    hold_runner.monitor = test_gpio
    hold_runner.make_server = local_server
    Flask.run = run_http
    hold_runner.main()


@dataclass(frozen=True)
class Inputs:
    executable: Path
    defaults: Path
    eeprom: Path


class Relay:
    """Only mission readback messages may cross from the owner to SITL."""

    def __init__(self, work, sock):
        self.work, self.sock = work, sock
        self.peer = self.master = None
        self.owner_parser = mav.MAVLink(None)
        self.sitl_parser = mav.MAVLink(None)
        self.sitl_parser.robust_parsing = True
        self.fabricated = mav.MAVLink(None, srcSystem=1, srcComponent=1)
        self.requests = []
        self.counts = {}
        self.items = {}
        self.acks = []
        self.old_component = None
        self.cut_on_item = False
        self.replay_old = False
        self.injected = 0
        self.drop_reply = None
        self.dropped = []
        self.suppress_current = False
        self.suppress_heartbeat = False

    def connect(self):
        master, slave = pty.openpty()
        try:
            tty.setraw(slave)
            os.set_blocking(master, False)
            (self.work / 'port').symlink_to(os.ttyname(slave))
        finally:
            os.close(slave)
        self.master = master

    def close(self):
        if self.master is not None:
            (self.work / 'port').unlink()
            os.close(self.master)
            self.master = None

    def to_owner(self, packet):
        if self.master is not None:
            if os.write(self.master, packet) != len(packet):
                raise OSError('Short write to test PTY')

    def inject(self, message):
        self.to_owner(message.pack(self.fabricated))
        self.fabricated.seq = (self.fabricated.seq + 1) % 256

    def from_owner(self, message):
        kind = message.get_type()
        assert kind in ('MISSION_REQUEST_LIST', 'MISSION_REQUEST_INT', 'MISSION_ACK'), kind
        component = message.get_srcComponent()
        if self.old_component is None:
            self.old_component = component
        self.requests.append((component, kind, getattr(message, 'seq', None)))
        if self.replay_old and component != self.old_component:
            if kind == 'MISSION_REQUEST_LIST':
                old = copy(self.counts[self.old_component])
                old.count = 2
                self.inject(old)
                self.injected += 1
            elif kind == 'MISSION_REQUEST_INT' and message.seq == 2:
                previous_item = self.items.get((self.old_component, 2))
                if previous_item is not None:
                    old = copy(previous_item)
                    old.x += 12345
                    self.inject(old)  # Same expected seq, addressed to the old transfer.
                    self.injected += 1
                self.to_owner(self.items[component, 0].get_msgbuf())  # Duplicate already accepted in this transfer.
                self.injected += 1
                self.replay_old = False
        if kind == 'MISSION_ACK':
            self.acks.append(component)
        self.sock.sendto(message.get_msgbuf(), self.peer)

    def from_sitl(self, message):
        kind = message.get_type()
        if kind == 'MISSION_COUNT':
            self.counts[message.target_component] = message
        elif kind == 'MISSION_ITEM_INT':
            self.items[message.target_component, message.seq] = message
            if self.cut_on_item and message.seq == 2:
                self.cut_on_item = False
                self.old_component = message.target_component
                self.close()
                return
        if self.drop_reply == (kind, getattr(message, 'seq', None)):
            self.drop_reply = None
            self.dropped.append((message.target_component, kind))
            self.old_component = message.target_component
            self.replay_old = True
            return
        if ((kind == 'MISSION_CURRENT' and self.suppress_current) or
                (kind == 'HEARTBEAT' and self.suppress_heartbeat)):
            return
        self.to_owner(message.get_msgbuf())

    def pump(self):
        descriptors = [self.sock]
        if self.master is not None:
            descriptors.append(self.master)
        # A master whose slave has not opened can signal EIO. The owner retries independently.
        ready, _, _ = select.select(descriptors, [], [], .02)
        if self.sock in ready:
            data, address = self.sock.recvfrom(65535)
            if address[0] == '127.0.0.1' and self.peer in (None, address):
                self.peer = address
                for message in self.sitl_parser.parse_buffer(data) or ():
                    if message.get_type() != 'BAD_DATA':
                        self.from_sitl(message)
        if self.master is not None and self.master in ready:
            try:
                data = os.read(self.master, 4096)
            except BlockingIOError:
                # Readiness can change when the owner opens the PTY slave.
                return
            except OSError as error:
                if error.errno != errno.EIO:  # A PTY before the owner has opened the slave.
                    raise
                return
            for message in self.owner_parser.parse_buffer(data) or ():
                self.from_owner(message)


class OwnerReconnectTests(unittest.TestCase):
    def setUp(self):
        work = Path(self.enterContext(TemporaryDirectory(prefix='ugv-full-owner-')))
        self.work = work
        work.chmod(0o710)
        (work / 'state').mkdir(mode=0o700)
        shutil.copytree(BASE, work / 'package')
        shutil.copytree(ROOT / 'owner_externalnav', work / 'package', dirs_exist_ok=True)
        shutil.copy2(self.inputs.eeprom, work / 'eeprom.bin')
        (work / 'gpio').write_text('RELEASED')
        sock = self.enterContext(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        sock.bind(('127.0.0.1', 0))
        self.relay = Relay(work, sock)
        self.addCleanup(self.relay.close)
        self.relay.connect()
        port = sock.getsockname()[1]
        self.sitl = self.launch('sitl', [
            str(self.inputs.executable), '--model', 'rover', '--home',
            '-35.363261,149.165230,584,353', '--base-port', '19470',
            '--serial0', f'udpclient:127.0.0.1:{port}',
            '--serial1', 'none', '--serial2', 'none', '--defaults', str(self.inputs.defaults),
        ])
        self.owner = self.launch('owner', [sys.executable, '-B', __file__, '--owner', str(work)])
        self.http = build_opener(ProxyHandler({}))

    def launch(self, name, args):
        with (self.work / f'{name}.log').open('w') as log:
            process = subprocess.Popen(args, cwd=self.work, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT)
        self.addCleanup(self.terminate, process)
        return process

    @staticmethod
    def terminate(process):
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)

    def until(self, predicate, context, timeout=25):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            for name, process in (('sitl', self.sitl), ('owner', self.owner)):
                if process.poll() is not None:
                    self.fail(f'{name} exited: {(self.work / (name + ".log")).read_text()[-4000:]}')
            result = predicate()
            if result:
                return result
            self.relay.pump()
        self.fail(f'Timed out waiting for {context}; last requests: {self.relay.requests[-12:]}; '
                  f'owner log: {(self.work / "owner.log").read_text()[-2000:]}')

    def trace(self, kind):
        path = self.work / 'trace.jsonl'
        return [record for record in (json.loads(line) for line in path.read_text().splitlines())
                if record['kind'] == kind] if path.exists() else []

    def telemetry(self):
        port = (self.work / 'http-8082').read_text().strip()
        with self.http.open(f'http://127.0.0.1:{port}/api/telemetry', timeout=.5) as response:
            return json.load(response)

    def rpc(self, operation):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(.7)
            sock.connect(str(self.work / 'backend.sock'))
            sock.sendall(json.dumps(dict(op=operation, actor_id=1, actor='sitl',
                                         request_id=str(uuid4()), nonce=uuid4().hex)).encode() + b'\n')
            with sock.makefile() as stream:
                return json.loads(stream.readline())

    def stop_events(self, event):
        import sqlite3
        path = self.work / 'state/disarm.db.stop-reset.sqlite3'
        if not path.exists():
            return []
        with sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True) as db:
            rows = db.execute('SELECT details FROM stop_events WHERE event=? ORDER BY sequence', (event,))
            return [json.loads(row[0]) for row in rows]

    def wait_drop(self):
        self.until(lambda: self.relay.master is None, 'PTY hangup while requesting item 2')
        losses = self.until(lambda: self.trace('DOWNLOAD_LOST'), 'owner link-loss handler')
        self.assertEqual(losses[-1]['partial'], [0, 1])
        self.assertTrue(losses[-1]['discarded'])
        self.assertEqual(self.telemetry()['armed'], {'state': 'STALE', 'value': None})

    def reconnect(self, *, replay=False):
        from production_stop.mission_checkpoint import RouteSnapshot

        old_component = self.relay.old_component
        previous_current = len(self.trace('CURRENT'))
        previous_opens = len(self.trace('OPEN'))
        self.relay.replay_old = replay
        self.relay.connect()
        previous_routes = len(self.trace('ROUTE'))
        routes = self.until(lambda: self.trace('ROUTE')[previous_routes:],
                            'complete route on the reopened owner descriptor')
        self.assertGreater(len(self.trace('OPEN')), previous_opens)
        route = routes[0]
        component = route['recipient']
        self.assertNotEqual(component, old_component)
        self.until(lambda: component in self.relay.acks, 'acknowledgment for the new download')
        self.assertEqual([r for c, r, _ in self.relay.requests if c == component][:6],
                         ['MISSION_REQUEST_LIST'] + ['MISSION_REQUEST_INT'] * 4 + ['MISSION_ACK'])
        self.assertEqual([s for c, r, s in self.relay.requests if c == component and r == 'MISSION_REQUEST_INT'][:4],
                         [0, 1, 2, 3])
        self.assertEqual(self.relay.counts[component].count, 4)
        self.assertEqual(route['count'], 4)
        self.assertEqual(route['items'], [0, 1, 2, 3])
        expected = RouteSnapshot(0, tuple(self.relay.items[component, n] for n in range(4)))
        self.assertEqual(route['sha256'], expected.fingerprint())
        self.until(lambda: len(self.trace('CURRENT')) > previous_current, 'new MISSION_CURRENT')
        self.assertEqual(self.trace('CURRENT')[-1]['seq'], 0)
        self.assertEqual(self.telemetry()['armed'], {'state': 'FRESH', 'value': 'DISARMED'})
        print(f'  recovered count=4 seq=0 SHA-256={route["sha256"]}', flush=True)

    def test_partial_read_restarts_and_rejects_old_responses(self):
        self.relay.cut_on_item = True
        self.wait_drop()
        self.assertFalse(self.trace('ROUTE'))
        (self.work / 'gpio').write_text('PRESSED')
        pressed = self.until(lambda: self.stop_events('FRONT_PRESSED'), 'GPIO stop with partial data')
        self.assertNotIn('route_sha256', pressed[-1])
        self.reconnect(replay=True)
        self.assertEqual(self.relay.injected, 3)
        self.assertEqual(self.rpc('STOP_STATUS')['command']['mission_state'], 'STOPPED')

    def test_existing_stop_checkpoint_is_retired_on_disconnect(self):
        # Present prior AUTO/ARMED telemetry to the owner only. SITL remains disarmed.
        self.until(lambda: self.trace('ROUTE'), 'initial complete read')
        self.inject_auto_observation(seq=2)
        (self.work / 'gpio').write_text('PRESSED')
        pressed = self.until(lambda: self.stop_events('FRONT_PRESSED'), 'STOP checkpoint')
        checkpoint = pressed[-1]
        self.assertEqual(checkpoint['waypoint_seq'], 2)
        self.relay.cut_on_item = True
        self.wait_drop()
        lost = self.until(lambda: self.stop_events('MAVLINK_LOST'), 'durable revocation')[-1]
        self.assertEqual(lost, {'checkpoint_revoked': True, 'stop_id': checkpoint['stop_id']})
        self.assertTrue(self.trace('ADAPTER_LOST')[-1]['revoked'])
        self.reconnect()
        state = self.rpc('STOP_STATUS')['command']
        self.assertTrue(state['stop_latch'])
        self.assertFalse(state['reset_available'])
        self.assertEqual(self.rpc('MISSION_RESUME')['error'], 'UNKNOWN_OPERATION')
        self.assertFalse(self.stop_events('RESET_ACCEPTED'))

    def inject_auto_observation(self, seq=None):
        flags = mav.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED | mav.MAV_MODE_FLAG_SAFETY_ARMED
        self.relay.inject(mav.MAVLink_heartbeat_message(mav.MAV_TYPE_GROUND_ROVER,
                          mav.MAV_AUTOPILOT_ARDUPILOTMEGA, flags, 10, mav.MAV_STATE_ACTIVE, 3))
        if seq is not None:
            self.relay.inject(mav.MAVLink_mission_current_message(seq, 3, mav.MISSION_STATE_ACTIVE, 1))

    def wait_packet_timeout(self, kind, prefix):
        self.until(lambda: self.relay.dropped, f'dropped {kind}')
        component = self.relay.dropped[0][0]
        timeout = self.until(lambda: next((r for r in self.trace('DOWNLOAD_TIMEOUT')
                                         if r['recipient'] == component), None), 'download timeout')
        self.assertEqual(timeout['pending'], kind)
        self.assertEqual(timeout['partial'], prefix)
        self.assertTrue(timeout['discarded'])
        self.assertEqual(self.telemetry()['armed'], {'state': 'FRESH', 'value': 'DISARMED'})
        self.assertEqual(len(self.trace('OPEN')), 1)
        self.assertFalse(self.trace('ROUTE'))

    def verify_packet_retry(self, master, *, injected):
        from production_stop.mission_checkpoint import RouteSnapshot

        route = self.until(lambda: next(iter(self.trace('ROUTE')), None), 'complete retry')
        component = route['recipient']
        self.assertNotEqual(component, self.relay.old_component)
        self.until(lambda: component in self.relay.acks, 'retry acknowledgment')
        self.assertEqual(self.relay.counts[component].count, 4)
        requests = [(kind, seq) for c, kind, seq in self.relay.requests if c == component]
        self.assertEqual(requests, [('MISSION_REQUEST_LIST', None),
                                    *[('MISSION_REQUEST_INT', n) for n in range(4)],
                                    ('MISSION_ACK', None)])
        self.assertEqual(route['items'], [0, 1, 2, 3])
        actual = RouteSnapshot(0, tuple(self.relay.items[component, n] for n in range(4)))
        self.assertEqual(route['sha256'], actual.fingerprint())
        self.assertEqual(self.relay.injected, injected)
        self.assertEqual(self.relay.master, master)
        self.assertEqual(len(self.trace('OPEN')), 1)
        self.assertFalse(self.trace('DOWNLOAD_LOST'))

    def test_packet_loss_count_times_out_and_starts_a_clean_read(self):
        master = self.relay.master
        self.relay.drop_reply = ('MISSION_COUNT', None)
        self.wait_packet_timeout('MISSION_COUNT', [])
        self.relay.suppress_current = self.relay.suppress_heartbeat = True
        self.inject_auto_observation(seq=2)
        (self.work / 'gpio').write_text('PRESSED')
        pressed = self.until(lambda: self.stop_events('FRONT_PRESSED'), 'STOP on unknown route')
        self.assertNotIn('route_sha256', pressed[-1])
        capture = self.trace('FRONT_CHECKPOINT')[-1]
        self.assertLess(capture['current_age'], .5)
        self.assertFalse(capture['route_available'])
        self.assertFalse(capture['captured'])
        self.assertEqual(self.rpc('MISSION_RESUME')['error'], 'UNKNOWN_OPERATION')
        previous_current = len(self.trace('CURRENT'))
        self.relay.suppress_current = self.relay.suppress_heartbeat = False
        self.verify_packet_retry(master, injected=2)
        self.until(lambda: len(self.trace('CURRENT')) > previous_current, 'actual MISSION_CURRENT')
        self.assertEqual(self.trace('CURRENT')[-1]['seq'], 0)

    def test_packet_loss_item_rejects_old_item_duplicate_and_stale_seq(self):
        master = self.relay.master
        self.relay.drop_reply = ('MISSION_ITEM_INT', 2)
        self.relay.suppress_current = True
        self.until(lambda: self.relay.dropped, 'dropped item 2')
        self.relay.inject(mav.MAVLink_mission_current_message(2, 3, mav.MISSION_STATE_ACTIVE, 1))
        self.until(lambda: self.trace('CURRENT'), 'synthetic prior MISSION_CURRENT')
        self.wait_packet_timeout('MISSION_ITEM_INT', [0, 1])
        self.verify_packet_retry(master, injected=3)
        self.relay.suppress_heartbeat = True
        self.inject_auto_observation()
        (self.work / 'gpio').write_text('PRESSED')
        capture = self.until(lambda: self.trace('FRONT_CHECKPOINT'), 'stale-current checkpoint check')[-1]
        self.assertTrue(capture['route_available'])
        self.assertGreaterEqual(capture['current_age'], .5)
        self.assertFalse(capture['captured'])
        self.assertTrue(self.rpc('STOP_STATUS')['command']['stop_latch'])
        self.assertEqual(self.rpc('MISSION_RESUME')['error'], 'UNKNOWN_OPERATION')
        self.relay.suppress_current = self.relay.suppress_heartbeat = False
        self.until(lambda: len(self.trace('CURRENT')) > 1, 'real post-timeout MISSION_CURRENT')
        self.assertEqual(self.trace('CURRENT')[-1]['seq'], 0)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--owner']:
        isolated_owner(Path(sys.argv[2]))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        for arg in ('ardurover', 'defaults', 'eeprom'):
            parser.add_argument(arg, type=Path)
        parser.add_argument('--packet-loss', action='store_true', help='Run only open-transport scenarios')
        args = parser.parse_args()
        sys.path.insert(0, str(ROOT / 'owner_externalnav'))
        OwnerReconnectTests.inputs = Inputs(*(p.resolve(strict=True) for p in
                                              (args.ardurover, args.defaults, args.eeprom)))
        names = unittest.defaultTestLoader.getTestCaseNames(OwnerReconnectTests)
        suite = unittest.TestSuite(OwnerReconnectTests(name) for name in names
                                   if name.startswith('test_packet_loss_') == args.packet_loss)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        sys.exit(not result.wasSuccessful())
