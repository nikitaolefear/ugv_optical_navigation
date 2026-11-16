"""Fail-closed software motion gate driven by monotonic time.

The owner must call snapshot/tick periodically and gate every motion command.
This module neither stops hardware nor grants authority to arm the vehicle.
"""
from dataclasses import dataclass
from enum import Enum
import math
from threading import RLock
import time
from typing import Callable, Tuple


_UNSET = object()


def _finite_number(value):
    """Accept built-in numbers only; never invoke user numeric conversions."""
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError, TypeError):
        return False


class Zone(str, Enum):
    FRONT = 'FRONT'
    REAR = 'REAR'
    LEFT = 'LEFT'
    RIGHT = 'RIGHT'


class Contact(str, Enum):
    PRESSED = 'PRESSED'
    RELEASED = 'RELEASED'
    UNKNOWN = 'UNKNOWN'
    NOT_INSTALLED = 'NOT_INSTALLED'


@dataclass(frozen=True)
class BumperInput:
    """Declared wiring only; constructing this object never accesses GPIO."""
    zone: Zone
    bcm_gpio: int

    def __post_init__(self):
        object.__setattr__(self, 'zone', Zone(self.zone))
        if type(self.bcm_gpio) is not int or not 0 <= self.bcm_gpio <= 27:
            raise ValueError('bcm_gpio must be an integer from 0 to 27')


DEFAULT_INPUTS = (BumperInput(Zone.FRONT, 17),)


@dataclass(frozen=True)
class ResetConditions:
    """Trusted supervisor evidence, in the same monotonic clock domain.

    No defaults: the caller must explicitly supply every confirmation.
    The supervisor must use the oldest underlying observation timestamp.
    Physical thresholds and evidence sources require validation before use.
    """
    observed_at: float
    disarmed: bool
    stationary: bool
    hold_confirmed: bool
    stop_transaction_idle: bool


@dataclass(frozen=True)
class Snapshot:
    stop_latch: bool
    motion_permitted: bool
    contacts: Tuple[Tuple[Zone, Contact], ...]
    reasons: Tuple[str, ...]
    installed_inputs: Tuple[BumperInput, ...]


class StopController:
    def __init__(self, freshness_s: float = 0.5,
                 clock: Callable[[], float] = time.monotonic, *,
                 inputs: Tuple[BumperInput, ...] = DEFAULT_INPUTS):
        if not _finite_number(freshness_s) or freshness_s <= 0:
            raise ValueError('freshness_s must be finite and positive')
        self._timeout = freshness_s
        inputs = tuple(inputs)
        if not inputs or any(not isinstance(item, BumperInput) for item in inputs):
            raise ValueError('At least one explicitly configured BumperInput is required')
        if (len({item.zone for item in inputs}) != len(inputs) or
                len({item.bcm_gpio for item in inputs}) != len(inputs)):
            raise ValueError('Duplicate bumper zone or BCM GPIO')
        self._inputs = inputs
        self._installed = frozenset(item.zone for item in inputs)
        self._clock = clock
        self._lock = RLock()
        self._samples = {}
        self._latched = True
        self._last_now = None
        self._clock_fault = False

    def _now(self):
        try:
            now = self._clock()
        except Exception:
            now = float('nan')
        if (not _finite_number(now) or
                (self._last_now is not None and now < self._last_now)):
            self._clock_fault = True  # requires a new controller, never RESET
            self._latched = True
        if _finite_number(now):
            self._last_now = now
            return now
        return float('nan')

    def _fresh(self, stamp, now):
        return (_finite_number(stamp) and _finite_number(now) and
                0 <= now - stamp < self._timeout)

    def _snapshot(self, now):
        contacts, reasons = [], []
        if self._clock_fault:
            reasons.append('CLOCK_FAULT')
        for zone in Zone:
            if zone not in self._installed:
                contacts.append((zone, Contact.NOT_INSTALLED))
                continue
            sample = self._samples.get(zone)
            contact = Contact.UNKNOWN
            if sample is None:
                reasons.append(f'{zone.value}:MISSING')
            elif not 0 <= now - sample[1] < self._timeout:
                reasons.append(f'{zone.value}:STALE')
            else:
                contact = sample[0]
                if contact != Contact.RELEASED:
                    reasons.append(f'{zone.value}:{contact.value}')
            contacts.append((zone, contact))
        if reasons:
            self._latched = True
        return Snapshot(self._latched, not self._latched and not reasons,
                        tuple(contacts), tuple(reasons), self._inputs)

    def update(self, zone: Zone, contact: Contact, *, observed_at=_UNSET):
        """Accept acquisition time, never refresh an old sample on delivery.

        Unknown contact values and invalid/out-of-order timestamps invalidate
        that zone. Unknown/unconfigured zones latch STOP and raise
        (wiring/config error). NOT_INSTALLED is output-only, not a reading.
        Omit observed_at only for synchronous acquisition; explicit None is
        invalid, as are bools, numeric subclasses and non-finite/huge values.
        """
        with self._lock:
            now = self._now()
            self._snapshot(now)  # detect any gap before accepting recovery
            try:
                zone = Zone(zone)
            except (ValueError, TypeError):
                self._latched = True
                raise ValueError('Unknown bumper zone') from None
            if zone not in self._installed:
                self._latched = True
                raise ValueError('Bumper zone is not configured')
            try:
                contact = Contact(contact)
            except (ValueError, TypeError):
                contact = Contact.UNKNOWN
            if contact == Contact.NOT_INSTALLED:
                contact = Contact.UNKNOWN
            stamp = now if observed_at is _UNSET else observed_at
            previous = self._samples.get(zone)
            valid = (self._fresh(stamp, now) and
                     (previous is None or stamp > previous[1]))
            if not valid:
                contact, stamp = Contact.UNKNOWN, now
            self._samples[zone] = (contact, stamp)
            return self._snapshot(now)

    def snapshot(self):
        with self._lock:
            return self._snapshot(self._now())

    tick = snapshot

    def reset(self, conditions: ResetConditions):
        """Explicit request; return (accepted, current snapshot).

        RESET only clears this software latch. It sends no command and does
        not arm, change mode, resume a mission, or override another gate.
        Rejected requests latch STOP even when motion was previously permitted.
        """
        with self._lock:
            now = self._now()
            state = self._snapshot(now)
            # A rejected or interrupted RESET must revoke an existing permit.
            self._latched = True
            accepted = False
            if type(conditions) is ResetConditions:
                stamp = conditions.observed_at
                fresh = self._fresh(stamp, now)
                accepted = (not state.reasons and fresh and all(value is True for value in (
                    conditions.disarmed, conditions.stationary,
                    conditions.hold_confirmed, conditions.stop_transaction_idle)))
            if accepted:
                self._latched = False
            return accepted, self._snapshot(now)
