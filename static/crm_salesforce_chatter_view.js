(() => {
  const TAB_ID = 'contactSalesforceChatterTab';
  const PANEL_ID = 'contactSalesforceChatterPanel';
  let installedContactId = '';
  let refreshTimer = null;

  const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  })[character]);

  const contactIdFromUrl = () => new URLSearchParams(window.location.search).get('fiche') || '';
  const currentContact = () => {
    const id = contactIdFromUrl();
    if (!id) return null;
    try {
      return contacts.find(contact => String(contact.id) === String(id)) || null;
    } catch (_error) {
      return null;
    }
  };

  const formatDate = value => {
    if (!value) return 'Date inconnue';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat('fr-FR', {
      dateStyle: 'medium', timeStyle: 'short', timeZone: 'Europe/Paris',
    }).format(date);
  };

  const safeLink = value => {
    const raw = String(value || '').trim();
    if (!raw) return '';
    try {
      const url = new URL(raw);
      return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
    } catch (_error) {
      return '';
    }
  };

  const typeLabel = value => ({
    TextPost: 'Publication',
    LinkPost: 'Lien',
    ContentPost: 'Pièce jointe',
    TrackedChange: 'Modification Salesforce',
    CallLogPost: 'Compte rendu d’appel',
    AdvancedTextPost: 'Publication',
    MissingFeedItem: 'Commentaire Salesforce',
  })[value] || value || 'Historique Salesforce';

  const itemText = item => [
    item.texte,
    item.author,
    item.salesforce_type,
    item.salesforce_title,
    ...(item.comments || []).flatMap(comment => [comment.texte, comment.author]),
  ].join(' ').toLocaleLowerCase('fr-FR');

  const commentHtml = comment => `
    <div class="salesforce-chatter-comment">
      <div class="salesforce-chatter-comment-head">
        <b>${escapeHtml(comment.author || 'Salesforce')}</b>
        <time>${escapeHtml(formatDate(comment.date))}</time>
      </div>
      <p>${escapeHtml(comment.texte || '').replace(/\n/g, '<br>')}</p>
    </div>`;

  const publicationHtml = item => {
    const link = safeLink(item.salesforce_link_url);
    const title = String(item.salesforce_title || '').trim();
    const comments = Array.isArray(item.comments) ? item.comments : [];
    return `
      <article class="salesforce-chatter-item" data-salesforce-chatter-id="${escapeHtml(item.salesforce_feed_item_id || item.id || '')}">
        <header>
          <div>
            <b>${escapeHtml(item.author || 'Salesforce')}</b>
            <time>${escapeHtml(formatDate(item.date))}</time>
          </div>
          <span>${escapeHtml(typeLabel(item.salesforce_type))}</span>
        </header>
        <p class="salesforce-chatter-text">${escapeHtml(item.texte || '').replace(/\n/g, '<br>')}</p>
        ${title && item.salesforce_type === 'ContentPost' ? `<div class="salesforce-chatter-attachment">📎 ${escapeHtml(title)}<small>Le fichier physique n’était pas inclus dans l’export CSV.</small></div>` : ''}
        ${link ? `<a class="salesforce-chatter-link" href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">Ouvrir le lien Salesforce ↗</a>` : ''}
        ${comments.length ? `<section class="salesforce-chatter-comments"><h4>${comments.length} commentaire${comments.length > 1 ? 's' : ''}</h4>${comments.map(commentHtml).join('')}</section>` : ''}
      </article>`;
  };

  const styles = () => {
    if (document.querySelector('#salesforceChatterViewStyles')) return;
    const style = document.createElement('style');
    style.id = 'salesforceChatterViewStyles';
    style.textContent = `
      .salesforce-chatter-card{padding:0;overflow:hidden}
      .salesforce-chatter-head{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;padding:22px 24px;border-bottom:1px solid var(--border)}
      .salesforce-chatter-head h2{margin:0 0 4px}.salesforce-chatter-head small{color:var(--muted)}
      .salesforce-chatter-search{min-width:280px;max-width:430px;width:40%;padding:11px 14px;border:1px solid var(--border);border-radius:12px;background:var(--surface);color:inherit}
      .salesforce-chatter-summary{display:flex;gap:10px;flex-wrap:wrap;padding:16px 24px;background:var(--surface-soft)}
      .salesforce-chatter-summary span{border:1px solid var(--border);border-radius:999px;padding:7px 11px;background:var(--surface);font-size:13px;font-weight:700}
      .salesforce-chatter-list{display:grid;gap:14px;padding:20px 24px}
      .salesforce-chatter-item{border:1px solid var(--border);border-radius:15px;padding:17px 18px;background:var(--surface)}
      .salesforce-chatter-item>header{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;margin-bottom:12px}
      .salesforce-chatter-item>header div{display:grid;gap:3px}.salesforce-chatter-item time{font-size:12px;color:var(--muted)}
      .salesforce-chatter-item>header>span{font-size:12px;font-weight:750;border-radius:999px;padding:6px 9px;background:#eef4ff;color:#2754b7}
      .salesforce-chatter-text{margin:0;line-height:1.55;overflow-wrap:anywhere}
      .salesforce-chatter-comments{margin-top:15px;padding-top:14px;border-top:1px solid var(--border)}
      .salesforce-chatter-comments h4{margin:0 0 10px;font-size:13px;color:var(--muted)}
      .salesforce-chatter-comment{margin-top:9px;padding:11px 13px;border-radius:12px;background:var(--surface-soft)}
      .salesforce-chatter-comment-head{display:flex;justify-content:space-between;gap:10px}.salesforce-chatter-comment p{margin:7px 0 0;line-height:1.5}
      .salesforce-chatter-attachment{display:grid;gap:3px;margin-top:13px;padding:11px 13px;border-radius:12px;background:var(--surface-soft);font-weight:700}
      .salesforce-chatter-attachment small{font-weight:400;color:var(--muted)}
      .salesforce-chatter-link{display:inline-flex;margin-top:12px;font-weight:700;color:var(--primary);text-decoration:none}
      .salesforce-chatter-empty{padding:42px 24px;text-align:center;color:var(--muted)}
      @media(max-width:760px){.salesforce-chatter-head{display:grid}.salesforce-chatter-search{width:100%;min-width:0}.salesforce-chatter-list{padding:16px}.salesforce-chatter-item>header{display:grid}}
    `;
    document.head.append(style);
  };

  const renderPanel = (contact, panel) => {
    const items = Array.isArray(contact.salesforce_chatter)
      ? [...contact.salesforce_chatter].sort((left, right) => new Date(right.date || 0) - new Date(left.date || 0))
      : [];
    const commentCount = items.reduce((total, item) => total + (Array.isArray(item.comments) ? item.comments.length : 0), 0);
    panel.innerHTML = `<section class="card salesforce-chatter-card">
      <div class="salesforce-chatter-head">
        <div><h2>Historique Salesforce</h2><small>Publications et commentaires importés depuis le fil Chatter. Lecture seule.</small></div>
        <input class="salesforce-chatter-search" id="salesforceChatterSearch" type="search" placeholder="Rechercher dans l’historique…">
      </div>
      <div class="salesforce-chatter-summary"><span>${items.length} publication${items.length > 1 ? 's' : ''}</span><span>${commentCount} commentaire${commentCount > 1 ? 's' : ''}</span></div>
      <div class="salesforce-chatter-list" id="salesforceChatterList">${items.length ? items.map(publicationHtml).join('') : '<div class="salesforce-chatter-empty">Aucun historique Salesforce importé pour cette fiche.</div>'}</div>
    </section>`;

    const search = panel.querySelector('#salesforceChatterSearch');
    const list = panel.querySelector('#salesforceChatterList');
    search?.addEventListener('input', () => {
      const query = search.value.trim().toLocaleLowerCase('fr-FR');
      const filtered = query ? items.filter(item => itemText(item).includes(query)) : items;
      list.innerHTML = filtered.length
        ? filtered.map(publicationHtml).join('')
        : '<div class="salesforce-chatter-empty">Aucun résultat dans cet historique.</div>';
    });
  };

  const openPanel = (tab, panel) => {
    document.querySelectorAll('.wedof-tab').forEach(button => {
      button.classList.remove('wedof-tab-active');
      button.setAttribute('aria-selected', 'false');
    });
    document.querySelectorAll('.wedof-panel').forEach(section => { section.hidden = true; });
    tab.classList.add('wedof-tab-active');
    tab.setAttribute('aria-selected', 'true');
    panel.hidden = false;
  };

  const install = () => {
    const contact = currentContact();
    const activityTab = document.querySelector('#contactActivityTab');
    const activityPanel = document.querySelector('#contactActivityPanel');
    const tabs = activityTab?.closest('.wedof-tabs');
    if (!contact || !activityTab || !activityPanel || !tabs) return;

    const id = String(contact.id || '');
    if (installedContactId && installedContactId !== id) {
      document.querySelector(`#${TAB_ID}`)?.remove();
      document.querySelector(`#${PANEL_ID}`)?.remove();
      installedContactId = '';
    }

    let tab = document.querySelector(`#${TAB_ID}`);
    let panel = document.querySelector(`#${PANEL_ID}`);
    const count = Array.isArray(contact.salesforce_chatter) ? contact.salesforce_chatter.length : 0;

    if (!tab) {
      tab = document.createElement('button');
      tab.type = 'button';
      tab.className = 'wedof-tab';
      tab.id = TAB_ID;
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', 'false');
      tab.setAttribute('aria-controls', PANEL_ID);
      activityTab.insertAdjacentElement('afterend', tab);
    }
    tab.innerHTML = `Historique Salesforce<span class="wedof-tab-count">${count}</span>`;

    if (!panel) {
      panel = document.createElement('section');
      panel.className = 'wedof-panel';
      panel.id = PANEL_ID;
      panel.setAttribute('role', 'tabpanel');
      panel.setAttribute('aria-labelledby', TAB_ID);
      panel.hidden = true;
      activityPanel.insertAdjacentElement('afterend', panel);
    }
    renderPanel(contact, panel);
    tab.onclick = () => openPanel(tab, panel);

    document.querySelectorAll('.wedof-tab:not(#contactSalesforceChatterTab)').forEach(other => {
      if (other.dataset.salesforceChatterBound === '1') return;
      other.dataset.salesforceChatterBound = '1';
      other.addEventListener('click', () => {
        panel.hidden = true;
        tab.classList.remove('wedof-tab-active');
        tab.setAttribute('aria-selected', 'false');
      });
    });

    installedContactId = id;
    styles();
  };

  const scheduleInstall = () => {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(install, 40);
  };

  const observer = new MutationObserver(scheduleInstall);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener('popstate', scheduleInstall);
  scheduleInstall();
})();
