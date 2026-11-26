"""Single-attempt navigation HOLD on the existing Rover owner descriptor."""

HOLD_MODE = 4
AUTO_MODE = 10
TARGET = (1, 1)
CONFIRM_TIMEOUT_S = 2.0


class NavigationHold:
    def __init__(self, reader, nav_snapshot, send_hold, *, monitor_stream=False):
        self.reader, self.nav_snapshot, self.send_hold = reader, nav_snapshot, send_hold
        self.monitor_stream = monitor_stream
        self.phase = 'PREFLIGHT'
        self.reason = None
        self.aborted = False
        self.command_pending = False
        self.sent_at = self.sent_serial = None

    def _heartbeat(self, now):
        heartbeat = self.reader.messages.get('HEARTBEAT')
        if (self.reader.state != 'READING' or self.reader.source != TARGET or
                heartbeat is None or not 0 <= now - heartbeat[0] < 1.5):
            return None
        _, hb = heartbeat
        if (hb.autopilot == 3 and hb.type == 10 and hb.base_mode & 1 and
                (hb.get_srcSystem(), hb.get_srcComponent()) == TARGET):
            return heartbeat
        return None

    def _navigation_ok(self, now):
        nav = self.nav_snapshot()
        ekf = self.reader.messages.get('EKF_STATUS_REPORT')
        return (nav['camera_thread_alive'] and nav['pose_fresh'] and not nav['fault'] and
                nav['alignment_verified'] and nav['tx_enabled'] and nav['sent'] > 0 and
                ekf is not None and 0 <= now - ekf[0] <= .5 and
                ekf[1].flags & 16 and not ekf[1].flags & (128 | 1024))

    def owner_lost(self):
        with self.reader.lock:
            if self.phase in ('READY', 'ACTIVE'):
                self._abort('OWNER_LINK_LOST')
            elif self.phase == 'WAIT_HOLD':
                self.aborted = True
                self.phase, self.reason = 'UNCONFIRMED', 'OWNER_LINK_LOST'

    def _abort(self, reason):
        self.aborted = True
        self.phase, self.reason = 'HOLD_PENDING', reason

    def tick(self, fd, *, command_pending=False):
        with self.reader.lock:
            self.command_pending = command_pending
            now = self.reader.clock()
            heartbeat = self._heartbeat(now)
            hb = heartbeat[1] if heartbeat else None
            mode = hb.custom_mode if hb else None
            if self.monitor_stream:
                nav = self.nav_snapshot()
                nav_ok = (hb is not None and nav['tx_enabled'] and nav['tx_fresh']
                          and nav['pose_fresh'] and not nav['fault'])
            else:
                nav_ok = hb is not None and self._navigation_ok(now)

            if self.phase == 'WAIT_HOLD':
                if not self.aborted and (not nav_ok or command_pending):
                    self.aborted = True
                    self.reason = 'NAVIGATION_OR_COMMAND_GATE_UNAVAILABLE'
                if now - self.sent_at >= CONFIRM_TIMEOUT_S:
                    self.aborted = True
                    self.phase, self.reason = 'UNCONFIRMED', 'HOLD_HEARTBEAT_TIMEOUT'
                elif (mode == HOLD_MODE and heartbeat[0] > self.sent_at and
                        self.reader.heartbeat_serial > self.sent_serial):
                    if self.aborted or not nav_ok:
                        self.aborted = True
                        self.phase = 'HELD'
                    else:
                        self.phase, self.reason = 'READY', None
                return

            if self.monitor_stream:
                if self.phase == 'PREFLIGHT' and nav['sent']:
                    self.phase = 'ACTIVE'
                if self.phase == 'ACTIVE' and (not nav_ok or command_pending):
                    self._abort('EXTERNALNAV_STREAM_LOST')
            elif self.phase in ('READY', 'ACTIVE'):
                if not nav_ok or command_pending:
                    self._abort('NAVIGATION_OR_COMMAND_GATE_UNAVAILABLE')
                elif mode == AUTO_MODE:
                    self.phase = 'ACTIVE'
                elif self.phase == 'ACTIVE' or mode != HOLD_MODE:
                    self.aborted = True
                    self.phase, self.reason = 'BLOCKED', 'LEFT_APPROVED_MODE'
            elif self.phase == 'PREFLIGHT':
                if mode == AUTO_MODE:
                    self._abort('AUTO_WITHOUT_CONFIRMED_HOLD')
                elif nav_ok and not hb.base_mode & 128 and not command_pending:
                    self.phase = 'HOLD_PENDING'

            if self.phase == 'HOLD_PENDING' and hb is not None and not command_pending:
                self.sent_at, self.sent_serial = now, self.reader.heartbeat_serial
                try:
                    self.send_hold(fd, TARGET)
                except (OSError, ValueError):
                    self.aborted = True
                    self.phase, self.reason = 'UNCONFIRMED', 'HOLD_WRITE_FAILED'
                else:
                    self.phase = 'WAIT_HOLD'

    def snapshot(self):
        with self.reader.lock:
            now = self.reader.clock()
            hb = self._heartbeat(now)
            return dict(phase=self.phase, reason=self.reason,
                        admission_ready=bool(self.phase == 'READY' and not self.command_pending and hb and
                                             hb[1].custom_mode == HOLD_MODE and self._navigation_ok(now)),
                        hold_observed=bool(self.phase in ('READY', 'HELD') and hb and
                                           hb[1].custom_mode == HOLD_MODE))
