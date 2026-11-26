'use strict';
const statisticsRoot = document.getElementById('statistics');
const statisticsAdmin = statisticsRoot.dataset.role === 'all';
const periodSelect = document.getElementById('period');
const operatorSelect = document.getElementById('operator');
const statisticsTime = new Intl.DateTimeFormat('uk-UA', {timeZone:'Europe/Kyiv', dateStyle:'medium', timeStyle:'long'});
const counterLabels = {started:'Старти у періоді', completed:'Успішні завершення у періоді', cancelled:'Скасування у періоді', failed:'Збої у періоді', interrupted:'Переривання у періоді', success_rate_pct:'Частка успішних серед завершених результатів обраного періоду, %'};
function counterText(values) {
  const prefix = values.results_complete ? '' : 'НЕПОВНА ІСТОРІЯ · показано лише відомі результати · ';
  return prefix + Object.entries(counterLabels).map(([key,label]) => `${label}: ${values[key] === null ? '—' : values[key]}`).join(' · ') + ` · Зараз активні (незалежно від періоду): ${values.active}`;
}
async function loadStatistics() {
  const button = document.getElementById('refresh-statistics'); button.disabled = true;
  statisticsRoot.replaceChildren();
  try {
    const query = new URLSearchParams({period:periodSelect.value});
    if (operatorSelect && operatorSelect.value) query.set('operator_id', operatorSelect.value);
    const response = await hmiFetch((statisticsAdmin ? '/api/admin/statistics' : '/api/statistics') + '?' + query);
    if (!response.ok) throw new Error('Статистика недоступна');
    const data = await response.json();
    const updated = document.createElement('p'); updated.textContent = 'Станом на: ' + statisticsTime.format(new Date(data.as_of*1000)); statisticsRoot.append(updated);
    for (const domain of ['DEMO','REAL']) {
      const section = document.createElement('section');
      const heading = document.createElement('h2'); heading.textContent = domain === 'DEMO' ? 'DEMO · симуляція' : 'Реальні місії'; section.append(heading);
      const source = data.sources[domain];
      if (!source.available) {
        const p = document.createElement('p'); p.textContent = 'UNKNOWN · джерело реальних місій не підключено. Телеметрія не підтверджує виконання місії.'; section.append(p);
      } else {
        const coverage = document.createElement('p'); coverage.textContent = 'Облік від: ' + statisticsTime.format(new Date(source.coverage_from*1000)); section.append(coverage);
        const gaps = source.history_without_end_time;
        if (gaps && Object.values(gaps).some(value => value > 0)) {
          const warning = document.createElement('p'); warning.setAttribute('role', 'status');
          warning.textContent = 'Історія без збереженого часу завершення: ' + Object.values(gaps).reduce((a,b) => a+b, 0) + '. Ці результати враховано у «Весь час»; їх не розподілено по періодах. Для неповного періоду відсоток не обчислюється.'; section.append(warning);
        }
        const total = document.createElement('p'); total.textContent = counterText(source.summary); section.append(total);
        if (statisticsAdmin) {
          for (const group of source.by_operator) {
            const p = document.createElement('p'); p.textContent = group.username + ' · ' + counterText(group); section.append(p);
          }
        }
      }
      statisticsRoot.append(section);
    }
    document.getElementById('statistics-error').textContent = '';
  } catch (error) {document.getElementById('statistics-error').textContent = error.message;}
  finally {button.disabled = false;}
}
async function initializeStatistics() {
  if (statisticsAdmin) {
    try {
      const response = await hmiFetch('/api/statistics/operators');
      if (!response.ok) throw new Error();
      const data = await response.json();
      for (const user of data.users) {
        const option = document.createElement('option'); option.value = user.id;
        option.textContent = user.username + ' (' + user.role + (user.active ? '' : ', неактивний') + ')'; operatorSelect.append(option);
      }
    } catch (_) {document.getElementById('statistics-error').textContent = 'Список користувачів недоступний';}
  }
  await loadStatistics();
}
periodSelect.onchange = loadStatistics;
if (operatorSelect) operatorSelect.onchange = loadStatistics;
document.getElementById('refresh-statistics').onclick = loadStatistics;
initializeStatistics();
