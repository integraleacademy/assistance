(() => {
  const currentContact = () => {
    const id = new URLSearchParams(window.location.search).get('fiche');
    if (!id) return null;
    try {
      return contacts.find(contact => String(contact.id) === String(id)) || null;
    } catch (_error) {
      return null;
    }
  };

  const safeUrl = value => {
    const raw = String(value || '').trim();
    if (!/^https?:\/\//i.test(raw)) return '';
    return raw;
  };

  const typeLabel = value => ({
    TextPost: 'Publication',
    LinkPost: 'Publication avec lien',
    ContentPost: 'Pièce jointe',
    TrackedChange: 'Modification Salesforce',
    MissingFeedItem: 'Commentaire historique',
  }[String(value || '')] || 'Historique Salesforce');

  const initials = name => String(name || 'SF')
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map(part => part[0])
    .join('')
    .toUpperCase() || 'SF';

  const commentHtml = comment => `
    <div style="display:grid;grid-template-columns:34px minmax(0,1fr);gap:10px;padding:12px 0;border-top:1px solid var(--line)">
      <span class="publication-comment-avatar" aria-hidden="true">${esc(initials(comment.author))}</span>
      <div>
        <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:baseline">
          <b>${esc(comment.author || 'Salesforce')}</b>
          <time style="font-size:12px;color:var(--muted)">${fmt(comment.date)}</time>
        </div>
        <p style="white-space:pre-wrap;margin:6px 0 0;line-height:1.55">${esc(comment.texte || '')}</p>
      </div>
    </div>`;

  const publicationHtml = publication => {
    const link = safeUrl(publication.salesforce_link_url);
    const comments = Array.isArray(publication.comments) ? publication.comments : [];
    return `
      <article class="card" style="box-shadow:none;padding:18px 20px">
        <header style="display:grid;grid-template-columns:42px minmax(0,1fr) auto;gap:12px;align-items:start">
          <span class="publication-avatar" aria-hidden="true">${esc(initials(publication.author))}</span>
          <div>
            <b>${esc(publication.author || 'Salesforce')}</b>
            <div style="display:flex;flex-wrap:wrap;gap:7px;margin-top:3px;color:var(--muted);font-size:12px">
              <time>${fmt(publication.date)}</time>
              <span>·</span>
              <span>${esc(typeLabel(publication.salesforce_type))}</span>
            </div>
          </div>
          <span class="badge">Salesforce</span>
        </header>
        <div style="white-space:pre-wrap;margin-top:14px;line-height:1.6;overflow-wrap:anywhere">${esc(publication.texte || '')}</div>
        ${link ? `<p style="margin:12px 0 0"><a class="btn" href="${esc(link)}" target="_blank" rel="noopener noreferrer">Ouvrir le lien Salesforce ↗</a></p>` : ''}
        ${comments.length ? `<div style="margin-top:14px;padding-left:14px;border-left:3px solid #dce7ff"><small style="display:block;margin-bottom:2px;color:var(--muted);font-weight:750">${comments.length} commentaire${comments.length > 1 ? 's' : ''}</small>${comments.map(commentHtml).join('')}</div>` : ''}
      </article>`;
  };

  const install = () => {
    const tabs = document.querySelector('.contact-subnav .wedof-tabs');
    const activityPanel = document.querySelector('#contactActivityPanel');
    if (!tabs || !activityPanel || document.querySelector('#contactSalesforceHistoryTab')) return;

    const contact = currentContact();
    const chatter = Array.isArray(contact?.salesforce_chatter)
      ? contact.salesforce_chatter
      : [];
    if (!contact || !chatter.length) return;

    const commentCount = chatter.reduce(
      (total, item) => total + (Array.isArray(item.comments) ? item.comments.length : 0),
      0
    );
    const tab = document.createElement('button');
    tab.className = 'wedof-tab';
    tab.id = 'contactSalesforceHistoryTab';
    tab.type = 'button';
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-selected', 'false');
    tab.setAttribute('aria-controls', 'contactSalesforceHistoryPanel');
    tab.innerHTML = `Historique Salesforce<span class="wedof-tab-count">${chatter.length}</span>`;

    const relanceTab = document.querySelector('#contactRelanceTab');
    if (relanceTab) tabs.insertBefore(tab, relanceTab);
    else tabs.append(tab);

    const panel = document.createElement('section');
    panel.className = 'wedof-panel';
    panel.id = 'contactSalesforceHistoryPanel';
    panel.hidden = true;
    panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('aria-labelledby', tab.id);
    panel.innerHTML = `
      <section class="card" style="padding:0;overflow:hidden">
        <div class="card-head" style="padding:20px 22px;border-bottom:1px solid var(--line)">
          <div>
            <h2>Historique Salesforce</h2>
            <small>${chatter.length} publication${chatter.length > 1 ? 's' : ''} · ${commentCount} commentaire${commentCount > 1 ? 's' : ''} importé${commentCount > 1 ? 's' : ''}</small>
          </div>
          <span class="badge">Lecture seule</span>
        </div>
        <div style="padding:16px 22px;border-bottom:1px solid var(--line);background:var(--soft)">
          <div class="workspace-search" style="max-width:none"><span>⌕</span><input id="salesforceHistorySearch" placeholder="Rechercher dans les publications et commentaires…"></div>
        </div>
        <div id="salesforceHistoryList" style="display:grid;gap:12px;padding:18px 22px"></div>
      </section>`;
    activityPanel.insertAdjacentElement('afterend', panel);

    const list = panel.querySelector('#salesforceHistoryList');
    const search = panel.querySelector('#salesforceHistorySearch');
    let limit = 80;

    const searchableText = publication => [
      publication.texte,
      publication.author,
      publication.salesforce_title,
      publication.salesforce_type,
      ...(publication.comments || []).flatMap(comment => [comment.texte, comment.author]),
    ].join(' ').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase('fr-FR');

    const renderList = () => {
      const query = String(search.value || '')
        .normalize('NFD')
        .replace(/[\u0300-\u036f]/g, '')
        .toLocaleLowerCase('fr-FR')
        .trim();
      const filtered = query
        ? chatter.filter(publication => searchableText(publication).includes(query))
        : chatter;
      const visible = filtered.slice(0, limit);
      list.innerHTML = visible.map(publicationHtml).join('') || '<div class="activity-empty">Aucun résultat dans l’historique Salesforce.</div>';
      if (filtered.length > visible.length) {
        const more = document.createElement('button');
        more.className = 'btn';
        more.type = 'button';
        more.textContent = `Afficher ${Math.min(80, filtered.length - visible.length)} publication(s) supplémentaire(s)`;
        more.onclick = () => {
          limit += 80;
          renderList();
        };
        list.append(more);
      }
    };

    search.addEventListener('input', () => {
      limit = 80;
      renderList();
    });
    renderList();

    const hideSalesforcePanel = () => {
      panel.hidden = true;
      tab.classList.remove('wedof-tab-active');
      tab.setAttribute('aria-selected', 'false');
    };

    [...tabs.querySelectorAll('.wedof-tab')]
      .filter(existing => existing !== tab)
      .forEach(existing => existing.addEventListener('click', hideSalesforcePanel));

    tab.onclick = () => {
      document.querySelectorAll('.contact-subnav .wedof-tab').forEach(existing => {
        existing.classList.remove('wedof-tab-active');
        existing.setAttribute('aria-selected', 'false');
      });
      document.querySelectorAll('.wedof-panel').forEach(existing => {
        existing.hidden = true;
      });
      tab.classList.add('wedof-tab-active');
      tab.setAttribute('aria-selected', 'true');
      panel.hidden = false;
      search.focus();
    };
  };

  const observer = new MutationObserver(install);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  install();
})();
