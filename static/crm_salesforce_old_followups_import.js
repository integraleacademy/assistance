(() => {
  if (!window.CRM_CONFIG?.is_admin) return;

  const adminMenu = document.querySelector('#adminToolsMenu');
  if (!adminMenu || document.querySelector('#salesforceOldFollowupsImport')) return;

  const button = document.createElement('button');
  button.id = 'salesforceOldFollowupsImport';
  button.type = 'button';
  button.textContent = '⇩ Importer les anciennes pistes avec relance';
  adminMenu.append(button);

  const formatNumber = value => new Intl.NumberFormat('fr-FR').format(Number(value || 0));
  const formatDate = value => {
    if (!value) return 'Date inconnue';
    const date = new Date(`${value}T12:00:00`);
    return Number.isNaN(date.valueOf())
      ? value
      : new Intl.DateTimeFormat('fr-FR', { dateStyle: 'medium' }).format(date);
  };

  const serverError = async response => {
    const text = await response.text();
    try {
      const payload = JSON.parse(text);
      if (payload?.error) return payload.error;
    } catch (_error) {
      // Une page de proxy ne doit pas masquer le statut HTTP.
    }
    return `Le serveur a refusé l’import (HTTP ${response.status}).`;
  };

  const sendFiles = async (leadsFile, tasksFile, dryRun, previewToken = '') => {
    const body = new FormData();
    body.append('leads_file', leadsFile, leadsFile.name);
    body.append('tasks_file', tasksFile, tasksFile.name);
    body.append('dry_run', dryRun ? '1' : '0');
    if (previewToken) body.append('preview_token', previewToken);

    const response = await fetch('/api/crm/import-salesforce-old-followups', {
      method: 'POST',
      body,
      credentials: 'same-origin',
    });
    if (!response.ok) throw new Error(await serverError(response));
    const payload = await response.json();
    if (!payload || typeof payload !== 'object') {
      throw new Error('Le serveur n’a pas renvoyé un résultat exploitable.');
    }
    return payload;
  };

  const readyRowsHtml = rows => {
    if (!rows?.length) return '<div class="activity-empty">Aucune ancienne piste importable.</div>';
    return `<div style="display:grid;gap:10px">${rows.map(row => `
      <article class="card" style="box-shadow:none;padding:14px 16px">
        <div style="display:flex;justify-content:space-between;gap:16px;align-items:flex-start">
          <div>
            <b style="font-size:16px">${esc(row.person || 'Piste sans nom')}</b>
            <p style="margin:5px 0 0;color:var(--muted)">${esc(row.formation || 'Formation non renseignée')} · créée en ${esc(String(row.source_year || 'année inconnue'))}</p>
          </div>
          <span class="badge">${esc(row.action || '')}</span>
        </div>
        <p style="margin:10px 0 0">Statut Salesforce : <b>${esc(row.source_status || 'non renseigné')}</b>${row.secondary_status ? ` · deuxième timeline : <b>${esc(row.secondary_status)}</b>` : ''}</p>
        <p style="margin:6px 0 0">Relance${Number(row.task_count || 0) > 1 ? 's' : ''} : ${(row.due_dates || []).map(formatDate).map(esc).join(', ') || 'date inconnue'}</p>
        <small style="display:block;margin-top:6px;color:var(--muted)">Rapprochement : ${esc(row.match_method || 'nouvelle fiche')} · ID Salesforce : ${esc(row.salesforce_id || 'non renseigné')}</small>
      </article>`).join('')}</div>`;
  };

  const blockedRowsHtml = rows => {
    if (!rows?.length) return '<div class="activity-empty">Aucun blocage détecté.</div>';
    return `<div style="display:grid;gap:10px">${rows.slice(0, 40).map(row => `
      <article class="card" style="box-shadow:none;padding:14px 16px;border-color:#f2c879">
        <b>${esc(row.person || row.crm_name || 'Ligne sans nom')}</b>
        ${row.scheduled_date ? `<p style="margin:5px 0 0">Relance : ${esc(formatDate(row.scheduled_date))}</p>` : ''}
        <p style="margin:7px 0 0;color:#8a5b08">${esc(row.reason || 'Vérification nécessaire.')}</p>
      </article>`).join('')}${rows.length > 40 ? `<small>${formatNumber(rows.length - 40)} autre(s) blocage(s) figurent dans le rapport JSON.</small>` : ''}</div>`;
  };

  const summaryHtml = (result, final = false) => `
    <div style="display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin-bottom:18px">
      <div class="stat" style="min-height:auto"><small>Anciennes pistes avec tâche</small><strong>${formatNumber(result.old_leads_with_open_task)}</strong></div>
      <div class="stat" style="min-height:auto"><small>${final ? 'Importées' : 'Prêtes'}</small><strong>${formatNumber(result.ready)}</strong></div>
      <div class="stat" style="min-height:auto"><small>${final ? 'Créées' : 'À créer'}</small><strong>${formatNumber(result.created)}</strong></div>
      <div class="stat" style="min-height:auto"><small>${final ? 'Mises à jour' : 'À mettre à jour'}</small><strong>${formatNumber(result.updated)}</strong></div>
      <div class="stat" style="min-height:auto"><small>Bloquées</small><strong>${formatNumber(result.blocked)}</strong></div>
    </div>
    <div class="integration-banner success">
      <div>
        <b>Import strictement limité aux pistes créées avant ${formatNumber(result.cutoff_year || 2026)} avec une tâche ouverte</b>
        <span>${formatNumber(result.lead_csv_rows)} lignes de pistes · ${formatNumber(result.task_csv_rows)} activités · ${formatNumber(result.prepared_open_tasks)} tâches ouvertes · ${formatNumber(result.skipped_events)} événements ignorés.</span>
      </div>
    </div>
    <div class="integration-banner" style="margin-top:14px">
      <div>
        <b>${formatNumber(result.relances_created)} relance(s) à créer · ${formatNumber(result.relances_updated)} à mettre à jour · ${formatNumber(result.relances_unchanged)} déjà identique(s)</b>
        <span>Les disqualifiés, convertis, BTS/CAP, fiches internes et tests restent exclus. Une relance déjà traitée dans le CRM ne sera jamais rouverte.</span>
      </div>
    </div>
    ${Number(result.blocked || 0) ? `<div class="integration-banner warning" style="margin-top:14px"><div><b>${formatNumber(result.blocked)} ligne(s) restent bloquée(s)</b><span>Les lignes sûres peuvent être importées ; les conflits resteront inchangés.</span></div></div>` : ''}
    <details class="card" open style="box-shadow:none;margin-top:16px">
      <summary style="cursor:pointer;padding:14px 16px;font-weight:750">Personnes qui seront importées (${formatNumber(result.ready)})</summary>
      <div style="padding:0 16px 16px">${readyRowsHtml(result.ready_rows || [])}</div>
    </details>
    <details class="card" style="box-shadow:none;margin-top:12px">
      <summary style="cursor:pointer;padding:14px 16px;font-weight:750">Éléments bloqués (${formatNumber(result.blocked)})</summary>
      <div style="padding:0 16px 16px">${blockedRowsHtml(result.blocked_rows || [])}</div>
    </details>`;

  const downloadReport = result => {
    const copy = { generated_at: new Date().toISOString(), ...result };
    delete copy.preview_token;
    const blob = new Blob([JSON.stringify(copy, null, 2)], { type: 'application/json;charset=utf-8' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `rapport-anciennes-pistes-relances-salesforce-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
  };

  button.onclick = () => {
    modal(
      'Importer les anciennes pistes avec une relance ouverte',
      `<div class="integration-banner" style="margin-bottom:18px">
        <div>
          <b>Traitement ciblé et sécurisé</b>
          <span>Le CRM croise les deux fichiers et ne reprend que les pistes créées avant 2026 qui possèdent une tâche Salesforce encore ouverte. Il n’importe pas toutes les anciennes pistes.</span>
        </div>
      </div>
      <div class="fields">
        <div class="field full">
          <label>1. Export complet des pistes Salesforce — toutes les dates</label>
          <input id="salesforceOldLeadsFile" type="file" accept=".csv,.txt,text/csv,text/plain">
          <small class="field-help">Utilise le rapport contenant toutes les pistes et leurs champs de qualification.</small>
        </div>
        <div class="field full">
          <label>2. Export des tâches et relances Salesforce</label>
          <input id="salesforceOldTasksFile" type="file" accept=".csv,.txt,text/csv,text/plain">
          <small class="field-help">Le fichier doit contenir l’ID de l’activité, l’échéance, l’e-mail et le téléphone.</small>
        </div>
      </div>
      <div id="salesforceOldFollowupsPreview" style="margin-top:18px"><div class="activity-empty">Sélectionne les deux fichiers pour lancer l’aperçu.</div></div>`,
      '<button class="btn" id="salesforceOldFollowupsCancel">Annuler</button><button class="btn" id="salesforceOldFollowupsReport" hidden>Télécharger le rapport</button><button class="btn blue" id="salesforceOldFollowupsConfirm" disabled>Importer les anciennes pistes</button>',
      'salesforce-old-followups-modal'
    );

    const leadsInput = document.querySelector('#salesforceOldLeadsFile');
    const tasksInput = document.querySelector('#salesforceOldTasksFile');
    const preview = document.querySelector('#salesforceOldFollowupsPreview');
    const confirm = document.querySelector('#salesforceOldFollowupsConfirm');
    const cancel = document.querySelector('#salesforceOldFollowupsCancel');
    const reportButton = document.querySelector('#salesforceOldFollowupsReport');
    cancel.onclick = closeModal;

    let previewToken = '';
    let previewResult = null;
    let sequence = 0;

    const analyze = async () => {
      const leadsFile = leadsInput.files?.[0] || null;
      const tasksFile = tasksInput.files?.[0] || null;
      previewToken = '';
      previewResult = null;
      confirm.disabled = true;
      reportButton.hidden = true;
      if (!leadsFile || !tasksFile) {
        preview.innerHTML = '<div class="activity-empty">Sélectionne les deux fichiers pour lancer l’aperçu.</div>';
        return;
      }
      if (leadsFile.size > 20 * 1024 * 1024 || tasksFile.size > 20 * 1024 * 1024) {
        preview.innerHTML = '<div class="integration-banner warning"><div><b>Fichier trop volumineux</b><span>Chaque fichier doit peser moins de 20 Mo.</span></div></div>';
        return;
      }

      const currentSequence = ++sequence;
      preview.innerHTML = '<div class="activity-empty">Croisement des pistes, tâches ouvertes et fiches CRM…</div>';
      try {
        const result = await sendFiles(leadsFile, tasksFile, true);
        if (currentSequence !== sequence) return;
        previewResult = result;
        previewToken = result.preview_token || '';
        preview.innerHTML = summaryHtml(result, false);
        confirm.disabled = Number(result.ready || 0) === 0 || !previewToken;
        reportButton.hidden = false;
        reportButton.onclick = () => downloadReport(result);
      } catch (error) {
        if (currentSequence !== sequence) return;
        preview.innerHTML = `<div class="integration-banner warning"><div><b>Analyse impossible</b><span>${esc(error.message)}</span></div></div>`;
      }
    };

    leadsInput.addEventListener('change', analyze);
    tasksInput.addEventListener('change', analyze);

    confirm.onclick = async () => {
      const leadsFile = leadsInput.files?.[0] || null;
      const tasksFile = tasksInput.files?.[0] || null;
      if (!leadsFile || !tasksFile || !previewToken || !previewResult) return;
      const message = `Confirmer l’import ciblé ?\n\n${formatNumber(previewResult.created)} fiche(s) seront créées, ${formatNumber(previewResult.updated)} mise(s) à jour et ${formatNumber(previewResult.relances_created)} relance(s) ajoutée(s). Les lignes bloquées resteront inchangées.`;
      if (!window.confirm(message)) return;

      confirm.disabled = true;
      leadsInput.disabled = true;
      tasksInput.disabled = true;
      confirm.textContent = 'Import en cours…';
      preview.innerHTML = '<div class="activity-empty">Enregistrement atomique des anciennes pistes et de leurs relances…</div>';
      try {
        const result = await sendFiles(leadsFile, tasksFile, false, previewToken);
        contacts = await api('/api/crm/contacts?compact=1');
        previewResult = result;
        preview.innerHTML = summaryHtml(result, true);
        confirm.textContent = 'Terminé';
        cancel.textContent = 'Fermer';
        reportButton.hidden = false;
        reportButton.onclick = () => downloadReport(result);
        render();
        toast(`${formatNumber(result.created)} anciennes pistes créées · ${formatNumber(result.updated)} mises à jour · ${formatNumber(result.relances_created)} relances ajoutées`);
      } catch (error) {
        preview.innerHTML = `<div class="integration-banner warning"><div><b>L’import n’a pas abouti</b><span>${esc(error.message)}</span></div></div>`;
        confirm.disabled = false;
        confirm.textContent = 'Réessayer';
        leadsInput.disabled = false;
        tasksInput.disabled = false;
      }
    };
  };
})();
