(() => {
  if (!window.CRM_CONFIG?.is_admin) return;

  const normalizeNumber = value => new Intl.NumberFormat('fr-FR').format(Number(value || 0));
  const importButton = document.createElement('button');
  importButton.className = 'primary';
  importButton.id = 'salesforceImport';
  importButton.type = 'button';
  importButton.textContent = '⇩ Importer Salesforce';
  importButton.style.background = '#eef3ff';
  importButton.style.color = '#153565';
  importButton.style.border = '1px solid #cad8f5';
  document.querySelector('#newContact')?.before(importButton);

  async function sendFile(file, options, dryRun) {
    const body = new FormData();
    body.append('file', file, file.name);
    body.append('include_converted', options.includeConverted ? '1' : '0');
    body.append('deduplicate', options.deduplicate ? '1' : '0');
    body.append('dry_run', dryRun ? '1' : '0');
    const response = await fetch('/api/crm/import-salesforce', {
      method: 'POST',
      body,
      credentials: 'same-origin',
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "L'import Salesforce a échoué.");
    return payload;
  }

  function summaryHtml(result, final = false) {
    const statusRows = Object.entries(result.status_counts || {})
      .map(([label, count]) => `<span><b>${normalizeNumber(count)}</b> ${esc(label)}</span>`)
      .join('');
    const formationRows = Object.entries(result.formation_counts || {}).slice(0, 10)
      .map(([label, count]) => `<span><b>${normalizeNumber(count)}</b> ${esc(label)}</span>`)
      .join('');
    return `
      <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:4px 0 18px">
        <div class="stat" style="min-height:auto"><small>${final ? 'Créées' : 'À créer'}</small><strong>${normalizeNumber(result.created)}</strong></div>
        <div class="stat" style="min-height:auto"><small>${final ? 'Mises à jour' : 'À mettre à jour'}</small><strong>${normalizeNumber(result.updated)}</strong></div>
        <div class="stat" style="min-height:auto"><small>Sans changement</small><strong>${normalizeNumber(result.unchanged)}</strong></div>
      </div>
      <div class="integration-banner success"><div><b>${normalizeNumber(result.prepared_rows)} pistes prêtes</b><span>${normalizeNumber(result.csv_rows)} lignes lues · ${normalizeNumber(result.duplicates_in_file)} doublons internes regroupés${result.skipped_converted ? ` · ${normalizeNumber(result.skipped_converted)} converties exclues` : ''}</span></div></div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px">
        <section class="card" style="box-shadow:none"><div class="card-head"><h3>Statuts</h3></div><div class="template-list" style="padding:12px;display:grid;gap:8px">${statusRows || '<span>Aucun statut</span>'}</div></section>
        <section class="card" style="box-shadow:none"><div class="card-head"><h3>Formations</h3></div><div class="template-list" style="padding:12px;display:grid;gap:8px">${formationRows || '<span>Non renseignées</span>'}</div></section>
      </div>`;
  }

  importButton.onclick = () => {
    modal(
      'Importer les pistes Salesforce',
      `<div class="fields">
        <div class="field full">
          <label>Fichier Salesforce au format CSV</label>
          <input id="salesforceFile" type="file" accept=".csv,text/csv">
          <small class="field-help">Utilise l’export de l’objet Lead. Le fichier peut être encodé en UTF-8 ou Windows-1252.</small>
        </div>
        <div class="field full"><label class="binary-choice" style="justify-content:flex-start"><input id="salesforceConverted" type="checkbox" checked> Importer aussi les pistes déjà converties comme « Converti »</label></div>
        <div class="field full"><label class="binary-choice" style="justify-content:flex-start"><input id="salesforceDeduplicate" type="checkbox" checked> Fusionner les personnes ayant le même e-mail ou téléphone</label></div>
      </div>
      <div id="salesforcePreview" style="margin-top:18px"><div class="activity-empty">Sélectionne ton fichier pour afficher l’aperçu avant import.</div></div>`,
      '<button class="btn" id="salesforceCancel">Annuler</button><button class="btn blue" id="salesforceConfirm" disabled>Importer les pistes</button>',
      'salesforce-import-modal'
    );

    const fileInput = document.querySelector('#salesforceFile');
    const convertedInput = document.querySelector('#salesforceConverted');
    const deduplicateInput = document.querySelector('#salesforceDeduplicate');
    const preview = document.querySelector('#salesforcePreview');
    const confirm = document.querySelector('#salesforceConfirm');
    document.querySelector('#salesforceCancel').onclick = closeModal;
    let selectedFile = null;
    let previewSequence = 0;

    const options = () => ({
      includeConverted: convertedInput.checked,
      deduplicate: deduplicateInput.checked,
    });

    const loadPreview = async () => {
      selectedFile = fileInput.files?.[0] || null;
      confirm.disabled = true;
      if (!selectedFile) {
        preview.innerHTML = '<div class="activity-empty">Sélectionne ton fichier pour afficher l’aperçu avant import.</div>';
        return;
      }
      const sequence = ++previewSequence;
      preview.innerHTML = '<div class="activity-empty">Analyse du fichier et recherche des doublons…</div>';
      try {
        const result = await sendFile(selectedFile, options(), true);
        if (sequence !== previewSequence) return;
        preview.innerHTML = summaryHtml(result, false);
        confirm.disabled = result.prepared_rows === 0;
      } catch (error) {
        if (sequence !== previewSequence) return;
        preview.innerHTML = `<div class="integration-banner warning"><div><b>Import impossible</b><span>${esc(error.message)}</span></div></div>`;
      }
    };

    fileInput.onchange = loadPreview;
    convertedInput.onchange = loadPreview;
    deduplicateInput.onchange = loadPreview;

    confirm.onclick = async () => {
      if (!selectedFile) return;
      confirm.disabled = true;
      confirm.textContent = 'Import en cours…';
      fileInput.disabled = convertedInput.disabled = deduplicateInput.disabled = true;
      preview.innerHTML = '<div class="activity-empty">Import des pistes et enregistrement dans le CRM…</div>';
      try {
        const result = await sendFile(selectedFile, options(), false);
        contacts = await api('/api/crm/contacts');
        preview.innerHTML = summaryHtml(result, true);
        confirm.textContent = 'Terminé';
        document.querySelector('#salesforceCancel').textContent = 'Fermer';
        render();
        toast(`${normalizeNumber(result.created)} pistes créées · ${normalizeNumber(result.updated)} mises à jour`);
      } catch (error) {
        preview.innerHTML = `<div class="integration-banner warning"><div><b>L’import n’a pas abouti</b><span>${esc(error.message)}</span></div></div>`;
        confirm.disabled = false;
        confirm.textContent = 'Réessayer';
        fileInput.disabled = convertedInput.disabled = deduplicateInput.disabled = false;
      }
    };
  };
})();
