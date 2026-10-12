/* The native CRM sheet uses the external account's dedicated, scoped API only. */
(function () {
  'use strict';
  const config = window.CRM_CONFIG;
  const editable = new Set(config.external_edit_fields);
  const details = new Map();
  const formStates = new WeakMap();
  let writes = Promise.resolve();
  let leaving = false;
  let booking = null;

  async function request(path, payload, timeout = 20000, method = payload ? 'PATCH' : 'GET') {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(path, {
        method, credentials: 'same-origin', cache: 'no-store',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': config.external_csrf},
        signal: controller.signal, ...(payload ? {body: JSON.stringify(payload)} : {}),
      });
      if (response.status === 401 || response.redirected) {
        location.replace('/login?next=/crm/pistes');
        throw new Error('Veuillez vous reconnecter.');
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(data.error || 'Impossible d’effectuer cette opération.');
        error.status = response.status;
        throw error;
      }
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('Le serveur met trop de temps à répondre. Réessayez.');
      throw error;
    } finally { clearTimeout(timer); }
  }

  function values(form) {
    const data = Object.fromEntries(new FormData(form));
    if ('telephone' in data) data.telephone = data.telephone.replace(/[^+\d]/g, '');
    return data;
  }

  function captureForm(form) { formStates.set(form, values(form)); }

  function formPayload(form, data = values(form)) {
    const saved = formStates.get(form) || {};
    return Object.fromEntries(Object.entries(data).filter(([key, value]) => editable.has(key) && value !== (saved[key] ?? '')));
  }

  function patch(id, changes) {
    const form = document.querySelector('#contactForm');
    // Include the last keystroke when a timeline button is clicked before the
    // native form's autosave debounce. Serialize writes before losing access.
    const draft = form && new URLSearchParams(location.search).get('fiche') === id ? formPayload(form) : {};
    const payload = {...draft, ...changes};
    const operation = writes.then(async () => {
      if (leaving) return details.get(id) || {id};
      if (!Object.keys(payload).length) return details.get(id) || {id};
      const result = await request(`/api/crm/external-meta/${encodeURIComponent(id)}`, payload);
      if (form) {
        const saved = formStates.get(form) || {};
        for (const [key, value] of Object.entries(payload)) if (editable.has(key)) saved[key] = value;
        formStates.set(form, saved);
      }
      if (!result.in_scope) {
        leaving = true;
        details.clear();
        location.replace('/crm/pistes');
        return {id, ...payload};
      }
      details.set(id, result.contact);
      return result.contact;
    });
    writes = operation.catch(() => {});
    return operation;
  }

  async function api(url, options = {}) {
    const parsed = new URL(url, location.origin), path = parsed.pathname;
    const method = (options.method || 'GET').toUpperCase();
    const body = options.body ? JSON.parse(options.body) : {};
    if (path === '/api/crm/bootstrap' && method === 'GET') {
      const data = await request('/api/crm/external-meta');
      return {...data, contacts: data.contacts.map(contact => ({...contact, _summary: true}))};
    }
    if (method === 'GET' && ['/api/crm/calendly/event-types', '/api/crm/calendly/availability'].includes(path)) {
      const id = new URLSearchParams(location.search).get('fiche');
      if (!id) throw new Error('Ouvrez d’abord la fiche de la personne.');
      const operation = path.split('/').pop();
      return request(`/api/crm/external-meta/${encodeURIComponent(id)}/calendly/${operation}${parsed.search}`, null, 60000);
    }
    const appointment = path.match(/^\/api\/crm\/contacts\/([^/]+)\/calendly\/appointments$/);
    if (appointment && method === 'POST') {
      if (booking) return booking;
      const id = decodeURIComponent(appointment[1]);
      booking = (async () => {
        await patch(id, {});
        const operation = writes.then(async () => {
          if (leaving) throw new Error('Cette piste n’est plus disponible dans Nouveaux META.');
          const result = await request(`/api/crm/external-meta/${encodeURIComponent(id)}/calendly/appointments`, body, 180000, 'POST');
          if (result.in_scope) details.set(id, result.contact);
          else { leaving = true; details.clear(); }
          return result;
        });
        writes = operation.catch(() => {});
        return operation;
      })().finally(() => { booking = null; });
      return booking;
    }
    const match = path.match(/^\/api\/crm\/contacts\/([^/]+)(\/appel)?$/);
    if (match && match[1] !== 'bulk') {
      const id = decodeURIComponent(match[1]);
      if (!match[2] && method === 'GET') {
        const contact = await request(`/api/crm/external-meta/${encodeURIComponent(id)}`, null, options.timeout);
        details.set(id, contact);
        return contact;
      }
      if (!match[2] && method === 'PATCH') return patch(id, body);
      if (match[2] === '/appel' && method === 'POST') return patch(id, {call_note: body.commentaire || ''});
    }
    // The native disqualification dialog uses a bulk URL, but this bridge only
    // accepts its single selected record and never calls the general bulk API.
    if (path === '/api/crm/contacts/bulk' && method === 'PATCH' && body.action === 'disqualify' && body.ids?.length === 1) {
      const contact = await patch(String(body.ids[0]), {
        statut: 'Disqualifié', disqualification_reason: body.reason || '', disqualification_detail: body.detail || '',
      });
      return {updated: [contact], count: 1};
    }
    const error = new Error('Cette action est réservée à l’équipe commerciale.');
    error.status = 403;
    throw error;
  }

  function prepareForm(contact) {
    restrictControls();
    // Preserve the existing follow-up comment in the same CRM form/autosave.
    const fields = document.querySelector('#contactForm .form-card .fields');
    const section = document.createElement('div');
    section.className = 'form-section external-followup-section';
    const label = document.createElement('label');
    label.htmlFor = 'externalFollowupComment';
    label.textContent = 'Commentaire de suivi';
    const textarea = document.createElement('textarea');
    textarea.id = 'externalFollowupComment';
    textarea.name = 'commentaires';
    textarea.maxLength = 5000;
    textarea.value = contact.commentaires || '';
    section.append(label, textarea);
    fields.append(section);
  }

  function restrictControls() {
    document.querySelectorAll('#contactForm [name], [form="contactForm"]').forEach(field => {
      if (!editable.has(field.name)) {
        field.disabled = true;
        field.closest('.field')?.setAttribute('hidden', '');
      }
    });
  }

  async function refresh() {
    if (leaving || booking) return;
    try {
      const data = await request('/api/crm/external-meta');
      const id = new URLSearchParams(location.search).get('fiche');
      if (id && !data.contacts.some(contact => String(contact.id) === id)) {
        leaving = true;
        details.clear();
        location.replace('/crm/pistes');
      }
    } catch (_) { /* A failed refresh must not interrupt typing. */ }
  }

  function finishBooking(result) {
    if (result.in_scope !== false) return false;
    sessionStorage.setItem('crm-external-booking-confirmed', '1');
    location.replace('/crm/pistes');
    return true;
  }

  window.CRMExternalMeta = {api, captureForm, formPayload, prepareForm, restrictControls, refresh,
    flush: id => patch(id, {}), finishBooking};
})();
