(() => {
  if (!window.CRM_CONFIG?.is_admin) return;
  if (document.querySelector('#salesforceFormationFollowupsImport')) return;

  const adminMenu = document.querySelector('#adminToolsMenu');
  if (!adminMenu) return;

  const formatNumber = value => new Intl.NumberFormat('fr-FR').format(Number(value || 0));
  const formatDate = value => {
    if (!value) return 'Date inconnue';
    const parsed = new Date(`${value}T12:00:00`);
    return Number.isNaN(parsed.getTime())
      ? value
      : new Intl.DateTimeFormat('fr-FR', { dateStyle: 'medium' }).format(parsed);
  };

  const importButton = document.createElement('button');
  importButton.id = 'salesforceFormationFollowupsImport';
  importButton.type = 'button';
  importButton.textContent = '↻ Régulariser les relances avec formation';
  const anomalyExportButton = document.querySelector('#salesforceRelancesManualExport');
  const tasksButton = document.querySelector('#salesforceRelancesImport');
  if (anomalyExportButton?.parentNode === adminMenu) {
    anomalyExportButton.insertAdjacentElement('afterend', importButton);
  } else if (tasksButton?.parentNode === adminMenu) {
    tasksButton.insertAdjacentElement('afterend', importButton);
  } else {
    adminMenu.append(importButton);
  }

  function responseFailure(response, payload, responseText) {
    if (payload?.error) return payload.error;
    const reasons = {
      400: 'Le serveur a refusé le fichier. Utilise la liste complète des anomalies téléchargée depuis l’aperçu des relances.',
      401: 'Ta session a expiré. Recharge la page puis reconnecte-toi.',
      403: 'Seul un administrateur peut effectuer cette régularisation.',
      404: 'Le service de régularisation n’est pas disponible sur le serveur.',
      409: 'Le CRM a changé depuis l’aperçu. Relance l’analyse avant de confirmer.',
      413: 'Le fichier est trop volumineux. La limite est de 20 Mo.',
      500: 'Le serveur a rencontré une erreur pendant la régularisation.',
      502: 'Le serveur est temporairement indisponible.',
      503: 'Le service est temporairement indisponible.',
      504: 'Le traitement a pris trop de temps.',
    };
    const plain = (responseText || '').trim();
    const detail = plain && !/<(?:!doctype|html|body)\b/i.test(plain)
      ? ` Réponse du serveur : ${plain.slice(0, 300)}`
      : '';
    return `${reasons[response.status] || `Réponse inattendue du serveur (HTTP ${response.status || 'inconnu'}).`}${detail}`;
  }

  async function sendFile(file, dryRun, previewToken = '') {
    const body = new FormData();
    body.append('file', file, file.name);
    body.append('dry_run', dryRun ? '1' : '0');
    if (previewToken) body.append('preview_token', previewToken);

    let response;
    try {
      response = await fetch('/api/crm/import-salesforce-anomaly-followups', {
        method: 'POST',
        body,
        credentials: 'same-origin',
      });
    } catch (_error) {
      throw new Error('Impossible de joindre le serveur. Vérifie ta connexion internet puis réessaie.');
    }

    const responseText = await response.text();
    let payload = null;
    try {
      payload = responseText ? JSON.parse(responseText) : null;
    } catch (_error) {
      // Une page HTML de proxy ne doit pas masquer l'erreur HTTP.
    }
    if (response.redirected && /\/login(?:[/?#]|$)/.test(response.url)) {
      throw new Error('Ta session a expiré. Recharge la page puis reconnecte-toi.');
    }
    if (!response.ok) throw new Error(responseFailure(response, payload, responseText));
    if (!payload || typeof payload !== 'object') {
      throw new Error('Le serveur n’a pas renvoyé un résultat exploitable.');
    }
    return payload;
  }

  function counterRows(values, formatter = label => label) {
    const entries = Object.entries(values || {});
    if (!entries.length) return '<span>Aucune donnée</span>';
    return entries
      .map(([label, count]) => `<span><b>${formatNumber(count)}</b> ${esc(formatter(label))}</span>`)
      .join('');
  }

  function readyTable(rows) {
    if (!rows?.length) return '<div class="activity-empty">Aucune fiche prête.</div>';
    return `
      <div style="overflow:auto">
        <table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead>
            <tr>
              <th style="text-align:left;padding:9px;border-bottom:1px solid #dfe5ee">Personne</th>
              <th style="text-align:left;padding:9px;border-bottom:1px solid #dfe5ee">Formation</th>
              <th style="text-align:left;padding:9px;border-bottom:1px solid #dfe5ee">Ancien statut</th>
              <th style="text-align:left;padding:9px;border-bottom:1px solid #dfe5ee">Relance</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map(row => `
              <tr>
                <td style="padding:9px;border-bottom:1px solid #eef1f5"><b>${esc(row.crm_name || row.person || 'Sans nom')}</b>${row.reactivated ? '<small style="display:block;color:#a23">Fiche disqualifiée réactivée</small>' : ''}</td>
                <td style="padding:9px;border-bottom:1px solid #eef1f5">${esc(row.formation || '')}</td>
                <td style="padding:9px;border-bottom:1px solid #eef1f5">${esc(row.old_status || 'Non renseigné')} → <b>A relancer</b></td>
                <td style="padding:9px;border-bottom:1px solid #eef1f5">${esc(formatDate(row.scheduled_date))}</td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>`;
  }

  function blockedTable(rows) {
    if (!rows?.length) return '';
    return `
      <details class="card" style="box-shadow:none;margin-top:16px" open>
        <summary style="cursor:pointer;padding:14px 16px;font-weight:700">Voir les lignes bloquées (${formatNumber(rows.length)})</summary>
        <div style="padding:0 16px 16px;display:grid;gap:9px">
          ${rows.map(row => `
            <div style="padding:10px 12px;border:1px solid #e1c7c7;border-radius:10px;background:#fff">
              <b>${esc(row.person || row.crm_name || 'Sans nom')}</b>
              <small style="display:block;margin-top:3px">Fiche CRM : ${esc(row.crm_current_name || row.crm_name || 'inconnue')} · ${esc(row.crm_formation || '')} · relance ${esc(formatDate(row.scheduled_date))}</small>
              <span style="display:block;margin-top:5px">${esc(row.block_reason || 'Vérification manuelle nécessaire.')}</span>
            </div>`).join('')}
        </div>
      </details>`;
  }

  function summaryHtml(result, final = false) {
    const selected = Number(result.selected_with_formation || 0);
    const ready = Number(result.ready || 0);
    const blocked = Number(result.blocked || 0);
    const ignored = Number(result.skipped_without_formation || 0);
    return `
      <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:4px 0 18px">
        <div class="stat" style="min-height:auto"><small>Lignes du fichier</small><strong>${formatNumber(result.csv_rows)}</strong></div>
        <div class="stat" style="min-height:auto"><small>Avec formation CRM</small><strong>${formatNumber(selected)}</strong></div>
        <div class="stat" style="min-height:auto"><small>${final ? 'Régularisées' : 'Prêtes à régulariser'}</small><strong>${formatNumber(ready)}</strong></div>
        <div class="stat" style="min-height:auto"><small>Bloquées</small><strong>${formatNumber(blocked)}</strong></div>
      </div>
      <div class="integration-banner success">
        <div>
          <b>${formatNumber(ready)} fiche${ready > 1 ? 's' : ''} existante${ready > 1 ? 's' : ''} sera${ready > 1 ? 'ont' : ''} placée${ready > 1 ? 's' : ''} en « A relancer »</b>
          <span>${formatNumber(result.relances_created)} relance(s) à créer · ${formatNumber(result.relances_updated)} à mettre à jour · ${formatNumber(result.already_followup)} fiche(s) déjà « A relancer » · ${formatNumber(result.reactivated_disqualified)} fiche(s) disqualifiée(s) à réactiver.</span>
        </div>
      </div>
      <div class="integration-banner" style="margin-top:16px">
        <div>
          <b>${formatNumber(ignored)} ligne${ignored > 1 ? 's' : ''} sans « Formation fiche CRM » ignorée${ignored > 1 ? 's' : ''}</b>
          <span>Ces lignes ne créeront aucune personne et ne modifieront aucune fiche.</span>
        </div>
      </div>
      ${blocked ? `
        <div class="integration-banner warning" style="margin-top:16px">
          <div>
            <b>${formatNumber(blocked)} ligne bloquée pour éviter une relance sur la mauvaise personne</b>
            <span>Elle restera inchangée et devra être vérifiée manuellement.</span>
          </div>
        </div>` : ''}
      <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin-top:16px">
        <section class="card" style="box-shadow:none"><div class="card-head"><h3>Formations</h3></div><div class="template-list" style="padding:12px;display:grid;gap:8px">${counterRows(result.formation_counts)}</div></section>
        <section class="card" style="box-shadow:none"><div class="card-head"><h3>Statuts actuels du fichier</h3></div><div class="template-list" style="padding:12px;display:grid;gap:8px">${counterRows(result.old_status_counts)}</div></section>
        <section class="card" style="box-shadow:none"><div class="card-head"><h3>Dates de relance</h3></div><div class="template-list" style="padding:12px;display:grid;gap:8px">${counterRows(result.due_date_counts, formatDate)}</div></section>
      </div>
      <details class="card" style="box-shadow:none;margin-top:16px" open>
        <summary style="cursor:pointer;padding:14px 16px;font-weight:700">Voir les fiches prêtes (${formatNumber(ready)})</summary>
        <div style="padding:0 16px 16px">${readyTable(result.ready_rows || [])}</div>
      </details>
      ${blockedTable(result.blocked_rows || [])}`;
  }

  importButton.onclick = () => {
    modal(
      'Régulariser les relances avec une formation CRM',
      `<div class="integration-banner" style="margin-bottom:18px">
        <div>
          <b>Ce fichier ne crée aucune nouvelle personne</b>
          <span>Le CRM utilisera uniquement les lignes dont la colonne <b>Formation fiche CRM</b> est renseignée. Les fiches existantes seront placées en <b>A relancer</b> et la date de tâche Salesforce deviendra leur date de relance.</span>
        </div>
      </div>
      <div class="integration-banner warning" style="margin-bottom:18px">
        <div>
          <b>Réactivation explicite</b>
          <span>Une fiche actuellement disqualifiée mais possédant une formation sera réactivée et placée en « A relancer ». Les identités manifestement incohérentes resteront bloquées.</span>
        </div>
      </div>
      <div class="fields">
        <div class="field full">
          <label>Liste complète des anomalies au format CSV</label>
          <input id="salesforceFormationFollowupsFile" type="file" accept=".csv,.txt,text/csv,text/plain">
          <small class="field-help">Utilise le fichier « liste-complete-anomalies-relances-salesforce…csv » téléchargé depuis l’aperçu.</small>
        </div>
      </div>
      <div id="salesforceFormationFollowupsPreview" style="margin-top:18px"><div class="activity-empty">Sélectionne le fichier pour afficher l’aperçu sans modifier le CRM.</div></div>`,
      '<button class="btn" id="salesforceFormationFollowupsCancel">Annuler</button><button class="btn blue" id="salesforceFormationFollowupsConfirm" disabled>Régulariser les fiches</button>',
      'salesforce-formation-followups-import-modal'
    );

    const fileInput = document.querySelector('#salesforceFormationFollowupsFile');
    const preview = document.querySelector('#salesforceFormationFollowupsPreview');
    const confirm = document.querySelector('#salesforceFormationFollowupsConfirm');
    const cancel = document.querySelector('#salesforceFormationFollowupsCancel');
    cancel.onclick = closeModal;

    let selectedFile = null;
    let previewToken = '';
    let previewResult = null;
    let sequence = 0;

    const loadPreview = async () => {
      selectedFile = fileInput.files?.[0] || null;
      previewToken = '';
      previewResult = null;
      confirm.disabled = true;
      confirm.textContent = 'Régulariser les fiches';
      if (!selectedFile) {
        preview.innerHTML = '<div class="activity-empty">Sélectionne le fichier pour afficher l’aperçu sans modifier le CRM.</div>';
        return;
      }
      if (selectedFile.size > 20 * 1024 * 1024) {
        preview.innerHTML = '<div class="integration-banner warning"><div><b>Fichier trop volumineux</b><span>La limite autorisée est de 20 Mo.</span></div></div>';
        return;
      }

      const currentSequence = ++sequence;
      preview.innerHTML = '<div class="activity-empty">Analyse des fiches et des relances à régulariser…</div>';
      try {
        const result = await sendFile(selectedFile, true);
        if (currentSequence !== sequence) return;
        previewResult = result;
        previewToken = result.preview_token || '';
        preview.innerHTML = summaryHtml(result, false);
        confirm.disabled = !previewToken || Number(result.ready || 0) === 0;
      } catch (error) {
        if (currentSequence !== sequence) return;
        preview.innerHTML = `<div class="integration-banner warning"><div><b>Analyse impossible</b><span>${esc(error.message)}</span></div></div>`;
      }
    };

    fileInput.addEventListener('change', loadPreview);

    confirm.onclick = async () => {
      if (!selectedFile || !previewToken || !previewResult) return;
      const reactivated = Number(previewResult.reactivated_disqualified || 0);
      const prompt = [
        `Confirmer la régularisation de ${formatNumber(previewResult.ready)} fiche(s) ?`,
        '',
        `${formatNumber(previewResult.relances_created)} relance(s) seront créées et toutes les fiches retenues seront placées en « A relancer ».`,
        reactivated ? `${formatNumber(reactivated)} fiche(s) disqualifiée(s) seront réactivées.` : '',
        `${formatNumber(previewResult.blocked)} ligne(s) incohérente(s) resteront ignorées.`,
      ].filter(Boolean).join('\n');
      if (!window.confirm(prompt)) return;

      confirm.disabled = true;
      fileInput.disabled = true;
      confirm.textContent = 'Régularisation en cours…';
      preview.innerHTML = '<div class="activity-empty">Enregistrement des statuts et des relances dans le CRM…</div>';
      try {
        const result = await sendFile(selectedFile, false, previewToken);
        contacts = await api('/api/crm/contacts?compact=1');
        preview.innerHTML = summaryHtml(result, true);
        confirm.textContent = 'Terminé';
        cancel.textContent = 'Fermer';
        render();
        toast(`${formatNumber(result.ready)} fiche(s) régularisée(s) · ${formatNumber(result.relances_created)} relance(s) créée(s)`);
      } catch (error) {
        preview.innerHTML = `<div class="integration-banner warning"><div><b>La régularisation n’a pas abouti</b><span>${esc(error.message)}</span></div></div>`;
        fileInput.disabled = false;
        confirm.disabled = false;
        confirm.textContent = 'Réessayer';
      }
    };
  };
})();
