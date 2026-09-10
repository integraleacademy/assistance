/* Exclusion META dans le tableau Pistes natif (crm.js), pas le Kanban Workspace. */
(function () {
  'use strict';
  let installed = false;
  let excludeMeta = false;
  let bindingInProgress = null;

  function install() {
    if (installed) return;
    const nativeListPage = window.listPage;
    const nativeBindList = window.bindList;
    const nativeContacts = window.listContactsForType;
    const origin = window.canonicalCrmOrigin;
    if (![nativeListPage, nativeBindList, nativeContacts, origin].every(fn => typeof fn === 'function')) return;

    // Les tableaux sont des copies locales : aucune fiche ni collection CRM n'est modifiée.
    const visibleContacts = rows => rows.filter(contact => !excludeMeta || origin(contact) !== 'META');
    window.listContactsForType = function (type, ...args) {
      const rows = nativeContacts.call(this, type, ...args);
      if (type !== 'pistes') return rows;
      const visible = visibleContacts(rows);
      if (bindingInProgress) Object.assign(bindingInProgress, {rows, visible});
      return visible;
    };

    window.listPage = function (type, ...args) {
      const html = nativeListPage.call(this, type, ...args);
      if (type !== 'pistes') return html;
      const control = `<label class="reminder-origin-exclusion lead-origin-exclusion" for="leadExcludeMeta"><input type="checkbox" id="leadExcludeMeta" aria-controls="resultTable" ${excludeMeta ? 'checked' : ''}> Exclure META</label>`;
      return html.replace(/(<select\b[^>]*\bid="originFilter"[^>]*>[\s\S]*?<\/select>)/, '$1' + control);
    };

    window.bindList = function (type, ...args) {
      if (type !== 'pistes') return nativeBindList.call(this, type, ...args);
      const binding = {};
      bindingInProgress = binding;
      try {
        nativeBindList.call(this, type, ...args);
      } finally {
        bindingInProgress = null;
      }
      const checkbox = document.querySelector('#leadExcludeMeta');
      if (!checkbox || !binding.visible) return;
      checkbox.checked = excludeMeta;
      checkbox.addEventListener('change', () => {
        excludeMeta = checkbox.checked;
        // bindList conserve sa base locale dans une fermeture. La renouveler en place
        // réutilise son filtrage, ses compteurs et sa sélection, sans relier les événements
        // ni réinitialiser la recherche, la formation, la session, le lieu ou le tri.
        const visible = visibleContacts(binding.rows);
        binding.visible.length = 0;
        for (const contact of visible) binding.visible.push(contact);
        document.querySelector('#listSearch')?.dispatchEvent(new Event('input', {bubbles: true}));
      });
    };

    installed = true;
    // L'installation est appelée juste après crm.js ; couvre aussi un bootstrap déjà rendu.
    if (window.CRM_CONFIG?.section === 'pistes' && document.querySelector('#listSearch')) window.render();
  }

  window.CRMPistesOriginFilter = {install};
})();
