'use strict';
(() => {
  const button = document.getElementById('pixhawk-disarm');
  const recheck = document.getElementById('disarm-recheck');
  const notice = document.getElementById('disarm-notice');
  if (!button || button.disabled || !recheck) return;
  let active = false, requestId = null, generation = 0, timer = null;
  let latest = null, renderedAt = 0, disposed = false, pendingUntil = 0;
  const deliveryLabels = {
    QUEUED: 'Запит прийнято. Надсилання ще не підтверджене.',
    SENT: 'Команду записано в serial. Виконання цього запиту НЕ підтверджено.',
    REJECTED: 'Запит відхилено до надсилання.',
    UNKNOWN: 'Доставка або результат UNKNOWN.'
  };
  function unknown(message) {
    latest = null;
    notice.textContent = `${message} Стан Pixhawk UNKNOWN. Виконання цього запиту не підтверджено; автоматичного повтору немає.`;
  }
  function render(value) {
    if (!value || value.state !== value.delivery_state || value.command_outcome !== 'UNKNOWN' ||
        !Object.hasOwn(deliveryLabels, value.delivery_state) ||
        typeof value.observation_pending !== 'boolean' ||
        !['UNKNOWN', 'DISARMED_OBSERVED', 'ARMED_OBSERVED'].includes(value.observation_state)) {
      throw new Error('Недостовірний результат gateway.');
    }
    latest = value;
    renderedAt = performance.now();
    const prefix = deliveryLabels[value.delivery_state];
    if (value.observation_state === 'UNKNOWN') {
      notice.textContent = `${prefix} Стан Pixhawk UNKNOWN. ${value.observation_pending ? 'Очікуємо свіжу телеметрію.' : 'Перевірте телеметрію перед новим запитом.'}`;
      return;
    }
    const source = value.heartbeat_source;
    const freshness = value.heartbeat_freshness_s;
    if (value.delivery_state !== 'SENT' || typeof value.heartbeat_age_s !== 'number' ||
        typeof freshness !== 'number' || !Number.isFinite(freshness) || freshness <= 0 || freshness > 3 ||
        !Number.isFinite(value.heartbeat_age_s) || value.heartbeat_age_s < 0 || value.heartbeat_age_s >= freshness ||
        !Array.isArray(source) || source.length !== 2 || !Number.isInteger(source[0]) ||
        source[0] < 1 || source[0] > 255 || source[1] !== 1) {
      throw new Error('Немає достовірного свіжого спостереження.');
    }
    const label = value.observation_state === 'DISARMED_OBSERVED' ? 'Спостерігається DISARMED' : 'Спостерігається ARMED';
    notice.textContent = `${prefix} ${label}, вік ${value.heartbeat_age_s.toFixed(2)} с, джерело ${source.join('/')}. Це спостереження стану, а не підтвердження виконання конкретного запиту.`;
  }
  async function request(url, options = {}) {
    const started = performance.now();
    const response = await hmiFetch(url, {...options, signal: AbortSignal.timeout(2000)});
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || 'Канал недоступний.');
    if (value.observation_state !== 'UNKNOWN' && typeof value.heartbeat_age_s === 'number') {
      value.heartbeat_age_s += (performance.now() - started) / 1000;
    }
    return value;
  }
  function schedule(token) {
    clearTimeout(timer);
    if (!disposed && requestId && !document.hidden && token === generation) {
      timer = setTimeout(() => poll(token), 500);
    }
  }
  async function poll(token) {
    if (disposed || document.hidden || token !== generation || !requestId) return;
    try {
      const value = await request(`/api/pixhawk/disarm/${requestId}`);
      if (token !== generation || disposed || document.hidden) return;
      render(value);
      const pending = value.delivery_state === 'QUEUED' || value.observation_pending === true;
      if (pending && performance.now() >= pendingUntil) {
        unknown('Час очікування спостереження вичерпано.');
        recheck.hidden = false;
      } else recheck.hidden = pending;
    } catch (error) {
      if (token !== generation || disposed) return;
      unknown(error.message);
      recheck.hidden = false;
    } finally {
      schedule(token);
    }
  }
  button.addEventListener('click', async () => {
    if (active || requestId) return;
    active = true;
    button.disabled = true;
    recheck.hidden = true;
    requestId = crypto.randomUUID();
    const token = ++generation;
    pendingUntil = performance.now() + 10000;
    unknown('Надсилання запиту…');
    try {
      const value = await request('/api/pixhawk/disarm', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({request_id: requestId})
      });
      if (disposed || token !== generation) return;
      render(value);
      recheck.hidden = value.delivery_state === 'QUEUED' || value.observation_pending === true;
    } catch (error) {
      if (disposed || token !== generation) return;
      unknown(error.message);
      recheck.hidden = false;
    } finally {
      active = false;
      schedule(token); // STATUS only, even if POST reply was lost; never retry POST.
    }
  });
  recheck.addEventListener('click', () => {
    if (active) return;
    generation++;
    clearTimeout(timer);
    requestId = null;
    recheck.hidden = true;
    button.disabled = false;
    unknown('Оператор дозволив новий окремий запит.');
  });
  const freshnessTimer = setInterval(() => {
    if (!latest || latest.observation_state === 'UNKNOWN') return;
    const age = latest.heartbeat_age_s + (performance.now() - renderedAt) / 1000;
    if (!Number.isFinite(age) || age >= Math.min(latest.heartbeat_freshness_s || 3, 3)) unknown('Спостереження застаріло.');
  }, 100);
  document.addEventListener('visibilitychange', () => {
    generation++;
    clearTimeout(timer);
    unknown('Видимість сторінки змінилася.');
    if (!document.hidden) schedule(generation);
  });
  addEventListener('pagehide', () => {
    disposed = true;
    generation++;
    clearTimeout(timer);
    clearInterval(freshnessTimer);
    unknown('Сторінка закрита.');
  });
})();
