(() => {
  const STYLE_ID = 'salesforceChatterHistoryStyles';
  const TAB_ID = 'contactSalesforceHistoryTab';
  const PANEL_ID = 'contactSalesforceHistoryPanel';
  const cache = new Map();
  const pending = new Map();

  const installStyles = () => {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = `
      .salesforce-history-card{display:grid;gap:12px;padding:18px 20px;border:1px solid var(--border);border-radius:16px;background:var(--surface,#fff)}
      .salesforce-history-head{display:flex;justify-content:space-between;gap:18px;align-items:flex-start}
      .salesforce-history-author{display:flex;gap:11px;align-items:center;min-width:0}
      .salesforce-history-avatar{display:grid;place-items:center;width:36px;height:36px;border-radius:12px;background:#eaf1ff;color:#2456cc;font-weight:800;flex:0 0 auto}
      .salesforce-history-author b{display:block}.salesforce-history-author small{display:block;color:var(--muted);margin-top:2px}
      .salesforce-history-type{padding:5px 9px;border-radius:999px;background:#eef3ff;color:#3557a8;font-size:12px;font-weight:750;white-space:nowrap}
      .salesforce-history-text{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.55;margin:0;color:var(--text)}
      .salesforce-history-link{display:inline-flex;align-items:center;gap:7px;width:fit-content;max-width:100%;overflow-wrap:anywhere}
      .salesforce-history-comments{display:grid;gap:9px;margin-top:2px;padding-left:22px;border-left:3px solid #e7edfa}
      .salesforce-history-comment{padding:11px 13px;border-radius:12px;background:#f7f9fd}
      .salesforce-history-comment header{display:flex;justify-content:space-between;gap:12px;margin-bottom:6px}
      .salesforce-history-comment p{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;line-height:1.45}
      .salesforce-history-toolbar{display:flex;gap:10px;align-items:center;margin-bottom:14px}
      .salesforce-history-toolbar input{flex:1;min-width:0}
      .salesforce-history-list{display:grid;gap:13px}
      @media(max-width:700px){.salesforce-history-head{display:grid}.salesforce-history-type{width:fit-content}.salesforce-history-comments{padding-left:12px}}
    `;
    document.head.append(style);
  };

  const currentContactId = () => new URLSearchParams(window.location.search).get('fiche') || '';

  const removeInstalled = () => {
    document.getElementById(TAB_ID)?.remove();
    document.getElementById(PANEL_ID)?.remove();
  };

  const initials = name => String(name || 'SF')
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map(part => part[0])
    .join('')
    .toLocaleUpperCase('fr-FR') || 'SF';

  const formatDate = value => {
    const date = new Date(value || '');
    if (Number.isNaN(date.valueOf())) return value || 'Date inconnue';
    return new Intl.DateTimeFormat('fr-FR', {
      dateStyle: 'medium',
      timeStyle: 'short',
      timeZone: 'Europe/Paris',
    }).format(date);
  };

  const typeLabel = type => ({
    TextPost: 'Publication',
    LinkPost: 'Lien',
    ContentPost: 'Pièce jointe',
    TrackedChange: 'Modification',
    MissingFeedItem: 'Commentaire isolé',
  }[type] || type || 'Salesforce');

  const safeLink = value => {
    try {
      const url = new URL(String(value || ''));
      return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
    } catch (_error) {
      return '';
    }
  };

  const normalized = value => String(value || '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLocaleLowerCase('fr-FR');

  const searchableText = item => [
    item.author,
    item.texte,
    item.salesforce_title,
    item.salesforce_type,
    ...(item.comments || []).flatMap(comment => [comment.author, comment.texte]),
  ].map(normalized).join(' ');

  const commentHtml = comment => `
    <article class="salesforce-history-comment">
      <header><b>${esc(comment.author || 'Salesforce')}</b><small>${esc(formatDate(comment.date))}</small></header>
      <p>${esc(comment.texte || '')}</p>
    </article>`;

  const itemHtml = item => {
    const link = safeLink(item.salesforce_link_url);
    const comments = Array.isArray(item.comments) ? item.comments : [];
    return `
      <article class="salesforce-history-card">
        <div class="salesforce-history-head">
          <div class="salesforce-history-author">
            <span class="salesforce-history-avatar" aria-hidden="true">${esc(initials(item.author))}</span>
            <div><b>${esc(item.author || 'Salesforce')}</b><small>${esc(formatDate(item.date))}</small></div>
          </div>
          <span class="salesforce-history-type">${esc(typeLabel(item.salesforce_type))}</span>
        </div>
        <p class="salesforce-history-text">${esc(item.texte || '')}</p>
        ${link ? `<a class="salesforce-history-link" href="${esc(link)}" target="_blank" rel="noopener noreferrer">↗ ${esc(item.salesforce_title || link)}</a>` : ''}
        ${item.salesforce_has_content && item.salesforce_title ? `<small>Pièce jointe d’origine : ${esc(item.salesforce_title)} — le fichier physique n’est pas inclus dans l’export CSV.</small>` : ''}
        ${comments.length ? `<div class="salesforce-history-comments"><b>${comments.length} commentaire${comments.length > 1 ? 's' : ''}</b>${comments.map(commentHtml).join('')}</div>` : ''}
      </article>`;
  };

  const renderList = (root, items, query = '') => {
    const needle = normalized(query).trim();
    const filtered = needle
      ? items.filter(item => searchableText(item).includes(needle))
      : items;
    root.innerHTML = filtered.length
      ? filtered.map(itemHtml).join('')
      : '<div class="activity-empty">Aucun élément Salesforce ne correspond à cette recherche.</div>';
    const counter = document.querySelector('#salesforceHistorySearchCount');
    if (counter) counter.textContent = `${filtered.length} sur ${items.length}`;
  };

  const showPanel = (tab, panel) => {
    document.querySelectorAll('.wedof-tab').forEach(button => {
      button.classList.toggle('wedof-tab-active', button === tab);
      button.setAttribute('aria-selected', button === tab ? 'true' : 'false');
    });
    document.querySelectorAll('.wedof-panel').forEach(candidate => {
      candidate.hidden = candidate !== panel;
    });
    panel.hidden = false;
  };

  const loadHistory = contactId => {
    if (cache.has(contactId)) return Promise.resolve(cache.get(contactId));
    if (pending.has(contactId)) return pending.get(contactId);

    const request = fetch(`/api/crm/contacts/${encodeURIComponent(contactId)}/salesforce-chatter`, {
      credentials: 'same-origin',
    }).then(async response => {
      if (response.status === 404) return { items: [], publication_count: 0, comment_count: 0 };
      if (!response.ok) throw new Error(`Historique Salesforce indisponible (HTTP ${response.status}).`);
      const payload = await response.json();
      cache.set(contactId, payload);
      return payload;
    }).finally(() => pending.delete(contactId));

    pending.set(contactId, request);
    return request;
  };

  const renderHistoryTab = (contactId, payload) => {
    if (currentContactId() !== contactId) return;
    const nav = document.querySelector('.contact-subnav .wedof-tabs');
    const activityPanel = document.querySelector('#contactActivityPanel');
    if (!nav || !activityPanel) return;

    const items = Array.isArray(payload?.items)
      ? [...payload.items].sort((a, b) => new Date(b.date || 0) - new Date(a.date || 0))
      : [];
    if (!items.length) {
      removeInstalled();
      return;
    }

    const existing = document.getElementById(TAB_ID);
    if (existing?.dataset.contactId === contactId) return;
    removeInstalled();
    installStyles();

    const commentCount = Number(payload.comment_count || items.reduce(
      (total, item) => total + (item.comments || []).length,
      0,
    ));
    const tab = document.createElement('button');
    tab.className = 'wedof-tab';
    tab.id = TAB_ID;
    tab.type = 'button';
    tab.dataset.contactId = contactId;
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-selected', 'false');
    tab.setAttribute('aria-controls', PANEL_ID);
    tab.innerHTML = `Historique Salesforce <span class="wedof-tab-count">${items.length}</span>`;

    const activityTab = document.querySelector('#contactActivityTab');
    if (activityTab) activityTab.insertAdjacentElement('afterend', tab);
    else nav.append(tab);

    const panel = document.createElement('section');
    panel.className = 'wedof-panel';
    panel.id = PANEL_ID;
    panel.dataset.contactId = contactId;
    panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('aria-labelledby', TAB_ID);
    panel.hidden = true;
    panel.innerHTML = `
      <section class="card activity-card">
        <div class="card-head">
          <div><h2>Historique Salesforce</h2><small>${items.length} publication${items.length > 1 ? 's' : ''} · ${commentCount} commentaire${commentCount > 1 ? 's' : ''}</small></div>
        </div>
        <div class="salesforce-history-toolbar">
          <input id="salesforceHistorySearch" type="search" placeholder="Rechercher dans l’historique Salesforce…">
          <small id="salesforceHistorySearchCount">${items.length} sur ${items.length}</small>
        </div>
        <div class="salesforce-history-list" id="salesforceHistoryList"></div>
      </section>`;
    activityPanel.insertAdjacentElement('afterend', panel);

    const list = panel.querySelector('#salesforceHistoryList');
    const search = panel.querySelector('#salesforceHistorySearch');
    renderList(list, items);
    search.addEventListener('input', () => renderList(list, items, search.value));

    tab.addEventListener('click', event => {
      event.preventDefault();
      showPanel(tab, panel);
    });
    nav.addEventListener('click', event => {
      const selected = event.target.closest('.wedof-tab');
      if (selected && selected !== tab) {
        panel.hidden = true;
        tab.classList.remove('wedof-tab-active');
        tab.setAttribute('aria-selected', 'false');
      }
    }, true);
  };

  const install = () => {
    const contactId = currentContactId();
    const nav = document.querySelector('.contact-subnav .wedof-tabs');
    const activityPanel = document.querySelector('#contactActivityPanel');
    if (!contactId || !nav || !activityPanel) {
      if (!contactId) removeInstalled();
      return;
    }

    const existing = document.getElementById(TAB_ID);
    if (existing && existing.dataset.contactId !== contactId) removeInstalled();
    if (document.getElementById(TAB_ID)?.dataset.contactId === contactId) return;

    loadHistory(contactId)
      .then(payload => renderHistoryTab(contactId, payload))
      .catch(error => console.error(error));
  };

  window.CRMRefreshSalesforceChatterHistory = contactId => {
    const id = String(contactId || currentContactId());
    if (id) cache.delete(id);
    removeInstalled();
    install();
  };

  const observer = new MutationObserver(install);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  install();
})();
