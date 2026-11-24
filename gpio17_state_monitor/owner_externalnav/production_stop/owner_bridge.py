"""Owner-local production latch. No transport/device creation, no motor commands.
Native ELRS authority is explicitly UNVERIFIED until a reviewed provider exists.
"""
import logging
import math
import re
import sqlite3
from .stop_journal import StopJournal
from .state import StopController, ResetConditions, Zone, Contact
from .interfaces import snapshot_payload
from .mission_resume import ResumeAcceptance, ResumeEvidence
from .rover_auto import RoverAutoAdapter
from .state_monitor import StateMonitor

UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


class OwnerProductionStop:
    def __init__(self, reader, bench, database, native=None, mission_supervisor=None, *, state_monitor=None):
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
        self.mission_supervisor = mission_supervisor
        self.state_monitor = state_monitor if state_monitor is not None else StateMonitor()
        self.resume_reset_id = None
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
        if state.stop_latch:
            self._revoke_resume()
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

    def mavlink_lost(self):
        with self.lock:
            adapter = self.mission_supervisor
            checkpoint = adapter.checkpoint if isinstance(adapter, RoverAutoAdapter) else None
            if isinstance(adapter, RoverAutoAdapter):
                adapter.link_lost()
            self._latch()
            details = {'checkpoint_revoked': checkpoint is not None}
            if checkpoint is not None:
                details['stop_id'] = checkpoint.stop_id
            self._record_state(self.core.snapshot(), 'MAVLINK_LOST', details)

    def tick(self, fd=None):
        with self.lock:
            state = self.core.tick()
            if self.pending_brake_event is not None:
                self._record_state(state, 'WEB_DISARM_BRAKE', {'request_id': self.pending_brake_event})
                self.pending_brake_event = None
            else:
                self._record_state(state)
            adapter = self.mission_supervisor
            if (fd is not None and isinstance(adapter, RoverAutoAdapter) and
                    adapter.phase in ('RESTORE_QUEUED', 'RESTORING')):
                try:
                    evidence = adapter.evidence()
                except Exception:
                    logging.getLogger(__name__).exception('Mission restore evidence unavailable')
                    evidence = None
                if (state.stop_latch or self.storage_fault or self.native is None or
                        self.brake is None or self.brake.phase != 'SENT' or
                        self.brake.request_id != adapter.reset_id or
                        self.native.blocked_at() is None or self.bench.disarm_pending() or
                        type(evidence) is not ResumeEvidence or not evidence.permits_resume(
                            self.reader.clock(), self.brake.release_sent_at)):
                    self._latch()
                    self._record_state(self.core.snapshot())
                    return
                adapter.tick(fd)
            if (isinstance(adapter, RoverAutoAdapter) and adapter.phase == 'UNKNOWN' and
                    not state.stop_latch):
                self._latch()
                self._record_state(self.core.snapshot())

    def gpio_observed(self, contact, observed_at):
        # Same acquisition as existing monitor; no second GPIO/serial reader.
        with self.lock:
            state = self.core.update(Zone.FRONT, Contact(contact), observed_at=observed_at)
            if self.storage_fault:
                self._latch()
            elif contact != self.last_contact:
                self.last_contact = contact
                details = {'observed_at': observed_at, 'reasons': list(state.reasons)}
                adapter = self.mission_supervisor
                if contact == 'PRESSED' and isinstance(adapter, RoverAutoAdapter):
                    pressed = dict(state.contacts)[Zone.FRONT] is Contact.PRESSED
                    adapter.front_pressed(observed_at if pressed else None)
                    checkpoint = adapter.checkpoint
                    if checkpoint is not None:
                        details.update(checkpoint.journal_details())
                self._record_state(state, 'FRONT_' + contact, details)
            else:
                self._record_state(state)
        if self.native is not None:
            self.native.cycle(owner_fault=self.storage_fault,
                              permit_committed=lambda: (not self.committed_latch and
                                                        (self.brake is None or self.brake.traction_ready())))
            self.tick()

    def _revoke_resume(self):
        self.resume_reset_id = None
        if isinstance(self.mission_supervisor, RoverAutoAdapter):
            self.mission_supervisor.stop_latched()

    def _latch(self):
        self._revoke_resume()
        # Existing core contract: any invalid RESET evidence latches STOP.
        self.core.reset(None)

    def _snapshot(self, *, include_state_monitor=False):
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
                      movement_allowed=False, test_only=False, drive_control_activated=False)
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
            if not self.brake.reset_link_ready(now):
                reasons.append('BRAKE_CONTROL_LINK_UNAVAILABLE')
        if self.storage_fault:reasons.append('RESET_STORAGE_FAULT')
        result['blocking_reasons'] = reasons
        result['reset_available'] = state.stop_latch and not reasons
        # Core permission alone is not physical authority; never claim ELRS permission.
        result['motion_permitted'] = False
        if isinstance(self.mission_supervisor, RoverAutoAdapter):
            result['mission_state'] = self.mission_supervisor.status()
        if include_state_monitor:
            try:
                result['state_monitor'] = self.state_monitor.snapshot(tele, result)
            except Exception:
                logging.getLogger(__name__).exception('StateMonitor unavailable')
        return result,conditions

    def rpc_operations(self):
        operations = {'STOP_STATUS': self.status}
        if self.native is not None and self.brake is not None and not self.brake.hold_only:
            operations['STOP_RESET'] = self.reset
            supervisor = self.mission_supervisor
            if supervisor is not None and (not isinstance(supervisor, RoverAutoAdapter) or supervisor.ipc is not None):
                operations['MISSION_RESUME'] = self.mission_resume
        return operations

    def status(self, actor_id, actor, request_id):
        with self.lock:
            try:
                return self._snapshot(include_state_monitor=True)[0]
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
                checkpoint = (self.mission_supervisor.checkpoint
                              if accepted and self.brake is not None and
                              isinstance(self.mission_supervisor, RoverAutoAdapter) else None)
                self.journal.record_reset(request_id, actor_id, actor, self.reader.clock(),
                                          accepted, state.stop_latch, result['blocking_reasons'], checkpoint)
                if accepted and self.brake is not None:
                    self.brake.begin_release(request_id)
                    self.resume_reset_id = request_id
                    if isinstance(self.mission_supervisor, RoverAutoAdapter):
                        self.mission_supervisor.reset_accepted(request_id)
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

    def mission_resume(self, actor_id, actor, request_id):
        if (type(actor_id) is not int or actor_id <= 0 or not isinstance(actor, str) or
                not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', actor) or
                not isinstance(request_id, str) or not UUID.fullmatch(request_id)):
            raise ValueError('Invalid authorized MISSION_RESUME identity')
        with self.lock:
            state = self.core.snapshot()
            self._record_state(state)
            reset_id = self.resume_reset_id
            result = {'request_id': request_id, 'resume_accepted': False, 'replayed': False,
                      'mission_state': 'UNKNOWN'}
            if self.storage_fault or 'MISSION_RESUME' not in self.rpc_operations():
                return dict(result, reason='RESUME_UNAVAILABLE')
            try:
                if not self.journal.claim_resume(request_id, actor_id, reset_id):
                    return dict(result, reason='REPLAY', replayed=True)
            except PermissionError:
                raise
            except (OSError, sqlite3.Error):
                self._storage_failed()
                return dict(result, reason='RESUME_STORAGE_FAULT')
            if (reset_id is None or state.stop_latch or self.committed_latch or
                    self.brake.request_id != reset_id or self.brake.phase != 'SENT' or
                    self.native.blocked_at() is None or self.bench.disarm_pending()):
                return dict(result, reason='RESET_OR_RELEASE_REQUIRED')
            try:
                evidence = self.mission_supervisor.evidence()
                if type(evidence) is not ResumeEvidence or not evidence.permits_resume(
                        self.reader.clock(), self.brake.release_sent_at):
                    return dict(result, reason='FRESH_PHYSICAL_EVIDENCE_REQUIRED')
                # Recheck freshness after evidence retrieval and before handoff.
                if self.core.snapshot().stop_latch or self.native.blocked_at() is None:
                    self.resume_reset_id = None
                    return dict(result, reason='STOP_OR_INTERLOCK_LOST')
                # Consume the RESET before handoff, including an uncertain outcome.
                self.resume_reset_id = None
                if isinstance(self.mission_supervisor, RoverAutoAdapter):
                    self.mission_supervisor.release_confirmed(reset_id)
                decision = self.mission_supervisor.resume(request_id, reset_id)
                if decision is ResumeAcceptance.ACCEPTED:
                    if isinstance(self.mission_supervisor, RoverAutoAdapter):
                        return dict(result, resume_accepted=True, reason='WAYPOINT_RESTORE_QUEUED',
                                    mission_state='RESTORE_QUEUED')
                    return dict(result, resume_accepted=True, reason='ACCEPTED')
                if decision is ResumeAcceptance.REFUSED:
                    return dict(result, reason='RESUME_SUPERVISOR_REFUSED')
            except Exception:
                self._latch()
                return dict(result, reason='RESUME_SUPERVISOR_ERROR')
            self._latch()
            return dict(result, reason='RESUME_OUTCOME_UNKNOWN')

    def record_brake_result(self, event, request_id, reason):
        self._record_state(self.core.snapshot(), event,
                           {'request_id': request_id, 'reason': reason})

    def close(self):
        if self.journal is not None:
            self.journal.close()
