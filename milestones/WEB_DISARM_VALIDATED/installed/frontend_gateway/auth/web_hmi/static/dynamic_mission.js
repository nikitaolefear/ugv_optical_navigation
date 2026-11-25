'use strict';
(() => {
  const byId = id => document.getElementById(id);
  const form = byId('mission-route');
  if (!form) return;
  const mode = byId('route-type');
  const marker = byId('route-marker');
  const distanceFrom = byId('route-distance-from');
  const requestButton = byId('route-request');
  const previewButton = byId('route-preview');
  const pilotButton = byId('route-pilot');
  const result = byId('route-result');
  const field = byId('route-state');
  const start = byId('route-start');
  const roundTrip = byId('route-return');
  let approvedInput = null;
  let busy = false;

  const updateInputs = () => {
    byId('route-marker-fields').hidden = mode.value !== 'marker';
    byId('route-distance-fields').hidden = mode.value !== 'distance';
    byId('route-direction-fields').hidden = distanceFrom.value !== 'current';
    byId('route-return-field').hidden = start.value !== 'home';
    roundTrip.disabled = start.value !== 'home' || mode.value !== 'marker';
    approvedInput = null;
    requestButton.disabled = true;
    result.textContent = '';
  };
  form.addEventListener('input', updateInputs);
  mode.addEventListener('change', updateInputs);
  distanceFrom.addEventListener('change', updateInputs);
  start.addEventListener('change', updateInputs);

  function inputs() {
    const body = {start: byId('route-start').value, target_type: mode.value};
    if (mode.value === 'marker') {
      if (!marker.value) throw new Error('Немає вибраної підтвердженої мітки');
      body.marker = Number(marker.value);
      if (!roundTrip.disabled && roundTrip.checked) body.round_trip = true;
    } else {
      const distance = byId('route-distance').valueAsNumber;
      if (!Number.isFinite(distance) || distance < 0) throw new Error('Вкажіть відстань');
      body.distance_m = distance;
      body.distance_from = distanceFrom.value;
      if (distanceFrom.value === 'current') body.direction = byId('route-direction').value;
    }
    return body;
  }

  async function submit(create) {
    if (busy) return;
    busy = previewButton.disabled = requestButton.disabled = true;
    try {
      const body = JSON.stringify(inputs());
      if (create && body !== approvedInput) throw new Error('Потрібен новий попередній перегляд');
      approvedInput = null;
      const response = await hmiFetch(`/api/pixhawk/mission/${create ? 'request' : 'preview'}`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body,
        signal: AbortSignal.timeout(2000)
      });
      const value = await response.json();
      if (!response.ok) throw new Error(value.error || 'BLOCKED');
      approvedInput = !create && !value.blocker ? body : null;
      result.textContent = `${value.state} · ${value.start_m.toFixed(2)} → ${value.target_m.toFixed(2)} м · ${value.reverse ? 'назад' : 'вперед'}${value.round_trip ? ' → HOME' : ''}${value.blocker ? ` · ${value.blocker}` : ''}`;
    } catch (error) {
      result.textContent = error.message;
    } finally {
      busy = previewButton.disabled = false;
      requestButton.disabled = !approvedInput;
    }
  }
  form.addEventListener('submit', event => { event.preventDefault(); submit(false); });
  requestButton.addEventListener('click', () => submit(true));
  pilotButton.addEventListener('click', () => {
    if (busy || !Array.from(marker.options).some(option => option.value === '23' && !option.disabled)) return;
    start.value = 'home';
    mode.value = 'marker';
    marker.value = '23';
    roundTrip.checked = true;
    updateInputs();
    submit(false);
  });

  async function poll() {
    try {
      const response = await hmiFetch('/api/pixhawk/mission/status', {signal: AbortSignal.timeout(2000)});
      if (!response.ok) throw new Error();
      const status = await response.json();
      if (!marker.options.length) {
        if (Array.isArray(status.marker_catalog)) {
          const labels = {PLANNED: 'заплановано', UNCONFIRMED: 'позиція не підтверджена', CONFIRMED: 'позиція підтверджена'};
          marker.add(new Option('Виберіть мітку', ''));
          for (const row of status.marker_catalog) {
            const option = new Option(`ID${row.id} · ${row.nominal_m} м · ${labels[row.state]}`, String(row.id));
            option.disabled = row.state !== 'CONFIRMED';
            marker.add(option);
          }
        } else {
          for (const id of status.marker_ids || []) marker.add(new Option(String(id), String(id)));
        }
      }
      const details = [status.state];
      if (status.traction_readiness && status.traction_readiness.ready !== true) {
        const messages = {
          BRAKE_HOLD_ONLY: 'Тяга заблокована профілем HOLD-only',
          BRAKE_RELEASE_INHIBITED: 'Тяга заблокована inhibit',
          NATIVE_ELRS_INTERLOCK_UNVERIFIED: 'Незалежний міжблокувальний захист тяги не підтверджено',
          INDEPENDENT_BRAKE_FEEDBACK_UNAVAILABLE: 'Фізичний стан гальм не підтверджено',
          STOP_LATCH: 'STOP_LATCH активний',
        };
        details.push(messages[status.traction_readiness.reason] || 'Допуск тяги не підтверджено');
      }
      const pilotAvailable = (status.marker_ids || []).includes(23);
      pilotButton.disabled = !pilotAvailable || !['DRAFT', 'COMPLETED'].includes(status.state) ||
                             !status.position || status.hold_pending;
      pilotButton.textContent = status.state === 'COMPLETED'
        ? 'Новий перегляд HOME → ID23 → HOME'
        : 'Переглянути HOME → ID23 → HOME';
      if (Number.isInteger(status.completed_passes)) details.push(`проїздів за сеанс: ${status.completed_passes}`);
      if (status.position) {
        const p = status.position;
        details.push(`X ${p.x_m.toFixed(2)} м · Y ${p.y_m.toFixed(2)} м · ID${p.marker}`);
      } else {
        details.push(status.localization === 'TX_STOPPED' ? 'ExternalNav TX припинено' : 'Позиція не підтверджена');
      }
      if (status.hold_pending) details.push('очікуємо підтвердження HOLD і зупинки');
      if (status.hold_observed) details.push('HOLD та оптична зупинка спостерігалися');
      if (status.stop) details.push(`остання зупинка ${status.stop.route_m.toFixed(2)} м`);
      if (status.reason) details.push(status.reason);
      field.textContent = details.join(' · ');
    } catch (_) {
      field.textContent = 'UNKNOWN';
      pilotButton.disabled = true;
    }
    setTimeout(poll, 1000);
  }
  poll();
})();
