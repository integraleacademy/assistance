(function () {
  'use strict';
  const config = window.EXTERNAL_META_CONFIG;
  const tbody = document.querySelector('#leads');
  const sheet = document.querySelector('#sheet');
  const notice = document.querySelector('#notice');
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const normalize = value => String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  const name = c => `${c.prenom || ''} ${c.nom || ''}`.trim() || 'Prospect';
  const date = value => value && !Number.isNaN(Date.parse(value)) ? new Date(value).toLocaleString('fr-FR', {timeZone:'Europe/Paris',dateStyle:'short',timeStyle:'short'}) : '—';
  let contacts = [], statuses = [], selected = null, loadVersion = 0, openVersion = 0, noticeTimer;

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
    statuses = data.statuses;
    rows();
    if (selected && !contacts.some(c => c.id === selected)) {
      close();
      notify('Cette piste a quitté Nouveaux META. Elle est désormais suivie par l’équipe commerciale.');
    }
  }

  function field(c, key, label, options, type = 'text') {
    const current = String(c[key] || '');
    let control;
    if (options) {
      const choices = [...options];
      if (current && !choices.some(item => (Array.isArray(item) ? item[0] : item) === current)) choices.push(current);
      control = `<select name="${key}" id="f-${key}">${choices.map(item => { const [value,text] = Array.isArray(item) ? item : [item,item || 'À renseigner']; return `<option value="${esc(value)}" ${value === current ? 'selected' : ''}>${esc(text)}</option>`; }).join('')}</select>`;
    } else {
      control = `<input id="f-${key}" name="${key}" type="${type}" maxlength="5000" value="${esc(current)}">`;
    }
    return `<div class="field"><label for="f-${key}">${esc(label)}</label>${control}</div>`;
  }

  function renderSheet(c) {
    const yn = ['', 'OUI', 'NON'];
    const group = (title, content) => `<fieldset><legend>${title}</legend><div class="form-grid">${content}</div></fieldset>`;
    document.querySelector('#sheetTitle').textContent = name(c);
    const telephone = String(c.telephone || '').replace(/[^+\d]/g, '');
    document.querySelector('#sheetBody').innerHTML = `<form id="leadForm"><div class="sheet-content">
      <div class="sheet-links">${telephone ? `<a href="tel:${esc(telephone)}">Appeler ${esc(c.telephone)}</a>` : ''}${c.mail ? `<a href="mailto:${encodeURIComponent(c.mail)}">${esc(c.mail)}</a>` : ''}<span class="muted">Reçue le ${esc(date(c.created_at))}</span></div>
      ${group('Coordonnées et projet', field(c,'prenom','Prénom') + field(c,'nom','Nom') + field(c,'telephone','Téléphone',null,'tel') + field(c,'mail','E-mail',null,'email') + field(c,'formation','Formation',['','APS','A3P','DESP','SSIAP 1','Chauffeur VTC']) + field(c,'dates_formation','Dates souhaitées') + field(c,'desp_type','Parcours DESP',['','INITIAL','VAE']))}
      <details><summary>Réponses du formulaire META</summary><dl class="answers">${(c.meta_answers || []).map(a => `<dt>${esc(a.question)}</dt><dd>${esc(a.answer)}</dd>`).join('') || '<dd>Aucune réponse complémentaire.</dd>'}</dl></details>
      ${group('Qualification du financement', field(c,'cpf','Compte CPF',yn) + field(c,'cpf_montant','Montant CPF exact (€)') + field(c,'cpf_palier','Palier CPF déclaré') + field(c,'identite_creation','Identité numérique créée',yn) + field(c,'identite_ok','Identité numérique fonctionnelle',yn) + field(c,'financement_ft','Financement France Travail envisagé',yn) + field(c,'inscrit_ft','Inscrit à France Travail',yn) + field(c,'financement_perso_possible','Financement personnel possible',yn))}
      ${group('Informations de qualification', field(c,'carte_pro','Carte professionnelle',yn) + field(c,'titre_sejour','Titulaire d’un titre de séjour',yn) + field(c,'garde_vue','Garde à vue ou prise d’empreintes',yn) + field(c,'antecedents','Antécédents judiciaires',yn) + field(c,'compte_cnaps','Compte CNAPS existant',yn) + field(c,'integration_dracar','Intégration DRACAR',yn))}
      ${group('Traitement de la demande', field(c,'qualification_flag','Qualification',[['','Aucun flag'],['green','Green Flag'],['red','Red Flag']]) + field(c,'statut','Étape après traitement',statuses) + `<label class="full">Prochaine action<input name="prochaine_action_manuelle" maxlength="300" value="${esc(c.prochaine_action_manuelle)}"></label><label class="full">Commentaire de suivi<textarea name="commentaires" maxlength="5000">${esc(c.commentaires)}</textarea></label><label class="full">Compte rendu de l’appel<textarea name="call_note" maxlength="5000" placeholder="Consigner l’échange ou la tentative d’appel…"></textarea></label><div class="full form-grid" id="followupFields" hidden>${field(c,'relance_date','Date de relance',null,'date') + field(c,'relance_motif','Motif de relance')}</div><div class="full" id="disqualificationFields" hidden>${field(c,'disqualification_reason','Motif de disqualification')}</div>`)}
      <details><summary>Derniers échanges et suivi</summary><ul class="history">${(c.activities || []).map(a => `<li><b>${esc(a.title)}</b><small>${esc(date(a.date))} · ${esc(a.author)}</small><p>${esc(a.detail)}</p></li>`).join('') || '<li>Aucun échange consigné.</li>'}</ul></details>
      <p class="sheet-error" id="sheetError" role="alert" hidden></p></div>
      <div class="sheet-footer"><p>Un changement d’étape retire la piste de votre espace.</p><button type="submit" class="primary">Enregistrer</button></div></form>`;
    const form = document.querySelector('#leadForm');
    const initial = Object.fromEntries(new FormData(form));
    function conditionals() {
      const status = form.elements.statut.value;
      document.querySelector('#followupFields').hidden = status !== 'A relancer';
      document.querySelector('#disqualificationFields').hidden = status !== 'Disqualifié';
      form.elements.relance_date.required = status === 'A relancer';
      form.elements.disqualification_reason.required = status === 'Disqualifié';
    }
    form.elements.statut.addEventListener('change', conditionals);
    conditionals();
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const submit = form.querySelector('[type=submit]');
      const error = document.querySelector('#sheetError');
      const values = Object.fromEntries(new FormData(form));
      const payload = Object.fromEntries(Object.entries(values).filter(([key,value]) => value !== initial[key]));
      if (values.statut === 'Disqualifié') payload.disqualification_reason = values.disqualification_reason;
      if (values.statut === 'A relancer') payload.relance_date = values.relance_date;
      if (!Object.keys(payload).length) { notify('Aucune modification à enregistrer.'); return; }
      submit.disabled = true;
      error.hidden = true;
      try {
        const result = await api(`/api/crm/external-meta/${encodeURIComponent(c.id)}`, payload);
        close();
        notify(result.in_scope ? 'Le traitement a été enregistré.' : 'Traitement enregistré. La piste est transmise à l’équipe commerciale.');
        await load();
      } catch (failure) {
        if (failure.status === 404) { close(); await load().catch(() => {}); }
        error.textContent = failure.message;
        error.hidden = false;
        notify(failure.message, true);
      } finally { submit.disabled = false; }
    });
  }

  async function open(id) {
    const version = ++openVersion;
    try {
      const contact = await api(`/api/crm/external-meta/${encodeURIComponent(id)}`);
      if (version !== openVersion) return;
      selected = id;
      renderSheet(contact);
      sheet.showModal();
    } catch (error) { notify(error.message, true); if (error.status === 404) load().catch(() => {}); }
  }

  function close() { openVersion++; selected = null; sheet.close(); document.querySelector('#sheetBody').replaceChildren(); }
  document.querySelector('#search').addEventListener('input', rows);
  document.querySelector('#refresh').addEventListener('click', () => load().catch(error => notify(error.message, true)));
  document.querySelector('#closeSheet').addEventListener('click', close);
  sheet.addEventListener('cancel', event => { event.preventDefault(); close(); });
  tbody.addEventListener('click', event => { const target = event.target.closest('[data-open]'); if (target) open(target.dataset.open); });
  load().catch(error => { document.querySelector('#listStatus').textContent = 'Chargement impossible'; notify(error.message, true); });
  setInterval(() => { if (!document.hidden && !sheet.open) load().catch(error => notify(error.message, true)); }, 45000);
})();
