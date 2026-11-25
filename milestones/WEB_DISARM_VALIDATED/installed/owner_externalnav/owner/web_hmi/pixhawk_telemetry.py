"""Passive by default. Explicit bench runner permits a single HOLD via the owner FD.

Import and HTTP access never open hardware. Only explicit start() does so.
"""
import errno
from collections import OrderedDict
import fcntl
import math
import logging
from contextlib import contextmanager
import os
import select
import subprocess
import termios
import threading
import time

PORT = '/dev/serial/by-id/usb-Holybro_Pixhawk6C_DEVICE_ID-if02'
TYPES = ('HEARTBEAT', 'SYS_STATUS', 'BATTERY_STATUS', 'ATTITUDE',
         'EKF_STATUS_REPORT', 'ESTIMATOR_STATUS')

# Diagnostic observation only: these never enter the HMI measurement cache.
ESC_GROUPS = {f'ESC_TELEMETRY_{first}_TO_{first + 3}': first for first in range(1, 33, 4)}
ESC_DIAGNOSTIC_TYPES = tuple(ESC_GROUPS) + ('ESC_STATUS',)
ESC_CACHE_LIMIT = 64


LOG = logging.getLogger(__name__)


class PortFailure(RuntimeError):
    """Port refusal with diagnostic context; never authorizes an open."""
    def __init__(self, state, stage, path, message, *, owners=(), result=None):
        super().__init__(message)
        self.state, self.stage, self.path = state, stage, path
        self.owners, self.result = tuple(owners), result


@contextmanager
def port_stage(stage, path):
    try:
        yield
    except (OSError, subprocess.SubprocessError) as exc:
        if stage.startswith(('proc', 'fuser')):
            state = 'PORT_CHECK_FAILED'
        elif getattr(exc, 'errno', None) in (errno.EACCES, errno.EPERM):
            state = 'PORT_ACCESS_DENIED'
        elif getattr(exc, 'errno', None) == errno.EBUSY or (
                stage == 'flock' and getattr(exc, 'errno', None) in (errno.EAGAIN, errno.EWOULDBLOCK)):
            state = 'PORT_BUSY'
        else:
            state = 'PORT_ERROR'
        raise PortFailure(state, stage, path, str(exc)) from exc


def verify_process_visibility():
    """fuser cannot prove exclusivity if another process hides its FDs."""
    with port_stage('proc_scan', '/proc'):
        for entry in os.scandir('/proc'):
            if not entry.name.isdigit():
                continue
            try:
                path = entry.path + '/fd'
                with port_stage('proc_list', path):
                    descriptors = os.listdir(path)
                for descriptor in descriptors:
                    try:
                        with port_stage('proc_readlink', path + '/' + descriptor):
                            os.readlink(path + '/' + descriptor)
                    except PortFailure as exc:
                        if isinstance(exc.__cause__, (FileNotFoundError, ProcessLookupError)):
                            continue
                        raise
            except PortFailure as exc:
                if isinstance(exc.__cause__, (FileNotFoundError, ProcessLookupError)):
                    continue  # process exited during inspection
                raise


def port_users(port, stage):
    verify_process_visibility()
    with port_stage(stage, port):
        result = subprocess.run(['fuser', port], capture_output=True, timeout=3)
    LOG.info('telemetry stage=%s hmi_pid=%s hmi_uid=%s path=%r '
             'fuser_rc=%s fuser_stdout=%r fuser_stderr=%r',
             stage, os.getpid(), os.getuid(), port,
             result.returncode, result.stdout, result.stderr)
    return result


def checked_owners(result, port, stage):
    tokens = result.stdout.split()
    if result.returncode == 1 and not tokens and not result.stderr.strip():
        return set()
    if result.returncode == 0 and tokens and all(t.isdigit() for t in tokens):
        owners = {int(t) for t in tokens}
        # Known foreign owners always justify refusal, even with warnings.
        if owners - {os.getpid()}:
            raise PortFailure('PORT_BUSY', stage, port, 'Foreign port owner',
                              owners=owners, result=result)
        # fuser normally prints the device label on stderr when it finds a user.
        if result.stderr.strip() in (b'', os.fsencode(port) + b':'):
            return owners
    raise PortFailure('PORT_CHECK_FAILED', stage, port, 'Unverifiable fuser result', result=result)


def open_passive(port=PORT, baud=115200, *, owner_check=None, bench_write=False):
    """Refuse unverifiable ownership; retain flock/TIOCEXCL/recheck.

    O_RDONLY by default; O_RDWR only for the explicitly enabled HOLD bench.

    A router is required for sharing. Privileged, uncooperative openers cannot
    be atomically coordinated by these checks.
    """
    fd = None
    try:
        with port_stage('tty_stat', port):
            os.stat(port)  # unlike exists(), preserves permission errors
        result = (port_users(port, 'fuser_before') if owner_check is None
                  else owner_check(port, 'fuser_before', None))
        owners = checked_owners(result, port, 'fuser_before')
        if owners:
            raise PortFailure('PORT_BUSY', 'fuser_before', port, 'Port already open',
                              owners=owners, result=result)
        with port_stage('open', port):
            fd = os.open(port, (os.O_RDWR if bench_write else os.O_RDONLY) | os.O_NOCTTY | os.O_NONBLOCK)
        with port_stage('flock', port):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with port_stage('tioc_excl', port):
            fcntl.ioctl(fd, termios.TIOCEXCL)
        check = (port_users(port, 'fuser_after') if owner_check is None
                 else owner_check(port, 'fuser_after', fd))
        owners = checked_owners(check, port, 'fuser_after')
        if owners != {os.getpid()}:
            raise PortFailure('PORT_CHECK_FAILED', 'fuser_after', port,
                              'Own descriptor not confirmed', owners=owners, result=check)
        with port_stage('tty_config', port):
            attrs = termios.tcgetattr(fd)
            attrs[0] = attrs[1] = attrs[3] = 0
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[4] = attrs[5] = getattr(termios, f'B{baud}')
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        return fd
    except BaseException as exc:
        if isinstance(exc, PortFailure):
            cause = exc.__cause__
            result = exc.result
            LOG.error('telemetry state=%s stage=%s hmi_pid=%s hmi_uid=%s '
                      'path=%r errno=%s owner_pids=%s fuser_rc=%s '
                      'fuser_stdout=%r fuser_stderr=%r',
                      exc.state, exc.stage, os.getpid(), os.getuid(), exc.path,
                      getattr(cause, 'errno', None), sorted(exc.owners),
                      getattr(result, 'returncode', getattr(cause, 'returncode', None)),
                      getattr(result, 'stdout', getattr(cause, 'stdout', None)),
                      getattr(result, 'stderr', getattr(cause, 'stderr', None)), exc_info=True)
        if fd is not None:
            os.close(fd)
        raise


class PixhawkTelemetry:
    def __init__(self, clock=time.monotonic, timeout=3.0, opener=open_passive, *, wall_clock=time.time):
        self.clock, self.timeout, self.opener = clock, timeout, opener
        self.lock = threading.RLock()
        self.messages = {}
        self.wall_clock = wall_clock
        self.received_unix_s = {}
        self.esc_messages = OrderedDict()
        self.esc_counts = dict.fromkeys(ESC_DIAGNOSTIC_TYPES, 0)
        self.esc_evictions = 0
        self.esc_parser_supported_types = None
        self.state = 'DISABLED'
        self.source = None
        self.stop_event = threading.Event()
        self.thread = None
        self.bench_tick = None
        self.command_observer = None
        self.heartbeat_serial = 0
        self.heartbeat_safety_serial = 0

    def ingest(self, msg):
        kind = msg.get_type()
        if self.command_observer is not None:
            self.command_observer(msg)
        if kind in ESC_DIAGNOSTIC_TYPES:
            # Observe before the legacy type and source filters. Do not grant these
            # packets authority over voltage_v or change autopilot source selection.
            with self.lock:
                self._observe_esc(msg, kind)
            return
        if kind not in TYPES:
            return
        with self.lock:
            # Only autopilot component; never mix GCS/other-system heartbeats.
            source = (msg.get_srcSystem(), msg.get_srcComponent())
            if source[1] != 1:
                return
            if kind == 'HEARTBEAT' and getattr(msg, 'autopilot', 8) != 8:
                if self.source is None:
                    self.source = source
            if self.source != source:
                return
            if kind == 'HEARTBEAT':
                previous = self.messages.get(kind)
                if msg.base_mode & 128 or (previous and self.clock() - previous[0] > self.timeout):
                    self.heartbeat_safety_serial += 1
            self.messages[kind] = (self.clock(), msg)
            if kind == 'HEARTBEAT':
                self.heartbeat_serial += 1
            if kind in ('SYS_STATUS', 'BATTERY_STATUS'):
                self.received_unix_s[kind] = self.wall_clock()

    def _observe_esc(self, msg, kind):
        source = (msg.get_srcSystem(), msg.get_srcComponent())
        if kind in ESC_GROUPS:
            first_index = ESC_GROUPS[kind] - 1
            unit, scale = 'cV', 100.0
        else:
            index = getattr(msg, 'index', None)
            first_index = index if type(index) is int and 0 <= index <= 60 and index % 4 == 0 else None
            unit, scale = 'V', 1.0
        raw = getattr(msg, 'voltage', None)
        values = list(raw[:4]) if isinstance(raw, (list, tuple)) else []
        entries = []
        for offset in range(4):
            value = values[offset] if offset < len(values) else None
            if type(value) not in (int, float) or not math.isfinite(value):
                value = None
            index = first_index + offset if first_index is not None else None
            entries.append(dict(esc_index=index, esc_number=index + 1 if index is not None else None,
                                raw_voltage=value, raw_unit=unit,
                                voltage_v=value / scale if value is not None else None,
                                positive_voltage=value > 0 if value is not None else False))
        filter_reason = ('COMPONENT_NOT_1' if source[1] != 1 else
                         'AUTOPILOT_NOT_SELECTED' if self.source is None else
                         'OTHER_SYSTEM_COMPONENT' if source != self.source else 'MATCH')
        key = (*source, kind, first_index)
        self.esc_counts[kind] += 1
        self.esc_messages[key] = (self.clock(), dict(message_type=kind, system=source[0], component=source[1],
            received_unix_s=self.wall_clock(), source_filter_at_receive=filter_reason,
            legacy_drop_reason='TYPE_NOT_IN_TYPES', controllers=entries))
        self.esc_messages.move_to_end(key)
        if len(self.esc_messages) > ESC_CACHE_LIMIT:
            self.esc_messages.popitem(last=False)
            self.esc_evictions += 1

    def _esc_snapshot(self, now, active):
        groups = []
        for stamp, message in self.esc_messages.values():
            age = max(0, now - stamp)
            groups.append(dict(message, controllers=[dict(item) for item in message['controllers']], age_s=round(age, 3),
                               state='FRESH' if active and age <= self.timeout else 'STALE',
                               matches_current_autopilot=(message['system'], message['component']) == self.source))
        return dict(scope='all_parsed_esc_messages_before_legacy_filters',
                    used_for_hmi_voltage=False, parser_supported_types=self.esc_parser_supported_types,
                    received_counts=dict(self.esc_counts), cache_limit=ESC_CACHE_LIMIT,
                    evicted_group_source_entries=self.esc_evictions, groups=groups)

    def snapshot(self):
        with self.lock:
            now = self.clock()
            ages = {k: max(0, now - self.messages[k][0]) if k in self.messages else None for k in TYPES}
            active = self.state == 'READING'
            def field(kind, value):
                age = ages[kind]
                state = 'UNKNOWN' if age is None else ('FRESH' if active and age <= self.timeout else 'STALE')
                if state == 'FRESH':
                    try:
                        result = value(self.messages[kind][1])
                    except (AttributeError, ValueError, TypeError, IndexError):
                        result = None
                    if result is None:
                        state = 'UNKNOWN'
                else:
                    result = None
                return dict(state=state, value=result if state == 'FRESH' else None)
            armed = field('HEARTBEAT', lambda m: 'ARMED' if m.base_mode & 128 else 'DISARMED')
            mode = field('HEARTBEAT', lambda m: dict(autopilot=m.autopilot, vehicle_type=m.type,
                                                      base_mode=m.base_mode, custom_mode=m.custom_mode))
            def battery(m):
                cells = list(m.voltages)
                if m.id != 0 or not cells or cells[0] == 65535:
                    return None
                values = [v for v in cells if v != 65535]
                values.extend(v for v in getattr(m, 'voltages_ext', []) if 0 < v < 65535)
                return sum(values) / 1000 if values and sum(values) > 0 else None
            def system_battery(m):
                return m.voltage_battery / 1000 if 0 < m.voltage_battery < 65535 else None
            battery_voltage = field('BATTERY_STATUS', battery)
            voltage = battery_voltage
            state_source = 'BATTERY_STATUS'
            if voltage['state'] != 'FRESH':
                fallback = field('SYS_STATUS', system_battery)
                if fallback['state'] == 'FRESH' or voltage['state'] == 'UNKNOWN':
                    voltage = fallback
                    state_source = 'SYS_STATUS'

            def safe_number(value):
                # Numeric MAVLink fields only; never stringify arbitrary objects.
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    try:
                        return value if math.isfinite(value) else None
                    except OverflowError:
                        return None
                return None

            def raw_array(message, name, limit):
                value = getattr(message, name, None)
                if not isinstance(value, (list, tuple, bytes, bytearray)):
                    return None
                return [safe_number(v) for v in value[:limit]]

            def validation_reason(kind, message):
                try:
                    if kind == 'SYS_STATUS':
                        raw = message.voltage_battery
                        if raw == 65535:
                            return 'UINT16_MAX_NOT_SENT'
                        if raw <= 0:
                            return 'NON_POSITIVE_VOLTAGE'
                        if raw > 65535:
                            return 'OUT_OF_RANGE'
                        return 'VALID' if system_battery(message) is not None else 'INVALID_VALUE'
                    cells = list(message.voltages)
                    if message.id != 0:
                        return 'NON_PRIMARY_BATTERY_ID'
                    if not cells:
                        return 'EMPTY_VOLTAGES'
                    if cells[0] == 65535:
                        return 'FIRST_VOLTAGE_UINT16_MAX'
                    return 'VALID' if battery(message) is not None else 'NON_POSITIVE_TOTAL'
                except (AttributeError, ValueError, TypeError, IndexError):
                    return 'DECODE_ERROR'

            def diagnostic(kind):
                received = kind in self.messages
                message = self.messages[kind][1] if received else None
                age = ages[kind]
                raw_reason = validation_reason(kind, message) if received else 'NOT_RECEIVED'
                # Freshness and raw validity are separate: retain old raw values for
                # diagnosis, but never turn them into a fresh voltage measurement.
                reason = ('NOT_RECEIVED' if not received else 'READER_NOT_READING' if not active
                          else 'MESSAGE_STALE' if age > self.timeout else raw_reason)
                result = dict(received=received, received_unix_s=safe_number(self.received_unix_s.get(kind)),
                              age_s=round(age, 3) if age is not None else None,
                              reason=reason, raw_validation_reason=raw_reason)
                if kind == 'SYS_STATUS':
                    result['voltage_battery_mv'] = safe_number(getattr(message, 'voltage_battery', None))
                else:
                    result.update(id=safe_number(getattr(message, 'id', None)),
                                  voltages_mv=raw_array(message, 'voltages', 10),
                                  voltages_ext_mv=raw_array(message, 'voltages_ext', 4))
                return result

            voltage_diagnostics = dict(scope='accepted_autopilot_messages', reader_state=self.state,
                autopilot_source=list(self.source) if self.source is not None else None,
                timeout_s=self.timeout, state_source=state_source,
                selected_source=state_source if voltage['state'] == 'FRESH' else None,
                SYS_STATUS=diagnostic('SYS_STATUS'), BATTERY_STATUS=diagnostic('BATTERY_STATUS'))
            def yaw(m):
                return round(math.degrees(m.yaw) % 360, 2) if math.isfinite(m.yaw) else None
            ekf = {k: field(k, lambda m: {'flags': int(m.flags)}) for k in TYPES[-2:]}
            hb = ages['HEARTBEAT']
            connection = self.state if not active else ('UNKNOWN' if hb is None else ('CONNECTED' if hb <= self.timeout else 'STALE'))
            return dict(read_only=self.bench_tick is None, connection=connection, armed=armed, flight_mode=mode,
                        voltage_v=voltage, voltage_diagnostics=voltage_diagnostics, esc_diagnostics=self._esc_snapshot(now, active), yaw_deg=field('ATTITUDE', yaw), ekf=ekf,
                        messages={k: dict(age_s=round(v, 2) if v is not None else None,
                                         state='UNKNOWN' if v is None else ('FRESH' if active and v <= self.timeout else 'STALE')) for k, v in ages.items()})

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name='pixhawk-read-only', daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _run(self):
        fd = None
        try:
            from pymavlink.dialects.v20 import ardupilotmega
            parser = ardupilotmega.MAVLink(None)  # parser only: no file/transport to write to
            parser.robust_parsing = True
            with self.lock:
                self.messages.clear()
                self.received_unix_s.clear()
                self.esc_messages.clear()
                self.esc_counts = dict.fromkeys(ESC_DIAGNOSTIC_TYPES, 0)
                self.esc_evictions = 0
                names = {getattr(cls, 'msgname', None) for cls in getattr(ardupilotmega, 'mavlink_map', {}).values()}
                self.esc_parser_supported_types = [name for name in ESC_DIAGNOSTIC_TYPES if name in names]
                self.source = None
                self.state = 'CONNECTING'
            fd = self.opener()
            with self.lock:
                self.state = 'READING'
            while not self.stop_event.is_set():
                if self.bench_tick is not None:
                    self.bench_tick(fd)
                if not select.select([fd], [], [], getattr(self,'tick_interval',.2))[0]:
                    continue
                try:
                    data = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                if not data:
                    raise EOFError('Serial disconnected')
                for msg in parser.parse_buffer(data) or []:
                    self.ingest(msg)
        except ImportError:
            with self.lock:
                self.state = 'DEPENDENCY_MISSING'
        except PortFailure as exc:
            with self.lock:
                self.state = exc.state
        except subprocess.SubprocessError:
            with self.lock:
                self.state = 'PORT_CHECK_FAILED'
        except (OSError, EOFError, ValueError) as exc:
            with self.lock:
                self.state = ('DISCONNECTED' if fd is not None else
                              'PORT_ACCESS_DENIED' if isinstance(exc, PermissionError) else
                              'PORT_BUSY' if getattr(exc, 'errno', None) in (errno.EBUSY, errno.EAGAIN)
                              else 'PORT_ERROR')
        finally:
            if fd is not None:
                os.close(fd)
            with self.lock:
                if self.stop_event.is_set():
                    self.state = 'DISABLED'
