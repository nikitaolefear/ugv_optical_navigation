"""Pure software simulation. No vehicle interfaces or hardware dependencies."""
import math
import threading
import time
from collections import OrderedDict
from copy import deepcopy


class TransitionError(ValueError):
    pass


class Simulator:
    LENGTH = 10.0
    SPEED = 0.5
    REAL_RESUME_REQUIREMENTS = (
        'fresh_aruco_position', 'healthy_ekf', 'no_obstacles',
        'no_active_protections', 'zero_speed', 'validated_route',
    )

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.state = 'READY'
        self.mission = 1
        self.distance = 0.0
        self.revision = 0
        self.last = clock()
        self.events = []
        self.requests = OrderedDict()
        self.mission_observer = None
        self._active_run = None
        self._pending_outcome = None
        self._pending_ended = None
        self._log('Симулятор готовий. Реальне керування відсутнє.')

    def track_missions(self, observer):
        """Optional accounting only; no hardware or actor authorization here."""
        with self.lock:
            if self._active_run and self.mission_observer.store.db_path != observer.store.db_path:
                raise RuntimeError('Cannot switch mission stores during an active mission')
            self.mission_observer = observer

    def _flush_mission_end(self):
        if self._active_run and self._pending_outcome:
            self.mission_observer.finish(self._active_run, self._pending_outcome, ended_at=self._pending_ended)
            self._active_run = self._pending_outcome = self._pending_ended = None

    def _end_mission(self, outcome):
        if self._active_run:
            self._pending_outcome = outcome
            self._pending_ended = self.mission_observer.store.clock()
            self._flush_mission_end()

    def _log(self, message):
        self.events.append({'number': self.revision, 'message': message})
        self.events = self.events[-100:]

    def _tick(self):
        self._flush_mission_end()
        now = self.clock()
        delta = now - self.last
        self.last = now
        if not math.isfinite(delta) or delta < 0:
            self.state = 'FAULT'
            self.revision += 1
            self._log('Помилка годинника симулятора. Потрібен RESET.')
            self._end_mission('FAULT')
        elif self.state == 'MOVING':
            self.distance = min(self.LENGTH, self.distance + delta * self.SPEED)
            if self.distance >= self.LENGTH:
                self.state = 'COMPLETE'
                self.revision += 1
                self._log('Місію завершено (симуляція).')
                self._end_mission('COMPLETED')

    def _resume_precheck(self):
        """DEMO gate only. Never authorizes hardware movement.

        A future real controller must implement a separate, fail-closed PRECHECK
        using fresh evidence for every REAL_RESUME_REQUIREMENTS item on each
        resume attempt. This simulator supplies none of that evidence.
        """
        if self.state != 'PAUSED':
            raise TransitionError('RESUME дозволений лише після штатного PAUSE.')

    def _snapshot(self):
        return dict(mode='SIMULATION', state=self.state, mission=self.mission,
                    direction='FORWARD' if self.mission == 1 else 'REVERSE',
                    target='B' if self.mission == 1 else 'A',
                    target_position_m=self.LENGTH if self.mission == 1 else 0.0,
                    resume_allowed=self.state == 'PAUSED',
                    resume_precheck=dict(mode='DEMO_ONLY', real_resume_supported=False,
                                         real_requirements=list(self.REAL_RESUME_REQUIREMENTS)),
                    revision=self.revision, progress=round(self.distance / self.LENGTH * 100, 2),
                    position_m=round(self.distance if self.mission == 1 else self.LENGTH - self.distance, 3),
                    speed_m_s=(self.SPEED if self.mission == 1 else -self.SPEED) if self.state == 'MOVING' else 0,
                    heading_deg=0, movement_allowed=False, pixhawk='UNAVAILABLE',
                    vision='UNAVAILABLE', armed=None, physical_telemetry='UNAVAILABLE',
                    events=deepcopy(self.events))

    def snapshot(self):
        with self.lock:
            self._tick()
            return self._snapshot()

    def command(self, body, *, actor=None):
        with self.lock:
            self._tick()
            action = body.get('action')
            request_id = body.get('request_id')
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
                raise TransitionError('Потрібен request_id (1–100 символів).')
            if request_id in self.requests:
                if self.requests[request_id] != body:
                    raise TransitionError('request_id вже використано для іншої команди.')
                return self._snapshot()
            # Protective actions work even from an outdated browser tab.
            if action not in ('stop', 'hold', 'fault', 'estop') and (type(body.get('revision')) is not int or body['revision'] != self.revision):
                raise TransitionError('Стан змінився. Оновіть дані та повторіть дію.')
            if action == 'select':
                if self.state != 'READY' or type(body.get('mission')) is not int or body['mission'] not in (1, 2):
                    raise TransitionError('Вибір місії дозволений лише у READY: 1 або 2.')
                self.mission = body['mission']
                message = f'Обрано місію №{self.mission}.'
            elif action == 'start':
                if self.state != 'READY' or body.get('confirmed') is not True:
                    raise TransitionError('START потребує READY та явного підтвердження.')
                if self.mission_observer is not None:
                    self._active_run = self.mission_observer.start_demo(actor, self.mission)
                self.state = 'MOVING'
                message = 'START: програмний рух запущено.'
            elif action == 'pause':
                if self.state != 'MOVING':
                    raise TransitionError('PAUSE дозволений лише під час руху симулятора.')
                self.state = 'PAUSED'
                message = 'PAUSE: місію призупинено зі збереженням прогресу.'
            elif action == 'resume':
                self._resume_precheck()
                self.state = 'MOVING'
                message = 'RESUME: попередню DEMO-місію продовжено.'
            elif action == 'stop':
                # Cancellation cannot clear a stronger safety latch.
                if self.state not in ('FAULT', 'E_STOP'):
                    self.state = 'STOPPED'
                message = 'STOP: місію скасовано. Для нового запуску потрібен RESET.'
            elif action == 'hold':
                if self.state not in ('FAULT', 'E_STOP', 'STOPPED'):
                    self.state = 'HOLD'
                message = 'HOLD: безпечна зупинка симулятора. RESUME заборонено.'
            elif action == 'estop':
                self.state = 'E_STOP'
                message = 'E-STOP: аварійне блокування симулятора. Потрібен RESET.'
            elif action == 'reset':
                self.state, self.distance = 'READY', 0.0
                message = 'RESET: початкова позиція обраної місії, рух не запущено.'
            elif action == 'fault':
                if self.state != 'E_STOP':
                    self.state = 'FAULT'
                message = 'Імітація несправності. Рух симулятора зупинено.'
            else:
                raise TransitionError('Невідома команда.')
            if action in ('stop', 'hold', 'estop', 'reset', 'fault'):
                self._end_mission('RESET' if action == 'reset' else self.state)
            self.revision += 1
            self._log(message)
            self.requests[request_id] = deepcopy(body)
            if len(self.requests) > 256:
                self.requests.popitem(last=False)
            return self._snapshot()
