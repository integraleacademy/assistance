(() => {
  if (!window.CRM_CONFIG?.is_admin) return;

  const button = document.querySelector('#salesforceImport');
  if (!button) return;

  button.textContent = '⇩ Importer Salesforce 2026';

  const formatNumber = value => new Intl.NumberFormat('fr-FR').format(Number(value || 0));

  const replaceText = root => {
    if (!root) return;
    const replacements = [
      [/Migration complète — toutes les années et toutes les formations/g, 'Pistes 2026 — disqualifiées, BTS et CAP exclus'],
      [/Migration complète/g, 'Migration Salesforce 2026 — hors disqualifiées, BTS et CAP'],
      [/hors 2025 ignorées/g, 'hors 2026 ignorées'],
      [/Ancien import — uniquement 2025 avec les exclusions historiques/g, 'Mode non disponible'],
    ];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode();
    while (node) {
      let value = node.nodeValue || '';
      replacements.forEach(([pattern, replacement]) => {
        value = value.replace(pattern, replacement);
      });
      node.nodeValue = value;
      node = walker.nextNode();
    }
  };

  const enhancePreview = payload => {
    const preview = document.querySelector('#salesforcePreview');
    if (!preview || !payload || typeof payload !== 'object') return;

    const count = Number(payload.skipped_disqualified || 0);
    let banner = preview.querySelector('#salesforceDisqualifiedSummary');
    if (!count) {
      if (banner) banner.remove();
      return;
    }

    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'salesforceDisqualifiedSummary';
      banner.className = 'integration-banner success';
      banner.style.marginTop = '16px';
      const firstSummary = preview.querySelector('.integration-banner.success');
      if (firstSummary) firstSummary.insertAdjacentElement('afterend', banner);
      else preview.prepend(banner);
    }
    banner.innerHTML = `
      <div>
        <b>${formatNumber(count)} piste${count > 1 ? 's' : ''} disqualifiée${count > 1 ? 's' : ''} exclue${count > 1 ? 's' : ''}</b>
        <span>Ces fiches ont été détectées dans le fichier Salesforce et ne seront jamais créées ni mises à jour dans le CRM.</span>
      </div>`;
  };

  // Le résultat JSON contient le décompte exact des disqualifiés. Le script
  // principal reste inchangé ; on enrichit simplement son aperçu une fois la
  // réponse reçue, sans consommer ni modifier le corps retourné à l'importeur.
  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const response = await nativeFetch(...args);
    const url = typeof args[0] === 'string' ? args[0] : String(args[0]?.url || '');
    if (url.includes('/api/crm/migrate-salesforce')) {
      response.clone().json().then(payload => {
        window.setTimeout(() => enhancePreview(payload), 0);
        window.setTimeout(() => enhancePreview(payload), 100);
      }).catch(() => {});
    }
    return response;
  };

  button.addEventListener('click', () => {
    window.setTimeout(() => {
      const modalRoot = document.querySelector('#modalRoot');
      const fileInput = document.querySelector('#salesforceFile');
      if (!modalRoot || !fileInput) return;

      const modeInput = document.querySelector('#salesforceMode');
      const fromInput = document.querySelector('#salesforceCreatedFrom');
      const toInput = document.querySelector('#salesforceCreatedTo');
      const preview = document.querySelector('#salesforcePreview');

      if (modeInput) {
        modeInput.value = 'complete';
        const modeField = modeInput.closest('.field');
        if (modeField) modeField.style.setProperty('display', 'none', 'important');
      }

      if (fromInput) {
        fromInput.value = '2026-01-01';
        const field = fromInput.closest('.field');
        if (field) field.style.setProperty('display', 'none', 'important');
      }
      if (toInput) {
        toInput.value = '2026-12-31';
        const field = toInput.closest('.field');
        if (field) field.style.setProperty('display', 'none', 'important');
      }

      const heading = modalRoot.querySelector('h2, h3');
      if (heading) heading.textContent = 'Importer les pistes Salesforce 2026';

      const firstBannerText = modalRoot.querySelector('.integration-banner span');
      if (firstBannerText) {
        firstBannerText.innerHTML = 'Exporte les <b>Pistes / Leads</b> avec leurs lignes de détail. Le CRM conservera uniquement celles créées en <b>2026</b> et écartera automatiquement les pistes <b>disqualifiées</b> ainsi que toutes les formations <b>BTS</b> et <b>CAP</b>.';
      }

      const fields = fileInput.closest('.fields');
      if (fields && !document.querySelector('#salesforceScopeNotice')) {
        const notice = document.createElement('div');
        notice.id = 'salesforceScopeNotice';
        notice.className = 'integration-banner success';
        notice.style.marginBottom = '18px';
        notice.innerHTML = `
          <div>
            <b>Périmètre verrouillé</b>
            <span>Uniquement les pistes créées du 1er janvier au 31 décembre 2026. Les pistes disqualifiées, les BTS et les CAP ne seront jamais importés, même s'ils figurent dans le fichier.</span>
          </div>`;
        fields.parentNode.insertBefore(notice, fields);
      }

      replaceText(modalRoot);
      if (preview) {
        const observer = new MutationObserver(() => replaceText(preview));
        observer.observe(preview, { childList: true, subtree: true });
      }
    }, 0);
  });
})();
