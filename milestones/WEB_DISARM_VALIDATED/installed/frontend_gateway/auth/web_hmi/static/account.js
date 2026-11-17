'use strict';
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
async function hmiFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.method && !['GET', 'HEAD'].includes(options.method.toUpperCase())) headers.set('X-CSRF-Token', csrfToken);
  const response = await fetch(url, {...options, headers, credentials:'same-origin', cache:'no-store'});
  if (response.status === 401) {location.replace('/login'); throw new Error('Сесію завершено');}
  return response;
}
// Automatic status/telemetry polling does not count as operator activity.
let lastActivity = 0;
async function activity(event) {
  if (!event.isTrusted || performance.now() - lastActivity < 60000) return;
  lastActivity = performance.now();
  try { await hmiFetch('/api/session/activity', {method:'POST'}); } catch (_) {}
}
for (const kind of ['pointerdown', 'keydown']) document.addEventListener(kind, activity, {passive:true});
