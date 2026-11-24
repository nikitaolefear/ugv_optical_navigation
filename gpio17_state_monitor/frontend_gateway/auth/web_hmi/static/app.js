'use strict';
const el = id => document.getElementById(id);
let current = null, busy = false, online = false, confirmedRevision = null;
let stopStatus = null, resetBusy = false;
function resetEnabled() {
  return document.body.dataset.stopReset === 'yes' && !resetBusy &&
    stopStatus?.reset_available === true && stopStatus?.stop_latch === true &&
    stopStatus?.physical_drive_interlocked === true &&
    performance.now() - stopStatus.receivedAt < 2000;
}
function render(s) {
  current = s;
  el('state').textContent = s.state;
  el('mission').value = s.mission;
  el('progress').value = s.progress;
  el('percent').textContent = `${s.progress.toFixed(1)} %`;
  el('speed').textContent = `${s.speed_m_s.toFixed(2)} м/с`;
  el('position').textContent = `${s.position_m.toFixed(2)} м`;
  controls();
}
function controls() {
  const blocked = document.body.dataset.control !== 'yes' || !online || busy || !current;
  el('stop').disabled = document.body.dataset.control !== 'yes';
  el('mission').disabled = blocked || current.state !== 'READY';
  el('start').disabled = blocked || current.state !== 'READY';
  el('pause').disabled = blocked || current.state !== 'MOVING';
  el('resume').disabled = blocked || !current.resume_allowed;
  el('reset').disabled = !resetEnabled();
  el('fault').disabled = blocked || current.state === 'FAULT';
}
async function command(action, extra = {}) {
  if (busy && action !== 'stop') return;
  busy = true; controls();
  const body = {action, revision: current?.revision, request_id: crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`, ...extra};
  try {
    const response = await hmiFetch('/api/command', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const data = await response.json();
    online = true;
    if (!response.ok) {if(data.status) render(data.status); throw new Error(data.error || 'Помилка сервера');}
    render(data); el('notice').textContent = 'Команду симулятора виконано.';
  } catch (error) {el('notice').textContent = `${error.message}. Перевірте поточний стан перед повторенням.`;}
  finally {busy = false; controls();}
}
async function poll() {
  if (!busy) {
    try {
      const response = await hmiFetch('/api/status');
      if (!response.ok) throw new Error();
      online = true; render(await response.json());
    } catch (_) {online = false; controls(); el('state').textContent = 'ЗВ’ЯЗОК ІЗ СИМУЛЯТОРОМ ВТРАЧЕНО'; el('notice').textContent = 'Дані застарілі. Серверна симуляція може продовжуватися.';}
  }
  setTimeout(poll, 500);
}
el('mission').onchange = () => command('select', {mission:Number(el('mission').value)});
el('start').onclick = () => {confirmedRevision = current.revision; el('route').textContent = el('mission').selectedOptions[0].textContent; el('confirm').showModal();};
el('cancel').onclick = () => el('confirm').close();
el('yes').onclick = () => {el('confirm').close(); command('start', {confirmed:true, revision:confirmedRevision});};
el('stop').onclick = () => {el('confirm').close(); command('stop');};
el('pause').onclick = () => command('pause');
el('resume').onclick = () => command('resume');
function renderStateMonitor(monitor) {
  const output = el('state-monitor');
  if (!output) return;
  const voltage = value => Number.isFinite(value) && value > 0 ? `${value.toFixed(2)} В` : 'UNKNOWN';
  const pwm = value => Number.isInteger(value) && value > 0 ? `${value} мкс` : 'UNKNOWN';
  const vesc = monitor?.vesc;
  const brakes = monitor?.brakes;
  const slots = Object.entries(vesc?.esc_slots_voltage_v ?? {})
    .filter(([, value]) => Number.isFinite(value) && value > 0)
    .map(([slot, value]) => `ESC №${slot} ${voltage(value)}`);
  output.textContent =
    `VESC лівий: ${voltage(vesc?.left?.voltage_v)} · VESC правий: ${voltage(vesc?.right?.voltage_v)}` +
    (slots.length ? ` · ${slots.join(' · ')}` : '') +
    ` · SERVO5: ${pwm(brakes?.servo5_pwm_us)} · SERVO6: ${pwm(brakes?.servo6_pwm_us)}` +
    ` · Фізичний стан гальм: ${brakes?.physical_state ?? 'UNKNOWN'}` +
    ` · AUTO-місія: ${monitor?.mission_state ?? 'UNKNOWN'}`;
}
async function readStopStatus() {
  try {
    const response = await hmiFetch('/api/production-stop', {signal:AbortSignal.timeout(1500)});
    if (!response.ok) throw new Error();
    stopStatus = {...await response.json(), receivedAt: performance.now()};
    const contact = stopStatus.contacts?.FRONT || 'UNKNOWN';
    const latch = stopStatus.stop_latch === true ? 'ON' : stopStatus.stop_latch === false ? 'OFF' : 'UNKNOWN';
    const interlock = stopStatus.physical_drive_interlocked === true ? 'VERIFIED' : 'UNVERIFIED';
    const brakes = {
      HOLD: 'Гальма: команда утримання',
      PENDING: 'Гальма: очікуємо надсилання запиту',
      SENT: 'Гальма: запит відпускання надіслано, фактичний стан невідомий',
      FAULT: 'Гальма: помилка керування, тягу заблоковано'
    }[stopStatus.brake_release];
    el('production-stop-status').textContent =
      `FRONT: ${contact} · STOP_LATCH: ${latch} · Interlock: ${interlock}${brakes ? ' · ' + brakes : ''}`;
    renderStateMonitor(stopStatus.state_monitor);
  } catch (_) {
    stopStatus = null;
    el('production-stop-status').textContent = 'Стан FRONT / STOP_LATCH недоступний.';
    renderStateMonitor(null);
  } finally {
    controls();
  }
}
async function pollStopStatus() {
  if (!resetBusy) await readStopStatus();
  setTimeout(pollStopStatus, 500);
}
el('reset').onclick = async () => {
  if (!resetEnabled()) return;
  el('confirm').close();
  resetBusy = true;
  stopStatus = null;
  controls();
  try {
    const response = await hmiFetch('/api/production-stop/reset', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      signal: AbortSignal.timeout(2000),
      body: JSON.stringify({request_id: crypto.randomUUID()})
    });
    const result = await response.json();
    if (!response.ok || result.reset_accepted !== true) throw new Error();
    el('notice').textContent = result.brake_release
      ? 'RESET прийнято. Перевірте фактичний стан гальм. Рух не запущено.'
      : 'STOP_LATCH скинуто. Рух не запущено.';
  } catch (_) {
    el('notice').textContent = 'RESET не підтверджено. Перевірте стан STOP_LATCH.';
  } finally {
    await readStopStatus();
    resetBusy = false;
    controls();
  }
};
if (document.body.dataset.status === 'yes') {
  pollStopStatus();
  setInterval(() => {
    el('reset').disabled = !resetEnabled();
    if (stopStatus && performance.now() - stopStatus.receivedAt >= 2000) renderStateMonitor(null);
  }, 250);
}
el('fault').onclick = () => command('fault');
if (document.body.dataset.status === 'yes') poll();
controls();

// Independent polling; DEMO command handlers never reference telemetry.
const roverModes = {0:'MANUAL', 1:'ACRO', 3:'STEERING', 4:'HOLD', 5:'LOITER', 6:'FOLLOW', 7:'SIMPLE', 10:'AUTO', 11:'RTL', 12:'SMART_RTL', 15:'GUIDED', 16:'INITIALISING'};
function telemetryField(field, format = String) {
  return field.state === 'FRESH' ? format(field.value) : field.state;
}
let voltagePacket = null;
function renderVoltage() {
  const age = voltagePacket?.age_s;
  const elapsed = voltagePacket ? (performance.now() - voltagePacket.renderedAt) / 1000 : 0;
  const currentAge = typeof age === 'number' ? age + elapsed : null;
  const valid = voltagePacket?.state === 'FRESH' && currentAge !== null && currentAge < 3;
  el('voltage').textContent = valid ? `${voltagePacket.value.toFixed(2)} V` : (currentAge !== null ? 'Застаріло' : 'Немає даних');
  el('voltage-source').textContent = voltagePacket?.source || '';
  el('voltage-age').textContent = currentAge === null ? '' : `(${currentAge.toFixed(1)} с)`;
}
function unavailableTelemetry() {
  if (voltagePacket) voltagePacket.state = 'STALE';
  renderVoltage();
  for (const id of ['connection', 'armed', 'flight-mode', 'yaw']) el(id).textContent = 'STALE';
}
async function pollTelemetry() {
  try {
    const response = await hmiFetch('/api/telemetry', {signal:AbortSignal.timeout(2000)});
    if (!response.ok) throw new Error();
    const s = await response.json();
    telemetryRenderedAt = performance.now();
    el('connection').textContent = s.connection;
    el('armed').textContent = telemetryField(s.armed);
    el('flight-mode').textContent = telemetryField(s.flight_mode, m => {
      const name = m.autopilot === 3 && m.vehicle_type === 10 && (m.base_mode & 1) ? roverModes[m.custom_mode] : null;
      return name || 'UNKNOWN';
    });
    voltagePacket = {...s.voltage_v, ...s.voltage_detail, renderedAt: performance.now()};
    renderVoltage();
    el('yaw').textContent = telemetryField(s.yaw_deg, v => `${v.toFixed(1)}°`);
  } catch (_) { unavailableTelemetry(); }
  setTimeout(pollTelemetry, 500);
}
// Clear displayed physical values if background timers/network stop advancing.
let telemetryRenderedAt = 0;
setInterval(() => { renderVoltage(); if (performance.now() - telemetryRenderedAt > 3000) unavailableTelemetry(); }, 100);
document.addEventListener('visibilitychange', () => {
  unavailableTelemetry();
  renderStateMonitor(null);
});
if (document.body.dataset.telemetry === 'yes') pollTelemetry();
