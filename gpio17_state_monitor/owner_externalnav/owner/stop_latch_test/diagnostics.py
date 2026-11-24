"""Bounded owner cache and allowlisted PARAM_REQUEST_READ, never PARAM_SET/control."""
from collections import deque
import math
import os
import time
from pymavlink.dialects.v20 import ardupilotmega as mav

def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


PARAMS=('RC5_OPTION','RCMAP_THROTTLE','RC_OPTIONS','RC_OVERRIDE_TIME','RC_PROTOCOLS','ARMING_CHECK','ARMING_REQUIRE','ARMING_SKIPCHK','VISO_TYPE',
        'SERIAL1_PROTOCOL','SERIAL2_PROTOCOL','SERIAL3_PROTOCOL','SERIAL4_PROTOCOL',
        'SERIAL5_PROTOCOL','SERIAL6_PROTOCOL','SERIAL7_PROTOCOL','SERIAL8_PROTOCOL')


BRAKE_PARAMS = tuple(
    f'SERVO{channel}_{field}'
    for field in ('FUNCTION', 'MIN', 'MAX', 'TRIM', 'REVERSED')
    for channel in range(1, 17)
) + (
    'RCMAP_ROLL', 'RCMAP_PITCH', 'RCMAP_THROTTLE', 'RCMAP_YAW',
    'RC8_OPTION', 'RC8_MIN', 'RC8_MAX', 'RC8_TRIM', 'RC8_DZ', 'RC8_REVERSED',
    'RC_OPTIONS', 'RC_PROTOCOLS', 'SERVO_RC_FS_MSK', 'SERVO_GPIO_MASK',
    'BRD_SAFETY_DEFLT', 'BRD_SAFETYOPTION', 'BRD_SAFETY_MASK', 'BRD_IO_ENABLE',
    'SERVO_RATE', 'SCR_ENABLE', 'FS_THR_ENABLE', 'FS_THR_VALUE', 'FS_TIMEOUT',
    'FS_ACTION', 'FS_OPTIONS', 'FS_GCS_ENABLE', 'FS_GCS_TIMEOUT', 'FS_EKF_ACTION',
    'FS_EKF_THRESH', 'FS_CRASH_CHECK',
)


class Diagnostics:
    def __init__(self,reader,encoder,query=False,write=os.write,*,brake_query=False,export_params=False, pwm_observe=False, pwm_interval=None):
        self.reader,self.encoder,self.query,self.write=reader,encoder,query,write
        self.texts=deque(maxlen=100);self.rc=None;self.sys=None;self.params={}
        self.overrides=deque(maxlen=20)
        self.requested = tuple(dict.fromkeys((PARAMS if query else ()) +
                                             (BRAKE_PARAMS if brake_query else ())))
        self.queue = deque(self.requested)
        self.attempted=[];self.last_query=None;self.fault=False
        self.pwm_observe = pwm_observe
        self.pwm_interval = pwm_interval
        self.brake = None
        self.servo_pwm = None
        self.pwm_probe_started = None
        self.pwm_request_state = 'WAITING' if pwm_interval is not None else 'DISABLED'
        self.export_state = 'WAITING' if export_params else 'DISABLED'
        self.export_deadline = None
        self.export_rows = {}
        self.export_names = {}
        self.export_count = None

    def _observe_pwm(self, kind, msg, now):
        if not self.pwm_observe or self.reader.source != (1, 1):
            return
        if kind == 'SERVO_OUTPUT_RAW' and msg.port == 0:
            self.servo_pwm = {
                'at': now, 'received_unix_s': time.time(), 'port': 0,
                'servo5_raw_us': msg.servo5_raw, 'servo6_raw_us': msg.servo6_raw,
            }
        elif (kind == 'COMMAND_ACK' and self.pwm_request_state == 'SENT' and
              msg.command == mav.MAV_CMD_SET_MESSAGE_INTERVAL and
              (msg.target_system, msg.target_component) in ((0, 0), (255, 190))):
            self.pwm_request_state = 'ACK_ACCEPTED' if msg.result == mav.MAV_RESULT_ACCEPTED else 'ACK_REJECTED'

    def _tick_pwm(self, fd, command_pending):
        if self.pwm_request_state != 'WAITING':
            return
        now = self.reader.clock()
        heartbeat = self.reader.messages.get('HEARTBEAT')
        if (command_pending or self.reader.state != 'READING' or self.reader.source != (1, 1) or
                heartbeat is None or not 0 <= now - heartbeat[0] < 1.5 or heartbeat[1].base_mode & 128):
            self.pwm_probe_started = None
            return
        if self.pwm_interval == 500000:
            if self.pwm_probe_started is None:
                self.pwm_probe_started = now
            if self.servo_pwm is not None and 0 <= now - self.servo_pwm['at'] < 3:
                self.pwm_request_state = 'PASSIVE_DATA_PRESENT'
                return
            if now - self.pwm_probe_started < 5:
                return
        packet = mav.MAVLink_command_long_message(
            1, 1, mav.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
            mav.MAVLINK_MSG_ID_SERVO_OUTPUT_RAW, self.pwm_interval, 0, 0, 0, 0, 0,
        ).pack(self.encoder)
        try:
            if self.write(fd, packet) != len(packet):
                raise OSError('Partial telemetry interval request')
            self.encoder.seq = (self.encoder.seq + 1) % 256
            self.pwm_request_state = 'SENT'
        except OSError:
            self.pwm_request_state = 'IO_ERROR'

    @staticmethod
    def _aged(row, now):
        if not row:
            return None
        at = row.get('at')
        age = now - at if _finite(now) and _finite(at) else None
        return dict(row, age_s=age if _finite(age) else None)

    def brake_pwm_snapshot(self):
        with self.reader.lock:
            return self._pwm_snapshot(self.reader.clock()) if self.pwm_observe else {}

    def _pwm_snapshot(self, now):
        servo = self._aged(self.servo_pwm, now)
        rc = self._aged(self.rc, now)
        rc8 = rc['channels'].get('8') if rc is not None else None
        current = self.reader.state == 'READING' and self.reader.source == (1, 1)

        def fresh(row):
            return current and row is not None and _finite(row['age_s']) and 0 <= row['age_s'] < 3

        rc_fresh, servo_fresh = fresh(rc), fresh(servo)
        return {
            'source': 'PIXHAWK_REPORTED_PWM',
            'rc_state': 'FRESH' if rc_fresh else 'STALE' if rc else 'NOT_RECEIVED',
            'servo_state': 'FRESH' if servo_fresh else 'STALE' if servo else 'NOT_RECEIVED',
            'rc8_us': rc8 if rc_fresh and type(rc8) is int and 800 <= rc8 <= 2200 else None,
            'rc_received_unix_s': rc.get('received_unix_s') if rc else None,
            'rc_age_s': rc.get('age_s') if rc else None,
            'servo5_raw_us': servo['servo5_raw_us'] if servo_fresh else None,
            'servo6_raw_us': servo['servo6_raw_us'] if servo_fresh else None,
            'servo_received_unix_s': servo.get('received_unix_s') if servo else None,
            'servo_age_s': servo.get('age_s') if servo else None,
            'interval_request': self.pwm_request_state,
            'release_confirmation': self._release_pwm_confirmation(servo if servo_fresh else None),
        }

    def _release_pwm_confirmation(self, servo):
        brake = self.brake
        if brake is None or brake.phase not in ('PENDING', 'SENT'):
            return {'state': 'NOT_REQUESTED', 'request_id': None}
        state = 'AWAITING_POST_RELEASE_PWM'
        if (servo is not None and brake.release_sent_at is not None and
                servo['at'] > brake.release_sent_at and
                brake.release_safety_serial == self.reader.heartbeat_safety_serial):
            pair = (servo['servo5_raw_us'], servo['servo6_raw_us'])
            state = 'CONFIRMED' if pair == (2000, 2000) else 'MISMATCH'
        return {'state': state, 'request_id': brake.request_id}

    def _export_observe(self, msg, now):
        if self.export_state != 'READING':
            return
        if now >= self.export_deadline:
            self.export_state = 'INCOMPLETE'
            return
        count, index = msg.param_count, msg.param_index
        name, value = str(msg.param_id), float(msg.param_value)
        if index == 65535:
            return  # An unsolicited parameter outside the indexed list.
        if (not 0 <= index < count <= 8192 or not 1 <= len(name) <= 16 or
                not name.isascii() or not math.isfinite(value) or msg.param_type not in (
                    mav.MAV_PARAM_TYPE_INT8, mav.MAV_PARAM_TYPE_INT16, mav.MAV_PARAM_TYPE_INT32,
                    mav.MAV_PARAM_TYPE_REAL32) or
                self.export_count not in (None, count)):
            self.export_state = 'INVALID'
            return
        row = {'name': name, 'value': value, 'type': msg.param_type}
        if self.export_rows.get(index, row) != row or self.export_names.get(name, index) != index:
            self.export_state = 'INVALID'
            return
        self.export_count = count
        self.export_rows[index] = row
        self.export_names[name] = index
        if len(self.export_rows) == count:
            self.export_state = 'COMPLETE'

    def _export_tick(self, fd, command_pending):
        if self.export_state not in ('WAITING', 'READING'):
            return
        now = self.reader.clock()
        hb = self.reader.messages.get('HEARTBEAT')
        ready = (not command_pending and self.reader.state == 'READING' and
                 self.reader.source == (1, 1) and hb is not None and
                 0 <= now - hb[0] < 1 and not hb[1].base_mode & 128)
        if self.export_state == 'READING':
            if not ready or now >= self.export_deadline:
                self.export_state = 'INCOMPLETE'
            return
        if ready:
            packet = mav.MAVLink_param_request_list_message(1, 1).pack(self.encoder)
            try:
                if self.write(fd, packet) != len(packet):
                    raise OSError('Partial parameter-list request')
                self.encoder.seq = (self.encoder.seq + 1) % 256
                self.export_state = 'READING'
                self.export_deadline = now + 180
            except OSError:
                self.export_state = 'IO_ERROR'

    def export_snapshot(self):
        with self.reader.lock:
            return {'state': self.export_state, 'received': len(self.export_rows),
                    'expected': self.export_count,
                    'parameters': [self.export_rows[i] for i in sorted(self.export_rows)]
                    if self.export_state == 'COMPLETE' else []}

    def observe(self,msg):
        k=msg.get_type();now=self.reader.clock()
        with self.reader.lock:
            # An echoed override can be evidence; lack of one cannot rule out other links.
            if k in ('RC_CHANNELS_OVERRIDE','MANUAL_CONTROL'):
                self.overrides.append(dict(type=k,source=[msg.get_srcSystem(),msg.get_srcComponent()],at=now))
            if (msg.get_srcSystem(),msg.get_srcComponent())!=(1,1):return
            self._observe_pwm(k, msg, now)
            if k == 'PARAM_VALUE':
                self._export_observe(msg, now)
            if k=='STATUSTEXT':self.texts.append(dict(at=now,severity=int(msg.severity),text=str(msg.text)))
            elif k=='RC_CHANNELS':
                self.rc=dict(at=now,channels={str(i):int(getattr(msg,f'chan{i}_raw')) for i in range(1,min(18,msg.chancount)+1)},rssi=int(msg.rssi),time_boot_ms=int(msg.time_boot_ms))
                if self.pwm_observe:
                    self.rc['received_unix_s'] = time.time()
            elif k=='SYS_STATUS':
                self.sys=dict(at=now,present=int(msg.onboard_control_sensors_present),enabled=int(msg.onboard_control_sensors_enabled),health=int(msg.onboard_control_sensors_health))
            elif k=='PARAM_VALUE' and (str(msg.param_id) in PARAMS or
                                      (str(msg.param_id) in BRAKE_PARAMS and str(msg.param_id) in self.attempted)):
                value=float(msg.param_value)
                if math.isfinite(value):self.params[str(msg.param_id)]=dict(value=value,at=now)

    def tick(self,fd,command_pending=False):
        with self.reader.lock:
            self._tick_pwm(fd, command_pending)
            if self.export_state != 'DISABLED':
                self._export_tick(fd, command_pending)
                return
            if not self.queue or self.fault or command_pending:return
            now=self.reader.clock();hb=self.reader.messages.get('HEARTBEAT')
            if self.reader.state!='READING' or hb is None or not 0<=now-hb[0]<1:return
            if hb[1].base_mode&128:return # Diagnostic requests only while fresh DISARMED.
            if self.last_query is not None and now-self.last_query<1:return
            target=self.reader.source
            if target is None:return
            name=self.queue.popleft();self.last_query=now;self.attempted.append(name)
            packet=mav.MAVLink_param_request_read_message(target[0],target[1],name.encode('ascii'),-1).pack(self.encoder)
            try:
                if self.write(fd,packet)!=len(packet):raise OSError('Partial diagnostic request')
                self.encoder.seq=(self.encoder.seq+1)%256
            except OSError:self.fault=True # No retry and no owner shutdown.

    def _attitude(self, now):
        # Use the source-validated receive cache, not the before-filter observer.
        result = dict(state='UNKNOWN', valid=False, fresh=False, source=None,
                      received_monotonic_s=None, time_boot_ms=None, age_s=None,
                      freshness_s=None, roll_rad=None, pitch_rad=None, yaw_rad=None,
                      roll_deg=None, pitch_deg=None, yaw_deg=None, yaw_deg_360=None,
                      convention='AERONAUTICAL_FRD_INTRINSIC_ZYX',
                      validity_scope='SOURCE_FINITE_EULER_AND_RECEIPT_FRESHNESS_ONLY',
                      reason='NOT_RECEIVED')
        try:
            sample = self.reader.messages.get('ATTITUDE')
            if sample is None:
                return result
            result.update(state='INVALID', reason='INVALID_ATTITUDE_SAMPLE')
            if type(sample) not in (tuple, list) or len(sample) != 2:
                return result
            received_at, message = sample
            if _finite(received_at):
                result['received_monotonic_s'] = received_at
            source = (message.get_srcSystem(), message.get_srcComponent())
            if (any(type(v) is not int for v in source) or
                    not 1 <= source[0] <= 255 or not 0 <= source[1] <= 255):
                result['reason'] = 'INVALID_SOURCE'
                return result
            result['source'] = list(source)
            boot_ms = getattr(message, 'time_boot_ms', None)
            if type(boot_ms) is int and 0 <= boot_ms <= 0xffffffff:
                result['time_boot_ms'] = boot_ms
            else:
                result['reason'] = 'INVALID_PACKET_TIME'
                return result
            if source != self.reader.source:
                result['reason'] = 'SOURCE_MISMATCH'
                return result
            if not _finite(now) or not _finite(received_at):
                result['reason'] = 'INVALID_RECEIPT_CLOCK'
                return result
            age = now - received_at
            if not _finite(age):
                result['reason'] = 'INVALID_RECEIPT_CLOCK'
                return result
            result['age_s'] = age
            if age < 0:
                result['reason'] = 'TIMESTAMP_IN_FUTURE'
                return result
            timeout = self.reader.timeout
            if not _finite(timeout) or timeout <= 0:
                result['reason'] = 'INVALID_FRESHNESS_LIMIT'
                return result
            result['freshness_s'] = timeout
            angles = (getattr(message, 'roll', None), getattr(message, 'pitch', None),
                      getattr(message, 'yaw', None))
            if not all(_finite(v) for v in angles):
                result['reason'] = 'NONFINITE_OR_MISSING_EULER'
                return result
            degrees = tuple(math.degrees(v) for v in angles)
            if not all(_finite(v) for v in degrees):
                result['reason'] = 'ANGLE_CONVERSION_OVERFLOW'
                return result
            if self.reader.state != 'READING':
                result.update(state='STALE', reason='READER_NOT_READING')
                return result
            if age > timeout:
                result.update(state='STALE', reason='ATTITUDE_STALE')
                return result
            result.update(state='FRESH', valid=True, fresh=True, reason=None,
                          roll_rad=angles[0], pitch_rad=angles[1], yaw_rad=angles[2],
                          roll_deg=degrees[0], pitch_deg=degrees[1], yaw_deg=degrees[2],
                          yaw_deg_360=degrees[2] % 360.0)
            return result
        except Exception:
            # Malformed diagnostics must not turn the read-only endpoint into500.
            result.update(state='INVALID', valid=False, fresh=False,
                          reason='INVALID_ATTITUDE_SAMPLE')
            return result

    def snapshot(self):
        with self.reader.lock:
            try:now=self.reader.clock()
            except Exception:now=None
            def aged(row):
                return self._aged(row, now)
            result = dict(attitude=self._attitude(now),rc=aged(self.rc),sys_status=aged(self.sys),statustext=[aged(x) for x in self.texts],
                        parameters={k:aged(v) for k,v in self.params.items()},
                        diagnostic_requests_attempted=list(self.attempted),query_fault=self.fault,
                        echoed_override_messages=[aged(x) for x in self.overrides],
                        absence_of_echo_does_not_prove_no_override=True)

            if self.pwm_observe:
                result['brake_pwm'] = self._pwm_snapshot(now)
            return result
