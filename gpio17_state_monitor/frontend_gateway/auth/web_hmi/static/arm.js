'use strict';
(() => {
  const button = document.getElementById('pixhawk-arm');
  const notice = document.getElementById('arm-notice');
  if (!button || button.dataset.armEnabled !== 'yes') return;

  let readyUntil = 0, busy = false, readinessGeneration = 0, unclear = false;
  const canArm = () => !busy && !unclear && performance.now() < readyUntil;
  const render = () => { button.disabled = !canArm(); };
  const message = text => {
    notice.hidden = false;
    notice.textContent = text;
  };
  const request = async (path, options) => {
    const response = await hmiFetch(path, {...options, signal: AbortSignal.timeout(1800)});
    if (!response.ok) throw new Error();
    return response.json();
  };

  async function pollReady() {
    if (!busy && !unclear) {
      const generation = readinessGeneration;
      try {
        const state = await request('/api/pixhawk/arm/ready');
        if (generation === readinessGeneration) readyUntil = state.ready === true ? performance.now() + 1400 : 0;
      } catch (_) {
        if (generation === readinessGeneration) readyUntil = 0;
      }
      render();
    }
    setTimeout(pollReady, 650);
  }

  button.addEventListener('click', async () => {
    if (!canArm()) return;
    busy = true;
    readinessGeneration += 1;
    readyUntil = 0;
    render();
    const requestId = crypto.randomUUID();
    const deadline = performance.now() + 7000;
    const pending = new Set(['QUEUED', 'ATTEMPT', 'SENT']);
    try {
      message('ARM · очікування свіжого HEARTBEAT.');
      let state = await request('/api/pixhawk/arm', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({request_id: requestId})
      });
      while (state.request_id === requestId && pending.has(state.state) && performance.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 350));
        state = await request('/api/pixhawk/arm/' + requestId);
      }
      if (state.request_id !== requestId) throw new Error();
      if (state.state === 'ARMED_OBSERVED') {
        message('Після запиту ARM отримано HEARTBEAT зі станом ARMED.');
      } else if (state.state === 'REJECTED') {
        message('ARM відхилено.');
      } else {
        throw new Error();
      }
    } catch (_) {
      unclear = true;
      message('Результат ARM невідомий. Перевірте стан Pixhawk перед новим запитом.');
    } finally {
      busy = false;
      render();
    }
  });

  setInterval(render, 200);
  pollReady();
})();
