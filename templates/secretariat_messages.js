const messageList = document.getElementById('deskMessages');
const messageFeedback = document.getElementById('deskMessagesFeedback');
const messageRefresh = document.getElementById('refreshDeskMessages');
const supplementDialog = document.getElementById('supplementDialog');
const supplementNote = document.getElementById('supplementNote');
const supplementSend = document.getElementById('sendSupplement');
const supplementError = document.getElementById('supplementError');
const messageAppointment = document.getElementById('messageAppointment');
const supplementAppointment = document.getElementById('supplementAppointment');
let supplementTarget;

function clearDeskMessages() {
  messageList.replaceChildren();
  messageFeedback.textContent = '';
  messageAppointment.hidden = supplementAppointment.hidden = true;
  messageAppointment.href = supplementAppointment.href = genericCalendly;
  supplementTarget = null;
  if (supplementDialog.open) supplementDialog.close();
  supplementNote.value = '';
}

function renderDossierMessages(messages, bookingUrl) {
  messageList.replaceChildren();
  messageFeedback.textContent = '';
  if (bookingUrl) messageAppointment.href = bookingUrl;
  messageAppointment.hidden = !messages.some(message => message.status === 'processed');
  document.getElementById('messageCount').textContent = messages.length ? messages.length + (messages.length === 1 ? ' message' : ' messages') : 'Aucun message';
  if (!messages.length) {
    messageList.textContent = 'Aucun message transmis pour ce dossier.';
    return;
  }
  const older = document.createElement('details'), olderTitle = document.createElement('summary');
  olderTitle.textContent = 'Messages précédents (' + (messages.length - 1) + ')';
  older.append(olderTitle);
  messages.forEach((message, index) => {
    const card = document.createElement('article'), title = document.createElement('strong');
    card.className = 'desk-message';
    title.textContent = message.subject;
    const recipient = document.createElement('p');
    recipient.className = 'message-meta';
    recipient.textContent = 'Transmis à ' + message.recipient + (message.sent ? ' le ' + message.sent : '');
    card.append(title, recipient);
    if (message.due) {
      const due = document.createElement('p');
      due.className = 'message-due';
      due.textContent = 'Rappel demandé avant le ' + message.due;
      card.append(due);
    }
    if (message.note) {
      const note = document.createElement('p');
      note.className = 'message-note';
      note.textContent = message.note;
      card.append(note);
    }
    if (message.events.length) {
      const history = document.createElement('ol');
      history.className = 'message-history';
      for (const event of message.events) {
        const line = document.createElement('li');
        line.textContent = [event.label, event.by, event.when].filter(Boolean).join(' · ');
        if (event.text) {
          const note = document.createElement('p');
          note.textContent = event.text;
          line.append(note);
        }
        history.append(line);
      }
      card.append(history);
    } else {
      const state = document.createElement('small');
      state.textContent = message.status === 'pending' ? 'En attente de prise en charge.' : message.status_label;
      card.append(state);
    }
    if (message.id && message.status !== 'processed') {
      const action = document.createElement('button');
      action.type = 'button';
      action.className = 'btn secondary';
      action.textContent = 'Compléter ce message';
      action.onclick = () => {
        supplementTarget = {message, key: newDeskCallKey(), identity: currentDeskIdentity(), version: callerLookupVersion};
        supplementNote.value = '';
        supplementError.hidden = true;
        document.getElementById('supplementContext').textContent = [message.subject, message.recipient, message.sent].filter(Boolean).join(' · ');
        supplementSend.hidden = false;
        supplementNote.readOnly = false;
        supplementAppointment.hidden = true;
        supplementDialog.showModal();
        supplementNote.focus();
      };
      card.append(action);
    }
    (index === 0 ? messageList : older).append(card);
  });
  if (messages.length > 1) messageList.append(older);
}

messageRefresh.onclick = async () => {
  const version = callerLookupVersion, identity = currentDeskIdentity();
  messageRefresh.disabled = true;
  messageFeedback.textContent = 'Consultation des messages…';
  try {
    const response = await aiFetch('/api/secretariat/crm-contact', identity);
    if (version !== callerLookupVersion || existingCrmContactId !== identity.confirmed_contact_id) return;
    if (!response.summary) throw new Error('Confirmez à nouveau l’identité de l’appelant.');
    renderDossierMessages(response.summary.messages, response.summary.phone_booking_url);
    messageFeedback.textContent = 'Messages à jour.';
  } catch (error) {
    if (version === callerLookupVersion) messageFeedback.textContent = error.message;
  } finally {
    messageRefresh.disabled = false;
  }
};
document.getElementById('closeSupplement').onclick = () => supplementDialog.close();
supplementDialog.addEventListener('cancel', event => { if (supplementSend.disabled) event.preventDefault(); });
document.getElementById('supplementForm').onsubmit = async event => {
  event.preventDefault();
  const target = supplementTarget;
  if (!target || target.closed || supplementSend.disabled || target.version !== callerLookupVersion ||
      existingCrmContactId !== target.identity.confirmed_contact_id || !supplementNote.value.trim()) return;
  supplementSend.disabled = document.getElementById('closeSupplement').disabled = true;
  try {
    const response = await fetch('/api/secretariat/messages/' + encodeURIComponent(target.message.id) + '/complements', {
      method: 'POST', headers: deskJsonHeaders(),
      body: JSON.stringify({...target.identity, call_key: target.key, note: supplementNote.value.trim()}),
    });
    const result = await response.json().catch(() => ({}));
    if (target.version !== callerLookupVersion || existingCrmContactId !== target.identity.confirmed_contact_id) return;
    if (response.status === 409 && result.messages) {
      renderDossierMessages(result.messages);
      target.closed = true;
      supplementSend.hidden = true;
      supplementNote.readOnly = true;
      supplementAppointment.href = messageAppointment.href;
      supplementAppointment.hidden = false;
      supplementAppointment.focus();
    }
    if (!response.ok) throw new Error(result.error || 'Le complément n’a pas pu être transmis. Réessayez.');
    renderDossierMessages(result.messages);
    supplementDialog.close();
    receiptArea.replaceChildren();
    document.getElementById('doneSummary').textContent = 'Le complément a été ajouté au message existant.';
    receiptRow('Message', target.message.subject);
    receiptRow('Destinataire', target.message.recipient);
    receiptRow('Complément transmis', supplementNote.value.trim(), 'sent');
    showStep(8);
  } catch (error) {
    supplementError.hidden = false;
    supplementError.textContent = error.message;
  } finally {
    supplementSend.disabled = document.getElementById('closeSupplement').disabled = false;
  }
};
