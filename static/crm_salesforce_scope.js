(() => {
  if (!window.CRM_CONFIG?.is_admin) return;

  const button = document.querySelector('#salesforceImport');
  if (!button) return;

  button.textContent = '⇩ Importer Salesforce 2026';

  const formatNumber = value => new Intl.NumberFormat('fr-FR').format(Number(value || 0));

  const replaceText = root => {
    if (!root) return;
    const replacements = [
      [/Migration complète — toutes les années et toutes les formations/g, 'Pistes 2026 — périmètre validé, objectif 0 Nouveau'],
      [/Migration complète/g, 'Migration Salesforce 2026 — objectif 0 Nouveau'],
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

    const disqualifiedCount = Number(payload.skipped_disqualified || 0);
    const testCount = Number(payload.skipped_test || 0);
    const internalCount = Number(payload.skipped_internal || 0);
    const openWithoutFormationCount = Number(payload.skipped_open_without_formation || 0);
    const newCount = Number(payload.status_counts?.Nouveaux || 0);

    let excludedBanner = preview.querySelector('#salesforceExcludedSummary');
    const hasExclusions = Boolean(
      disqualifiedCount
      || testCount
      || internalCount
      || openWithoutFormationCount
    );
    if (!hasExclusions) {
      if (excludedBanner) excludedBanner.remove();
    } else {
      if (!excludedBanner) {
        excludedBanner = document.createElement('div');
        excludedBanner.id = 'salesforceExcludedSummary';
        excludedBanner.className = 'integration-banner success';
        excludedBanner.style.marginTop = '16px';
        const firstSummary = preview.querySelector('.integration-banner.success');
        if (firstSummary) firstSummary.insertAdjacentElement('afterend', excludedBanner);
        else preview.prepend(excludedBanner);
      }

      const labels = [];
      if (disqualifiedCount) {
        labels.push(`${formatNumber(disqualifiedCount)} disqualifiée${disqualifiedCount > 1 ? 's' : ''}`);
      }
      if (openWithoutFormationCount) {
        labels.push(`${formatNumber(openWithoutFormationCount)} Open - Not Contacted sans formation`);
      }
      if (internalCount) {
        labels.push(`${formatNumber(internalCount)} fiche interne Cassandre`);
      }
      if (testCount) {
        labels.push(`${formatNumber(testCount)} fiche TEST APS`);
      }
      excludedBanner.innerHTML = `
        <div>
          <b>Exclusions confirmées : ${labels.join(' · ')}</b>
          <span>Ces fiches ne seront jamais créées ni mises à jour dans le CRM. Les BTS et CAP restent également exclus et sont comptés dans « formations exclues ».</span>
        </div>`;
    }

    let statusBanner = preview.querySelector('#salesforceZeroNewSummary');
    if (!statusBanner) {
      statusBanner = document.createElement('div');
      statusBanner.id = 'salesforceZeroNewSummary';
      statusBanner.style.marginTop = '16px';
      const excluded = preview.querySelector('#salesforceExcludedSummary');
      if (excluded) excluded.insertAdjacentElement('afterend', statusBanner);
      else preview.prepend(statusBanner);
    }

    const confirm = document.querySelector('#salesforceConfirm');
    if (newCount === 0) {
      statusBanner.className = 'integration-banner success';
      statusBanner.innerHTML = `
        <div>
          <b>Objectif atteint : 0 fiche « Nouveau »</b>
          <span>Les dossiers Session FT, Def MOB, POEI et Financement FT en cours sont classés « A relancer » tout en conservant leur deuxième statut.</span>
        </div>`;
    } else {
      statusBanner.className = 'integration-banner warning';
      statusBanner.innerHTML = `
        <div>
          <b>Attention : ${formatNumber(newCount)} fiche${newCount > 1 ? 's' : ''} « Nouveau » reste${newCount > 1 ? 'nt' : ''}</b>
          <span>L’import est bloqué par sécurité. Vérifie le fichier ou le mapping avant de confirmer.</span>
        </div>`;
      if (confirm) confirm.disabled = true;
    }
  };

  // Le résultat JSON contient les décomptes exacts. Le script principal reste
  // inchangé ; on enrichit simplement son aperçu une fois la réponse reçue.
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
        firstBannerText.innerHTML = 'Le CRM conservera uniquement le périmètre validé : pistes 2026 non disqualifiées, avec une formation exploitable, hors fiches internes/de test, BTS et CAP. Les dossiers Session FT, Def MOB, POEI et Financement FT en cours seront classés <b>A relancer</b> avec leur deuxième statut.';
      }

      const fields = fileInput.closest('.fields');
      if (fields && !document.querySelector('#salesforceScopeNotice')) {
        const notice = document.createElement('div');
        notice.id = 'salesforceScopeNotice';
        notice.className = 'integration-banner success';
        notice.style.marginBottom = '18px';
        notice.innerHTML = `
          <div>
            <b>Périmètre verrouillé — objectif 0 Nouveau</b>
            <span>Les pistes « Open - Not Contacted » sans formation, la fiche interne Cassandre MENARD, TEST APS, les disqualifiés, BTS et CAP sont exclus. Les dossiers à deuxième statut restent dans « A relancer ».</span>
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
