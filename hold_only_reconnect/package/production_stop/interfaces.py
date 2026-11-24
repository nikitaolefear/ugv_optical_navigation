"""Contracts for future adapters; no transport or hardware implementation."""
from typing import Optional, Protocol

from .state import Contact, ResetConditions, Snapshot, Zone


class StopStatePort(Protocol):
    """Supervisor-facing core. Each motion decision needs a fresh snapshot."""

    def update(self, zone: Zone, contact: Contact, *,
               observed_at: float = ...) -> Snapshot: ...

    def snapshot(self) -> Snapshot: ...

    def tick(self) -> Snapshot: ...

    def reset(self, conditions: ResetConditions) -> tuple[bool, Snapshot]: ...


class SafetyEvidenceProvider(Protocol):
    """Trusted supervisor evidence, never confirmations supplied by HMI.

    Return None if evidence is unavailable. observed_at must represent the
    oldest underlying observation, in the core's monotonic clock domain.
    Cached telemetry must not be re-stamped as fresh on each request.
    """

    def reset_conditions(self) -> Optional[ResetConditions]: ...


def snapshot_payload(state: Snapshot) -> dict:
    """Detached JSON-compatible HMI view; does not evaluate freshness.

    Caller must obtain state via snapshot() immediately before publishing.
    This view is diagnostic data, never a reusable motion authorization.
    """
    return {
        'stop_latch': state.stop_latch,
        'motion_permitted': state.motion_permitted,
        'contacts': {zone.value: contact.value for zone, contact in state.contacts},
        'installed_inputs': {item.zone.value: {'bcm_gpio': item.bcm_gpio}
                             for item in state.installed_inputs},
        'reasons': list(state.reasons),
    }
