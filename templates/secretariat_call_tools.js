let browsingCourses = false;
let fromCatalogue = false;
function newDeskCallKey() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
}
const deskCallKey = newDeskCallKey();
const notesButton = document.getElementById('openCallNotes');
const notesDialog = document.getElementById('notesDialog');
const liveNotes = document.getElementById('liveNotes');
const notesForm = document.getElementById('notesForm');
const interruption = document.getElementById('interruptedFields');
const receiptArea = document.getElementById('callReceipt');

document.getElementById('browseCourses').onclick = () => {
  browsingCourses = true;
  fromCatalogue = false;
  requestType = '';
  showStep(3);
  formationSearch.focus();
};

function setCpfBalance(consulted) {
  const balance = document.getElementById('cpf_montant');
  const pending = document.getElementById('cpfPending');
  cpfMontantField.hidden = !consulted;
  pending.disabled = !consulted;
  pending.required = false;
  if (!consulted) pending.checked = false;
  balance.disabled = !consulted || pending.checked;
  balance.required = !balance.disabled;
  if (balance.disabled) balance.value = '';
}
document.getElementById('cpfPending').addEventListener('change', () => setCpfBalance(true));
for (const input of [callerForm.elements.prenom, callerForm.elements.nom]) {
  input.addEventListener('input', () => {
    if (useCallerMatch.disabled || (!verifiedDossier.hidden) || (pendingCallerMatch && callerMatch.hidden)) scheduleCallerLookup();
  });
}

function deskJsonHeaders() {
  const headers = {'Content-Type': 'application/json'};
  if (document.body.dataset.deskCsrf) headers['X-CSRF-Token'] = document.body.dataset.deskCsrf;
  return headers;
}

function dossierGroup(parent, facts) {
  const entries = facts.filter(([, value]) => typeof value === 'string' && value.trim());
  if (!entries.length) return;
  const list = document.createElement('dl');
  list.className = 'dossier-rows';
  for (const [label, value] of entries) {
    const row = document.createElement('div');
    const term = document.createElement('dt'), detail = document.createElement('dd');
    term.textContent = label;
    detail.textContent = value;
    row.append(term, detail);
    list.append(row);
  }
  parent.append(list);
}

function renderDeskCnaps(facts) {
  cnapsDesk.hidden = !facts;
  cnapsResult.replaceChildren();
  const original = document.getElementById('cnapsOriginal');
  original.replaceChildren();
  if (!facts) return;
  const documentCount = facts.documents?.length || 0;
  document.getElementById('cnapsPreview').textContent = documentCount ? documentCount + (documentCount === 1 ? ' document' : ' documents') : '';
  dossierGroup(cnapsResult, [
    ['Demande auprès du CNAPS', facts.request_wording], ['Dernière consultation', facts.checked],
  ]);
  if (facts.documents?.length) {
    const list = document.createElement('ul');
    list.className = 'dossier-documents';
    for (const doc of facts.documents) {
      const item = document.createElement('li');
      const name = document.createElement('strong'), wording = document.createElement('span');
      name.textContent = doc.name;
      wording.textContent = doc.wording;
      item.append(name, wording);
      list.append(item);
    }
    cnapsResult.append(list);
  }
  dossierGroup(cnapsResult, [
    ['Vérification des documents', facts.document_note], ['Documents consultés le', facts.card_checked !== facts.checked ? facts.card_checked : ''],
    ['Ce que l’appelant a déclaré', [
      facts.account === 'Créé' ? 'Compte en ligne du CNAPS créé.' : (facts.account === 'Non créé' ? 'Compte en ligne du CNAPS non créé.' : ''),
      facts.card === 'Oui' ? 'Dit détenir une carte professionnelle.' : (facts.card === 'Non' ? 'Dit ne pas détenir de carte professionnelle.' : ''),
    ].filter(Boolean).join(' ')],
  ]);
  dossierGroup(original, [
    ['Suivi CNAPS', facts.status], ['Références des documents', (facts.titles || []).join(' • ')],
  ]);
  refreshDeskCnaps.hidden = !facts.can_check;
}

function showDossier(summary) {
  dossierFacts.replaceChildren();
  const exchanges = document.getElementById('dossierExchanges');
  exchanges.replaceChildren();
  document.getElementById('requestPreview').textContent = summary.centre || '';
  document.getElementById('exchangePreview').textContent = summary.conseiller || '';
  for (const panel of verifiedDossier.querySelectorAll('details')) panel.open = false;
  dossierStatus.textContent = summary.stage_label || 'Situation à préciser';
  dossierStatus.dataset.tone = (summary.archive || summary.statut === 'Disqualifié') ? 'closed' : (summary.statut === 'Converti' ? 'success' : 'active');
  document.getElementById('dossierCourse').textContent = summary.course_label || 'Formation à préciser avec l’école';
  const purpose = document.getElementById('dossierPurpose');
  purpose.textContent = summary.course_purpose || '';
  purpose.hidden = !summary.course_purpose;
  document.getElementById('dossierWording').textContent = summary.phone_wording || 'Je peux demander à votre conseiller de vérifier.';
  dossierGroup(dossierFacts, [
    ['Démarche en cours', summary.secondary_label], ['Situation mise à jour le', summary.statut_depuis],
    ['Raison de la clôture', summary.motif_disqualification], ['Reprise du suivi prévue le', summary.reactivation],
    ['Centre de formation', summary.centre], ['Dates de formation', summary.session], ['Financement évoqué', summary.financement],
  ]);
  document.getElementById('requestPanel').hidden = !dossierFacts.childElementCount;
  dossierGroup(exchanges, [
    ['Conseiller qui suit le dossier', summary.conseiller || 'Non renseigné'],
    ['Dernier échange', summary.dernier_echange || 'Aucun échange consigné'],
    ['Prochain rendez-vous', summary.rdv || 'Aucun rendez-vous confirmé'],
    ['Relance prévue', summary.relance], ['Prochaine action', summary.prochaine_action],
  ]);
  renderDossierMessages(summary.messages || [], summary.phone_booking_url);
  document.getElementById('messagePanel').open = Boolean(summary.messages?.some(message => message.status !== 'processed'));
  const original = document.getElementById('dossierOriginal');
  original.replaceChildren();
  dossierGroup(original, [
    ['Formation', summary.formation], ['Statut du dossier', summary.statut], ['Étape complémentaire', summary.statut_secondaire],
  ]);
  document.getElementById('dossierReferences').hidden = !original.childElementCount && !summary.cnaps;
  cnapsFeedback.textContent = '';
  renderDeskCnaps(summary.cnaps);
  verifiedDossier.hidden = false;
  if (currentStep === 2) {
    const heading = verifiedDossier.querySelector('h3');
    heading.tabIndex = -1;
    heading.focus({preventScroll: true});
    const top = document.getElementById('callerIdentity').hidden ? verifiedDossier : document.getElementById('callerResult');
    top.scrollIntoView?.({block: 'start'});
  }
  if (summary.cnaps?.can_check) loadDeskCnaps(false);
}

function currentDeskIdentity() {
  const form = callerForm.elements;
  return {confirmed_contact_id: existingCrmContactId, verify_identity: true, email: form.email.value,
    telephone: form.telephone.value, prenom: form.prenom.value, nom: form.nom.value};
}

async function loadDeskCnaps(refresh) {
  const version = callerLookupVersion, identity = currentDeskIdentity();
  refreshDeskCnaps.disabled = true;
  cnapsFeedback.textContent = 'Consultation du suivi…';
  try {
    const result = await aiFetch('/api/secretariat/suivi-cnaps', {...identity, refresh});
    if (version !== callerLookupVersion || existingCrmContactId !== identity.confirmed_contact_id) return;
    if (!result.cnaps) throw new Error('Suivi indisponible');
    renderDeskCnaps(result.cnaps);
    cnapsFeedback.textContent = '';
  } catch (_) {
    if (version === callerLookupVersion) cnapsFeedback.textContent = 'Actualisation indisponible. Les informations affichées proviennent du dernier suivi enregistré. Réessayez ou transmettez la question à un conseiller.';
  } finally {
    if (version === callerLookupVersion) refreshDeskCnaps.disabled = false;
  }
}
refreshDeskCnaps.onclick = () => loadDeskCnaps(true);

function setCallbackMode(active) {
  interruption.hidden = !active;
  prepareCallback.hidden = resumeCall.hidden = active;
  saveInterrupted.hidden = !active;
  liveNotes.required = interruptPhone.required = active;
  notesHeading.textContent = active ? 'Enregistrer un rappel' : 'Notes d’appel';
  interruptError.hidden = true;
}
notesButton.onclick = () => {
  liveNotes.value = precision.value;
  interruptPhone.value = callerForm.elements.telephone.value;
  interruptOwner.value = callbackRecipient.value;
  interruptDue.value = callbackDue.value;
  setCallbackMode(false);
  notesDialog.showModal();
  liveNotes.focus();
};
liveNotes.addEventListener('input', () => { precision.value = liveNotes.value; });
interruptDue.oninput = () => { callbackDue.value = interruptDue.value; };
interruptOwner.onchange = () => { callbackRecipient.value = interruptOwner.value; };
interruptPhone.oninput = () => {
  callerForm.elements.telephone.value = interruptPhone.value;
  callerForm.elements.telephone.dispatchEvent(new Event('input', {bubbles: true}));
};
notesDialog.addEventListener('cancel', event => { if (saveInterrupted.disabled) event.preventDefault(); });
for (const button of [closeCallNotes, resumeCall]) button.onclick = () => notesDialog.close();
prepareCallback.onclick = () => {
  setCallbackMode(true);
  (interruptPhone.value ? liveNotes : interruptPhone).focus();
};
notesForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (interruption.hidden || saveInterrupted.disabled || !notesForm.reportValidity()) return;
  if (!normalizedPhone(interruptPhone.value) || !liveNotes.value.trim()) {
    interruptError.textContent = 'Indiquez un numéro de téléphone valide et le motif du rappel.';
    interruptError.hidden = false;
    return;
  }
  saveInterrupted.disabled = closeCallNotes.disabled = true;
  const payload = callData(!calendlyEvent);
  if (browsingCourses) {
    const choice = trainingInfo.querySelector('input[name="formation_date_souhaitee"]:checked');
    payload.formation = formation.value;
    payload.formation_date_souhaitee = choice?.value || '';
    payload.formation_date_status = choice?.dataset.undecided || choice?.dataset.intake ? 'a_definir' : '';
    payload.rentree_souhaitee = choice?.dataset.intake || document.getElementById('rentreeSouhaitee').value;
  }
  Object.assign(payload, {
    type: 'autre', share_with_desk: true, telephone: interruptPhone.value.trim(), notes: liveNotes.value.trim(),
    callback_motif: requestType === 'formation' || browsingCourses ? 'formation' : (requestType === 'suivi' ? 'suivi' : 'message'),
    callback_recipient: interruptOwner.value, callback_due_at: interruptDue.value,
  });
  if (!calendlyEvent && !document.querySelector('input[name="wants_rdv"]:checked')) payload.rdv = 'Non abordé';
  try {
    await submitDeskCall(payload);
    notesDialog.close();
    document.querySelector('[data-step="8"] h2').focus();
  } catch (exception) {
    interruptError.textContent = exception.message;
    interruptError.hidden = false;
  } finally {
    saveInterrupted.disabled = closeCallNotes.disabled = false;
  }
});

async function postDeskData(path, payload) {
  const response = await fetch(path, {method: 'POST', headers: deskJsonHeaders(), body: JSON.stringify(payload)});
  const result = await response.json().catch(() => ({}));
  if (!response.ok || result.ok === false) throw new Error(result.error || 'L’enregistrement a échoué. Réessayez.');
  return result;
}

async function submitDeskCall(payload) {
  const result = await postDeskData('/api/secretariat/demandes', payload);
  browsingCourses = false;
  renderReceipt(result, payload);
  showStep(8);
}

function receiptRow(label, value, status = '') {
  const row = document.createElement('div');
  row.className = 'receipt-row';
  if (status) row.dataset.status = status;
  const title = document.createElement('strong'), detail = document.createElement('span');
  title.textContent = label;
  detail.textContent = value;
  row.append(title, detail);
  receiptArea.append(row);
  return row;
}

function receiptDeadline(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat('fr-FR', {dateStyle: 'short', timeStyle: 'short', timeZone: 'Europe/Paris'}).format(date);
}

function renderReceipt(result, payload) {
  const item = result.demande || {};
  receiptArea.replaceChildren();
  const training = (item.type || payload.type) === 'formation';
  doneSummary.textContent = training ? (selectedFormation()?.label || 'Demande de formation') : 'Demande transmise à la file de rappel du CRM.';
  if (!training) {
    receiptRow('À rappeler par', item.callback_recipient || payload.callback_recipient || 'Équipe');
    const due = receiptDeadline(item.callback_due_at);
    receiptRow('Échéance du rappel', due || 'Sans échéance convenue');
    receiptRow('Objet', payload.notes);
  }
  const booked = item.rdv_status === 'scheduled' || /réservé via Calendly/i.test(item.rdv || payload.rdv || '');
  if (training || booked) {
    const time = [item.rdv_date, item.rdv_time].filter(Boolean).join(' à ');
    receiptRow('Rendez-vous', booked ? (time || 'Réservation confirmée par Calendly') : 'Aucun rendez-vous réservé', booked ? 'sent' : '');
  }
  if (training && item.devis === 'OUI') receiptRow('Devis', item.quote_created ? 'Devis créé' : 'Demandé · à vérifier', item.quote_created ? 'sent' : 'failed');
  if (item.financement_a_confirmer?.length) receiptRow('À confirmer avec le conseiller', item.financement_a_confirmer.join(', '));
  if (!training) return;
  if (item.linked === false) receiptRow('Rapprochement CRM', 'Identité à vérifier par l’équipe', 'failed');
  for (const [channel, label] of [['email', 'E-mail'], ['sms', 'SMS']]) {
    const state = result.messages?.[channel];
    const sent = ['sent', 'already_sent'].includes(state);
    const failed = ['failed', 'recipient_missing', 'template_missing'].includes(state);
    const recipient = payload[channel === 'sms' ? 'telephone' : 'email'] || '';
    const row = receiptRow(label, sent ? 'Envoyé · ' + recipient : failed ? 'Échec de l’envoi' : (state === 'sending' ? 'Envoi à vérifier' : 'Envoi non effectué'), sent ? 'sent' : 'failed');
    if (failed && item.id) addRetryForm(row, channel, label, item.id, recipient, payload);
  }
}

function addRetryForm(row, channel, label, id, recipient, payload) {
  const form = document.createElement('form');
  form.className = 'retry-form';
  const address = document.createElement('input');
  address.type = channel === 'sms' ? 'tel' : 'email';
  address.required = true;
  address.value = recipient;
  address.setAttribute('aria-label', channel === 'sms' ? 'Numéro de téléphone à utiliser' : 'Adresse e-mail à utiliser');
  const button = document.createElement('button');
  button.type = 'submit';
  button.className = 'btn secondary';
  button.textContent = channel === 'sms' ? 'Renvoyer le SMS' : 'Renvoyer l’e-mail';
  const feedback = document.createElement('small');
  feedback.setAttribute('role', 'status');
  form.append(address, button, feedback);
  row.append(form);
  form.onsubmit = async event => {
    event.preventDefault();
    if (button.disabled || !form.reportValidity()) return;
    button.disabled = address.disabled = true;
    feedback.textContent = 'Envoi en cours…';
    try {
      const result = await postDeskData('/api/secretariat/demandes/' + encodeURIComponent(id) + '/renvoyer/' + channel,
        {call_key: deskCallKey, recipient: address.value.trim()});
      payload[channel === 'sms' ? 'telephone' : 'email'] = address.value.trim();
      if (result.messages?.[channel] === 'sent') {
        row.dataset.status = 'sent';
        row.querySelector('span').textContent = 'Envoyé · ' + address.value.trim();
        form.remove();
      } else feedback.textContent = label + ' toujours en échec. Vérifiez les coordonnées ou le service d’envoi.';
    } catch (exception) {
      feedback.textContent = exception.message;
    } finally { button.disabled = address.disabled = false; }
  };
}
