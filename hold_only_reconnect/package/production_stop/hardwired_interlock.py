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
    # Acquisition time of the oldest signal, in the owner's monotonic domain.
    observed_at: float
    controller_healthy: bool
    stop_inputs_released: bool
    outputs_deenergized: bool
    k1_open: bool
    k2_open: bool
    traction_bus_safe: bool
    restart_required: bool
    wheels_stationary: bool


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

    def _blocked_feedback(self):
        feedback = self._healthy_feedback()
        if feedback is None or not all(value is True for value in (
            feedback.outputs_deenergized, feedback.k1_open, feedback.k2_open,
            feedback.traction_bus_safe, feedback.restart_required,
        )):
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
        telemetry = self.reader.snapshot()
        return ResetConditions(
            observed_at=feedback.observed_at,
            disarmed=telemetry.get('armed') == {'state': 'FRESH', 'value': 'DISARMED'},
            stationary=feedback.wheels_stationary is True,
            hold_confirmed=True,  # _blocked_feedback verifies hardware traction inhibit
            stop_transaction_idle=feedback.restart_required is True,
        )
