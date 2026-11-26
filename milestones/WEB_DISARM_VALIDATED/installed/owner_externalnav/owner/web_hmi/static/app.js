'use strict';
const el = id => document.getElementById(id);
let current = null, busy = false, online = false, confirmedRevision = null;
function render(s) {
  current = s;
  el('state').textContent = s.state;
  el('mission').value = s.mission;
  el('progress').value = s.progress;
  el('percent').textContent = `${s.progress.toFixed(1)} %`;
  el('speed').textContent = `${s.speed_m_s.toFixed(2)} м/с`;
  el('position').textContent = `${s.position_m.toFixed(2)} м`;
  el('events').replaceChildren(...s.events.slice().reverse().map(e => {
    const li = document.createElement('li'); li.textContent = `#${e.number} · ${e.message}`; return li;
  }));
  controls();
}
function controls() {
  const blocked = !online || busy || !current;
  el('mission').disabled = blocked || current.state !== 'READY';
  el('start').disabled = blocked || current.state !== 'READY';
  el('pause').disabled = blocked || current.state !== 'MOVING';
  el('resume').disabled = blocked || !current.resume_allowed;
  el('reset').disabled = blocked;
  el('fault').disabled = blocked || current.state === 'FAULT';
}
async function command(action, extra = {}) {
  if (busy && action !== 'stop') return;
  busy = true; controls();
  const body = {action, revision: current?.revision, request_id: crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`, ...extra};
  try {
    const response = await fetch('/api/command', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
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
      const response = await fetch('/api/status');
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
el('reset').onclick = () => {el('confirm').close(); command('reset');};
el('fault').onclick = () => command('fault');
poll();

// Independent polling; DEMO command handlers never reference telemetry.
const roverModes = {0:'MANUAL', 1:'ACRO', 3:'STEERING', 4:'HOLD', 5:'LOITER', 6:'FOLLOW', 7:'SIMPLE', 10:'AUTO', 11:'RTL', 12:'SMART_RTL', 15:'GUIDED', 16:'INITIALISING'};
function telemetryField(field, format = String) {
  return field.state === 'FRESH' ? format(field.value) : field.state;
}
function unavailableTelemetry() {
  for (const id of ['connection', 'armed', 'flight-mode', 'voltage', 'yaw', 'ekf', 'freshness']) el(id).textContent = 'STALE';
}
async function pollTelemetry() {
  try {
    const response = await fetch('/api/telemetry', {signal:AbortSignal.timeout(2000)});
    if (!response.ok) throw new Error();
    const s = await response.json();
    telemetryRenderedAt = performance.now();
    el('connection').textContent = s.connection;
    el('armed').textContent = telemetryField(s.armed);
    el('flight-mode').textContent = telemetryField(s.flight_mode, m => {
      const name = m.autopilot === 3 && m.vehicle_type === 10 && (m.base_mode & 1) ? roverModes[m.custom_mode] : null;
      return `${name || 'UNKNOWN'} (custom_mode=${m.custom_mode}, base_mode=${m.base_mode})`;
    });
    el('voltage').textContent = telemetryField(s.voltage_v, v => `${v.toFixed(2)} V`);
    el('yaw').textContent = telemetryField(s.yaw_deg, v => `${v.toFixed(1)}°`);
    el('ekf').textContent = Object.entries(s.ekf).map(([k,v]) => `${k}: ${telemetryField(v, x => 'flags=' + x.flags)}`).join(' · ');
    el('freshness').textContent = Object.entries(s.messages).map(([k,v]) => `${k}: ${v.state}${v.age_s === null ? '' : ' (' + v.age_s + ' s)'}`).join(' · ');
  } catch (_) { unavailableTelemetry(); }
  setTimeout(pollTelemetry, 500);
}
// Clear displayed physical values if background timers/network stop advancing.
let telemetryRenderedAt = 0;
setInterval(() => { if (performance.now() - telemetryRenderedAt > 3000) unavailableTelemetry(); }, 500);
document.addEventListener('visibilitychange', () => { unavailableTelemetry(); });
pollTelemetry();
