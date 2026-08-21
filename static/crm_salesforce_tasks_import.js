(() => {
  if (!window.CRM_CONFIG?.is_admin) return;
  if (document.querySelector('#salesforceRelancesImport')) return;

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
  const topEntries = (values, limit = 12) => Object.entries(values || {}).slice(0, limit);

  const importButton = document.createElement('button');
  importButton.id = 'salesforceRelancesImport';
  importButton.type = 'button';
  importButton.textContent = '↻ Importer les relances Salesforce';
  const leadButton = document.querySelector('#salesforceImport');
  if (leadButton?.parentNode === adminMenu) leadButton.insertAdjacentElement('afterend', importButton);
  else adminMenu.append(importButton);

  function responseFailure(response, payload, responseText) {
    if (payload?.error) return payload.error;
    const reasons = {
      400: 'Le serveur a refusé le fichier. Vérifie qu’il s’agit bien de l’export CSV des tâches Salesforce.',
      401: 'Ta session a expiré. Recharge la page puis reconnecte-toi.',
      403: 'Seul un administrateur peut importer les relances Salesforce.',
      404: 'Le service d’import des relances Salesforce n’est pas disponible sur le serveur.',
      409: 'Le CRM a changé depuis l’aperçu. Relance l’analyse avant d’importer.',
      413: 'Le fichier est trop volumineux. La limite est de 20 Mo.',
      500: 'Le serveur a rencontré une erreur pendant l’import des relances.',
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
      response = await fetch('/api/crm/import-salesforce-relances', {
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
      // Une page HTML de proxy ne doit pas masquer le code HTTP.
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

  function counterRows(values, formatter = label => label, limit = 12) {
    const entries = topEntries(values, limit);
    if (!entries.length) return '<span>Aucune donnée</span>';
    return entries
      .map(([label, count]) => `<span><b>${formatNumber(count)}</b> ${esc(formatter(label))}</span>`)
      .join('');
  }

  function sampleRows(items, empty = 'Aucune ligne') {
    if (!items?.length) return `<div class="activity-empty">${empty}</div>`;
    return `<div style="display:grid;gap:9px">${items.slice(0, 12).map(item => `
      <div style="padding:10px 12px;border:1px solid #e0e6ef;border-radius:10px;background:#fff">
        <b>${esc(item.person || item.task_name || item.activity_id || 'Sans nom')}</b>
        <small style="display:block;margin-top:3px">${esc(item.date || '')}${item.email ? ` · ${esc(item.email)}` : ''}${item.phone ? ` · ${esc(item.phone)}` : ''}</small>
        <span style="display:block;margin-top:5px">${esc(item.reason || (item.crm_name ? `Fiche CRM : ${item.crm_name}` : ''))}</span>
      </div>`).join('')}</div>`;
  }

  function warningsHtml(result) {
    const unmatched = Number(result.unmatched || 0);
    const notLinked = Number(result.skipped_not_salesforce_linked || 0);
    const ambiguous = Number(result.ambiguous || 0);
    const excluded = Number(result.skipped_excluded_contact || 0);
    const nameWarnings = Number(result.name_warnings || 0);
    if (!unmatched && !notLinked && !ambiguous && !excluded && !nameWarnings) return '';
    return `
      <div class="integration-banner warning" style="margin-top:16px">
        <div>
          <b>Relances non importables automatiquement</b>
          <span>${formatNumber(unmatched)} sans fiche correspondante · ${formatNumber(notLinked)} fiche(s) non encore reliée(s) à Salesforce · ${formatNumber(ambiguous)} correspondance(s) ambiguë(s) · ${formatNumber(excluded)} fiche(s) exclue(s) · ${formatNumber(nameWarnings)} avertissement(s) de nom.</span>
        </div>
      </div>`;
  }

  function summaryHtml(result, final = false) {
    const actionable = Number(result.created || 0) + Number(result.updated || 0) + Number(result.promoted_to_followup || 0);
    const eventText = Number(result.skipped_events || 0)
      ? ` · ${formatNumber(result.skipped_events)} événements ignorés`
      : '';
    const closedText = Number(result.skipped_closed || 0)
      ? ` · ${formatNumber(result.skipped_closed)} tâches terminées ignorées`
      : '';
    return `
      <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:4px 0 18px">
        <div class="stat" style="min-height:auto"><small>Tâches ouvertes</small><strong>${formatNumber(result.prepared_tasks)}</strong></div>
        <div class="stat" style="min-height:auto"><small>${final ? 'Relances créées' : 'À créer'}</small><strong>${formatNumber(result.created)}</strong></div>
        <div class="stat" style="min-height:auto"><small>${final ? 'Relances mises à jour' : 'À mettre à jour'}</small><strong>${formatNumber(result.updated)}</strong></div>
        <div class="stat" style="min-height:auto"><small>Déjà identiques</small><strong>${formatNumber(result.unchanged)}</strong></div>
      </div>
      <div class="integration-banner success">
        <div>
          <b>${formatNumber(result.matched_contacts)} fiche${Number(result.matched_contacts) > 1 ? 's' : ''} CRM rapprochée${Number(result.matched_contacts) > 1 ? 's' : ''}</b>
          <span>${formatNumber(result.csv_rows)} lignes lues · ${formatNumber(result.task_rows)} tâches détectées${eventText}${closedText} · ${formatNumber(result.promoted_to_followup)} fiche(s) « Nouveaux » passées en « A relancer ».</span>
        </div>
      </div>
      ${warningsHtml(result)}
      <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin-top:16px">
        <section class="card" style="box-shadow:none"><div class="card-head"><h3>Dates de relance</h3></div><div class="template-list" style="padding:12px;display:grid;gap:8px">${counterRows(result.due_date_counts, formatDate, 15)}</div></section>
        <section class="card" style="box-shadow:none"><div class="card-head"><h3>Attribuées à</h3></div><div class="template-list" style="padding:12px;display:grid;gap:8px">${counterRows(result.owner_counts)}</div></section>
        <section class="card" style="box-shadow:none"><div class="card-head"><h3>Objet des tâches</h3></div><div class="template-list" style="padding:12px;display:grid;gap:8px">${counterRows(result.subject_counts)}</div></section>
      </div>
      ${(result.unmatched_samples || []).length ? `
        <details class="card" style="box-shadow:none;margin-top:16px">
          <summary style="cursor:pointer;padding:14px 16px;font-weight:700">Voir les relances non rattachées</summary>
          <div style="padding:0 16px 16px">${sampleRows(result.unmatched_samples)}</div>
        </details>` : ''}
      ${(result.ambiguous_samples || []).length ? `
        <details class="card" style="box-shadow:none;margin-top:16px">
          <summary style="cursor:pointer;padding:14px 16px;font-weight:700">Voir les correspondances à vérifier</summary>
          <div style="padding:0 16px 16px">${sampleRows(result.ambiguous_samples)}</div>
        </details>` : ''}
      ${(result.warning_samples || []).length ? `
        <details class="card" style="box-shadow:none;margin-top:16px">
          <summary style="cursor:pointer;padding:14px 16px;font-weight:700">Voir les différences de nom non bloquantes</summary>
          <div style="padding:0 16px 16px">${sampleRows(result.warning_samples)}</div>
        </details>` : ''}
      ${!actionable && !Number(result.unchanged || 0) ? '<div class="activity-empty" style="margin-top:16px">Aucune relance ne peut être importée avec ce fichier et l’état actuel du CRM.</div>' : ''}`;
  }

  function downloadReport(result) {
    const payload = { generated_at: new Date().toISOString(), ...result };
    delete payload.preview_token;
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `rapport-import-relances-salesforce-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.append(link);
    link.click();
    URL.revokeObjectURL(link.href);
    link.remove();
  }

  importButton.onclick = () => {
    modal(
      'Importer les relances Salesforce',
      `<div class="integration-banner" style="margin-bottom:18px">
        <div>
          <b>Ordre obligatoire</b>
          <span>Importe d’abord le fichier des pistes Salesforce 2026. Les tâches ne créeront jamais de nouvelle personne : elles seront uniquement rattachées aux fiches CRM déjà reliées à Salesforce.</span>
        </div>
      </div>
      <div class="fields">
        <div class="field full">
          <label>Rapport « Tâches et événements » Salesforce au format CSV</label>
          <input id="salesforceRelancesFile" type="file" accept=".csv,.txt,text/csv,text/plain">
          <small class="field-help">Le CRM conservera uniquement les tâches ouvertes. Les événements Calendly, les tâches terminées et les correspondances incertaines seront ignorés.</small>
        </div>
      </div>
      <div id="salesforceRelancesPreview" style="margin-top:18px"><div class="activity-empty">Sélectionne le fichier pour afficher l’aperçu sans modifier le CRM.</div></div>`,
      '<button class="btn" id="salesforceRelancesCancel">Annuler</button><button class="btn" id="salesforceRelancesReport" hidden>Télécharger le rapport</button><button class="btn blue" id="salesforceRelancesConfirm" disabled>Importer les relances</button>',
      'salesforce-relances-import-modal'
    );

    const fileInput = document.querySelector('#salesforceRelancesFile');
    const preview = document.querySelector('#salesforceRelancesPreview');
    const confirm = document.querySelector('#salesforceRelancesConfirm');
    const cancel = document.querySelector('#salesforceRelancesCancel');
    const report = document.querySelector('#salesforceRelancesReport');
    cancel.onclick = closeModal;

    let selectedFile = null;
    let previewToken = '';
    let previewResult = null;
    let finalResult = null;
    let sequence = 0;

    const loadPreview = async () => {
      selectedFile = fileInput.files?.[0] || null;
      previewToken = '';
      previewResult = null;
      finalResult = null;
      confirm.disabled = true;
      confirm.textContent = 'Importer les relances';
      report.hidden = true;
      if (!selectedFile) {
        preview.innerHTML = '<div class="activity-empty">Sélectionne le fichier pour afficher l’aperçu sans modifier le CRM.</div>';
        return;
      }
      if (selectedFile.size > 20 * 1024 * 1024) {
        preview.innerHTML = '<div class="integration-banner warning"><div><b>Fichier trop volumineux</b><span>La limite autorisée est de 20 Mo.</span></div></div>';
        return;
      }
      const currentSequence = ++sequence;
      preview.innerHTML = '<div class="activity-empty">Analyse des tâches et rapprochement avec les fiches CRM…</div>';
      try {
        const result = await sendFile(selectedFile, true);
        if (currentSequence !== sequence) return;
        previewResult = result;
        previewToken = result.preview_token || '';
        preview.innerHTML = summaryHtml(result, false);
        report.hidden = false;
        report.textContent = 'Télécharger le rapport d’aperçu';
        report.onclick = () => downloadReport(previewResult);
        const actionable = Number(result.created || 0) + Number(result.updated || 0) + Number(result.promoted_to_followup || 0);
        confirm.disabled = !previewToken || actionable === 0;
      } catch (error) {
        if (currentSequence !== sequence) return;
        preview.innerHTML = `<div class="integration-banner warning"><div><b>Analyse impossible</b><span>${esc(error.message)}</span></div></div>`;
      }
    };

    fileInput.addEventListener('change', loadPreview);

    confirm.onclick = async () => {
      if (!selectedFile || !previewToken || !previewResult) return;
      const prompt = `Confirmer l’import des relances Salesforce ?\n\n${formatNumber(previewResult.created)} relance(s) seront créées et ${formatNumber(previewResult.updated)} mise(s) à jour. ${formatNumber(previewResult.unmatched)} tâche(s) sans fiche CRM et ${formatNumber(previewResult.ambiguous)} correspondance(s) ambiguë(s) resteront ignorées.`;
      if (!window.confirm(prompt)) return;
      confirm.disabled = true;
      fileInput.disabled = true;
      confirm.textContent = 'Import en cours…';
      preview.innerHTML = '<div class="activity-empty">Enregistrement des relances dans le CRM…</div>';
      try {
        const result = await sendFile(selectedFile, false, previewToken);
        finalResult = result;
        contacts = await api('/api/crm/contacts?compact=1');
        preview.innerHTML = summaryHtml(result, true);
        confirm.textContent = 'Terminé';
        cancel.textContent = 'Fermer';
        report.hidden = false;
        report.textContent = 'Télécharger le rapport final';
        report.onclick = () => downloadReport(finalResult);
        render();
        toast(`${formatNumber(result.created)} relances créées · ${formatNumber(result.updated)} mises à jour`);
      } catch (error) {
        preview.innerHTML = `<div class="integration-banner warning"><div><b>L’import n’a pas abouti</b><span>${esc(error.message)}</span></div></div>`;
        confirm.disabled = false;
        confirm.textContent = 'Réessayer';
        fileInput.disabled = false;
      }
    };
  };
})();
