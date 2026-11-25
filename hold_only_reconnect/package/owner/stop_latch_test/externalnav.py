"""Latest-only optical pose, encoded solely by owner serial thread. Default observe-only."""
import math
import os
import threading
import hashlib
import json
from pathlib import Path
from pymavlink.dialects.v20 import ardupilotmega as mav


def planar_vision_message(*, acquired_at, now, north_m, east_m, yaw_ned_rad):
    """Encode NED X/Y/yaw from an already aligned planar pose.

    Z, roll and pitch are required wire fields. Their zeros are placeholders
    for this level-rover profile, and covariance is unknown. The caller must
    verify that the estimator uses its other sensors for height and tilt.
    """
    values = (acquired_at, now, north_m, east_m, yaw_ned_rad)
    if not all(type(value) in (int, float) and math.isfinite(value) for value in values):
        raise ValueError('Planar pose and capture time must be finite')
    if not 0 < acquired_at <= now or now - acquired_at > .2:
        raise ValueError('Planar pose capture time is stale or invalid')
    if not -math.pi <= yaw_ned_rad <= math.pi:
        raise ValueError('Planar yaw must be normalized NED radians')
    return mav.MAVLink_vision_position_estimate_message(
        int(acquired_at * 1e6), north_m, east_m, 0., 0., 0., yaw_ned_rad,
        [float('nan')] + [0.] * 20, 0)


def alignment_record():
    try:
        p=Path('/etc/ugv-externalnav/alignment-verified.json')
        for item in (p.parent,p):
            info=item.lstat()
            if item.is_symlink() or info.st_uid!=0 or info.st_mode&0o022:return False
        d=json.loads(p.read_text())
        if not isinstance(d, dict) or not isinstance(d.get('sha256'), dict):return False
        if d.get('camera_mount_verified') is not True or d.get('mapped_markers_verified') is not True:return False
        if d.get('boot_id')!=Path('/proc/sys/kernel/random/boot_id').read_text().strip():return False
        optical=Path(__file__).resolve().parents[2]/'optical'
        assets = ('camera_calibration_fisheye.npz', 'rover_config.json', 'marker_map.json')
        if 'nav_mission' in d:
            assets += ('pose_transform_2d.py',)
        for name in assets:
            if d.get('sha256',{}).get(name)!=hashlib.sha256((optical/name).read_bytes()).hexdigest():return False
        return d
    except (OSError,ValueError,TypeError):return False


def alignment_verified():
    return bool(alignment_record())


def static_planar_message(pose, now, origin, parameters, record, *, mission_session=False):
    """Build only the approved, origin-aligned pose message."""
    # Canonical map emitted from navigation/aruco_station_candidate_23_25.json.
    if record.get('sha256', {}).get('marker_map.json') != (
            'b1a0707d53614c83869e4adffd5ab0d82a9a593969cc1fd606c4b0fabe49039d'):
        return None
    grant = record.get('nav_mission' if mission_session else 'nav_bench', {})
    if not isinstance(grant, dict) or grant.get('vesc_physically_isolated') is not True:
        return None
    deadline = grant.get('expires_monotonic')
    if (type(deadline) not in (float, int) or not math.isfinite(deadline) or deadline <= now
            or (not mission_session and deadline - now > 120)):
        return None
    keys = ('latitude_deg', 'longitude_deg', 'altitude_msl_m')
    if not isinstance(origin, dict):
        return None
    coordinates = [origin.get(k) for k in keys]
    if (coordinates in ([50, 30, 0], [0, 0, 0]) or
            grant.get('ekf_origin_wgs84_msl') != coordinates):
        return None
    required = {'AHRS_EKF_TYPE': 3, 'EK3_ENABLE': 1, 'EK3_SRC_OPTIONS': 0,
                'EK3_SRC2_POSXY': 6, 'EK3_SRC2_YAW': 6, 'EK3_SRC2_POSZ': 1,
                'EK3_SRC2_VELXY': 0, 'VISO_TYPE': 1, 'VISO_QUAL_MIN': 0,
                'VISO_POS_X': 0, 'VISO_POS_Y': 0, 'VISO_POS_Z': 0}
    if any(parameters.get(k, {}).get('value') != value for k, value in required.items()):
        return None
    # The active source may change independently of the companion computer.
    for source in (1, 2, 3):
        for field in ('POSZ', 'VELZ', 'VELXY'):
            if parameters.get(f'EK3_SRC{source}_{field}', {}).get('value') in (None, 6):
                return None
    transform = grant.get('datum_to_ekf_ned')
    if (not isinstance(transform, list) or len(transform) != 3 or
            not all(type(v) in (int, float) and math.isfinite(v) for v in transform)):
        return None
    north, east, heading = transform
    if max(abs(north), abs(east)) > 10000:
        return None
    turn = math.radians(heading % 360)
    c, s = math.cos(turn), math.sin(turn)
    w, _, _, z = pose['q']
    yaw = (turn + 2 * math.atan2(z, w) + math.pi) % math.tau - math.pi
    return planar_vision_message(acquired_at=pose['acquired_at'], now=now,
                                north_m=north + c*pose['x'] - s*pose['y'],
                                east_m=east + s*pose['x'] + c*pose['y'], yaw_ned_rad=yaw)


class ExternalNav:
    def __init__(self, reader, tx=False, write=os.write, alignment_check=alignment_verified,
                 bench_profile=None):
        if bench_profile is not None and tx:
            raise ValueError('Measured bench geometry is OBSERVE_ONLY; TX is forbidden')
        self.reader, self.tx, self.write = reader, tx, write
        self.bench_profile = bench_profile
        self.alignment_check = alignment_check
        self.lock = threading.Lock()
        self.pending = None
        self.sequence = self.used = self.sent = 0
        self.last_sent = None
        self.last_frame = None
        self.last_pose = None
        self.last_reason = 'NO_VALID_POSE'
        self.bench_image_diagnostics = None
        self.fault = False
        self.static_writer = None
        self.static_enabled = False
        self.static_revoked = False
        # Preserve old optical source identity and message semantics.
        self.encoder = mav.MAVLink(None, srcSystem=1, srcComponent=191)
        self.stop = threading.Event()
        self.thread = None

    def submit(self, pose):
        with self.lock:
            self.pending = None
            if pose is None:
                if (self.bench_profile is not None and self.bench_image_diagnostics is not None
                        and not self.bench_image_diagnostics.get('valid',False)):
                    self.last_reason=str(self.bench_image_diagnostics.get('reason','NO_CURRENT_VALID_POSE'))[:96]
                else:
                    self.last_reason = 'NO_CURRENT_VALID_POSE'
                return
            now = self.reader.clock()
            stamp = pose.get('acquired_at')
            values = [pose.get(k) for k in ('x','y','z')]
            q = pose.get('q')
            if (type(stamp) not in (int,float) or not math.isfinite(stamp) or not 0 <= now-stamp <= .2
                    or (self.last_frame is not None and stamp <= self.last_frame)
                    or any(type(v) not in (int,float) or not math.isfinite(v) for v in values)
                    or type(q) is not list or len(q)!=4
                    or any(type(v) not in (int,float) or not math.isfinite(v) for v in q)
                    or not .99 <= sum(v*v for v in q) <= 1.01
                    or pose.get('source_marker') not in (23,24,25)
                    or (self.bench_profile is not None and pose.get('source_marker') != 24)):
                self.last_reason = 'INVALID_OR_STALE_POSE'
                return
            self.sequence += 1
            self.last_frame = stamp
            self.pending = dict(pose, q=list(q), sequence=self.sequence)
            self.last_pose = dict(self.pending)
            self.last_reason = 'VALID_POSE'

    def tick(self, fd, command_pending=False):
        # Called only in existing serial owner's reader/tick thread, after DISARM tick.
        with self.reader.lock:
            with self.lock:
                p = self.pending
                if self.fault or self.static_revoked:return
                now = self.reader.clock()
                if self.static_writer is not None:
                    packet = None
                    if p is not None and 0 <= now-p['acquired_at'] <= .2 and not command_pending:
                        try:
                            packet = self.static_writer(p, now)
                        except ValueError:
                            pass
                    if packet is None:
                        if self.static_enabled:
                            self.static_revoked = True
                        self.last_reason = 'NAV_BENCH_TX_BLOCKED'
                        return
                    if p['sequence'] != self.used and (self.last_sent is None or now-self.last_sent >= .1):
                        self._send(fd, p, now, packet.pack(self.encoder))
                        self.static_enabled = not self.fault
                    return
                if p is None or p['sequence']==self.used:return
                if not 0 <= now-p['acquired_at'] <= .2:
                    self.pending = None;self.last_reason='POSE_STALE';return
                # Independent of CLI and root alignment records: bench frames
                # are never estimator-aligned or authorized for transmission.
                if self.bench_profile is not None or not self.tx or command_pending:return
                if not self.alignment_check():
                    self.last_reason='PHYSICAL_ALIGNMENT_UNVERIFIED';return
                hb = self.reader.messages.get('HEARTBEAT')
                if (self.reader.state!='READING' or hb is None or not 0 <= now-hb[0] <= 1
                        or self.reader.source!=(1,1) or hb[1].autopilot!=3 or hb[1].type!=10):
                    self.last_reason='NO_FRESH_AUTOPILOT';return
                if self.last_sent is not None and now-self.last_sent < .1:return
                # Consume before write: partial/failed writes are never retried.
                cov = [float('nan')] + [0.0]*20
                packet = mav.MAVLink_odometry_message(
                    int(p['acquired_at']*1e6),mav.MAV_FRAME_LOCAL_FRD,mav.MAV_FRAME_BODY_FRD,
                    p['x'],p['y'],p['z'],p['q'],0.,0.,0.,0.,0.,0.,cov,cov,0,
                    mav.MAV_ESTIMATOR_TYPE_VISION,100).pack(self.encoder)
                self._send(fd, p, now, packet)

    def _send(self, fd, pose, now, packet):
        self.used = pose['sequence']
        try:
            if self.write(fd,packet)!=len(packet):raise OSError('Partial ExternalNav write')
            self.encoder.seq=(self.encoder.seq+1)%256
            self.sent += 1;self.last_sent=now
        except OSError:
            self.fault=True;self.last_reason='TX_FAULT_NO_RETRY'

    def route_pose(self):
        # A map pose is qualified by the owner before being used for missions.
        with self.lock:
            if self.bench_profile is not None or self.pending is None or self.fault:
                return None
            if not 0 <= self.reader.clock() - self.pending['acquired_at'] <= .2:
                return None
            return dict(self.pending, q=list(self.pending['q']))

    def snapshot(self):
        with self.lock:
            now=self.reader.clock()
            age=now-self.last_frame if self.last_frame is not None else None
            fresh=age is not None and 0 <= age <= .2 and self.pending is not None
            image=None
            if self.bench_image_diagnostics is not None:
                image_age=now-self.bench_image_diagnostics['received_monotonic_s']
                frame_at=self.bench_image_diagnostics.get('acquired_at')
                frame_age=now-frame_at if frame_at is not None else None
                image=dict(self.bench_image_diagnostics,age_s=image_age,
                           frame_age_s=frame_age,
                           fresh=frame_age is not None and 0 <= frame_age <= .2)
            return dict(tx_enabled=False if self.bench_profile is not None else (
                            self.tx or (self.static_enabled and not self.static_revoked and not self.fault)),
                        nav_bench_tx=self.static_writer is not None,
                        alignment_verified=False if self.bench_profile is not None else self.alignment_check(),
                        geometry_mode='MEASURED_6DOF_BENCH_OBSERVE_ONLY' if self.bench_profile is not None else 'LEGACY_PLANAR_Z0_YAW_ONLY',
                        bench_geometry=self.last_pose.get('bench_geometry') if fresh and self.bench_profile is not None else None,
                        bench_image_diagnostics=image,
                        optical_measurement=(dict(self.last_pose['optical_measurement'],age_s=age,fresh=True,
                                                  snapshot_monotonic_s=now)
                                             if fresh and self.last_pose.get('optical_measurement') else None),
                        serial_writer='EXISTING_OWNER_THREAD',
                        finite_pose_count=self.sequence,sent=self.sent,fault=self.fault,
                        pose_age_s=age,pose_fresh=fresh,reason=self.last_reason,
                        marker=self.last_pose.get('source_marker') if self.last_pose else None,
                        camera_thread_alive=self.thread.is_alive() if self.thread else False)

    def start(self):
        def status(details):
            with self.lock:
                self.pending=None
                if isinstance(details,dict):
                    # Image quality is separate from physical survey/alignment.
                    self.bench_image_diagnostics={k:v for k,v in details.items() if k!='bench_geometry'}
                    self.bench_image_diagnostics['received_monotonic_s']=self.reader.clock()
                    self.last_reason=str(details.get('reason','BENCH_NO_VALID_POSE'))[:96]
                else:
                    self.last_reason=str(details)[:96]
        def worker():
            try:
                if self.bench_profile is None:
                    from owner_optical_worker import run
                    run(self.submit,self.stop)
                else:
                    from bench_optical_worker import run
                    run(self.submit,self.stop,self.bench_profile,on_status=status)
            except Exception as e:
                with self.lock:
                    self.pending=None
                    self.last_reason=('BENCH_PROFILE_INVALID' if type(e).__name__=='ProfileError'
                                      else 'CAMERA_ERROR_'+type(e).__name__)
        self.thread=threading.Thread(target=worker,name='optical-pose-no-serial',daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=2)
