'use strict';
const message = document.getElementById('access-message');
let accessBusy = false;
for (const form of document.querySelectorAll('.access-form')) {
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (accessBusy || (form.dataset.confirm && !window.confirm(form.dataset.confirm))) return;
    const values = new FormData(form);
    const kind = form.dataset.kind;
    let body;
    if (kind === 'user-create') body = Object.fromEntries(values);
    else if (kind === 'user-role') body = {action:'role', role:values.get('role')};
    else if (kind === 'user-password') body = {action:'password', password:values.get('password')};
    else if (kind === 'role-create' || kind === 'role-edit') {
      body = {rank:Number(values.get('rank')), permissions:values.getAll('permissions')};
      if (kind === 'role-create') body.name = values.get('name'); else body.action = 'edit';
    } else body = {action:kind.endsWith('-delete') ? 'delete' : kind};
    accessBusy = true;
    const buttons = document.querySelectorAll('.access-form button');
    for (const button of buttons) button.disabled = true;
    try {
      const response = await hmiFetch(form.dataset.url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Не вдалося зберегти зміни.');
      message.textContent = result.message;
      message.scrollIntoView({block:'center'});
      setTimeout(() => location.reload(), 900);
    } catch (error) {
      message.textContent = error.message;
      message.scrollIntoView({block:'center'});
    } finally {
      for (const input of form.querySelectorAll('input[type="password"]')) input.value = '';
      accessBusy = false;
      for (const button of buttons) button.disabled = false;
    }
  });
}
