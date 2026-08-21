(() => {
  if (!window.CRM_CONFIG?.is_admin) return;

  const formatNumber = value => new Intl.NumberFormat('fr-FR').format(Number(value || 0));
  const topEntries = (values, limit = 12) => Object.entries(values || {}).slice(0, limit);
  const importButton = document.createElement('button');
  importButton.id = 'salesforceImport';
  importButton.type = 'button';
  importButton.textContent = '⇩ Migrer Salesforce';
  const adminMenu = document.querySelector('#adminToolsMenu');
  if (adminMenu) adminMenu.append(importButton);

  function responseFailure(response, payload, responseText) {
    if (payload?.error) return payload.error;
    const statusReasons = {
      400: 'Le serveur a refusé le fichier. Vérifie qu’il s’agit bien d’un export CSV de pistes Salesforce.',
      401: 'Ta session a expiré. Recharge la page, reconnecte-toi, puis réessaie.',
      403: 'Ton compte n’est pas autorisé à importer des pistes Salesforce.',
      404: 'Le service de migration Salesforce est introuvable sur le serveur.',
      409: 'Le CRM ou les options ont changé depuis l’aperçu. Relance l’analyse avant d’importer.',
      413: 'Le fichier envoyé est trop volumineux. La taille maximale est de 20 Mo.',
      429: 'Trop de demandes ont été envoyées. Réessaie dans quelques instants.',
      500: 'Le serveur a rencontré une erreur pendant la migration.',
      502: 'Le serveur est temporairement indisponible.',
      503: 'Le service de migration est temporairement indisponible.',
      504: 'Le traitement a pris trop de temps. Utilise un fichier plus petit ou fractionné.',
    };
    const plainText = (responseText || '').trim();
    const serverDetail = plainText && !/<(?:!doctype|html|body)\b/i.test(plainText)
      ? ` Réponse du serveur : ${plainText.slice(0, 300)}`
      : '';
    return `${statusReasons[response.status] || `Réponse inattendue du serveur (HTTP ${response.status || 'inconnu'}).`}${serverDetail}`;
  }

  async function sendFile(file, options, dryRun, previewToken = '') {
    const body = new FormData();
    body.append('file', file, file.name);
    body.append('mode', options.mode);
    body.append('merge_policy', options.mergePolicy);
    body.append('include_converted', options.includeConverted ? '1' : '0');
    body.append('deduplicate', options.deduplicate ? '1' : '0');
    body.append('created_from', options.createdFrom || '');
    body.append('created_to', options.createdTo || '');
    body.append('dry_run', dryRun ? '1' : '0');
    if (previewToken) body.append('preview_token', previewToken);

    let response;
    try {
      response = await fetch('/api/crm/migrate-salesforce', {
        method: 'POST',
        body,
        credentials: 'same-origin',
      });
    } catch (_error) {
      throw new Error('Impossible de joindre le serveur. Vérifie ta connexion internet, puis réessaie.');
    }

    const responseText = await response.text();
    let payload = null;
    try {
      payload = responseText ? JSON.parse(responseText) : null;
    } catch (_error) {
      // Une page HTML de proxy ou de connexion ne doit pas masquer l’erreur HTTP.
    }
    if (response.redirected && /\/login(?:[/?#]|$)/.test(response.url)) {
      throw new Error('Ta session a expiré. Recharge la page, reconnecte-toi, puis réessaie.');
    }
    if (!response.ok) throw new Error(responseFailure(response, payload, responseText));
    if (!payload || typeof payload !== 'object') {
      throw new Error('Le serveur n’a pas renvoyé un résultat exploitable. Recharge la page puis réessaie.');
    }
    return payload;
  }

  function counterRows(values, emptyMessage = 'Aucune donnée', limit = 12) {
    const entries = topEntries(values, limit);
    if (!entries.length) return `<span>${emptyMessage}</span>`;
    return entries
      .map(([label, count]) => `<span><b>${formatNumber(count)}</b> ${esc(label)}</span>`)
      .join('');
  }

  function skippedSummary(result) {
    const parts = [];
    const definitions = [
      ['skipped_deleted', 'supprimées ignorées'],
      ['skipped_converted', 'converties ignorées'],
      ['skipped_other_year', 'hors 2025 ignorées'],
      ['skipped_formation', 'formations exclues'],
      ['skipped_outside_date_range', 'hors période'],
      ['skipped_invalid', 'lignes sans identité'],
    ];
    definitions.forEach(([key, label]) => {
      if (Number(result[key] || 0)) parts.push(`${formatNumber(result[key])} ${label}`);
    });
    return parts.length ? ` · ${parts.join(' · ')}` : '';
  }

  function ambiguityHtml(result) {
    const ambiguous = Number(result.ambiguous || 0);
    const sourceConflicts = Number(result.duplicate_conflicts_in_file || 0);
    if (!ambiguous && !sourceConflicts) return '';
    const samples = (result.ambiguous_samples || []).slice(0, 8)
      .map(item => `<li><b>${esc(item.nom || item.salesforce_id || 'Piste sans nom')}</b> — ${esc(item.raison || 'Correspondance ambiguë')}</li>`)
      .join('');
    return `
      <div class="integration-banner warning" style="margin-top:16px">
        <div>
          <b>${formatNumber(ambiguous)} rapprochement${ambiguous > 1 ? 's' : ''} bloqué${ambiguous > 1 ? 's' : ''}</b>
          <span>Aucune fusion incertaine n’a été effectuée. ${sourceConflicts ? `${formatNumber(sourceConflicts)} conflit(s) de doublons ont aussi été détectés dans le fichier.` : ''}</span>
          ${samples ? `<ul style="margin:10px 0 0 18px;display:grid;gap:5px">${samples}</ul>` : ''}
        </div>
      </div>`;
  }

  function summaryHtml(result, final = false) {
    const modeLabel = result.mode === 'complete' ? 'Migration complète' : 'Ancien import filtré 2025';
    const imported = Number(result.created || 0) + Number(result.updated || 0) + Number(result.unchanged || 0);
    return `
      <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:4px 0 18px">
        <div class="stat" style="min-height:auto"><small>${final ? 'Créées' : 'À créer'}</small><strong>${formatNumber(result.created)}</strong></div>
        <div class="stat" style="min-height:auto"><small>${final ? 'Mises à jour' : 'À mettre à jour'}</small><strong>${formatNumber(result.updated)}</strong></div>
        <div class="stat" style="min-height:auto"><small>Déjà identiques</small><strong>${formatNumber(result.unchanged)}</strong></div>
        <div class="stat" style="min-height:auto"><small>À vérifier</small><strong>${formatNumber(result.ambiguous)}</strong></div>
      </div>
      <div class="integration-banner success">
        <div>
          <b>${esc(modeLabel)} · ${formatNumber(imported)} piste${imported > 1 ? 's' : ''} exploitable${imported > 1 ? 's' : ''}</b>
          <span>${formatNumber(result.csv_rows)} lignes lues · ${formatNumber(result.prepared_rows)} préparées · ${formatNumber(result.duplicates_in_file)} doublons internes regroupés${skippedSummary(result)}</span>
        </div>
      </div>
      ${ambiguityHtml(result)}
      <div class="integration-banner" style="margin-top:16px">
        <div>
          <b>Qualité des coordonnées</b>
          <span>${formatNumber(result.missing_email)} sans e-mail · ${formatNumber(result.missing_phone)} sans téléphone · ${formatNumber(result.missing_email_and_phone)} sans les deux</span>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin-top:16px">
        <section class="card" style="box-shadow:none"><div class="card-head"><h3>Années de création</h3></div><div class="template-list" style="padding:12px;display:grid;gap:8px">${counterRows(result.year_counts)}</div></section>
        <section class="card" style="box-shadow:none"><div class="card-head"><h3>Statuts CRM obtenus</h3></div><div class="template-list" style="padding:12px;display:grid;gap:8px">${counterRows(result.status_counts)}</div></section>
        <section class="card" style="box-shadow:none"><div class="card-head"><h3>Formations</h3></div><div class="template-list" style="padding:12px;display:grid;gap:8px">${counterRows(result.formation_counts, 'Non renseignées')}</div></section>
        <section class="card" style="box-shadow:none"><div class="card-head"><h3>Origines</h3></div><div class="template-list" style="padding:12px;display:grid;gap:8px">${counterRows(result.source_counts, 'Non renseignées')}</div></section>
      </div>`;
  }

  function downloadReport(result) {
    const report = {
      generated_at: new Date().toISOString(),
      ...result,
    };
    delete report.preview_token;
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json;charset=utf-8' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `rapport-migration-salesforce-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
  }

  importButton.onclick = () => {
    modal(
      'Migrer les pistes Salesforce',
      `<div class="integration-banner" style="margin-bottom:18px">
        <div>
          <b>Avant de commencer dans Salesforce</b>
          <span>Exporte l’objet <b>Pistes / Leads</b> en CSV, sans filtre de date et avec les lignes de détail. Conserve le fichier original comme sauvegarde.</span>
        </div>
      </div>
      <details class="card" style="box-shadow:none;margin-bottom:18px">
        <summary style="cursor:pointer;padding:14px 16px;font-weight:700">Champs recommandés dans l’export</summary>
        <div style="padding:0 16px 16px;line-height:1.6">
          Identifiant, prénom, nom, e-mail, téléphone mobile, téléphone, statut, converti, date de création, dernière modification, source, propriétaire, formation, lieu, dates souhaitées, CPF, France Travail, informations complémentaires et description.
          <br><small>Les colonnes françaises ou anglaises, les exports UTF-8/Windows-1252/UTF-16 et les séparateurs virgule, point-virgule ou tabulation sont acceptés.</small>
        </div>
      </details>
      <div class="fields">
        <div class="field full">
          <label>Type d’import</label>
          <select id="salesforceMode">
            <option value="complete" selected>Migration complète — toutes les années et toutes les formations</option>
            <option value="legacy_2025">Ancien import — uniquement 2025 avec les exclusions historiques</option>
          </select>
          <small class="field-help" id="salesforceModeHelp">C’est le mode à utiliser pour quitter définitivement Salesforce.</small>
        </div>
        <div class="field full">
          <label>Fichier Salesforce au format CSV</label>
          <input id="salesforceFile" type="file" accept=".csv,.txt,text/csv,text/plain">
          <small class="field-help">Le fichier est d’abord analysé sans rien modifier dans le CRM.</small>
        </div>
        <div class="field" data-salesforce-date-filter>
          <label>Créées à partir du <small>(facultatif)</small></label>
          <input id="salesforceCreatedFrom" type="date">
        </div>
        <div class="field" data-salesforce-date-filter>
          <label>Créées jusqu’au <small>(facultatif)</small></label>
          <input id="salesforceCreatedTo" type="date">
        </div>
        <div class="field full"><label class="binary-choice" style="justify-content:flex-start"><input id="salesforceConverted" type="checkbox" checked> Importer aussi les pistes déjà converties</label></div>
        <div class="field full"><label class="binary-choice" style="justify-content:flex-start"><input id="salesforceDeduplicate" type="checkbox" checked> Regrouper les doublons sûrs par identifiant Salesforce, e-mail ou téléphone</label></div>
        <div class="field full"><label class="binary-choice" style="justify-content:flex-start"><input id="salesforceProtectCrm" type="checkbox" checked> Protéger les informations déjà saisies dans le nouveau CRM</label><small class="field-help">Salesforce complétera les champs vides, mais n’écrasera pas les données déjà travaillées dans le CRM.</small></div>
      </div>
      <div id="salesforcePreview" style="margin-top:18px"><div class="activity-empty">Sélectionne ton fichier pour afficher l’aperçu avant migration.</div></div>`,
      '<button class="btn" id="salesforceCancel">Annuler</button><button class="btn" id="salesforceReport" hidden>Télécharger le rapport</button><button class="btn blue" id="salesforceConfirm" disabled>Importer les pistes</button>',
      'salesforce-import-modal'
    );

    const fileInput = document.querySelector('#salesforceFile');
    const modeInput = document.querySelector('#salesforceMode');
    const modeHelp = document.querySelector('#salesforceModeHelp');
    const fromInput = document.querySelector('#salesforceCreatedFrom');
    const toInput = document.querySelector('#salesforceCreatedTo');
    const convertedInput = document.querySelector('#salesforceConverted');
    const deduplicateInput = document.querySelector('#salesforceDeduplicate');
    const protectInput = document.querySelector('#salesforceProtectCrm');
    const preview = document.querySelector('#salesforcePreview');
    const confirm = document.querySelector('#salesforceConfirm');
    const reportButton = document.querySelector('#salesforceReport');
    const cancel = document.querySelector('#salesforceCancel');
    const dateFields = [...document.querySelectorAll('[data-salesforce-date-filter]')];
    cancel.onclick = closeModal;

    let selectedFile = null;
    let previewToken = '';
    let previewResult = null;
    let finalResult = null;
    let previewSequence = 0;

    const options = () => ({
      mode: modeInput.value,
      mergePolicy: modeInput.value === 'legacy_2025'
        ? 'legacy'
        : (protectInput.checked ? 'safe' : 'salesforce'),
      includeConverted: convertedInput.checked,
      deduplicate: deduplicateInput.checked,
      createdFrom: modeInput.value === 'complete' ? fromInput.value : '',
      createdTo: modeInput.value === 'complete' ? toInput.value : '',
    });

    const updateMode = () => {
      const complete = modeInput.value === 'complete';
      dateFields.forEach(field => { field.hidden = !complete; });
      protectInput.closest('.field').hidden = !complete;
      modeHelp.textContent = complete
        ? 'C’est le mode à utiliser pour quitter définitivement Salesforce.'
        : 'Ce mode conserve exactement le filtre historique : pistes créées en 2025 et certaines formations exclues.';
    };

    const loadPreview = async () => {
      selectedFile = fileInput.files?.[0] || null;
      previewToken = '';
      previewResult = null;
      finalResult = null;
      reportButton.hidden = true;
      confirm.disabled = true;
      confirm.textContent = 'Importer les pistes';
      if (!selectedFile) {
        preview.innerHTML = '<div class="activity-empty">Sélectionne ton fichier pour afficher l’aperçu avant migration.</div>';
        return;
      }
      if (selectedFile.size > 20 * 1024 * 1024) {
        preview.innerHTML = '<div class="integration-banner warning"><div><b>Fichier trop volumineux</b><span>La limite est de 20 Mo. Fractionne l’export en plusieurs périodes.</span></div></div>';
        return;
      }
      const sequence = ++previewSequence;
      preview.innerHTML = '<div class="activity-empty">Analyse du fichier, comparaison avec le CRM et recherche des doublons…</div>';
      try {
        const result = await sendFile(selectedFile, options(), true);
        if (sequence !== previewSequence) return;
        previewResult = result;
        previewToken = result.preview_token || '';
        preview.innerHTML = summaryHtml(result, false);
        const importable = Number(result.created || 0) + Number(result.updated || 0) + Number(result.unchanged || 0);
        confirm.disabled = importable === 0 || !previewToken;
      } catch (error) {
        if (sequence !== previewSequence) return;
        preview.innerHTML = `<div class="integration-banner warning"><div><b>Analyse impossible</b><span>${esc(error.message)}</span></div></div>`;
      }
    };

    [fileInput, modeInput, fromInput, toInput, convertedInput, deduplicateInput, protectInput]
      .forEach(input => input.addEventListener('change', () => {
        updateMode();
        loadPreview();
      }));
    updateMode();

    confirm.onclick = async () => {
      if (!selectedFile || !previewToken || !previewResult) return;
      const toCreate = formatNumber(previewResult.created);
      const toUpdate = formatNumber(previewResult.updated);
      const prompt = `Confirmer la migration Salesforce ?\n\n${toCreate} fiche(s) seront créées et ${toUpdate} fiche(s) seront complétées. Les correspondances ambiguës resteront bloquées.`;
      if (!window.confirm(prompt)) return;

      confirm.disabled = true;
      confirm.textContent = 'Migration en cours…';
      [fileInput, modeInput, fromInput, toInput, convertedInput, deduplicateInput, protectInput]
        .forEach(input => { input.disabled = true; });
      preview.innerHTML = '<div class="activity-empty">Migration et enregistrement atomique dans le CRM…</div>';
      try {
        const result = await sendFile(selectedFile, options(), false, previewToken);
        finalResult = result;
        contacts = await api('/api/crm/contacts?compact=1');
        preview.innerHTML = summaryHtml(result, true);
        confirm.textContent = 'Terminé';
        cancel.textContent = 'Fermer';
        reportButton.hidden = false;
        reportButton.onclick = () => downloadReport(finalResult);
        render();
        toast(`${formatNumber(result.created)} pistes créées · ${formatNumber(result.updated)} mises à jour · ${formatNumber(result.ambiguous)} à vérifier`);
      } catch (error) {
        preview.innerHTML = `<div class="integration-banner warning"><div><b>La migration n’a pas abouti</b><span>${esc(error.message)}</span></div></div>`;
        confirm.disabled = false;
        confirm.textContent = 'Réessayer';
        [fileInput, modeInput, fromInput, toInput, convertedInput, deduplicateInput, protectInput]
          .forEach(input => { input.disabled = false; });
      }
    };
  };
})();
