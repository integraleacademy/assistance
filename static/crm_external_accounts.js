(function () {
  'use strict';
  const root = document.querySelector('#accounts');
  const notice = document.querySelector('#notice');
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function notify(message, error = false) { notice.textContent = message; notice.classList.toggle('error', error); notice.hidden = false; }
  async function api(method = 'GET', body) {
    const response = await fetch('/api/crm/external-accounts', {method, credentials:'same-origin', cache:'no-store', headers:{'Content-Type':'application/json','X-CSRF-Token':window.EXTERNAL_META_CONFIG.csrf}, ...(body ? {body:JSON.stringify(body)} : {})});
    if (response.status === 401 || response.redirected) { location.assign('/login'); throw new Error('Veuillez vous reconnecter.'); }
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || (response.status === 409 ? 'Un compte existe déjà pour cette adresse.' : 'L’opération a échoué. Vérifiez les informations et réessayez.'));
    return result;
  }
  async function load() {
    const accounts = await api();
    root.innerHTML = accounts.map((account,index) => `<article class="account" data-email="${esc(account.email)}"><div class="account-title"><div><b>${esc(account.name)}</b><small>${esc(account.email)}</small></div><span class="state ${account.enabled ? '' : 'disabled'}">${account.enabled ? 'Actif' : 'Désactivé'}</span></div><div class="account-actions"><button type="button" data-enable="${!account.enabled}" class="${account.enabled ? 'danger' : ''}">${account.enabled ? 'Désactiver' : 'Réactiver'}</button><form class="reset-form"><label for="reset-${index}">Nouveau mot de passe pour ${esc(account.name)}</label><input id="reset-${index}" name="password" type="password" minlength="12" maxlength="256" required autocomplete="new-password" placeholder="Nouveau mot de passe"><button type="submit">Réinitialiser</button></form></div></article>`).join('') || '<p class="muted">Aucun accès externe pour le moment.</p>';
  }
  document.querySelector('#createAccount').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget, button = form.querySelector('[type=submit]');
    button.disabled = true;
    try { await api('POST', Object.fromEntries(new FormData(form))); form.reset(); notify('Accès créé. Le compte peut se connecter au CRM et accéder aux nouvelles demandes META.'); await load(); }
    catch (error) { notify(error.message, true); }
    finally { button.disabled = false; }
  });
  root.addEventListener('click', async event => {
    const button = event.target.closest('[data-enable]');
    if (!button) return;
    button.disabled = true;
    const enabled = button.dataset.enable === 'true';
    try { await api('PATCH', {email:button.closest('[data-email]').dataset.email,enabled}); notify(enabled ? 'Accès réactivé.' : 'Accès désactivé. Les sessions de ce compte sont révoquées.'); await load(); }
    catch (error) { button.disabled = false; notify(error.message, true); }
  });
  root.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.target, button = form.querySelector('[type=submit]');
    button.disabled = true;
    try { await api('PATCH', {email:form.closest('[data-email]').dataset.email,password:form.elements.password.value}); form.reset(); notify('Mot de passe réinitialisé. Le compte doit se reconnecter.'); await load(); }
    catch (error) { button.disabled = false; notify(error.message, true); }
  });
  load().catch(error => { root.textContent = 'Chargement impossible.'; notify(error.message, true); });
})();
