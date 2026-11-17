'use strict';
const el=id=>document.getElementById(id);
let token=null;
function render(s) {
 el('contact').textContent=s.contact;
 el('latch').textContent=s.stop_latch?'АКТИВНЕ':'ЗНЯТО';
 el('run').textContent=s.test_state;
 el('probe').textContent=`HOLD: ${s.hold_phase} | mode=${s.pixhawk_mode === 4 ? 'HOLD' : s.pixhawk_mode === 0 ? 'MANUAL' : s.pixhawk_mode} | DISARMED=${s.pixhawk_disarmed} | HB age=${s.heartbeat_age_s ?? 'UNKNOWN'} | ${s.heartbeat_reason ?? ''}`;
 el('events').textContent=[...s.events].reverse().map(e=>`${e.time} | ${e.event} | contact=${e.contact} | STOP_LATCH=${e.stop_latch} | ${e.test_state} | ${e.detail}`).join('\n');
}
async function poll() {
 try {
  const r=await fetch('/api/state',{cache:'no-store',signal:AbortSignal.timeout(1500)});
  if(!r.ok)throw Error('HTTP '+r.status);
  const s=await r.json(); token=s.csrf;render(s);
  el('connection').textContent='Зв’язок із тестом є';
 }catch(e){token=null;el('connection').textContent='Немає зв’язку — показані дані неактуальні';el('contact').textContent='UNKNOWN';el('latch').textContent='НЕВІДОМО';el('run').textContent='НЕВІДОМО';}
 setTimeout(poll,300);
}
for(const b of document.querySelectorAll('[data-action]')) b.onclick=async()=>{
 if(!token){el('result').textContent='Немає зв’язку';return;}
 try{
  const r=await fetch('/api/test-action',{method:'POST',headers:{'Content-Type':'application/json','X-Test-CSRF':token},body:JSON.stringify({action:b.dataset.action}),signal:AbortSignal.timeout(1500)});
  const s=await r.json();el('result').textContent=r.ok?'Дію прийнято':s.error;if(s.events)render(s);
 }catch(e){el('result').textContent='Дію не підтверджено; перевірте журнал';}
};
poll();
