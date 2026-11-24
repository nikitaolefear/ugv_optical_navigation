"""Offline contract for the mission supervisor. The owner supplies no drive adapter."""
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .state import _finite_number


@dataclass(frozen=True)
class PhysicalConfirmation:
    confirmed: bool
    observed_at: float

    def fresh_after(self, now, boundary):
        return (self.confirmed is True and _finite_number(self.observed_at) and
                _finite_number(now) and _finite_number(boundary) and
                self.observed_at > boundary and 0 <= now - self.observed_at < .5)


@dataclass(frozen=True)
class ResumeEvidence:
    left_brake_released: PhysicalConfirmation
    right_brake_released: PhysicalConfirmation
    hardware_motion_ready: PhysicalConfirmation
    mission_preconditions_met: PhysicalConfirmation

    def permits_resume(self, now, release_sent_at):
        return all(type(signal) is PhysicalConfirmation and
                   signal.fresh_after(now, release_sent_at) for signal in (
                       self.left_brake_released, self.right_brake_released,
                       self.hardware_motion_ready, self.mission_preconditions_met,
                   ))


class ResumeAcceptance(str, Enum):
    ACCEPTED = 'ACCEPTED'
    REFUSED = 'REFUSED'
    UNKNOWN = 'UNKNOWN'


class MissionSupervisor(Protocol):
    def evidence(self) -> ResumeEvidence | None:
        """Return independently acquired feedback, without blocking on I/O."""

    def resume(self, request_id: str, reset_id: str) -> ResumeAcceptance:
        """Return command acceptance only. The independent STOP chain stays authoritative."""


class MissionSupervisorAdapter:
    """One bounded non-serial exchange per call, through an injected transport.

    The production transport must authenticate its peer, honor the overall
    timeout and never retry a request. Physical evidence is supplied separately.
    """

    def __init__(self, exchange, read_evidence, clock, *, timeout=.15):
        if not _finite_number(timeout) or not 0 < timeout <= .15:
            raise ValueError('Invalid mission exchange timeout')
        self.exchange = exchange
        self.read_evidence = read_evidence
        self.clock = clock
        self.timeout = timeout

    def evidence(self):
        return self.read_evidence()

    def _query(self, op, request_id, reset_id):
        try:
            response = self.exchange(op, request_id, reset_id, timeout=self.timeout)
        except OSError:
            return None
        if (type(response) is not dict or response.get('request_id') != request_id or
                response.get('reset_id') != reset_id):
            return None
        return response

    def resume(self, request_id, reset_id):
        response = self._query('MISSION_RESUME', request_id, reset_id)
        decision = response.get('acceptance') if response is not None else None
        if decision == 'ACCEPTED':
            return ResumeAcceptance.ACCEPTED
        if decision == 'REFUSED':
            return ResumeAcceptance.REFUSED
        return ResumeAcceptance.UNKNOWN

    def mission_state(self, request_id, reset_id):
        # This read-only query is separate from command acceptance.
        response = self._query('MISSION_STATUS', request_id, reset_id)
        if response is None:
            return 'UNKNOWN'
        now, observed_at = self.clock(), response.get('observed_at')
        state = response.get('mission_state')
        if (_finite_number(now) and _finite_number(observed_at) and
                0 <= now - observed_at < .5 and state in ('RUNNING', 'PAUSED', 'STOPPED')):
            return state
        return 'UNKNOWN'
