"""Proposed owner-local deadline gate for a reviewed diagnostic profile."""
from threading import Lock
import math


class Stage3Lease:
    """Single-use lease. A timeout revokes it permanently, including late renewals."""
    def __init__(self, clock, *, idle_s=.75):
        if not 0 < idle_s <= 1:
            raise ValueError('invalid idle timeout')
        self.clock = clock
        self.idle_s = idle_s
        self.lock = Lock()
        self.client = None
        self.hard_end = None
        self.idle_end = None
        self.closed = False

    def _valid(self, now):
        if self.closed or self.client is None:
            return False
        if now >= min(self.hard_end, self.idle_end):
            self.closed = True
            return False
        return True

    def open(self, client, end):
        # Caller must bind client to an authenticated peer and check all owner guards.
        with self.lock:
            now = self.clock()
            if (self.closed or self.client is not None or not client
                    or not math.isfinite(end) or not 0 < end - now <= 150):
                return False
            self.client = client
            self.hard_end = end
            self.idle_end = min(now + self.idle_s, end)
            return True

    def renew(self, client):
        with self.lock:
            now = self.clock()
            if not self._valid(now) or client != self.client:
                return False
            self.idle_end = min(now + self.idle_s, self.hard_end)
            return True

    def permits(self):
        with self.lock:
            return self._valid(self.clock())

    def expired_or_revoked(self):
        with self.lock:
            self._valid(self.clock())
            return self.closed

    def revoke(self):
        with self.lock:
            self.closed = True
