'use strict';
(() => {
  const apply = document.getElementById('brake-apply');
  const release = document.getElementById('brake-release');
  const status = document.getElementById('brake-status');
  if (!apply || !release || !status) return;
  let latest = 0;
  let applying = false;
  let releasePending = false;
  let releaseLocked = false;
  const reasons = {
    BRAKE_HOLD_ONLY: 'Профіль HOLD-only.',
    BRAKE_RELEASE_INHIBITED: 'Діє inhibit.',
    BRAKE_STOP_RESET_REQUIRED: 'Потрібен авторизований STOP_RESET.',
    BRAKE_CH8_APPLY_REQUIRED: 'Перед вебзапитом переведіть CH8 у APPLY.',
    BRAKE_RC_LINK_UNAVAILABLE: 'Немає свіжого валідного RC.',
    BRAKE_RC_CHANNEL_INVALID: 'Некоректний сигнал CH3 або CH8.',
    BRAKE_CH8_POSITION_INVALID: 'Невизначене положення CH8.',
    BRAKE_RC_PROFILE_UNVERIFIED: 'Профіль RC/Pixhawk не підтверджено.',
    BRAKE_THROTTLE_NOT_NEUTRAL: 'CH3 поза нейтраллю.',
    BRAKE_OWNER_LINK_UNAVAILABLE: 'Немає свіжого зв’язку з Pixhawk.',
    BRAKE_OWNER_LEASE_LOST: 'Дозвіл owner прострочено.',
    BRAKE_HOLD_LEASE_UNAVAILABLE: 'Немає чинного дозволу після HOLD.',
    TRACTION_INTERLOCK_FEEDBACK_LOST: 'Немає незалежного підтвердження блокування тяги.',
    BRAKE_RELEASE_UNSAFE: 'Захисні умови RELEASE не виконані.',
    BRAKE_RELEASE_ALREADY_REQUESTED: 'Запит RELEASE уже зареєстровано.',
    BRAKE_STORAGE_FAULT: 'Журнал owner недоступний.',
    BRAKE_CONTROL_UNAVAILABLE: 'BrakeLink недоступний.'
  };

  async function send(action) {
    const sequence = ++latest;
    const button = action.toUpperCase();
    status.textContent = `${button} · UNKNOWN · очікуємо відповідь owner`;
    let state = 'UNKNOWN';
    let reason;
    try {
      const requestId = crypto.randomUUID();
      const body = {request_id: requestId};
      if (action === 'release') body.confirmed = true;
      const response = await hmiFetch(`/api/pixhawk/brakes/${action}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(2000)
      });
      const result = await response.json();
      if (result.request_id === requestId) {
        if (response.status === 202 && result.state === 'REQUESTED') state = 'REQUESTED';
        if (response.status === 409 && result.state === 'BLOCKED') state = 'BLOCKED';
        if (result.state === state && Object.hasOwn(reasons, result.reason)) reason = reasons[result.reason];
      }
    } catch (_) {}
    if (sequence === latest) {
      const detail = reason || {
        REQUESTED: 'Owner прийняв запит.',
        BLOCKED: 'Owner або gateway відхилив запит.',
        UNKNOWN: 'Приймання запиту не підтверджено.'
      }[state];
      status.textContent = `${button} · ${state} · ${detail} Фізичне положення гальм не підтверджено.`;
    }
    return state;
  }

  apply.addEventListener('click', async () => {
    if (applying) return;
    applying = apply.disabled = release.disabled = true;
    await send('apply');
    applying = apply.disabled = false;
    release.disabled = releasePending || releaseLocked;
  });

  release.addEventListener('click', async () => {
    if (applying || releasePending || releaseLocked) return;
    if (!window.confirm('Запитати RELEASE обох гальм? Потрібні чинний STOP_RESET і фізичне перемикання CH8.')) return;
    releasePending = release.disabled = true;
    releaseLocked = await send('release') !== 'BLOCKED';
    releasePending = false;
    release.disabled = applying || releaseLocked;
  });
})();
