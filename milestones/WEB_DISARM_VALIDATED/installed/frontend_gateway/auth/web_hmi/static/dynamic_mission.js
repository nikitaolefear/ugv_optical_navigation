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
  const routeItems = byId('route-items');
  const readiness = byId('route-readiness');
  const start = byId('route-start');
  const roundTrip = byId('route-return');
  let approvedInput = null;
  let busy = false;
  const pilotRoute = 'HOME → ID23 → ID24 → ID25 → ID24 → ID23 → HOME';

  const updateInputs = () => {
    byId('route-marker-fields').hidden = mode.value !== 'marker';
    byId('route-distance-fields').hidden = mode.value !== 'distance';
    byId('route-direction-fields').hidden = distanceFrom.value !== 'current';
    byId('route-return-field').hidden = start.value !== 'home';
    roundTrip.disabled = start.value !== 'home' || mode.value !== 'marker';
    approvedInput = null;
    requestButton.disabled = true;
    result.textContent = '';
    showRouteItems();
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
      if (!roundTrip.disabled && roundTrip.checked) {
        body.round_trip = true;
        if (body.marker === 25) body.via_markers = [23, 24];
      }
    } else {
      const distance = byId('route-distance').valueAsNumber;
      if (!Number.isFinite(distance) || distance < 0) throw new Error('Вкажіть відстань');
      body.distance_m = distance;
      body.distance_from = distanceFrom.value;
      if (distanceFrom.value === 'current') body.direction = byId('route-direction').value;
    }
    return body;
  }

  function showRouteItems(labels) {
    routeItems.replaceChildren();
    routeItems.hidden = !Array.isArray(labels) || labels.length !== 7;
    if (routeItems.hidden) return;
    const items = [...labels];
    items.splice(4, 0, 'DO_SET_REVERSE=1');
    items.splice(1, 0, 'DO_SET_REVERSE=0');
    for (const label of items) {
      const item = document.createElement('li');
      item.textContent = label;
      routeItems.append(item);
    }
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
      showRouteItems(value.route_labels);
      const route = value.route_labels?.join(' → ');
      result.textContent = route
        ? `${value.state} · ${route} · ${value.target_m.toFixed(2)} м уперед / назад${value.blocker ? ` · ${value.blocker}` : ''}`
        : `${value.state} · ${value.start_m.toFixed(2)} → ${value.target_m.toFixed(2)} м · ${value.reverse ? 'назад' : 'вперед'}${value.round_trip ? ' → HOME' : ''}${value.blocker ? ` · ${value.blocker}` : ''}`;
    } catch (error) {
      showRouteItems();
      result.textContent = error.message;
    } finally {
      busy = previewButton.disabled = false;
      requestButton.disabled = !approvedInput;
    }
  }
  form.addEventListener('submit', event => { event.preventDefault(); submit(false); });
  requestButton.addEventListener('click', () => submit(true));
  pilotButton.addEventListener('click', () => {
    if (busy || !Array.from(marker.options).some(option => option.value === '25' && !option.disabled)) return;
    start.value = 'home';
    mode.value = 'marker';
    marker.value = '25';
    roundTrip.checked = true;
    updateInputs();
    submit(false);
  });

  function showReadiness(status) {
    readiness.textContent = [
      `MISSION_UPLOAD_READY: ${status.mission_upload_ready === true ? 'так' : 'ні'}`,
      `MISSION_EXECUTION_READY: ${status.mission_execution_ready === true ? 'так' : 'ні'}`,
      `DRIVE_READY: ${status.drive_ready || 'UNKNOWN'}`,
      `BRAKE_STATUS: ${status.brake_status || 'UNKNOWN'}`,
      `MOTION_AUTHORIZED: ${status.motion_authorized === true ? 'так' : 'ні'}`,
      `Етап команди гальм: ${status.brake_command || 'UNKNOWN'}`,
    ].join(' · ');
  }

  async function poll() {
    try {
      const response = await hmiFetch('/api/pixhawk/mission/status', {signal: AbortSignal.timeout(2000)});
      if (!response.ok) throw new Error();
      const status = await response.json();
      showReadiness(status);
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
      const details = [status.state === 'VERIFIED' ? 'MISSION VERIFIED' : status.state];
      const nav = status.externalnav;
      if (nav) {
        details.push(`ExternalNav TX=${nav.tx ? 'ON' : 'OFF'} · ${nav.hz} Гц · передано ${nav.sent}`);
        details.push(nav.ekf_flags == null ? 'EKF: UNKNOWN'
          : `EKF flags=${nav.ekf_flags} · XY ${nav.ekf_xy_valid ? 'валідна' : 'невалідна'}`);
        if (nav.reason && !nav.tx) details.push(nav.reason);
        if (nav.hold?.reason) details.push(`HOLD: ${nav.hold.phase} · ${nav.hold.reason}`);
      }
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
      const pilotAvailable = [23, 24, 25].every(id => (status.marker_ids || []).includes(id));
      pilotButton.disabled = !pilotAvailable || !['DRAFT', 'COMPLETED'].includes(status.state) ||
                             status.mission_upload_ready !== true || !status.position || status.hold_pending;
      pilotButton.textContent = `Переглянути ${pilotRoute}`;
      if (status.state === 'VERIFIED' && status.verified_readback) {
        const {item_count, reverse_commands} = status.verified_readback;
        details.push(`перечитано: ${item_count} пунктів · DO_SET_REVERSE: ${reverse_commands.map(item => item.value).join(' → ')}`);
      }
      if (Number.isInteger(status.completed_passes)) details.push(`проїздів за сеанс: ${status.completed_passes}`);
      const p = status.position || nav?.position;
      if (p) {
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
      showReadiness({});
      pilotButton.disabled = true;
    }
    setTimeout(poll, 1000);
  }
  poll();
})();
