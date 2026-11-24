"""Adapter for reviewed, isolated I/O on an independent traction interlock.

This module defines the signal contract. It cannot select a pin, energize
a contactor, release a brake, or reset the safety controller.
"""
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from .state import ResetConditions, _finite_number


@dataclass(frozen=True)
class InterlockFeedback:
    # Acquisition time of the controller/interlock status in the owner's clock.
    observed_at: float
    controller_healthy: bool
    stop_inputs_released: bool
    outputs_deenergized: bool
    k1_open: bool
    k2_open: bool
    traction_bus_safe: bool
    restart_required: bool
    wheels_stationary: bool | None
    traction_observed_at: float | None = None
    wheels_observed_at: float | None = None
    left_brake_hold_confirmed: bool | None = None
    left_brake_observed_at: float | None = None
    right_brake_hold_confirmed: bool | None = None
    right_brake_observed_at: float | None = None


class InterlockIO(Protocol):
    def exchange(self, *, pulse_watchdog: bool) -> InterlockFeedback:
        """Independent I/O driver with bounded read/write.

        True allows at most one edge of the hardware permissive heartbeat.
        False stops the pulses. A separate hardwired, monitored manual start
        still controls the contactors. Feedback must be a new acquisition,
        never a command echo or a cached frame with a new timestamp.
        """


class HardwiredInterlock:
    def __init__(self, io: InterlockIO, reader):
        self.io = io
        self.reader = reader
        self.core = None
        self._feedback = None
        self._last_sample_at = None
        self._lock = Lock()

    def bind_stop_controller(self, core):
        if self.core is not None:
            raise ValueError('Interlock already bound')
        self.core = core

    def _healthy_feedback(self):
        now = self.reader.clock()
        with self._lock:
            feedback = self._feedback
        if (feedback is None or not _finite_number(now) or
                not 0 <= now - feedback.observed_at < .5 or
                feedback.controller_healthy is not True):
            return None
        return feedback

    @staticmethod
    def _fresh_signal(now, observed_at):
        return (_finite_number(now) and _finite_number(observed_at) and
                0 <= now - observed_at < .5)

    def _traction_isolated(self, feedback, now):
        return self._fresh_signal(now, feedback.traction_observed_at) and all(
            value is True for value in (
                feedback.outputs_deenergized, feedback.k1_open, feedback.k2_open,
                feedback.traction_bus_safe,
            )
        )

    def _blocked_feedback(self):
        feedback = self._healthy_feedback()
        if (feedback is None or feedback.restart_required is not True or
                not self._traction_isolated(feedback, self.reader.clock())):
            return None
        return feedback

    def cycle(self, *, owner_fault, permit_committed):
        """Called by the existing GPIO monitor. No serial/HTTP thread I/O."""
        prior = self._healthy_feedback()
        pulse = (not owner_fault and permit_committed() and not self.core.snapshot().stop_latch and
                 prior is not None and prior.stop_inputs_released is True)
        try:
            feedback = self.io.exchange(pulse_watchdog=pulse)
            now = self.reader.clock()
            if (type(feedback) is not InterlockFeedback or not _finite_number(feedback.observed_at) or
                    not _finite_number(now) or not 0 <= now - feedback.observed_at < .5 or
                    (self._last_sample_at is not None and feedback.observed_at <= self._last_sample_at)):
                raise ValueError('Invalid interlock feedback')
            consistent = (
                type(feedback.outputs_deenergized) is bool and
                type(feedback.k1_open) is bool and type(feedback.k2_open) is bool and
                (feedback.outputs_deenergized == (feedback.k1_open and feedback.k2_open)) and
                feedback.k1_open == feedback.k2_open
            )
        except Exception:
            feedback = None
            consistent = False
        with self._lock:
            self._feedback = feedback if consistent else None
            if feedback is not None:
                self._last_sample_at = feedback.observed_at
        if (not consistent or feedback.controller_healthy is not True or
                feedback.stop_inputs_released is not True or owner_fault):
            # No new permissive pulses after a fault. The independent
            # controller owns the watchdog deadline and drops its outputs.
            self.core.reset(None)

    def enforces_elrs_stop_latch(self):
        # Instantaneous feedback here is meaningful only after commissioning
        # the actual hardwired I/O and validating the complete safety circuit.
        return self._healthy_feedback() is not None

    def blocked_at(self):
        feedback = self._blocked_feedback()
        return feedback.observed_at if feedback is not None else None

    def arm_gate_verified_at(self):
        feedback = self._blocked_feedback()
        return (feedback.observed_at if feedback is not None and
                feedback.stop_inputs_released is True else None)

    def reset_conditions(self):
        feedback = self._blocked_feedback()
        if feedback is None or feedback.stop_inputs_released is not True:
            return None
        now = self.reader.clock()
        physical_times = (
            feedback.wheels_observed_at, feedback.left_brake_observed_at,
            feedback.right_brake_observed_at,
        )
        if not all(self._fresh_signal(now, at) for at in physical_times):
            return None
        telemetry = self.reader.snapshot()
        return ResetConditions(
            observed_at=min(feedback.observed_at, feedback.traction_observed_at, *physical_times),
            disarmed=telemetry.get('armed') == {'state': 'FRESH', 'value': 'DISARMED'},
            stationary=feedback.wheels_stationary is True,
            hold_confirmed=(feedback.left_brake_hold_confirmed is True and
                            feedback.right_brake_hold_confirmed is True),
            stop_transaction_idle=feedback.restart_required is True,
        )
