const directBooking = document.getElementById('directBooking');
const hostedBooking = document.getElementById('hostedBooking');
const chooseSmsBooking = document.getElementById('chooseSmsBooking');
let useSmsBooking = false;
let deskBookingWeek = 0, deskBookingCourse = '', deskBookingEvent = '';
let bookingSlotsVersion = 0, bookingPending = false, bookingUncertain = false, nativeReceipt = null;
function deskBookingConfirmed() { return Boolean(formationFirst && calendlyEvent); }
function lockBookingChoice() {
  document.querySelectorAll('[name=wants_rdv]').forEach(input => { input.disabled = true; });
  document.querySelector('[data-step="6"] [data-back]').disabled = true;
  calendlyInline.hidden = true;
  if (directBooking) directBooking.hidden = hostedBooking.hidden = chooseSmsBooking.hidden = true;
  document.getElementById('hostedBookingIntro').hidden = true;
  updateProgress();
}
const parisTime = new Intl.DateTimeFormat('fr-FR', {timeZone: 'Europe/Paris', hour: '2-digit', minute: '2-digit'});
const parisDay = new Intl.DateTimeFormat('fr-FR', {timeZone: 'Europe/Paris', weekday: 'long', day: 'numeric', month: 'long'});

function bookingNotice(text, tone = '') {
  calendlyStatus.className = ('calendly-status ' + tone).trim();
  calendlyStatus.textContent = text;
}
function showProviderFallback(message) {
  directBooking.hidden = true;
  hostedBooking.hidden = false;
  bookingNotice(message, 'is-error');
}
function deskQuestionInputs(questions) {
  const container = document.getElementById('deskEventQuestions');
  const previous = Object.fromEntries([...container.querySelectorAll('[data-answer]')].map(input => [input.dataset.answer, input.value]));
  container.replaceChildren();
  for (const question of questions) {
    const field = document.createElement('div'), label = document.createElement('label');
    field.className = 'field';
    const input = document.createElement(question.type === 'text' ? 'textarea' : 'input');
    input.id = 'desk-booking-q' + question.position;
    label.htmlFor = input.id;
    label.textContent = question.name + (question.required ? '' : ' (facultatif)');
    input.required = question.required;
    if (question.type === 'phone_number') {
      input.type = 'tel';
      input.readOnly = true;
      input.value = normalizedPhone(callerForm.elements.telephone.value) || callerForm.elements.telephone.value;
    } else {
      input.dataset.answer = question.position;
      input.value = previous[question.position] || '';
      input.maxLength = 1500;
    }
    field.append(label, input);
    container.append(field);
  }
}
async function listDeskSlots() {
  const version = ++bookingSlotsVersion;
  const query = new URLSearchParams({formation: formation.value, week: deskBookingWeek, event_type: deskBookingEvent});
  const slot = document.getElementById('deskSlot'), confirm = document.getElementById('bookDeskSlot');
  confirm.disabled = slot.disabled = true;
  slot.replaceChildren(new Option('Choisir un horaire', ''));
  document.getElementById('previousBookingWeek').disabled = deskBookingWeek === 0;
  bookingNotice('Recherche des disponibilités Calendly…');
  try {
    const response = await fetch('/api/secretariat/calendly/creneaux?' + query, {credentials: 'same-origin'});
    const data = await response.json();
    if (version !== bookingSlotsVersion || currentStep !== 6 || directBooking.hidden) return;
    if (!response.ok || data.hosted_only) {
      showProviderFallback(data.error || 'Cet agenda utilise le formulaire Calendly. Le numéro de rappel SMS peut y être copié.');
      return;
    }
    const select = document.getElementById('deskBookingAgenda');
    document.getElementById('deskAgendaChoice').hidden = data.agendas.length < 2;
    select.replaceChildren(new Option('Choisir un agenda', ''), ...data.agendas.map(item => new Option(item.name, item.uri)));
    deskBookingEvent = data.event_type || '';
    select.value = deskBookingEvent;
    const dayLabel = iso => new Intl.DateTimeFormat('fr-FR', {timeZone: 'UTC', day: 'numeric', month: 'short'}).format(new Date(iso + 'T12:00:00Z'));
    document.getElementById('bookingWeekLabel').textContent = dayLabel(data.day) + ' au ' + dayLabel(data.end_day);
    deskQuestionInputs(data.questions || []);
    let previousDay = '', group;
    for (const value of data.slots || []) {
      const instant = new Date(value), day = parisDay.format(instant);
      if (day !== previousDay) {
        group = document.createElement('optgroup'); group.label = day; slot.append(group); previousDay = day;
      }
      group.append(new Option(day + ' · ' + parisTime.format(instant), value));
    }
    slot.disabled = !data.slots?.length;
    bookingNotice(!deskBookingEvent ? 'Choisissez l’agenda correspondant à l’appel.' :
      slot.disabled ? 'Aucun créneau cette semaine. Consultez la semaine suivante.' : 'Choisissez un créneau avec l’appelant.');
  } catch {
    if (version === bookingSlotsVersion && !directBooking.hidden) showProviderFallback('Les créneaux sont momentanément indisponibles.');
  }
}
function openDeskBooking() {
  if (deskBookingConfirmed() || bookingUncertain) {
    directBooking.hidden = hostedBooking.hidden = true;
    if (bookingUncertain) bookingNotice('Vérifiez dans l’agenda si le rendez-vous a été réservé.', 'is-error');
    else bookingNotice(nativeReceipt ? 'Rendez-vous réservé le ' + nativeReceipt.date + ' à ' + nativeReceipt.time + '.' : 'Rendez-vous réservé via Calendly.', 'is-success');
    return;
  }
  directBooking.hidden = false;
  hostedBooking.hidden = false;
  document.getElementById('hostedBookingIntro').hidden = true;
  calendlyInline.hidden = true;
  const number = callerForm.elements.telephone.value.trim();
  const signature = [formation.value, number, callerForm.elements.email.value].join('|');
  if (deskBookingCourse !== signature) {
    deskBookingCourse = signature; deskBookingWeek = 0; deskBookingEvent = '';
    directBooking.reset();
    document.getElementById('deskEventQuestions').replaceChildren();
    document.getElementById('deskSmsNumber').value = normalizedPhone(number) || number;
  }
  return listDeskSlots();
}
if (directBooking) {
  chooseSmsBooking.onclick = () => {
    const yes = document.querySelector('[name=wants_rdv][value=oui]');
    yes.checked = true;
    selectAppointmentChoice(yes, true);
  };
  document.getElementById('deskSlot').onchange = event => { document.getElementById('bookDeskSlot').disabled = !event.target.value; };
  document.getElementById('deskBookingAgenda').onchange = event => {
    deskBookingEvent = event.target.value; deskBookingWeek = 0;
    document.getElementById('deskEventQuestions').replaceChildren(); listDeskSlots();
  };
  document.getElementById('previousBookingWeek').onclick = () => { if (deskBookingWeek > 0) deskBookingWeek--; listDeskSlots(); };
  document.getElementById('nextBookingWeek').onclick = () => { deskBookingWeek++; listDeskSlots(); };
  document.getElementById('deskSmsConsent').onchange = event => { document.getElementById('deskSmsNumber').required = event.target.checked; };
  hostedBooking.onclick = () => {
    if (bookingPending || deskBookingConfirmed() || bookingUncertain) return;
    useSmsBooking = false;
    renderCalendlyWidget();
  };
  directBooking.onsubmit = async event => {
    event.preventDefault();
    if (bookingPending || deskBookingConfirmed() || bookingUncertain || !directBooking.reportValidity()) return;
    const consent = document.getElementById('deskSmsConsent').checked;
    const sms = consent ? normalizedPhone(document.getElementById('deskSmsNumber').value) : '';
    if (consent && !sms) { bookingNotice('Vérifiez le numéro du rappel SMS.', 'is-error'); return; }
    const payload = {...Object.fromEntries(new FormData(callerForm)), call_key: deskCallKey, formation: formation.value,
      event_type: deskBookingEvent, start_time: document.getElementById('deskSlot').value, sms,
      answers: Object.fromEntries([...directBooking.querySelectorAll('[data-answer]')].map(input => [input.dataset.answer, input.value]))};
    bookingPending = true;
    hostedBooking.hidden = true;
    openCallNotes.disabled = true;
    directBooking.inert = true;
    document.querySelectorAll('[name=wants_rdv]').forEach(input => { input.disabled = true; });
    bookingNotice('Réservation du créneau…');
    try {
      const response = await fetch('/api/secretariat/calendly/reserver', {method: 'POST', headers: deskJsonHeaders(), body: JSON.stringify(payload)});
      const data = await response.json();
      if (!response.ok || !data.appointment) {
        bookingNotice(data.error || 'La réservation n’a pas été confirmée.', 'is-error');
        if (data.reason === 'booking_unconfirmed') {
          bookingUncertain = true;
          directBooking.hidden = true;
        } else if (data.reason === 'calendly_rejected') {
          hostedBooking.hidden = false;
        }
        return;
      }
      nativeReceipt = calendlyAppointment = data.appointment;
      calendlyEvent = 'Rendez-vous réservé via Calendly';
      reviewRequest.disabled = false;
      openDeskBooking();
      lockBookingChoice();
    } catch {
      bookingUncertain = true;
      directBooking.hidden = true;
      bookingNotice('La réponse n’a pas été reçue. Vérifiez l’agenda avant de réserver à nouveau.', 'is-error');
    } finally {
      bookingPending = false;
      openCallNotes.disabled = false;
      directBooking.inert = false;
      hostedBooking.hidden = Boolean(nativeReceipt || bookingUncertain);
      document.querySelectorAll('[name=wants_rdv]').forEach(input => { input.disabled = Boolean(nativeReceipt); });
      updateProgress();
    }
  };
}
