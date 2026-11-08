(() => {
  const panel = document.getElementById('deskAgenda');
  const input = document.getElementById('agendaDay');
  const toggle = document.getElementById('agendaToggle'), body = document.getElementById('agendaPanel');
  const compact = window.matchMedia?.('(max-width: 800px)');
  function setExpanded(value) {
    toggle.setAttribute('aria-expanded', String(value));
    body.hidden = !value;
  }
  setExpanded(!compact?.matches);
  toggle.onclick = () => {
    setExpanded(body.hidden);
    if (input && !body.hidden) refreshVisible();
  };
  compact?.addEventListener('change', event => {
    setExpanded(!event.matches);
    if (input && !body.hidden) refreshVisible();
  });
  if (!input) return;
  const status = document.getElementById('agendaStatus'), list = document.getElementById('agendaList');
  const search = document.getElementById('agendaSearch');
  const todayButton = document.getElementById('agendaToday');
  const label = new Intl.DateTimeFormat('fr-FR', {weekday: 'short', day: 'numeric', month: 'long', timeZone: 'UTC'});
  const states = {a_venir: 'Prévu', sans_resultat: 'Résultat non renseigné', repondu: 'A répondu', sans_reponse: 'Sans réponse'};
  const lastRead = new Intl.DateTimeFormat('fr-FR', {hour:'2-digit', minute:'2-digit', timeZone:'Europe/Paris'});
  const crmLink = document.getElementById('agendaCrm');
  let today = panel.dataset.today, followToday = true, meetings = [], loadedDate = '', revision = 0, controller;

  function displayDay() {
    const day = new Date(input.value + 'T12:00:00Z');
    document.getElementById('agendaCaption').textContent = (input.value === today ? 'Aujourd’hui · ' : '') + label.format(day);
    todayButton.disabled = input.value === today;
    if (crmLink) {
      const url = new URL(crmLink.href);
      url.searchParams.set('date', input.value);
      crmLink.href = url.href;
    }
  }
  function render() {
    const term = normalizedSearch(search.value).replace(/(\d)\s*h\s*(\d{2})/g, '$1:$2');
    const visible = meetings.filter(m => normalizedSearch([m.person, m.time, m.host, m.subject].join(' ')).includes(term));
    list.replaceChildren();
    for (const meeting of visible) {
      const item = document.createElement('li'), time = document.createElement('time');
      const details = document.createElement('div'), person = document.createElement('strong');
      const subject = document.createElement('small'), result = document.createElement('span');
      time.textContent = meeting.time;
      person.textContent = meeting.person;
      subject.textContent = meeting.subject;
      const meta = document.createElement('div');
      meta.className = 'meeting-meta';
      meta.append(subject);
      if (meeting.host) {
        const host = document.createElement('span');
        host.className = 'meeting-host';
        host.textContent = meeting.host;
        meta.append(host);
      }
      result.className = 'meeting-state';
      result.dataset.state = meeting.state;
      result.textContent = states[meeting.state] || 'Résultat non renseigné';
      const heading = document.createElement('div');
      heading.className = 'meeting-heading';
      heading.append(person, result);
      details.append(heading, meta);
      item.append(time, details);
      list.append(item);
    }
    status.textContent = !meetings.length ? 'Aucun rendez-vous ce jour-là.'
      : term && !visible.length ? 'Aucun rendez-vous ne correspond à la recherche.'
      : (term ? visible.length + ' sur ' : '') + meetings.length + ' rendez-vous';
  }
  async function loadDay(serverToday = false) {
    const date = input.value;
    if (!date || !input.validity.valid) return;
    const current = ++revision;
    if (controller) controller.abort();
    controller = new AbortController();
    const pending = controller, timeout = setTimeout(() => pending.abort(), 15000);
    displayDay();
    if (loadedDate !== date) {
      meetings = [];
      loadedDate = '';
      list.replaceChildren();
      search.value = '';
      agendaSync.hidden = true;
      agendaSearchBox.hidden = true;
      status.textContent = 'Chargement des rendez-vous…';
    }
    list.setAttribute('aria-busy', 'true');
    agendaRefresh.disabled = true;
    agendaReconnect.hidden = true;
    try {
      const query = serverToday === true ? '' : '?date=' + encodeURIComponent(date);
      const response = await fetch('/api/secretariat/agenda' + query,
        {signal: controller.signal, cache: 'no-store', headers: {Accept: 'application/json'}});
      if (current !== revision) return;
      if (response.status === 401 || response.status === 403) {
        meetings = [];
        loadedDate = '';
        list.replaceChildren();
        agendaSearchBox.hidden = true;
        agendaReconnect.hidden = false;
        agendaSync.hidden = true;
        status.textContent = 'Reconnectez-vous pour consulter les rendez-vous.';
        return;
      }
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (current !== revision) return;
      if (data.date !== (serverToday === true ? data.today : date) || !Array.isArray(data.appointments)) throw new Error();
      if (input.value !== data.date) search.value = '';
      today = data.today;
      input.value = loadedDate = data.date;
      meetings = data.appointments;
      agendaSync.hidden = false;
      agendaSync.textContent = 'Consulté à ' + lastRead.format(new Date());
      agendaSync.title = data.last_sync ? 'Dernière synchronisation Calendly enregistrée le ' + data.last_sync : '';
      agendaSearchBox.hidden = meetings.length < 2;
      displayDay();
      render();
    } catch (err) {
      if (current === revision) status.textContent = loadedDate === date
        ? 'Actualisation indisponible. Derniers rendez-vous chargés ci-dessous.' : 'Agenda indisponible. Cliquez sur Actualiser pour réessayer.';
    } finally {
      clearTimeout(timeout);
      if (current === revision) {
        list.removeAttribute('aria-busy');
        agendaRefresh.disabled = false;
      }
    }
  }
  function choose(date) {
    if (!date) return;
    input.value = date;
    followToday = date === today;
    loadDay();
  }
  function shift(offset) {
    const day = new Date((input.value || today) + 'T12:00:00Z');
    day.setUTCDate(day.getUTCDate() + offset);
    choose(day.toISOString().slice(0, 10));
  }
  function refreshVisible() {
    if (body.hidden || document.hidden) return;
    loadDay(followToday);
  }
  agendaPrevious.onclick = () => shift(-1);
  agendaNext.onclick = () => shift(1);
  todayButton.onclick = () => { followToday = true; refreshVisible(); };
  input.onchange = () => choose(input.value || today);
  agendaRefresh.onclick = () => loadDay(followToday);
  search.oninput = render;
  document.addEventListener('visibilitychange', refreshVisible);
  window.setInterval(refreshVisible, 60000);
  if (!body.hidden) loadDay();
})();
