(() => {
  if (!window.CRM_CONFIG?.is_admin) return;

  const adminMenu = document.querySelector('#adminToolsMenu');
  if (!adminMenu || document.querySelector('#salesforceChatterImport')) return;

  const button = document.createElement('button');
  button.id = 'salesforceChatterImport';
  button.type = 'button';
  button.textContent = '⇩ Importer publications et commentaires Salesforce';
  adminMenu.append(button);

  const formatNumber = value => new Intl.NumberFormat('fr-FR').format(Number(value || 0));

  const serverError = async response => {
    const text = await response.text();
    try {
      const payload = JSON.parse(text);
      if (payload?.error) return payload.error;
    } catch (_error) {
      // Une page HTML du proxy ne doit pas masquer le statut HTTP.
    }
    return `Le serveur a refusé l’import (HTTP ${response.status}).`;
  };

  const sendFiles = async (publicationsFile, commentsFile, usersFile, dryRun, previewToken = '') => {
    const body = new FormData();
    body.append('publications_file', publicationsFile, publicationsFile.name);
    body.append('comments_file', commentsFile, commentsFile.name);
    body.append('users_file', usersFile, usersFile.name);
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

  const matchedRowsHtml = rows => {
    if (!rows?.length) {
      return '<div class="activity-empty">Aucune fiche CRM reliée à ces publications.</div>';
    }
    const visible = rows.slice(0, 80);
    return `<div style="display:grid;gap:10px">${visible.map(row => `
      <article class="card" style="box-shadow:none;padding:14px 16px">
        <div style="display:flex;justify-content:space-between;gap:14px;align-items:flex-start">
          <div>
            <b style="font-size:16px">${esc(row.person || 'Fiche sans nom')}</b>
            <p style="margin:5px 0 0;color:var(--muted)">${esc(row.formation || 'Formation non renseignée')}</p>
          </div>
          <span class="badge">${formatNumber(row.publications)} publication${Number(row.publications) > 1 ? 's' : ''}</span>
        </div>
        <p style="margin:9px 0 0">${formatNumber(row.comments)} commentaire${Number(row.comments) > 1 ? 's' : ''} · ID Salesforce : ${esc((row.salesforce_ids || []).join(', ') || 'non renseigné')}</p>
      </article>`).join('')}${rows.length > visible.length ? `<small>${formatNumber(rows.length - visible.length)} autre(s) fiche(s) figurent dans le rapport JSON.</small>` : ''}</div>`;
  };

  const unmatchedRowsHtml = rows => {
    if (!rows?.length) {
      return '<div class="activity-empty">Aucun identifiant de piste sans correspondance.</div>';
    }
    const visible = rows.slice(0, 60);
    return `<div style="display:grid;gap:10px">${visible.map(row => `
      <article class="card" style="box-shadow:none;padding:14px 16px;border-color:#f2c879">
        <b>${esc(row.salesforce_parent_id || 'ID Salesforce inconnu')}</b>
        <p style="margin:6px 0 0">${formatNumber(row.publication_count)} publication${Number(row.publication_count) > 1 ? 's' : ''} · ${formatNumber(row.comment_count)} commentaire${Number(row.comment_count) > 1 ? 's' : ''}</p>
        <small style="display:block;margin-top:5px;color:#8a5b08">Aucune fiche CRM ne possède cet identifiant Salesforce. Aucun contenu ne sera créé.</small>
      </article>`).join('')}${rows.length > visible.length ? `<small>${formatNumber(rows.length - visible.length)} autre(s) identifiant(s) figurent dans le rapport JSON.</small>` : ''}</div>`;
  };

  const ambiguousRowsHtml = rows => {
    if (!rows?.length) {
      return '<div class="activity-empty">Aucun identifiant Salesforce utilisé par plusieurs fiches.</div>';
    }
    return `<div style="display:grid;gap:10px">${rows.slice(0, 40).map(row => `
      <article class="card" style="box-shadow:none;padding:14px 16px;border-color:#ef9a9a">
        <b>${esc(row.salesforce_parent_id || 'ID Salesforce inconnu')}</b>
        <p style="margin:6px 0 0">Fiches CRM : ${esc((row.crm_contacts || []).join(', ') || 'non renseignées')}</p>
        <small style="display:block;margin-top:5px;color:#9f2d2d">Correspondance bloquée pour éviter de rattacher l’historique à la mauvaise personne.</small>
      </article>`).join('')}</div>`;
  };

  const summaryHtml = (result, final = false) => `
    <div style="display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin-bottom:18px">
      <div class="stat" style="min-height:auto"><small>Fiches CRM retrouvées</small><strong>${formatNumber(result.matched_contacts)}</strong></div>
      <div class="stat" style="min-height:auto"><small>${final ? 'Publications ajoutées' : 'Publications à ajouter'}</small><strong>${formatNumber(result.publications_created)}</strong></div>
      <div class="stat" style="min-height:auto"><small>${final ? 'Publications actualisées' : 'À actualiser'}</small><strong>${formatNumber(result.publications_updated)}</strong></div>
      <div class="stat" style="min-height:auto"><small>${final ? 'Commentaires ajoutés' : 'Commentaires à ajouter'}</small><strong>${formatNumber(result.comments_created)}</strong></div>
      <div class="stat" style="min-height:auto"><small>Fiches modifiées</small><strong>${formatNumber(result.contacts_updated)}</strong></div>
    </div>
    <div class="integration-banner success">
      <div>
        <b>${formatNumber(result.prepared_publications)} publications Salesforce exploitables · ${formatNumber(result.lead_comments)} commentaires de pistes</b>
        <span>${formatNumber(result.publication_csv_rows)} lignes FeedItem analysées, dont ${formatNumber(result.lead_publications)} rattachées à des pistes. ${formatNumber(result.publications_ignored_empty)} changements automatiques sans texte ont été ignorés.</span>
      </div>
    </div>
    <div class="integration-banner" style="margin-top:14px">
      <div>
        <b>Import sans création de personne</b>
        <span>Le rapprochement utilise uniquement l’identifiant Salesforce de la fiche CRM. Les ${formatNumber(result.publications_ignored_non_lead)} éléments rattachés à des tâches, contacts, opportunités ou autres objets sont ignorés pour éviter les doublons avec les relances déjà importées.</span>
      </div>
    </div>
    <div class="integration-banner" style="margin-top:14px">
      <div>
        <b>Auteurs résolus : ${formatNumber(result.authors_resolved)} utilisateur(s) Salesforce</b>
        <span>Les dates, auteurs, textes complets, liens, titres de pièces jointes et fils de commentaires sont conservés. Les fichiers physiques joints ne figurent pas dans ces trois CSV.</span>
      </div>
    </div>
    ${Number(result.unmatched_parent_count || 0) ? `<div class="integration-banner warning" style="margin-top:14px"><div><b>${formatNumber(result.unmatched_parent_count)} identifiant(s) de piste ne correspondent à aucune fiche CRM</b><span>Leur historique restera inchangé. Aucun nouveau contact ne sera créé.</span></div></div>` : ''}
    ${Number(result.ambiguous_parent_count || 0) ? `<div class="integration-banner warning" style="margin-top:14px"><div><b>${formatNumber(result.ambiguous_parent_count)} identifiant(s) sont ambigus</b><span>Ces éléments seront bloqués individuellement.</span></div></div>` : ''}
    <details class="card" open style="box-shadow:none;margin-top:16px">
      <summary style="cursor:pointer;padding:14px 16px;font-weight:750">Fiches qui recevront l’historique (${formatNumber(result.matched_contacts)})</summary>
      <div style="padding:0 16px 16px">${matchedRowsHtml(result.ready_rows || [])}</div>
    </details>
    <details class="card" style="box-shadow:none;margin-top:12px">
      <summary style="cursor:pointer;padding:14px 16px;font-weight:750">Pistes Salesforce sans fiche CRM (${formatNumber(result.unmatched_parent_count)})</summary>
      <div style="padding:0 16px 16px">${unmatchedRowsHtml(result.unmatched_rows || [])}</div>
    </details>
    <details class="card" style="box-shadow:none;margin-top:12px">
      <summary style="cursor:pointer;padding:14px 16px;font-weight:750">Correspondances ambiguës (${formatNumber(result.ambiguous_parent_count)})</summary>
      <div style="padding:0 16px 16px">${ambiguousRowsHtml(result.ambiguous_rows || [])}</div>
    </details>`;

  const downloadReport = result => {
    const copy = { generated_at: new Date().toISOString(), ...result };
    delete copy.preview_token;
    const blob = new Blob([JSON.stringify(copy, null, 2)], { type: 'application/json;charset=utf-8' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `rapport-import-historique-salesforce-${new Date().toISOString().slice(0, 10)}.json`;
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
          <b>Historique Chatter des pistes</b>
          <span>Le CRM importe uniquement les publications rattachées à une fiche déjà reliée à Salesforce. Le contenu sera visible dans un onglet séparé « Historique Salesforce » et ne polluera pas le fil d’actualité interne.</span>
        </div>
      </div>
      <div class="fields">
        <div class="field full">
          <label>1. Publications — export FeedItem</label>
          <input id="salesforceChatterPublicationsFile" type="file" accept=".csv,.txt,text/csv,text/plain">
          <small class="field-help">Fichier attendu : salesforce-publications.csv</small>
        </div>
        <div class="field full">
          <label>2. Commentaires — export FeedComment</label>
          <input id="salesforceChatterCommentsFile" type="file" accept=".csv,.txt,text/csv,text/plain">
          <small class="field-help">Fichier attendu : salesforce-commentaires.csv</small>
        </div>
        <div class="field full">
          <label>3. Utilisateurs — export User</label>
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

    const analyze = async () => {
      const publicationsFile = publicationsInput.files?.[0] || null;
      const commentsFile = commentsInput.files?.[0] || null;
      const usersFile = usersInput.files?.[0] || null;
      previewToken = '';
      previewResult = null;
      confirm.disabled = true;
      reportButton.hidden = true;

      if (!publicationsFile || !commentsFile || !usersFile) {
        preview.innerHTML = '<div class="activity-empty">Sélectionne les trois fichiers pour lancer l’aperçu.</div>';
        return;
      }
      if (publicationsFile.size > 30 * 1024 * 1024 || commentsFile.size > 15 * 1024 * 1024 || usersFile.size > 5 * 1024 * 1024) {
        preview.innerHTML = '<div class="integration-banner warning"><div><b>Un fichier est trop volumineux</b><span>Limites : 30 Mo pour les publications, 15 Mo pour les commentaires et 5 Mo pour les utilisateurs.</span></div></div>';
        return;
      }

      const currentSequence = ++sequence;
      preview.innerHTML = '<div class="activity-empty">Analyse des publications, commentaires, auteurs et identifiants des fiches CRM…</div>';
      try {
        const result = await sendFiles(publicationsFile, commentsFile, usersFile, true);
        if (currentSequence !== sequence) return;
        previewResult = result;
        previewToken = result.preview_token || '';
        preview.innerHTML = summaryHtml(result, false);
        confirm.disabled = Number(result.contacts_updated || 0) === 0 || !previewToken;
        reportButton.hidden = false;
        reportButton.onclick = () => downloadReport(result);
      } catch (error) {
        if (currentSequence !== sequence) return;
        preview.innerHTML = `<div class="integration-banner warning"><div><b>Analyse impossible</b><span>${esc(error.message)}</span></div></div>`;
      }
    };

    publicationsInput.addEventListener('change', analyze);
    commentsInput.addEventListener('change', analyze);
    usersInput.addEventListener('change', analyze);

    confirm.onclick = async () => {
      const publicationsFile = publicationsInput.files?.[0] || null;
      const commentsFile = commentsInput.files?.[0] || null;
      const usersFile = usersInput.files?.[0] || null;
      if (!publicationsFile || !commentsFile || !usersFile || !previewToken || !previewResult) return;

      const message = `Confirmer l’import ?\n\n${formatNumber(previewResult.publications_created)} publication(s) et ${formatNumber(previewResult.comments_created)} commentaire(s) seront ajoutés sur ${formatNumber(previewResult.contacts_updated)} fiche(s). Aucune personne ne sera créée.`;
      if (!window.confirm(message)) return;

      confirm.disabled = true;
      publicationsInput.disabled = true;
      commentsInput.disabled = true;
      usersInput.disabled = true;
      confirm.textContent = 'Import en cours…';
      preview.innerHTML = '<div class="activity-empty">Enregistrement atomique de l’historique Salesforce…</div>';
      try {
        const result = await sendFiles(publicationsFile, commentsFile, usersFile, false, previewToken);
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
        publicationsInput.disabled = false;
        commentsInput.disabled = false;
        usersInput.disabled = false;
      }
    };
  };
})();
