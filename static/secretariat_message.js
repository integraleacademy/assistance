(() => {
  'use strict';

  const form = document.getElementById('messageForm');
  const submit = document.getElementById('messageSubmit');
  const error = document.getElementById('messageError');
  const success = document.getElementById('messageSuccess');
  const successText = document.getElementById('messageSuccessText');
  const newMessage = document.getElementById('newMessage');
  let callKey = newCallKey();

  function newCallKey() {
    return Array.from(crypto.getRandomValues(new Uint8Array(16)),
      byte => byte.toString(16).padStart(2, '0')).join('');
  }

  function formPayload() {
    const fields = new FormData(form);
    const prenom = String(fields.get('prenom') || '').trim();
    const nomFamille = String(fields.get('nom_famille') || '').trim();
    const recipient = String(fields.get('recipient') || '');
    const motif = form.elements.call_reason.selectedOptions[0].textContent.trim();
    const note = String(fields.get('message') || '').trim();
    return {
      call_key: callKey,
      type: 'autre',
      callback_motif: 'message',
      share_with_desk: Boolean(document.body.dataset.deskRole),
      prenom,
      nom_famille: nomFamille,
      nom: [prenom, nomFamille].filter(Boolean).join(' '),
      email: String(fields.get('email') || '').trim(),
      telephone: String(fields.get('telephone') || '').trim(),
      callback_recipient: recipient === '__team__' ? '' : recipient,
      notes: 'Motif de l’appel : ' + motif + '\nMessage : ' + note,
      rdv: 'Non abordé',
    };
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!form.reportValidity() || submit.disabled) return;
    const payload = formPayload();
    if (!payload.prenom || !payload.nom_famille || !payload.email || !payload.telephone ||
        !String(form.elements.message.value || '').trim()) {
      error.textContent = 'Renseignez toutes les coordonnées et le message à transmettre.';
      error.hidden = false;
      return;
    }
    error.hidden = true;
    submit.disabled = true;
    submit.textContent = 'Transmission en cours…';
    try {
      const headers = {'Content-Type': 'application/json'};
      if (document.body.dataset.deskCsrf) headers['X-CSRF-Token'] = document.body.dataset.deskCsrf;
      const response = await fetch('/api/secretariat/demandes', {
        method: 'POST',
        credentials: 'same-origin',
        headers,
        body: JSON.stringify(payload),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok || result.ok === false) {
        throw new Error(result.error || 'Le message n’a pas pu être transmis. Réessayez.');
      }
      successText.textContent = 'Le message de ' + payload.nom +
        ' a été enregistré et transmis à ' + (payload.callback_recipient || 'l’équipe') + ' dans le CRM.';
      form.hidden = true;
      success.hidden = false;
      success.querySelector('h2').focus();
    } catch (exception) {
      error.textContent = exception.message || 'Une erreur est survenue. Réessayez.';
      error.hidden = false;
    } finally {
      submit.disabled = false;
      submit.textContent = 'Transmettre le message';
    }
  });

  newMessage.addEventListener('click', () => {
    callKey = newCallKey();
    form.reset();
    error.hidden = true;
    success.hidden = true;
    form.hidden = false;
    document.getElementById('firstName').focus();
  });
})();
