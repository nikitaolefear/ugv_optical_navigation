"""Latest-only optical pose, encoded solely by owner serial thread. Default observe-only."""
import math
import os
import threading
import hashlib
import json
from pathlib import Path
from pymavlink.dialects.v20 import ardupilotmega as mav


def alignment_verified():
    try:
        p=Path('/etc/ugv-externalnav/alignment-verified.json')
        for item in (p.parent,p):
            info=item.lstat()
            if item.is_symlink() or info.st_uid!=0 or info.st_mode&0o022:return False
        d=json.loads(p.read_text())
        if d.get('camera_mount_verified') is not True or d.get('mapped_markers_verified') is not True:return False
        if d.get('boot_id')!=Path('/proc/sys/kernel/random/boot_id').read_text().strip():return False
        optical=Path(__file__).resolve().parents[2]/'optical'
        for name in ('camera_calibration_fisheye.npz','rover_config.json','marker_map.json'):
            if d.get('sha256',{}).get(name)!=hashlib.sha256((optical/name).read_bytes()).hexdigest():return False
        return True
    except (OSError,ValueError,TypeError):return False


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
                if p is None or p['sequence']==self.used or self.fault:return
                now = self.reader.clock()
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
                self.used = p['sequence']
                cov = [float('nan')] + [0.0]*20
                packet = mav.MAVLink_odometry_message(
                    int(p['acquired_at']*1e6),mav.MAV_FRAME_LOCAL_FRD,mav.MAV_FRAME_BODY_FRD,
                    p['x'],p['y'],p['z'],p['q'],0.,0.,0.,0.,0.,0.,cov,cov,0,
                    mav.MAV_ESTIMATOR_TYPE_VISION,100).pack(self.encoder)
                try:
                    if self.write(fd,packet)!=len(packet):raise OSError('Partial ODOMETRY write')
                    self.encoder.seq=(self.encoder.seq+1)%256
                    self.sent += 1;self.last_sent=now
                except OSError:
                    self.fault=True;self.last_reason='TX_FAULT_NO_RETRY'

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
            return dict(tx_enabled=False if self.bench_profile is not None else self.tx,
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
