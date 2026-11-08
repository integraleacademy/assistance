let browsingCourses = false;
let fromCatalogue = false;
const deskCallKey = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
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
    if (pendingCallerMatch && callerMatch.hidden) scheduleCallerLookup();
  });
}

function showDossier(summary) {
  dossierFacts.replaceChildren();
  for (const [label, value] of [
    ['Formation', summary.formation || 'À préciser'],
    ['Conseiller chargé du dossier', summary.conseiller || 'Non renseigné'],
    ['Dernier échange', summary.dernier_echange || 'Aucun échange consigné'],
    ['Prochain rendez-vous', summary.rdv || 'Aucun rendez-vous confirmé'],
    ['Rappel demandé', summary.rappel || 'Aucune demande en attente'],
    ['Relance prévue', summary.relance],
  ]) {
    if (!value) continue;
    const term = document.createElement('dt'), detail = document.createElement('dd');
    term.textContent = label;
    detail.textContent = value;
    dossierFacts.append(term, detail);
  }
  verifiedDossier.hidden = false;
}

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
    type: 'autre', telephone: interruptPhone.value.trim(), notes: liveNotes.value.trim(),
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
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
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
