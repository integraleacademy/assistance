(() => {
  if (!window.CRM_CONFIG?.is_admin) return;

  const ENDPOINT = '/api/crm/import-salesforce-relances';
  let latestPayload = null;

  const formatNumber = value => new Intl.NumberFormat('fr-FR').format(Number(value || 0));
  const csvCell = value => `"${String(value ?? '').replaceAll('"', '""')}"`;

  function manualRows(payload) {
    return Array.isArray(payload?.manual_review_rows)
      ? payload.manual_review_rows
      : [];
  }

  function downloadManualCsv(payload) {
    const rows = manualRows(payload);
    if (!rows.length) return;

    const headers = [
      'Catégorie',
      'Action recommandée',
      'Importable après vérification',
      'Personne',
      'E-mail',
      'Téléphone',
      'Date de relance',
      'Objet',
      'Attribué à',
      'Priorité',
      'Statut Salesforce',
      'Commentaires',
      'ID activité Salesforce',
      'Type de relation',
      'Société / Compte',
      'Motif',
      'Méthode de rapprochement',
      'ID fiche CRM',
      'Nom fiche CRM',
      'Statut fiche CRM',
      'Formation fiche CRM',
    ];

    const fields = [
      'category',
      'recommended_action',
      'importable_after_check',
      'person',
      'email',
      'phone',
      'scheduled_date',
      'subject',
      'owner',
      'priority',
      'salesforce_status',
      'comments',
      'activity_id',
      'relation_type',
      'company',
      'reason',
      'match_method',
      'crm_contact_id',
      'crm_contact_name',
      'crm_contact_status',
      'crm_contact_formation',
    ];

    const lines = [headers.map(csvCell).join(';')];
    rows.forEach(row => {
      lines.push(fields.map(field => {
        const value = field === 'importable_after_check'
          ? (row[field] ? 'Oui' : 'Non')
          : row[field];
        return csvCell(value);
      }).join(';'));
    });

    const blob = new Blob([
      '\ufeff',
      lines.join('\r\n'),
    ], { type: 'text/csv;charset=utf-8' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `liste-complete-anomalies-relances-salesforce-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
  }

  function enhanceModal(payload) {
    latestPayload = payload;
    const rows = manualRows(payload);
    const preview = document.querySelector('#salesforceRelancesPreview');
    const reportButton = document.querySelector('#salesforceRelancesReport');
    if (!preview || !reportButton) return;

    let button = document.querySelector('#salesforceRelancesManualCsv');
    if (!button) {
      button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn';
      button.id = 'salesforceRelancesManualCsv';
      button.textContent = 'Télécharger la liste complète des anomalies';
      reportButton.insertAdjacentElement('afterend', button);
    }
    button.hidden = !rows.length;
    button.onclick = () => downloadManualCsv(latestPayload);

    let banner = preview.querySelector('#salesforceRelancesFullAnomalies');
    if (!rows.length) {
      if (banner) banner.remove();
      return;
    }
    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'salesforceRelancesFullAnomalies';
      banner.className = 'integration-banner success';
      banner.style.marginTop = '16px';
      const warning = preview.querySelector('.integration-banner.warning');
      if (warning) warning.insertAdjacentElement('afterend', banner);
      else preview.prepend(banner);
    }

    const counts = payload.manual_review_counts || {};
    const labels = Object.entries(counts)
      .map(([label, count]) => `${formatNumber(count)} ${label}`)
      .join(' · ');
    banner.innerHTML = `
      <div>
        <b>Liste exhaustive disponible : ${formatNumber(rows.length)} ligne${rows.length > 1 ? 's' : ''}</b>
        <span>${labels}. Le bouton ci-dessous télécharge toutes les personnes et toutes les informations nécessaires au traitement manuel, sans limite à 30 exemples.</span>
      </div>`;
  }

  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const response = await nativeFetch(...args);
    const url = typeof args[0] === 'string'
      ? args[0]
      : String(args[0]?.url || '');
    if (url.includes(ENDPOINT)) {
      response.clone().json().then(payload => {
        window.setTimeout(() => enhanceModal(payload), 0);
        window.setTimeout(() => enhanceModal(payload), 120);
      }).catch(() => {});
    }
    return response;
  };
})();
