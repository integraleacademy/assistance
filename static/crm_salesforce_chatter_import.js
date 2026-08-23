(() => {
  if (!window.CRM_CONFIG?.is_admin) return;

  const adminMenu = document.querySelector('#adminToolsMenu');
  if (!adminMenu || document.querySelector('#salesforceChatterImport')) return;

  const button = document.createElement('button');
  button.id = 'salesforceChatterImport';
  button.type = 'button';
  button.textContent = '⇩ Importer l’historique Salesforce';
  const deleteButton = adminMenu.querySelector('.admin-delete-database');
  if (deleteButton) adminMenu.insertBefore(button, deleteButton);
  else adminMenu.append(button);

  const formatNumber = value => new Intl.NumberFormat('fr-FR').format(Number(value || 0));

  const serverError = async response => {
    const text = await response.text();
    try {
      const payload = JSON.parse(text);
      if (payload?.error) return payload.error;
    } catch (_error) {
      // Une réponse HTML de proxy ne doit pas masquer le statut HTTP.
    }
    return `Le serveur a refusé l’import (HTTP ${response.status}).`;
  };

  const sendFiles = async (files, dryRun, previewToken = '') => {
    const body = new FormData();
    body.append('publications_file', files.publications, files.publications.name);
    body.append('comments_file', files.comments, files.comments.name);
    body.append('users_file', files.users, files.users.name);
    body.append('dry_run', dryRun ? '1' : '0');
    if (previewToken) body.append('preview_token', previewToken);

    const response = await fetch('/api/crm/import-salesforce-chatter', {
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
    if (!rows?.length) {
      return '<div class="activity-empty">Aucune fiche CRM ne possède d’historique Salesforce importable.</div>';
    }
    const shown = rows.slice(0, 80);
    return `<div style="display:grid;gap:9px">${shown.map(row => `
      <article class="card" style="box-shadow:none;padding:13px 15px">
        <div style="display:flex;justify-content:space-between;gap:14px;align-items:flex-start">
          <div>
            <b>${esc(row.person || 'Fiche sans nom')}</b>
            <small style="display:block;margin-top:4px;color:var(--muted)">${esc(row.formation || 'Formation non renseignée')} · ID ${esc((row.salesforce_ids || []).join(', ') || 'non renseigné')}</small>
          </div>
          <span class="badge">${formatNumber(row.publications)} publication${Number(row.publications) > 1 ? 's' : ''}</span>
        </div>
        <p style="margin:7px 0 0">${formatNumber(row.comments)} commentaire${Number(row.comments) > 1 ? 's' : ''}</p>
      </article>`).join('')}${rows.length > shown.length ? `<small>${formatNumber(rows.length - shown.length)} autre(s) fiche(s) figurent dans le rapport JSON.</small>` : ''}</div>`;
  };

  const unmatchedHtml = rows => {
    if (!rows?.length) return '<div class="activity-empty">Aucun identifiant sans fiche CRM.</div>';
    const shown = rows.slice(0, 80);
    return `<div style="display:grid;gap:9px">${shown.map(row => `
      <article class="card" style="box-shadow:none;padding:13px 15px;border-color:#f2c879">
        <b>${esc(row.parent_type || 'Enregistrement')} Salesforce ${esc(row.salesforce_parent_id || '')}</b>
        <p style="margin:6px 0 0">${formatNumber(row.publication_count)} publication${Number(row.publication_count) > 1 ? 's' : ''} · ${formatNumber(row.comment_count)} commentaire${Number(row.comment_count) > 1 ? 's' : ''}</p>
        <small style="display:block;margin-top:5px;color:#8a5b08">Aucune fiche CRM reliée à cet identifiant : rien ne sera créé automatiquement.</small>
      </article>`).join('')}${rows.length > shown.length ? `<small>${formatNumber(rows.length - shown.length)} autre(s) identifiant(s) figurent dans le rapport JSON.</small>` : ''}</div>`;
  };

  const ambiguousHtml = rows => {
    if (!rows?.length) return '<div class="activity-empty">Aucune ambiguïté détectée.</div>';
    return `<div style="display:grid;gap:9px">${rows.slice(0, 50).map(row => `
      <article class="card" style="box-shadow:none;padding:13px 15px;border-color:#e89a9a">
        <b>${esc(row.salesforce_parent_id || 'Identifiant Salesforce')}</b>
        <p style="margin:6px 0 0">Plusieurs fiches CRM : ${esc((row.crm_contacts || []).join(', '))}</p>
        <small style="display:block;margin-top:5px;color:#a22">Cette ligne restera bloquée.</small>
      </article>`).join('')}</div>`;
  };

  const summaryHtml = (result, final = false) => `
    <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:18px">
      <div class="stat" style="min-height:auto"><small>Fiches retrouvées</small><strong>${formatNumber(result.matched_contacts)}</strong></div>
      <div class="stat" style="min-height:auto"><small>${final ? 'Publications enregistrées' : 'Publications à rattacher'}</small><strong>${formatNumber(result.matched_publications)}</strong></div>
      <div class="stat" style="min-height:auto"><small>${final ? 'Commentaires enregistrés' : 'Commentaires à rattacher'}</small><strong>${formatNumber(result.matched_comments)}</strong></div>
      <div class="stat" style="min-height:auto"><small>Fiches sans correspondance</small><strong>${formatNumber(result.unmatched_parent_count)}</strong></div>
    </div>
    <div class="integration-banner success">
      <div>
        <b>Historique Salesforce analysé sans créer de nouvelles personnes</b>
        <span>${formatNumber(result.publication_csv_rows)} lignes FeedItem · ${formatNumber(result.comment_csv_rows)} lignes FeedComment · ${formatNumber(result.user_rows)} utilisateurs. ${formatNumber(result.prepared_publications)} publications utiles ont été retenues après exclusion des lignes système vides.</span>
      </div>
    </div>
    <div class="integration-banner" style="margin-top:14px">
      <div>
        <b>${formatNumber(result.publications_created)} publication(s) à créer · ${formatNumber(result.publications_updated)} à actualiser · ${formatNumber(result.publications_unchanged)} déjà identique(s)</b>
        <span>${formatNumber(result.comments_created)} commentaire(s) à créer · ${formatNumber(result.comments_updated)} à actualiser. Les identifiants FeedItem et FeedComment empêchent les doublons lors d’un nouvel import.</span>
      </div>
    </div>
    <div class="integration-banner" style="margin-top:14px">
      <div>
        <b>${formatNumber(result.lead_publications)} éléments rattachés à des pistes · ${formatNumber(result.task_publications)} à des tâches</b>
        <span>Les publications et commentaires liés à une tâche sont repris uniquement lorsque cette tâche est déjà rattachée à une relance du CRM. Les enregistrements Contact Salesforce (003) et les changements système vides sont ignorés.</span>
      </div>
    </div>
    ${Number(result.ambiguous_parent_count || 0) ? `<div class="integration-banner warning" style="margin-top:14px"><div><b>${formatNumber(result.ambiguous_parent_count)} identifiant(s) ambigu(s)</b><span>Ils resteront bloqués et ne seront pas fusionnés automatiquement.</span></div></div>` : ''}
    <details class="card" open style="box-shadow:none;margin-top:16px">
      <summary style="cursor:pointer;padding:14px 16px;font-weight:750">Fiches qui recevront l’historique (${formatNumber(result.matched_contacts)})</summary>
      <div style="padding:0 16px 16px">${readyRowsHtml(result.ready_rows || [])}</div>
    </details>
    <details class="card" style="box-shadow:none;margin-top:12px">
      <summary style="cursor:pointer;padding:14px 16px;font-weight:750">Identifiants sans fiche CRM (${formatNumber(result.unmatched_parent_count)})</summary>
      <div style="padding:0 16px 16px">${unmatchedHtml(result.unmatched_rows || [])}</div>
    </details>
    <details class="card" style="box-shadow:none;margin-top:12px">
      <summary style="cursor:pointer;padding:14px 16px;font-weight:750">Correspondances ambiguës (${formatNumber(result.ambiguous_parent_count)})</summary>
      <div style="padding:0 16px 16px">${ambiguousHtml(result.ambiguous_rows || [])}</div>
    </details>`;

  const downloadReport = result => {
    const report = { generated_at: new Date().toISOString(), ...result };
    delete report.preview_token;
    const blob = new Blob([JSON.stringify(report, null, 2)], {
      type: 'application/json;charset=utf-8',
    });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `rapport-historique-salesforce-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
  };

  button.onclick = () => {
    modal(
      'Importer les publications et commentaires Salesforce',
      `<div class="integration-banner" style="margin-bottom:18px">
        <div>
          <b>Import en lecture seule de l’historique</b>
          <span>Le CRM rattache uniquement les publications aux fiches déjà reliées à Salesforce. Il ne crée aucune personne et ne modifie ni le statut, ni la formation, ni les relances.</span>
        </div>
      </div>
      <div class="fields">
        <div class="field full">
          <label>1. Publications — objet FeedItem</label>
          <input id="salesforceChatterPublicationsFile" type="file" accept=".csv,.txt,text/csv,text/plain">
          <small class="field-help">Fichier attendu : salesforce-publications.csv</small>
        </div>
        <div class="field full">
          <label>2. Commentaires — objet FeedComment</label>
          <input id="salesforceChatterCommentsFile" type="file" accept=".csv,.txt,text/csv,text/plain">
          <small class="field-help">Fichier attendu : salesforce-commentaires.csv</small>
        </div>
        <div class="field full">
          <label>3. Utilisateurs — objet User</label>
          <input id="salesforceChatterUsersFile" type="file" accept=".csv,.txt,text/csv,text/plain">
          <small class="field-help">Fichier attendu : salesforce-utilisateurs.csv</small>
        </div>
      </div>
      <div id="salesforceChatterPreview" style="margin-top:18px"><div class="activity-empty">Sélectionne les trois fichiers pour lancer l’aperçu.</div></div>`,
      '<button class="btn" id="salesforceChatterCancel">Annuler</button><button class="btn" id="salesforceChatterReport" hidden>Télécharger le rapport</button><button class="btn blue" id="salesforceChatterConfirm" disabled>Importer l’historique</button>',
      'salesforce-chatter-modal'
    );

    const publicationsInput = document.querySelector('#salesforceChatterPublicationsFile');
    const commentsInput = document.querySelector('#salesforceChatterCommentsFile');
    const usersInput = document.querySelector('#salesforceChatterUsersFile');
    const preview = document.querySelector('#salesforceChatterPreview');
    const confirm = document.querySelector('#salesforceChatterConfirm');
    const cancel = document.querySelector('#salesforceChatterCancel');
    const reportButton = document.querySelector('#salesforceChatterReport');
    cancel.onclick = closeModal;

    let previewToken = '';
    let previewResult = null;
    let sequence = 0;

    const selectedFiles = () => ({
      publications: publicationsInput.files?.[0] || null,
      comments: commentsInput.files?.[0] || null,
      users: usersInput.files?.[0] || null,
    });

    const analyze = async () => {
      const files = selectedFiles();
      previewToken = '';
      previewResult = null;
      confirm.disabled = true;
      reportButton.hidden = true;
      if (!files.publications || !files.comments || !files.users) {
        preview.innerHTML = '<div class="activity-empty">Sélectionne les trois fichiers pour lancer l’aperçu.</div>';
        return;
      }
      if (
        files.publications.size > 30 * 1024 * 1024
        || files.comments.size > 15 * 1024 * 1024
        || files.users.size > 5 * 1024 * 1024
      ) {
        preview.innerHTML = '<div class="integration-banner warning"><div><b>Un fichier est trop volumineux</b><span>Limites : 30 Mo pour FeedItem, 15 Mo pour FeedComment et 5 Mo pour User.</span></div></div>';
        return;
      }

      const currentSequence = ++sequence;
      preview.innerHTML = '<div class="activity-empty">Analyse des publications, commentaires, auteurs et identifiants Salesforce…</div>';
      try {
        const result = await sendFiles(files, true);
        if (currentSequence !== sequence) return;
        previewResult = result;
        previewToken = result.preview_token || '';
        preview.innerHTML = summaryHtml(result, false);
        confirm.disabled = Number(result.matched_publications || 0) === 0 || !previewToken;
        reportButton.hidden = false;
        reportButton.onclick = () => downloadReport(result);
      } catch (error) {
        if (currentSequence !== sequence) return;
        preview.innerHTML = `<div class="integration-banner warning"><div><b>Analyse impossible</b><span>${esc(error.message)}</span></div></div>`;
      }
    };

    [publicationsInput, commentsInput, usersInput].forEach(input => {
      input.addEventListener('change', analyze);
    });

    confirm.onclick = async () => {
      const files = selectedFiles();
      if (!files.publications || !files.comments || !files.users || !previewToken || !previewResult) return;
      const message = `Confirmer l’import ?\n\n${formatNumber(previewResult.matched_publications)} publication(s) et ${formatNumber(previewResult.matched_comments)} commentaire(s) seront rattachés à ${formatNumber(previewResult.matched_contacts)} fiche(s). Aucune nouvelle personne ne sera créée.`;
      if (!window.confirm(message)) return;

      confirm.disabled = true;
      [publicationsInput, commentsInput, usersInput].forEach(input => { input.disabled = true; });
      confirm.textContent = 'Import en cours…';
      preview.innerHTML = '<div class="activity-empty">Enregistrement atomique de l’historique Salesforce…</div>';
      try {
        const result = await sendFiles(files, false, previewToken);
        contacts = await api('/api/crm/contacts?compact=1');
        previewResult = result;
        preview.innerHTML = summaryHtml(result, true);
        confirm.textContent = 'Terminé';
        cancel.textContent = 'Fermer';
        reportButton.hidden = false;
        reportButton.onclick = () => downloadReport(result);
        render();
        toast(`${formatNumber(result.publications_created)} publications et ${formatNumber(result.comments_created)} commentaires Salesforce importés`);
      } catch (error) {
        preview.innerHTML = `<div class="integration-banner warning"><div><b>L’import n’a pas abouti</b><span>${esc(error.message)}</span></div></div>`;
        confirm.disabled = false;
        confirm.textContent = 'Réessayer';
        [publicationsInput, commentsInput, usersInput].forEach(input => { input.disabled = false; });
      }
    };
  };
})();
