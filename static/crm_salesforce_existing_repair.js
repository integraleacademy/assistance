(() => {
  if (!window.CRM_CONFIG?.is_admin) return;
  if (document.querySelector('#salesforceExistingRepair')) return;

  const adminMenu = document.querySelector('#adminToolsMenu');
  if (!adminMenu) return;

  const formatNumber = value => new Intl.NumberFormat('fr-FR').format(Number(value || 0));
  const valueLabel = value => String(value || '').trim() || 'Non renseigné';

  const button = document.createElement('button');
  button.id = 'salesforceExistingRepair';
  button.type = 'button';
  button.textContent = '↻ Corriger les fiches existantes Salesforce';
  const migrationButton = document.querySelector('#salesforceImport');
  if (migrationButton?.parentNode === adminMenu) {
    migrationButton.insertAdjacentElement('afterend', button);
  } else {
    adminMenu.append(button);
  }

  function responseFailure(response, payload, responseText) {
    if (payload?.error) return payload.error;
    const reasons = {
      400: 'Le serveur a refusé le fichier. Utilise un export CSV de pistes Salesforce avec les lignes de détail.',
      401: 'Ta session a expiré. Recharge la page puis reconnecte-toi.',
      403: 'Seul un administrateur peut corriger les fiches Salesforce.',
      404: 'Le service de correction Salesforce n’est pas disponible.',
      409: 'Le CRM a changé depuis l’aperçu. Relance l’analyse avant de confirmer.',
      413: 'Le fichier dépasse la limite de 20 Mo.',
      500: 'Le serveur a rencontré une erreur pendant la correction.',
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
      response = await fetch('/api/crm/repair-existing-salesforce', {
        method: 'POST',
        body,
        credentials: 'same-origin',
      });
    } catch (_error) {
      throw new Error('Impossible de joindre le serveur. Vérifie ta connexion puis réessaie.');
    }

    const responseText = await response.text();
    let payload = null;
    try {
      payload = responseText ? JSON.parse(responseText) : null;
    } catch (_error) {
      // Une page HTML du proxy ne doit pas masquer le statut HTTP.
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

  const changesFor = row => {
    const labels = {
      formation: 'Formation',
      lieu: 'Lieu',
      dates_formation: 'Dates',
      statut: 'Statut',
      statut_secondaire: '2e timeline',
      origine: 'Origine',
    };
    return Object.entries(labels)
      .filter(([key]) => valueLabel(row.before?.[key]) !== valueLabel(row.after?.[key]))
      .map(([key, label]) => `${label} : ${valueLabel(row.before?.[key])} → ${valueLabel(row.after?.[key])}`);
  };

  function readyTable(rows) {
    if (!rows?.length) return '<div class="activity-empty">Aucune fiche correspondante.</div>';
    return `
      <div style="overflow:auto;max-height:460px">
        <table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead><tr>
            <th style="text-align:left;padding:9px;border-bottom:1px solid #dfe5ee">Personne</th>
            <th style="text-align:left;padding:9px;border-bottom:1px solid #dfe5ee">Rapprochement</th>
            <th style="text-align:left;padding:9px;border-bottom:1px solid #dfe5ee">Corrections prévues</th>
          </tr></thead>
          <tbody>${rows.map(row => {
            const changes = changesFor(row);
            return `<tr>
              <td style="padding:9px;border-bottom:1px solid #eef1f5"><b>${esc(row.person || 'Sans nom')}</b><small style="display:block">${esc(row.salesforce_id || '')}</small></td>
              <td style="padding:9px;border-bottom:1px solid #eef1f5">${esc(row.match_method || '')}</td>
              <td style="padding:9px;border-bottom:1px solid #eef1f5">${changes.length ? changes.map(change => `<span style="display:block">${esc(change)}</span>`).join('') : '<span>Aucune différence</span>'}</td>
            </tr>`;
          }).join('')}</tbody>
        </table>
      </div>`;
  }

  function blockedTable(rows) {
    if (!rows?.length) return '';
    return `
      <details class="card" style="box-shadow:none;margin-top:16px">
        <summary style="cursor:pointer;padding:14px 16px;font-weight:700">Voir les lignes non appliquées (${formatNumber(rows.length)})</summary>
        <div style="padding:0 16px 16px;display:grid;gap:8px;max-height:340px;overflow:auto">
          ${rows.map(row => `<div style="padding:10px 12px;border:1px solid #e1c7c7;border-radius:10px;background:#fff"><b>${esc(row.person || row.salesforce_id || 'Sans nom')}</b><span style="display:block;margin-top:4px">${esc(row.reason || 'Vérification manuelle nécessaire.')}</span></div>`).join('')}
        </div>
      </details>`;
  }

  function counterRows(values) {
    const entries = Object.entries(values || {});
    return entries.length
      ? entries.map(([label, count]) => `<span><b>${formatNumber(count)}</b> ${esc(label)}</span>`).join('')
      : '<span>Aucune donnée</span>';
  }

  function summaryHtml(result, final = false) {
    const blocked = Number(result.not_found || 0)
      + Number(result.ambiguous || 0)
      + Number(result.without_crm_formation || 0)
      + Number(result.preserved_disqualified || 0);
    return `
      <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:4px 0 18px">
        <div class="stat" style="min-height:auto"><small>Lignes préparées</small><strong>${formatNumber(result.prepared_rows)}</strong></div>
        <div class="stat" style="min-height:auto"><small>Fiches retrouvées</small><strong>${formatNumber(result.matched)}</strong></div>
        <div class="stat" style="min-height:auto"><small>${final ? 'Corrigées' : 'À corriger'}</small><strong>${formatNumber(result.updated)}</strong></div>
        <div class="stat" style="min-height:auto"><small>Non appliquées</small><strong>${formatNumber(blocked)}</strong></div>
      </div>
      <div class="integration-banner success">
        <div>
          <b>Aucune nouvelle personne ne sera créée</b>
          <span>${formatNumber(result.updated)} fiche(s) ${final ? 'corrigée(s)' : 'à corriger'} · ${formatNumber(result.unchanged)} déjà identique(s) · toutes les années Salesforce acceptées.</span>
        </div>
      </div>
      <div class="integration-banner warning" style="margin-top:16px">
        <div>
          <b>Salesforce est prioritaire dans ce mode</b>
          <span>Formation, lieu, dates, origine et statuts peuvent être remplacés. Les relances, activités, commentaires et inscriptions déjà converties restent protégés.</span>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin-top:16px">
        <section class="card" style="box-shadow:none"><div class="card-head"><h3>Années</h3></div><div class="template-list" style="padding:12px;display:grid;gap:8px">${counterRows(result.year_counts)}</div></section>
        <section class="card" style="box-shadow:none"><div class="card-head"><h3>Formations</h3></div><div class="template-list" style="padding:12px;display:grid;gap:8px">${counterRows(result.formation_counts)}</div></section>
        <section class="card" style="box-shadow:none"><div class="card-head"><h3>Statuts principaux</h3></div><div class="template-list" style="padding:12px;display:grid;gap:8px">${counterRows(result.status_counts)}</div></section>
        <section class="card" style="box-shadow:none"><div class="card-head"><h3>Deuxièmes statuts</h3></div><div class="template-list" style="padding:12px;display:grid;gap:8px">${counterRows(result.secondary_status_counts)}</div></section>
      </div>
      <details class="card" style="box-shadow:none;margin-top:16px" open>
        <summary style="cursor:pointer;padding:14px 16px;font-weight:700">Voir les fiches retrouvées (${formatNumber(result.ready_rows?.length || 0)})</summary>
        <div style="padding:0 16px 16px">${readyTable(result.ready_rows || [])}</div>
      </details>
      ${blockedTable(result.blocked_rows || [])}`;
  }

  button.onclick = () => {
    modal(
      'Corriger les fiches existantes depuis Salesforce',
      `<div class="integration-banner" style="margin-bottom:18px">
        <div>
          <b>Pour les anciennes pistes déjà présentes dans le CRM</b>
          <span>Utilise un export de pistes Salesforce sans filtre d’année, ou filtré sur les personnes à corriger. Ce mode ne crée jamais de fiche : il rapproche uniquement les contacts existants par identifiant Salesforce, e-mail ou téléphone.</span>
        </div>
      </div>
      <div class="fields">
        <div class="field full">
          <label>Export CSV des pistes Salesforce</label>
          <input id="salesforceExistingRepairFile" type="file" accept=".csv,.txt,text/csv,text/plain">
          <small class="field-help">Inclure au minimum : ID de piste, prénom, nom, e-mail, téléphone, statut, type de formation, lieu et dates souhaitées.</small>
        </div>
      </div>
      <div id="salesforceExistingRepairPreview" style="margin-top:18px"><div class="activity-empty">Sélectionne le fichier pour afficher l’aperçu sans modifier le CRM.</div></div>`,
      '<button class="btn" id="salesforceExistingRepairCancel">Annuler</button><button class="btn blue" id="salesforceExistingRepairConfirm" disabled>Corriger les fiches existantes</button>',
      'salesforce-existing-repair-modal'
    );

    const fileInput = document.querySelector('#salesforceExistingRepairFile');
    const preview = document.querySelector('#salesforceExistingRepairPreview');
    const confirm = document.querySelector('#salesforceExistingRepairConfirm');
    const cancel = document.querySelector('#salesforceExistingRepairCancel');
    cancel.onclick = closeModal;

    let selectedFile = null;
    let previewToken = '';
    let previewResult = null;
    let sequence = 0;

    fileInput.onchange = async () => {
      selectedFile = fileInput.files?.[0] || null;
      previewToken = '';
      previewResult = null;
      confirm.disabled = true;
      if (!selectedFile) {
        preview.innerHTML = '<div class="activity-empty">Sélectionne le fichier pour afficher l’aperçu.</div>';
        return;
      }
      if (selectedFile.size > 20 * 1024 * 1024) {
        preview.innerHTML = '<div class="integration-banner warning"><div><b>Fichier trop volumineux</b><span>La limite est de 20 Mo.</span></div></div>';
        return;
      }
      const currentSequence = ++sequence;
      preview.innerHTML = '<div class="activity-empty">Analyse de toutes les années et comparaison avec les fiches CRM existantes…</div>';
      try {
        const result = await sendFile(selectedFile, true);
        if (currentSequence !== sequence) return;
        previewResult = result;
        previewToken = result.preview_token || '';
        preview.innerHTML = summaryHtml(result, false);
        confirm.disabled = !previewToken || Number(result.updated || 0) === 0;
      } catch (error) {
        if (currentSequence !== sequence) return;
        preview.innerHTML = `<div class="integration-banner warning"><div><b>Analyse impossible</b><span>${esc(error.message)}</span></div></div>`;
      }
    };

    confirm.onclick = async () => {
      if (!selectedFile || !previewToken || !previewResult) return;
      const prompt = `Confirmer la correction de ${formatNumber(previewResult.updated)} fiche(s) existante(s) ?\n\nAucune nouvelle personne ne sera créée. Les valeurs Salesforce renseignées deviendront prioritaires.`;
      if (!window.confirm(prompt)) return;

      confirm.disabled = true;
      fileInput.disabled = true;
      confirm.textContent = 'Correction en cours…';
      preview.innerHTML = '<div class="activity-empty">Mise à jour atomique des fiches existantes…</div>';
      try {
        const result = await sendFile(selectedFile, false, previewToken);
        contacts = await api(`/api/crm/contacts?section=${encodeURIComponent(window.CRM_CONFIG.section)}`);
        preview.innerHTML = summaryHtml(result, true);
        confirm.textContent = 'Terminé';
        cancel.textContent = 'Fermer';
        render();
        toast(`${formatNumber(result.updated)} fiche(s) corrigée(s) depuis Salesforce`);
      } catch (error) {
        preview.innerHTML = `<div class="integration-banner warning"><div><b>La correction n’a pas abouti</b><span>${esc(error.message)}</span></div></div>`;
        fileInput.disabled = false;
        confirm.disabled = false;
        confirm.textContent = 'Réessayer';
      }
    };
  };
})();
