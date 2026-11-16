"""Opt-in passive FRONT reader. Import/construction never accesses GPIO."""
from dataclasses import dataclass
import re
import subprocess
from threading import Event, RLock, Thread, TIMEOUT_MAX
import time
from typing import Protocol

from .interfaces import StopStatePort, snapshot_payload
from .state import Contact, Zone, _finite_number


@dataclass(frozen=True)
class PinReading:
    output: str
    observed_at: float


class GpioSource(Protocol):
    kind: str

    def read(self) -> PinReading: ...


class PassivePinctrlSource:
    """Read-only get; no line request, bias configuration, set, or ownership.

    Only read() launches a command. Timeout also exists at adapter level.
    The timestamp is taken before acquisition, conservatively including delay.
    """
    kind = 'PASSIVE_GPIO'

    def __init__(self, *, clock=time.monotonic, timeout_s=0.1,
                 runner=subprocess.run):
        if not _finite_number(timeout_s) or not 0 < timeout_s <= TIMEOUT_MAX:
            raise ValueError('timeout_s must be positive and within TIMEOUT_MAX')
        self._clock, self._timeout, self._runner = clock, timeout_s, runner

    def read(self):
        observed_at = self._clock()
        result = self._runner(['/usr/bin/pinctrl', 'get', '17'],
                              capture_output=True, text=True, check=True,
                              timeout=self._timeout)
        return PinReading(result.stdout, observed_at)


class SimulatedFrontSource:
    """Explicit test simulator; never falls back to a real GPIO source."""
    kind = 'SIMULATOR'

    def __init__(self, *, high=True, clock=time.monotonic):
        self.high, self._clock = high, clock

    def read(self):
        if type(self.high) is not bool:
            raise ValueError('Simulator level must be bool')
        return PinReading('17: ip pu | ' + ('hi' if self.high else 'lo'),
                          self._clock())


_PIN17 = re.compile(r'17:\s+ip\s+pu\s*\|\s*(hi|lo)(?:\s*//[^\r\n]*)?\s*')


class FrontGpioAdapter:
    """Bounded polling with at most one outstanding source read per instance.

    Timed-out results are discarded, never delivered later. A daemon worker
    cannot be forcibly killed; while hung, subsequent polls report READ_BUSY
    without spawning more workers. A supervisor must keep ticking the core.
    """

    def __init__(self, controller: StopStatePort, source: GpioSource, *,
                 clock=time.monotonic, timeout_s=0.1, freshness_s=0.5):
        for value in (timeout_s, freshness_s):
            if not _finite_number(value) or value <= 0:
                raise ValueError('Timeout and freshness must be finite and positive')
        if timeout_s > TIMEOUT_MAX:
            raise ValueError('timeout_s exceeds TIMEOUT_MAX')
        self._controller, self._source, self._clock = controller, source, clock
        self._timeout, self._freshness = timeout_s, freshness_s
        self._lock = RLock()
        self._pending = None
        self._observed = None
        self._reason = 'NO_SAMPLE'
        self._last_now = None
        self._clock_fault = False

    def _now(self):
        try:
            now = self._clock()
        except Exception:
            now = None
        if (not _finite_number(now) or
                (self._last_now is not None and now < self._last_now)):
            self._clock_fault = True
            return None
        self._last_now = now
        return now

    def _unknown(self, reason, now):
        self._reason = reason
        self._observed = None
        return self._controller.update(Zone.FRONT, Contact.UNKNOWN, observed_at=now)

    def poll(self):
        with self._lock:
            started = self._now()
            if self._clock_fault:
                return self._unknown('CLOCK_FAULT', started)
            if self._pending is not None and not self._pending.is_set():
                return self._unknown('READ_BUSY', started)
            done, box = Event(), []

            def acquire():
                try:
                    box.append(self._source.read())
                except (TimeoutError, subprocess.TimeoutExpired):
                    box.append('TIMEOUT')
                except Exception:
                    box.append(None)
                finally:
                    done.set()

            self._pending = done
            try:
                worker = Thread(target=acquire, daemon=True, name='front-gpio-read')
                worker.start()
            except Exception:
                done.set()
                return self._unknown('READ_ERROR', started)
            try:
                completed = done.wait(self._timeout)
            except Exception:
                # Keep pending: the worker may still be running. Never accept
                # its result or spawn another reader until it has finished.
                return self._unknown('READ_WAIT_ERROR', self._now())
            if not completed:
                return self._unknown('READ_TIMEOUT', self._now())
            now = self._now()
            if self._clock_fault:
                return self._unknown('CLOCK_FAULT', now)
            reading = box[0] if box else None
            if type(reading) is str and reading == 'TIMEOUT':
                return self._unknown('READ_TIMEOUT', now)
            if type(reading) is not PinReading:
                return self._unknown('READ_ERROR', now)
            stamp = reading.observed_at
            if (not _finite_number(stamp) or not started <= stamp <= now or
                    not 0 <= now - stamp < self._freshness or
                    now - started >= self._timeout):
                return self._unknown('INVALID_OR_STALE_SAMPLE', now)
            output = reading.output.strip() if type(reading.output) is str else None
            match = (_PIN17.fullmatch(output) if output is not None and
                     '\n' not in output and '\r' not in output else None)
            if match is None:
                return self._unknown('INVALID_GPIO_MODE_OR_DATA', now)
            self._observed, self._reason = stamp, None
            contact = Contact.RELEASED if match[1] == 'hi' else Contact.PRESSED
            return self._controller.update(Zone.FRONT, contact, observed_at=stamp)

    def payload(self):
        """Fresh diagnostic JSON view; does not read GPIO or issue RESET."""
        with self._lock:
            now = self._now()
            age = (now - self._observed if now is not None and
                   self._observed is not None else None)
            if not _finite_number(age):
                age = None
            fresh = (not self._clock_fault and age is not None and
                     0 <= age < self._freshness)
            if not fresh:
                reason = ('CLOCK_FAULT' if self._clock_fault else
                          'STALE' if self._observed is not None else self._reason)
                observed = self._observed
                self._unknown(reason, now)
                self._observed = observed
            state = self._controller.snapshot()
            result = snapshot_payload(state)
            result['blocking_reasons'] = (list(state.reasons) or
                                          (['STOP_LATCH'] if state.stop_latch else []))
            # Core may have a shorter freshness limit or reject a duplicate stamp.
            fresh = fresh and dict(state.contacts)[Zone.FRONT] != Contact.UNKNOWN
            result['front_signal'] = {
                'source': self._source.kind,
                'bcm_gpio': 17,
                'contact': dict(state.contacts)[Zone.FRONT].value,
                'fresh': fresh,
                'age_s': age,
                'freshness_s': self._freshness,
                'reason': (self._reason if self._reason is not None else
                           'CORE_INPUT_INVALID_OR_STALE' if not fresh else None),
            }
            return result
