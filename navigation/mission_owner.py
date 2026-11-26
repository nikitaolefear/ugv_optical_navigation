"""IPCs queue work; only the serial owner tick performs the mission protocol."""

from collections import deque
import json
import math
from pathlib import Path
import re

from .dynamic_mission import Blocked, MissionSession, OwnerEvidence, Pose, check_stopped, compile_leg
from .mission_transfer import MissionTransfer, TARGET

HOLD = 4
AUTO = 10
OPERATIONS = ('MISSION_PREVIEW', 'MISSION_REQUEST', 'MISSION_STATUS')


class MissionOwner:
    def __init__(self, reader, nav, diagnostics, encoder, config, marker_map, directory,
                 safety_ready, send_hold, *, surveyed_map, write=None, catalog=None,
                 require_live_tx=False, traction_readiness=None, latch_stop=None, brake_command=None,
                 static_upload=False):
        self.reader, self.nav, self.diagnostics = reader, nav, diagnostics
        self.safety_ready, self.send_hold = safety_ready, send_hold
        self.surveyed_map = surveyed_map
        self.require_live_tx = require_live_tx
        self.static_upload = static_upload
        self.traction_readiness = traction_readiness or (
            lambda: dict(ready=False, reason='TRACTION_READINESS_UNAVAILABLE'))
        self.brake_command = brake_command or (lambda: None)
        self.latch_stop = latch_stop
        self.transfer = MissionTransfer(encoder, directory, **({'write': write} if write is not None else {}))
        self.samples = deque(maxlen=64)
        self.session = MissionSession(config, marker_map, self._evidence, reader.clock,
                                      request_hold=self._queue_hold, catalog=catalog, storage_without_coverage=True)
        self.state, self.reason = 'DRAFT', None
        self.pending = None
        self.epoch = None
        self.hold_queue = self.hold_request_at = self.hold_deadline = None
        self.failed_after_hold = False
        self.latest_done = False
        self.execution_admitted = False
        self.hold_observed = False
        self.navigation_hold_status = None

    @classmethod
    def from_profile(cls, path, map_path, *args, **kwargs):
        from hashlib import sha256
        import stat

        profile = Path(path)
        info = profile.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise PermissionError('Mission survey profile must be protected and root-owned')
        config = json.loads(profile.read_text())
        catalog_path = profile.parent / config['marker_catalog_file']
        catalog_info = catalog_path.lstat()
        if not stat.S_ISREG(catalog_info.st_mode) or catalog_info.st_uid != 0 or catalog_info.st_mode & 0o022:
            raise PermissionError('Marker catalog must be protected and root-owned')
        catalog = json.loads(catalog_path.read_text())
        from .marker_catalog import validate
        validate(catalog)
        raw = Path(map_path).read_bytes()
        return cls(*args, config, json.loads(raw), **kwargs,
                   catalog=catalog, surveyed_map=bool(config.get('surveyed_map_sha256') == sha256(raw).hexdigest()))

    def _hb(self, now):
        sample = self.reader.messages.get('HEARTBEAT')
        if (self.reader.state != 'READING' or self.reader.source != TARGET or
                sample is None or not 0 <= now - sample[0] < 1.5):
            return None
        hb = sample[1]
        if (hb.autopilot, hb.type) != (3, 10) or not hb.base_mode & 1:
            return None
        return sample

    def _nav_valid(self, now):
        if self.require_live_tx and not self._live_tx():
            return False
        ekf = self.reader.messages.get('EKF_STATUS_REPORT')
        return (self.surveyed_map and self.nav.alignment_check() and ekf is not None and
                0 <= now - ekf[0] < .5 and ekf[1].flags & 16 and not ekf[1].flags & (128 | 1024))

    def _upload_navigation_valid(self, now):
        if not self.static_upload:
            return self._nav_valid(now)
        tx = self.nav.snapshot()
        return (self.surveyed_map and self.nav.alignment_check()
                and tx['tx_enabled'] is False and tx['sent'] == 0
                and tx.get('nav_bench_tx') is False
                and self._position(now) is not None)

    def _live_tx(self):
        tx = self.nav.snapshot()
        return tx['tx_enabled'] and tx['pose_fresh'] and tx.get('tx_fresh') is True

    def _position(self, now):
        sample = self.nav.route_pose()
        if sample is None or not 0 <= now - sample['acquired_at'] <= .2:
            return None
        w, _, _, z = sample['q']
        return Pose(sample['x'], -sample['y'], -math.degrees(2 * math.atan2(z, w)),
                    sample['acquired_at'], sample['source_marker'])

    def _capture(self, now, pose):
        # Local aligned observations can support stop confirmation with TX off.
        if pose is None:
            self.samples.clear()
        elif not self.samples or self.samples[-1].acquired_at < pose.acquired_at:
            if self.samples and (self.samples[-1].marker != pose.marker or
                                 pose.acquired_at - self.samples[-1].acquired_at > .2):
                self.samples.clear()
            self.samples.append(pose)
        while self.samples and now - self.samples[0].acquired_at > 1:
            self.samples.popleft()

    def _evidence(self, *, stationary_since=None):
        now = self.reader.clock()
        if stationary_since is None and self.session.state == 'HOLD_REQUIRED' and self.hold_queue == 'SENT':
            stationary_since = self.hold_request_at
        optical_stop = stationary_since is not None and self.surveyed_map and self.nav.alignment_check()
        hb = self._hb(now)
        pose = self._position(now) if optical_stop or self._upload_navigation_valid(now) or self._nav_valid(now) else None
        if pose is not None:
            # The camera can publish between serial ticks. Use the same fresh
            # observation to update the window and to evaluate stationarity.
            self._capture(now, pose)
        holding = hb and hb[1].custom_mode == HOLD
        stationary_at = None
        samples = [p for p in self.samples if stationary_since is None or p.acquired_at > stationary_since]
        if (pose and samples and samples[-1] == pose and
                pose.acquired_at - samples[0].acquired_at >= .6 and
                all(math.hypot(p.x_m - pose.x_m, p.y_m - pose.y_m) <= .025 for p in samples)):
            stationary_at = pose.acquired_at
        origin, home = self.diagnostics.origin, self.diagnostics.home
        epoch = self.diagnostics.readback_epoch == self.reader.heartbeat_safety_serial
        return OwnerEvidence(pose, hb[0] if holding else None, stationary_at,
                             home if epoch else None, origin if epoch else None)

    def _safe_upload(self, now):
        hb = self._hb(now)
        return bool(hb and hb[1].custom_mode == HOLD and not hb[1].base_mode & 128 and self.safety_ready())

    def _upload_ready(self, now):
        if not self._safe_upload(now) or not self._upload_navigation_valid(now):
            return False
        evidence = self._evidence()
        if self.static_upload and (evidence.home is None or evidence.origin is None):
            return False
        try:
            check_stopped(evidence, self.session.config, now)
        except Blocked:
            return False
        return True

    def _execution_safe(self, now):
        return (self.session.execution_coverage_confirmed and self.nav.tx and self._live_tx() and bool(self._nav_valid(now))
                and self.traction_readiness()['ready'] is True)

    def _execution_ready(self, now):
        hb = self._hb(now)
        return (self.state == 'VERIFIED' and self.session.state == 'REQUESTED' and not self.execution_admitted
                and hb is not None and hb[1].custom_mode == HOLD and not hb[1].base_mode & 128
                and self._execution_safe(now))

    def _motion_authorized(self, now):
        hb = self._hb(now)
        return (self.state == 'RUNNING' and hb is not None and hb[1].custom_mode == AUTO
                and bool(hb[1].base_mode & 128) and self._execution_safe(now))

    def command(self, operation, actor_id, actor, request_id, payload):
        if (operation not in OPERATIONS or type(actor_id) is not int or actor_id <= 0 or
                type(actor) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', actor) or
                type(request_id) is not str or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', request_id)):
            raise ValueError('Invalid mission identity')
        with self.reader.lock:
            if operation == 'MISSION_STATUS':
                return dict(self.snapshot(), id=request_id, actor_id=actor_id)
            result = {'id': request_id, 'actor_id': actor_id}
            now = self.reader.clock()
            try:
                if self.pending or self.execution_admitted or self.state in ('RUNNING', 'HOLD'):
                    raise Blocked('Незавершена місія або HOLD')
                if self.state == 'FAILED':
                    raise Blocked('Потрібне окреме приймання після помилки')
                if not self._safe_upload(now):
                    raise Blocked('Потрібні HOLD, DISARMED і чинні блокування')
                if not self._upload_navigation_valid(now):
                    raise Blocked('Вирівнювання, свіжість пози або стан ExternalNav не підтверджені')
                if self.static_upload:
                    evidence = self._evidence()
                    if evidence.home is None or evidence.origin is None:
                        raise Blocked('Немає свіжого перечитування HOME_POSITION та GPS_GLOBAL_ORIGIN')
                if not self._upload_ready(now):
                    raise Blocked('Немає свіжого підтвердження HOLD і фактичної зупинки')
                if operation == 'MISSION_REQUEST':
                    plan = self.session.prepare(payload, create=True)
                    try:
                        self.transfer.start(self.session.items, request_id, actor_id, now)
                    except (OSError, ValueError, KeyError, TypeError):
                        self.session.state = 'ABORTED'
                        raise Blocked('Запит уже використаний або резервна копія недоступна')
                    self.pending = dict(payload)
                    self.execution_admitted = False
                    self.latest_done = False
                    self.failed_after_hold = False
                    self.hold_observed = False
                    self.epoch = self.reader.heartbeat_safety_serial
                    self.state, self.reason = 'DRAFT', None
                    return dict(result, **(plan | {'state': 'DRAFT'}))
                return dict(result, **(self.session.prepare(payload) | {'state': 'DRAFT'}))
            except Blocked as exc:
                return dict(result, state='BLOCKED', error=str(exc))

    def _queue_hold(self):
        if self.latch_stop is not None:
            self.latch_stop()
        self.hold_observed = False
        self.hold_queue = 'PENDING'
        self.hold_deadline = self.reader.clock() + 3

    def admit_execution(self):
        """Owner-local only. The current storage-only profile cannot pass this gate."""
        with self.reader.lock:
            if not self._execution_ready(self.reader.clock()):
                raise Blocked('Виконання місії не погоджене для цього профілю')
            self.session.owner_started()
            self.execution_admitted = True

    def observe(self, message):
        with self.reader.lock:
            self.transfer.observe(message)
            if (self.state == 'RUNNING' and message.get_type() == 'MISSION_CURRENT' and
                    (message.get_srcSystem(), message.get_srcComponent()) == TARGET and
                    message.mission_state == 5 and message.seq == self.transfer.expected[-1]['seq'] and
                    (not self.transfer.plan_id or getattr(message, 'mission_id', None) == self.transfer.plan_id)):
                self.latest_done = True

    def owner_lost(self):
        with self.reader.lock:
            self.samples.clear()
            self.execution_admitted = False
            if self.pending is not None:
                self._fail('OWNER_LINK_LOST')
            elif self.state == 'RUNNING':
                self.session.owner_progress(navigation_valid=False)
                self.state, self.reason = 'HOLD', 'OWNER_LINK_LOST'
            elif self.state in ('VERIFIED', 'UPLOADED'):
                self.state, self.reason = 'FAILED', 'OWNER_LINK_LOST'

    def _fail(self, reason):
        self.transfer.fail(reason)
        self.session.state = 'ABORTED'
        self.pending = None
        self.execution_admitted = False
        self.state, self.reason = 'FAILED', reason

    def tick(self, fd, command_pending=False):
        with self.reader.lock:
            now = self.reader.clock()
            pose = self._position(now) if self.surveyed_map and self.nav.alignment_check() else None
            self._capture(now, pose)
            hb = self._hb(now)
            if self.pending is not None:
                if (command_pending or not self._upload_ready(now) or
                        self.reader.heartbeat_safety_serial != self.epoch):
                    self._fail('UPLOAD_PRECONDITION_LOST')
                    if hb and hb[1].custom_mode == AUTO:
                        self._queue_hold()
                    return
                try:
                    evidence = self._evidence()
                    route = self.session.preview(self.pending, evidence, now)
                    if compile_leg(route, self.session.config, evidence) != self.transfer.expected:
                        raise Blocked('Route changed')
                except Blocked:
                    self._fail('UPLOAD_ROUTE_EVIDENCE_LOST')
                    return
                self.transfer.tick(fd, now)
                if self.transfer.phase == 'FAILED':
                    self._fail(self.transfer.error)
                elif self.transfer.phase == 'VERIFIED':
                    self.pending = None
                    self.state = 'VERIFIED'
                elif self.transfer.phase == 'RECOVERY_BASELINE_CONFIRMED':
                    self.pending = None
                    self.session.state = 'ABORTED'
                    self.state, self.reason = 'DRAFT', 'RECOVERY_BASELINE_CONFIRMED'
                elif self.transfer.uploaded:
                    self.state = 'UPLOADED'

            if self.state == 'VERIFIED' and hb and hb[1].custom_mode == AUTO:
                if (self.execution_admitted and hb[1].base_mode & 128
                        and not command_pending and self._execution_safe(now)):
                    self.execution_admitted = False
                    self.state, self.latest_done = 'RUNNING', False
                else:
                    self.state, self.reason = 'HOLD', 'EXECUTION_NOT_ADMITTED'
                    self.execution_admitted = False
                    self.failed_after_hold = True
                    self._queue_hold()
            elif self.state == 'RUNNING':
                navigation_valid = not command_pending and self._motion_authorized(now)
                running = self.session.owner_progress(
                    navigation_valid=navigation_valid,
                    finished=self.latest_done)
                if running == 'HOLD_REQUIRED':
                    self.state = 'HOLD'
                    if not navigation_valid:
                        self.reason = 'NAVIGATION_OR_TRACTION_LOST'
            elif self.execution_admitted and (command_pending or hb is None
                                               or hb[1].custom_mode != HOLD
                                               or not self._execution_safe(now)):
                self.state, self.reason = 'HOLD', 'EXECUTION_ADMISSION_LOST'
                self.execution_admitted = False
                self.failed_after_hold = True
                self._queue_hold()
            if self.hold_queue == 'PENDING' and hb and not command_pending and now < self.hold_deadline:
                try:
                    self.send_hold(fd, TARGET)
                except OSError:
                    self.reason = 'HOLD_WRITE_FAILED'
                self.hold_request_at, self.hold_queue = now, 'SENT'
            if self.hold_queue and now >= self.hold_deadline:
                self.hold_queue, self.reason = None, 'HOLD_NOT_CONFIRMED'
                self.state = 'FAILED'
            if (self.hold_queue == 'SENT' and hb and hb[0] > self.hold_request_at and
                    hb[1].custom_mode == HOLD and
                    self._evidence(stationary_since=self.hold_request_at).stationary_at is not None):
                self.hold_observed = True
                if self.failed_after_hold:
                    self.hold_queue = None
                    self.state, self.reason = 'FAILED', 'EXECUTION_REJECTED_HOLD_OBSERVED'
                else:
                    result = self.session.owner_progress(navigation_valid=False)
                    if result in ('COMPLETED', 'ABORTED'):
                        self.hold_queue = None
                        self.state = 'COMPLETED' if result == 'COMPLETED' else 'FAILED'
                    elif self.state == 'FAILED':
                        self.hold_queue = None

    def snapshot(self):
        now = self.reader.clock()
        pose = self._position(now) if self._upload_navigation_valid(now) or self._nav_valid(now) else None
        locality = 'FRESH' if pose else 'UNAVAILABLE'
        if self.require_live_tx and not self.nav.snapshot()['tx_enabled']:
            locality = 'TX_STOPPED'
        tx = self.nav.snapshot()
        ekf = self.reader.messages.get('EKF_STATUS_REPORT')
        flags = ekf[1].flags if ekf is not None and 0 <= now - ekf[0] <= .5 else None
        optical = self._position(now) if self.surveyed_map and tx.get('alignment_verified') is True else None
        externalnav = dict(position=(dict(x_m=optical.x_m, y_m=optical.y_m, marker=optical.marker)
                                     if optical else None),
                           hold=self.navigation_hold_status() if self.navigation_hold_status else None,
                           tx=tx.get('tx_fresh') is True, hz=tx.get('tx_hz', 0), sent=tx.get('sent', 0),
                           planar_session=tx.get('nav_bench_tx') is True, reason=tx.get('reason'),
                           ekf_flags=flags, ekf_xy_valid=bool(flags is not None and flags & 16
                                                            and not flags & (128 | 1024)))
        return dict(self.session.snapshot(), state=self.state, reason=self.reason,
                    externalnav=externalnav,
                    phase=self.transfer.phase,
                    verified_readback=self.transfer.verified_readback(), hold_pending=self.hold_queue is not None,
                    hold_observed=self.hold_observed,
                    mission_upload_ready=self.state not in ('RUNNING', 'HOLD', 'FAILED')
                    and self.pending is None and not self.execution_admitted and self._upload_ready(now),
                    mission_execution_ready=self._execution_ready(now),
                    execution_coverage_confirmed=self.session.execution_coverage_confirmed,
                    drive_ready='UNKNOWN',
                    brake_status='UNKNOWN',
                    brake_command=self.brake_command(),
                    motion_authorized=self._motion_authorized(now),
                    traction_readiness=self.traction_readiness(),
                    localization=locality,
                    position=dict(x_m=pose.x_m, y_m=pose.y_m, marker=pose.marker) if pose else None)
