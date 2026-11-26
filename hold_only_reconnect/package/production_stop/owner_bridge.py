"""Owner-local production latch. No transport/device creation, no motor commands.
Native ELRS authority is explicitly UNVERIFIED until a reviewed provider exists.
"""
import logging
import math
import re
import sqlite3
from functools import partial
from disarm_control.engine import Rejected
from .stop_journal import StopJournal
from .state import StopController, ResetConditions, Zone, Contact
from .interfaces import snapshot_payload

UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


class OwnerProductionStop:
    def __init__(self, reader, bench, database, native=None):
        self.reader, self.bench = reader, bench
        self.core = StopController(clock=reader.clock)
        self.native = native  # Never built from HTTP/body/config booleans.
        if native is not None:
            native.bind_stop_controller(self.core)
        self.lock = reader.lock
        self.storage_fault = False
        self.journal = None
        self.committed_latch = True
        self.last_contact = None
        self.pending_brake_event = None
        self.brake = None
        try:
            self.journal = StopJournal(database)
        except (OSError, ValueError, sqlite3.Error):
            self._storage_failed()

    def _storage_failed(self):
        self.storage_fault = True
        self.committed_latch = True
        self._latch()
        logging.getLogger(__name__).exception('STOP journal unavailable; STOP remains latched')

    def _record_state(self, state, event='STOP_LATCHED', details=None):
        if self.storage_fault or (details is None and state.stop_latch == self.committed_latch):
            return
        try:
            self.journal.record(event, state.stop_latch,
                                details if details is not None else {'reasons': list(state.reasons)})
            self.committed_latch = state.stop_latch
        except (OSError, sqlite3.Error):
            self._storage_failed()

    def latch_for_brake(self, request_id=None):
        with self.lock:
            self._latch()
            if request_id is not None:
                self.pending_brake_event = request_id

    def tick(self):
        with self.lock:
            state = self.core.tick()
            if self.pending_brake_event is not None:
                self._record_state(state, 'WEB_DISARM_BRAKE', {'request_id': self.pending_brake_event})
                self.pending_brake_event = None
            else:
                self._record_state(state)

    def gpio_observed(self, contact, observed_at):
        # Same acquisition as existing monitor; no second GPIO/serial reader.
        with self.lock:
            state = self.core.update(Zone.FRONT, Contact(contact), observed_at=observed_at)
            if self.storage_fault:
                self._latch()
            elif contact != self.last_contact:
                self.last_contact = contact
                self._record_state(state, 'FRONT_' + contact,
                                   {'observed_at': observed_at, 'reasons': list(state.reasons)})
            else:
                self._record_state(state)
        if self.native is not None:
            self.native.cycle(owner_fault=self.storage_fault,
                              permit_committed=lambda: (not self.committed_latch and
                                                        (self.brake is None or self.brake.traction_ready())))
            self.tick()

    def _latch(self):
        # Existing core contract: any invalid RESET evidence latches STOP.
        self.core.reset(None)

    def traction_readiness(self):
        if self.brake is None:
            return dict(ready=False, reason='BRAKE_CONTROL_UNAVAILABLE')
        if self.brake.hold_only:
            return dict(ready=False, reason='BRAKE_HOLD_ONLY')
        if self.brake.release_inhibited():
            return dict(ready=False, reason='BRAKE_RELEASE_INHIBITED')
        if self.native is None or self.native.enforces_elrs_stop_latch() is not True:
            return dict(ready=False, reason='NATIVE_ELRS_INTERLOCK_UNVERIFIED')
        if self.storage_fault or self.committed_latch or self.core.snapshot().stop_latch:
            return dict(ready=False, reason='STOP_LATCH')
        if not self.brake.traction_ready():
            return dict(ready=False, reason='INDEPENDENT_BRAKE_FEEDBACK_UNAVAILABLE')
        return dict(ready=True, reason=None)

    def _snapshot(self):
        self._record_state(self.core.snapshot())
        state = self.core.snapshot()
        result = snapshot_payload(state)
        native_ok = self.native is not None and self.native.enforces_elrs_stop_latch() is True
        now = self.reader.clock()
        blocked_at = self.native.blocked_at() if native_ok else None
        blocked = (type(blocked_at) in (int, float) and math.isfinite(blocked_at) and
                   0 <= now - blocked_at < .5)
        result.update(native_elrs_interlock='VERIFIED' if native_ok else 'UNVERIFIED',
                      physical_drive_interlocked=blocked, reset_available=False,
                      movement_allowed=False, test_only=False, drive_control_activated=False,
                      traction_readiness=self.traction_readiness())
        reasons = list(state.reasons)
        if not native_ok:
            reasons.append('NATIVE_ELRS_INTERLOCK_UNVERIFIED')
        if not blocked:
            reasons.append('PHYSICAL_TRACTION_ISOLATION_UNCONFIRMED')
        conditions = self.native.reset_conditions() if blocked else None
        if type(conditions) is not ResetConditions:
            reasons.append('TRUSTED_STATIONARY_HOLD_EVIDENCE_UNAVAILABLE')
        else:
            if (type(conditions.observed_at) not in (int,float) or not math.isfinite(conditions.observed_at)
                    or not 0 <= now-conditions.observed_at < .5):reasons.append('RESET_EVIDENCE_STALE')
            for name in ('disarmed','stationary','hold_confirmed','stop_transaction_idle'):
                if getattr(conditions,name) is not True:reasons.append('RESET_'+name.upper()+'_REQUIRED')
        tele = self.reader.snapshot()
        if tele.get('armed') != {'state':'FRESH','value':'DISARMED'}:
            reasons.append('FRESH_DISARMED_REQUIRED')
        if self.bench.disarm_pending():reasons.append('DISARM_TRANSACTION_NOT_IDLE')
        if self.brake is not None:
            result.update(brake_release=self.brake.phase, brake_fault=self.brake.fault_reason)
            blocker = self.brake.reset_blocker(now)
            if blocker:
                reasons.append(blocker)
        if self.storage_fault:reasons.append('RESET_STORAGE_FAULT')
        result['blocking_reasons'] = reasons
        result['reset_available'] = state.stop_latch and not reasons
        # Core permission alone is not physical authority; never claim ELRS permission.
        result['motion_permitted'] = False
        return result,conditions

    def status(self, actor_id, actor, request_id):
        with self.lock:
            try:
                return self._snapshot()[0]
            except BaseException:
                self._latch()
                raise

    def reset(self, actor_id, actor, request_id):
        if type(actor_id) is not int or actor_id<=0 or not isinstance(actor,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',actor) or not isinstance(request_id,str) or not UUID.fullmatch(request_id):
            raise ValueError('Invalid authorized RESET identity')
        with self.lock:
            if self.storage_fault:
                return dict(self._snapshot()[0], reset_accepted=False, replayed=False, request_id=request_id)
            try:
                prior = self.journal.previous_actor(request_id)
            except sqlite3.Error:
                self._storage_failed()
                return dict(self._snapshot()[0], reset_accepted=False, replayed=False, request_id=request_id)
            if prior:
                if prior[0]!=actor_id:raise PermissionError('RESET request belongs to another actor')
                result=self._snapshot()[0]
                return dict(result,reset_accepted=False,replayed=True,request_id=request_id)
            result,conditions=self._snapshot()
            accepted=False
            try:
                if result['reset_available']:
                    accepted,_=self.core.reset(conditions)
                else:
                    self._latch()
                state = self.core.snapshot()
                accepted = accepted and not state.stop_latch
                self.journal.record_reset(request_id, actor_id, actor, self.reader.clock(),
                                          accepted, state.stop_latch, result['blocking_reasons'])
                if accepted and self.brake is not None:
                    self.brake.begin_release(request_id)
                self.committed_latch = state.stop_latch
            except (OSError, sqlite3.Error):
                self._storage_failed()
                accepted = False
            except BaseException:
                self.committed_latch = True
                self._latch()
                raise
            result=self._snapshot()[0]
            return dict(result,reset_accepted=accepted,replayed=False,request_id=request_id)

    def record_brake_result(self, event, request_id, reason):
        self._record_state(self.core.snapshot(), event,
                           {'request_id': request_id, 'reason': reason})

    def web_brake(self, operation, actor_id, actor, request_id):
        if (type(actor_id) is not int or actor_id <= 0 or type(actor) is not str or
                not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', actor) or
                type(request_id) is not str or not UUID.fullmatch(request_id)):
            raise Rejected('INVALID_REQUEST')
        reply = partial(dict, id=request_id, actor_id=actor_id)
        with self.lock:
            if operation == 'BRAKE_APPLY':
                self._latch()
                self._record_state(self.core.snapshot(), 'WEB_BRAKE_APPLY_REQUESTED',
                                   {'request_id': request_id, 'actor_id': actor_id, 'actor': actor})
                if self.storage_fault:
                    return reply(state='UNKNOWN', reason='BRAKE_STORAGE_FAULT')
                if self.brake is None:
                    return reply(state='UNKNOWN', reason='BRAKE_CONTROL_UNAVAILABLE')
                return reply(state='REQUESTED')
            if self.brake is None:
                return reply(state='BLOCKED', reason='BRAKE_CONTROL_UNAVAILABLE')
            if self.brake.hold_only:
                return reply(state='BLOCKED', reason='BRAKE_HOLD_ONLY')
            if operation == 'BRAKE_RESET':
                result = self.reset(actor_id, actor, request_id)
                return reply(state='REQUESTED' if result['reset_accepted'] else 'BLOCKED')
            if operation != 'BRAKE_RELEASE':
                raise Rejected('INVALID_REQUEST')
            blocker = self.brake.web_release_blocker(self.reader.clock())
            if blocker:
                return reply(state='BLOCKED', reason=blocker)
            try:
                if not self.journal.record_web_release(request_id, actor_id, actor):
                    return reply(state='BLOCKED', reason='BRAKE_RELEASE_ALREADY_REQUESTED')
            except (OSError, sqlite3.Error):
                self._storage_failed()
                return reply(state='UNKNOWN', reason='BRAKE_STORAGE_FAULT')
            self.brake.accept_web_release(request_id)
            return reply(state='REQUESTED')

    def close(self):
        if self.journal is not None:
            self.journal.close()
