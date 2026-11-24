"""Bounded, read-only mission download on the serial owner's MAVLink stream."""
from itertools import cycle
import os
import struct

from pymavlink.dialects.v20 import ardupilotmega as mav

from .mission_checkpoint import ROUTE_MAX_AGE, RouteSnapshot
from .state import _finite_number

TARGET = (1, 1)
MISSION = mav.MAV_MISSION_TYPE_MISSION
REPLY_TIMEOUT = .75
DOWNLOAD_TIMEOUT = 10.0
REFRESH_INTERVAL = .35
RETRY_INTERVAL = 1.0
LINK_MAX_AGE = 1.5
MISSION_COMPONENTS = tuple(sorted(
    value for name, value in vars(mav).items() if name.startswith('MAV_COMP_ID_USER')
))


class OwnerMissionReader:
    """Called by the owner receive observer and its serial tick.

    The owner supplies the encoder, clock, lock and open descriptor. An
    in-progress, preempted or failed transfer has no usable route snapshot.
    """

    def __init__(self, reader, encoder, write=os.write):
        self.reader, self.encoder, self.write = reader, encoder, write
        self.recipient = encoder.srcSystem, encoder.srcComponent
        self.components = cycle((encoder.srcComponent, *(
            c for c in MISSION_COMPONENTS if c != encoder.srcComponent
        )))
        self._clear_link()

    def link_lost(self):
        with self.reader.lock:
            self._clear_link()

    def _clear_link(self):
        self.plan_total = self.plan_id = None
        self._finish(None, 0.0)

    def _finish(self, route, next_read):
        self.route, self.next_read = route, next_read
        self.started_at = self.reply_deadline = self.expected = None
        self.count = self.count_id = self.last_item_at = None
        self.items = []

    def _abort(self, now):
        self._finish(None, now + RETRY_INTERVAL)

    def _linked(self, now):
        if self.reader.state != 'READING' or self.reader.source != TARGET or not _finite_number(now):
            return False
        heartbeat = self.reader.messages.get('HEARTBEAT')
        if heartbeat is None:
            return False
        observed_at, message = heartbeat
        return (_finite_number(observed_at) and 0 <= now - observed_at < LINK_MAX_AGE and
                (message.get_srcSystem(), message.get_srcComponent()) == TARGET and
                message.autopilot == mav.MAV_AUTOPILOT_ARDUPILOTMEGA and
                message.type == mav.MAV_TYPE_GROUND_ROVER)

    def snapshot(self):
        with self.reader.lock:
            now = self.reader.clock()
            if not self._linked(now):
                self._clear_link()
            elif self.route is not None and not 0 <= now - self.route.observed_at < ROUTE_MAX_AGE:
                self.route = None
            return self.route

    def _expired(self, now):
        return (now < self.started_at or now - self.started_at >= DOWNLOAD_TIMEOUT or
                (self.reply_deadline is not None and now >= self.reply_deadline))

    def _send(self, fd, message, now):
        try:
            original = self.encoder.srcComponent
            try:
                self.encoder.srcComponent = self.recipient[1]
                packet = message.pack(self.encoder)
            finally:
                self.encoder.srcComponent = original
            self.encoder.seq = (self.encoder.seq + 1) % 256
            if self.write(fd, packet) != len(packet):
                raise OSError('Partial mission-read frame write')
        except OSError:
            self._abort(now)
            return False
        return True

    def tick(self, fd, *, command_pending=False):
        with self.reader.lock:
            now = self.reader.clock()
            if not self._linked(now):
                self._clear_link()
                return
            if command_pending:
                if self.started_at is not None:
                    self._abort(now)
                return
            if self.started_at is None:
                if now >= self.next_read:
                    self._finish(None, now)
                    self.started_at = now
                    # Replies echo the request's sender as their recipient.
                    # A new list download gets its own address on the same encoder.
                    self.recipient = self.encoder.srcSystem, next(self.components)
                    if self._send(fd, mav.MAVLink_mission_request_list_message(*TARGET, MISSION), now):
                        self.expected, self.reply_deadline = 'MISSION_COUNT', now + REPLY_TIMEOUT
                return
            if self._expired(now):
                self._abort(now)
                return
            if self.expected is not None:
                return
            if len(self.items) < self.count:
                request = mav.MAVLink_mission_request_int_message(*TARGET, len(self.items), MISSION)
                if self._send(fd, request, now):
                    self.expected, self.reply_deadline = 'MISSION_ITEM_INT', now + REPLY_TIMEOUT
                return
            route = RouteSnapshot(self.last_item_at, tuple(self.items), started_at=self.started_at,
                                  opaque_id=self.count_id or self.plan_id)
            try:
                route.fingerprint()
            except (ValueError, TypeError, AttributeError, OverflowError, struct.error):
                self._abort(now)
                return
            # ACK completes the download protocol. It carries no mission data.
            ack = mav.MAVLink_mission_ack_message(*TARGET, mav.MAV_MISSION_ACCEPTED, MISSION)
            if self._send(fd, ack, now):
                self._finish(route, now + REFRESH_INTERVAL)

    def _observe_current(self, message, now):
        # ArduPilot's MISSION_CURRENT.total excludes HOME. Older dialects can
        # omit the total and the opaque plan ID.
        total = message.total
        if total == 65535 or message.mission_state == mav.MISSION_STATE_NO_MISSION:
            self.plan_total = self.plan_id = None
            self._abort(now)
            return
        total = total if 0 < total < 65535 else None
        plan_id = getattr(message, 'mission_id', 0) or None
        identity_changed = ((self.plan_total is not None and total is not None and
                             self.plan_total != total) or
                            (self.plan_id is not None and plan_id is not None and
                             self.plan_id != plan_id))
        self.plan_total = total if total is not None else self.plan_total
        self.plan_id = plan_id if plan_id is not None else self.plan_id
        count = len(self.route.items) if self.route is not None else self.count
        route_id = self.route.opaque_id if self.route is not None else self.count_id
        if (identity_changed or (count is not None and total is not None and count != total + 1) or
                (route_id is not None and plan_id is not None and route_id != plan_id)):
            self._abort(now)

    def observe(self, message):
        kind = message.get_type()
        if kind not in ('HEARTBEAT', 'MISSION_CURRENT', 'MISSION_COUNT', 'MISSION_ITEM_INT', 'MISSION_ACK'):
            return
        with self.reader.lock:
            now = self.reader.clock()
            if not self._linked(now):
                self._clear_link()
                return
            if kind == 'HEARTBEAT' or (message.get_srcSystem(), message.get_srcComponent()) != TARGET:
                return
            if kind == 'MISSION_CURRENT':
                self._observe_current(message, now)
                return
            if (self.started_at is None or
                    (message.target_system, message.target_component) != self.recipient or
                    message.mission_type != MISSION):
                return
            if self._expired(now) or kind == 'MISSION_ACK':
                self._abort(now)
                return
            if kind != self.expected:
                return
            if kind == 'MISSION_COUNT':
                self.count = message.count
                self.count_id = getattr(message, 'opaque_id', 0) or None
                if (self.count < 2 or
                        (self.plan_total is not None and self.count != self.plan_total + 1) or
                        (self.plan_id is not None and self.count_id is not None and
                         self.plan_id != self.count_id)):
                    self._abort(now)
                    return
            else:
                if message.seq != len(self.items):
                    return
                self.items.append(message)
                self.last_item_at = now
            self.expected = self.reply_deadline = None
