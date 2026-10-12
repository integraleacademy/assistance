(function () {
  'use strict';
  const config = window.EXTERNAL_META_CONFIG;
  const tbody = document.querySelector('#leads');
  const notice = document.querySelector('#notice');
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const normalize = value => String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  const name = c => `${c.prenom || ''} ${c.nom || ''}`.trim() || 'Prospect';
  const date = value => value && !Number.isNaN(Date.parse(value)) ? new Date(value).toLocaleString('fr-FR', {timeZone:'Europe/Paris',dateStyle:'short',timeStyle:'short'}) : '—';
  let contacts = [], loadVersion = 0, noticeTimer;

  function notify(message, error = false) {
    clearTimeout(noticeTimer);
    notice.textContent = message;
    notice.classList.toggle('error', error);
    notice.hidden = false;
    noticeTimer = setTimeout(() => { notice.hidden = true; }, 7000);
  }

  async function api(path, body) {
    const response = await fetch(path, {method: body ? 'PATCH' : 'GET', credentials:'same-origin', cache:'no-store', headers:{'Content-Type':'application/json','X-CSRF-Token':config.csrf}, ...(body ? {body:JSON.stringify(body)} : {})});
    if (response.status === 401 || response.redirected) {
      location.assign('/login?next=/crm/pistes');
      throw new Error('Veuillez vous reconnecter.');
    }
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(result.error || 'Impossible d’effectuer cette opération. Réessayez.');
      error.status = response.status;
      throw error;
    }
    return result;
  }

  function rows() {
    const query = normalize(document.querySelector('#search').value);
    const filtered = contacts.filter(c => normalize(`${name(c)} ${c.mail} ${c.telephone} ${c.formation}`).includes(query));
    document.querySelector('#count').textContent = contacts.length;
    document.querySelector('#listStatus').textContent = `${filtered.length} demande${filtered.length === 1 ? '' : 's'}`;
    tbody.innerHTML = filtered.map(c => `<tr class="lead-row" data-open="${esc(c.id)}"><td><button type="button" class="lead-name" aria-label="Ouvrir la fiche CRM de ${esc(name(c))}">${esc(name(c))}</button><small>META</small></td><td>${esc(c.formation || 'À définir')}<small>${esc(c.lieu || '')}</small></td><td>${esc(c.telephone || '—')}<small>${esc(c.mail)}</small></td><td>${esc(date(c.created_at))}</td><td><button type="button" data-open="${esc(c.id)}" aria-label="Traiter la demande de ${esc(name(c))}">Traiter →</button></td></tr>`).join('') || '<tr><td colspan="5" class="empty">Aucune nouvelle demande META à traiter.</td></tr>';
  }

  async function load() {
    const version = ++loadVersion;
    const data = await api('/api/crm/external-meta');
    if (version !== loadVersion) return;
    contacts = data.contacts;
    rows();

  }

  function open(id) { location.assign(`/crm/pistes?fiche=${encodeURIComponent(id)}`); }

  document.querySelector('#search').addEventListener('input', rows);
  document.querySelector('#refresh').addEventListener('click', () => load().catch(error => notify(error.message, true)));
  tbody.addEventListener('click', event => { const target = event.target.closest('[data-open]'); if (target) open(target.dataset.open); });
  load().catch(error => { document.querySelector('#listStatus').textContent = 'Chargement impossible'; notify(error.message, true); });
  setInterval(() => { if (!document.hidden) load().catch(error => notify(error.message, true)); }, 45000);
})();
