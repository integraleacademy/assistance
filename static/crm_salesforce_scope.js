(() => {
  if (!window.CRM_CONFIG?.is_admin) return;

  const button = document.querySelector('#salesforceImport');
  if (!button) return;

  button.textContent = '⇩ Importer Salesforce 2026';

  const replaceText = root => {
    if (!root) return;
    const replacements = [
      [/Migration complète — toutes les années et toutes les formations/g, 'Pistes créées en 2026 — BTS et CAP exclus'],
      [/Migration complète/g, 'Migration Salesforce 2026 — hors BTS et CAP'],
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
        firstBannerText.innerHTML = 'Exporte les <b>Pistes / Leads</b> avec leurs lignes de détail. Le CRM conservera uniquement celles créées en <b>2026</b> et écartera automatiquement toutes les formations <b>BTS</b> et <b>CAP</b>.';
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
            <span>Uniquement les pistes créées du 1er janvier au 31 décembre 2026. Les BTS et CAP ne seront jamais importés, même s'ils figurent dans le fichier.</span>
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
