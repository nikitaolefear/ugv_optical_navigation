"""Bounded owner cache and allowlisted PARAM_REQUEST_READ, never PARAM_SET/control."""
from collections import deque
import math
import os
from pymavlink.dialects.v20 import ardupilotmega as mav

def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


PARAMS=('RC5_OPTION','RCMAP_THROTTLE','RC_OPTIONS','RC_OVERRIDE_TIME','RC_PROTOCOLS','ARMING_CHECK','ARMING_REQUIRE','ARMING_SKIPCHK','VISO_TYPE',
        'SERIAL1_PROTOCOL','SERIAL2_PROTOCOL','SERIAL3_PROTOCOL','SERIAL4_PROTOCOL',
        'SERIAL5_PROTOCOL','SERIAL6_PROTOCOL','SERIAL7_PROTOCOL','SERIAL8_PROTOCOL')


class Diagnostics:
    def __init__(self,reader,encoder,query=False,write=os.write):
        self.reader,self.encoder,self.query,self.write=reader,encoder,query,write
        self.texts=deque(maxlen=100);self.rc=None;self.sys=None;self.params={}
        self.overrides=deque(maxlen=20)
        self.queue=deque(PARAMS if query else ());self.attempted=[];self.last_query=None;self.fault=False

    def observe(self,msg):
        k=msg.get_type();now=self.reader.clock()
        with self.reader.lock:
            # An echoed override can be evidence; lack of one cannot rule out other links.
            if k in ('RC_CHANNELS_OVERRIDE','MANUAL_CONTROL'):
                self.overrides.append(dict(type=k,source=[msg.get_srcSystem(),msg.get_srcComponent()],at=now))
            if (msg.get_srcSystem(),msg.get_srcComponent())!=(1,1):return
            if k=='STATUSTEXT':self.texts.append(dict(at=now,severity=int(msg.severity),text=str(msg.text)))
            elif k=='RC_CHANNELS':
                self.rc=dict(at=now,channels={str(i):int(getattr(msg,f'chan{i}_raw')) for i in range(1,min(18,msg.chancount)+1)},rssi=int(msg.rssi),time_boot_ms=int(msg.time_boot_ms))
            elif k=='SYS_STATUS':
                self.sys=dict(at=now,present=int(msg.onboard_control_sensors_present),enabled=int(msg.onboard_control_sensors_enabled),health=int(msg.onboard_control_sensors_health))
            elif k=='PARAM_VALUE' and str(msg.param_id) in PARAMS:
                value=float(msg.param_value)
                if math.isfinite(value):self.params[str(msg.param_id)]=dict(value=value,at=now)

    def tick(self,fd,command_pending=False):
        with self.reader.lock:
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
                if not row:return None
                at=row.get('at')
                age=now-at if _finite(now) and _finite(at) else None
                return dict(row,age_s=age if _finite(age) else None)
            return dict(attitude=self._attitude(now),rc=aged(self.rc),sys_status=aged(self.sys),statustext=[aged(x) for x in self.texts],
                        parameters={k:aged(v) for k,v in self.params.items()},
                        diagnostic_requests_attempted=list(self.attempted),query_fault=self.fault,
                        echoed_override_messages=[aged(x) for x in self.overrides],
                        absence_of_echo_does_not_prove_no_override=True)
