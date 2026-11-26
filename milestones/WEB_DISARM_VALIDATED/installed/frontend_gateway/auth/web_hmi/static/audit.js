'use strict';
let cursor = null;
const audit = document.getElementById('audit');
const auditTime = new Intl.DateTimeFormat('uk-UA', {timeZone:'Europe/Kyiv', dateStyle:'medium', timeStyle:'long'});
const endpoint = audit.dataset.scope === 'all' ? '/api/admin/audit' : '/api/audit';
async function loadAudit() {
  const more = document.getElementById('more'); more.disabled = true;
  try {
    const response = await hmiFetch(endpoint + (cursor ? '?before=' + cursor : ''));
    if (!response.ok) throw new Error('Журнал недоступний');
    const data = await response.json();
    for (const entry of data.entries) {
      const p = document.createElement('p');
      p.textContent = `${auditTime.format(new Date(entry.time * 1000))} · ${entry.actor || 'система / локальний CLI'} [${entry.role || 'system'}] · ${entry.action} · ${entry.result}${entry.detail ? ' · ' + entry.detail : ''}`;
      audit.appendChild(p);
    }
    cursor = data.next_before; more.hidden = !cursor;
    document.getElementById('audit-error').textContent = '';
  } catch (error) {document.getElementById('audit-error').textContent = error.message;}
  finally {more.disabled = false;}
}
document.getElementById('more').onclick = loadAudit;
loadAudit();
