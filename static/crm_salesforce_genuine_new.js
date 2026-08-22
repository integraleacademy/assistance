(() => {
  if (!window.CRM_CONFIG?.is_admin) return;

  const formatNumber = value => new Intl.NumberFormat('fr-FR').format(Number(value || 0));
  const fold = value => String(value || '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLocaleLowerCase('fr-FR')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
  const genuineNewSources = new Set(['nouveau', 'nouveaux', 'new']);

  const replaceScopeWording = root => {
    if (!root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode();
    while (node) {
      const current = node.nodeValue || '';
      node.nodeValue = current
        .replace(/objectif 0 Nouveau/gi, 'statuts Nouveau contrôlés')
        .replace(/Objectif atteint : 0 fiche « Nouveau »/g, 'Aucun statut Nouveau inattendu');
      node = walker.nextNode();
    }
  };

  const inferredGenuineCount = payload => {
    const explicit = Number(payload.genuine_new_count);
    if (Number.isFinite(explicit)) return explicit;
    return Object.entries(payload.new_status_source_counts || {})
      .filter(([label]) => genuineNewSources.has(fold(label)))
      .reduce((total, [, count]) => total + Number(count || 0), 0);
  };

  const inferredUnexpectedCount = (payload, newCount, genuineCount) => {
    const explicit = Number(payload.unexpected_new_count);
    if (Number.isFinite(explicit)) return explicit;
    return Math.max(0, newCount - genuineCount);
  };

  const applyGenuineNewDecision = payload => {
    if (!payload || typeof payload !== 'object' || payload.dry_run !== true) return;

    const preview = document.querySelector('#salesforcePreview');
    const confirm = document.querySelector('#salesforceConfirm');
    if (!preview || !confirm) return;

    replaceScopeWording(document.querySelector('#modalRoot'));

    const newCount = Number(payload.status_counts?.Nouveaux || 0);
    const genuineCount = inferredGenuineCount(payload);
    const unexpectedCount = inferredUnexpectedCount(payload, newCount, genuineCount);
    const importable = Number(payload.created || 0)
      + Number(payload.updated || 0)
      + Number(payload.unchanged || 0);
    const hasPreviewToken = Boolean(payload.preview_token);
    let banner = preview.querySelector('#salesforceZeroNewSummary');

    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'salesforceZeroNewSummary';
      banner.style.marginTop = '16px';
      preview.prepend(banner);
    }

    if (newCount > 0 && unexpectedCount === 0 && genuineCount === newCount) {
      const samples = (payload.genuine_new_samples || [])
        .slice(0, 8)
        .map(item => item.nom || item.salesforce_id)
        .filter(Boolean);
      banner.className = 'integration-banner success';
      banner.innerHTML = `
        <div>
          <b>${formatNumber(genuineCount)} vraie${genuineCount > 1 ? 's' : ''} piste${genuineCount > 1 ? 's' : ''} Salesforce au statut « Nouveau » sera${genuineCount > 1 ? 'ont' : ''} importée${genuineCount > 1 ? 's' : ''}</b>
          <span>Le statut source a été reconnu explicitement. ${samples.length ? `Personnes concernées : ${samples.join(', ')}.` : ''} Les statuts vides ou inconnus continuent d’être bloqués.</span>
        </div>`;
      confirm.disabled = importable === 0 || !hasPreviewToken;
      const mergeMode = document.querySelector('#salesforceMergeMode');
      confirm.textContent = mergeMode?.value === 'salesforce'
        ? 'Corriger et importer les fiches'
        : 'Importer les pistes';
      return;
    }

    if (unexpectedCount > 0) {
      const samples = (payload.unexpected_new_samples || [])
        .slice(0, 8)
        .map(item => `${item.nom || item.salesforce_id || 'Piste sans nom'} (${item.source_status || 'statut vide'})`)
        .join(', ');
      banner.className = 'integration-banner warning';
      banner.innerHTML = `
        <div>
          <b>Attention : ${formatNumber(unexpectedCount)} statut${unexpectedCount > 1 ? 's' : ''} non reconnu${unexpectedCount > 1 ? 's' : ''} abouti${unexpectedCount > 1 ? 'ssent' : 't'} à « Nouveau »</b>
          <span>L’import reste bloqué pour éviter un mauvais mapping. ${samples ? `À vérifier : ${samples}.` : ''}</span>
        </div>`;
      confirm.disabled = true;
    }
  };

  const previousFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const response = await previousFetch(...args);
    const url = typeof args[0] === 'string' ? args[0] : String(args[0]?.url || '');
    if (url.includes('/api/crm/migrate-salesforce')) {
      response.clone().json().then(payload => {
        window.setTimeout(() => applyGenuineNewDecision(payload), 180);
        window.setTimeout(() => applyGenuineNewDecision(payload), 450);
      }).catch(() => {});
    }
    return response;
  };

  const modalObserver = new MutationObserver(() => replaceScopeWording(document.querySelector('#modalRoot')));
  modalObserver.observe(document.documentElement, { childList: true, subtree: true });
})();
