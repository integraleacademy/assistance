(() => {
  const fold = value => String(value || '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLocaleLowerCase('fr-FR')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();

  const canonicalLocation = value => {
    const raw = String(value || '').trim();
    const normalized = fold(raw);
    if (!normalized) return '';
    if (
      normalized.includes('cote d azur')
      || normalized.includes('cote azur')
      || normalized.includes('puget sur argens')
      || normalized.includes('saint raphael')
      || normalized.includes('frejus')
      || normalized.includes('paca')
    ) return 'Côte d’Azur';
    if (
      normalized.includes('auvergne')
      || normalized.includes('aurillac')
      || normalized.includes('arpajon sur cere')
      || normalized.includes('cantal')
    ) return 'Auvergne';
    if (normalized.includes('paris') || normalized.includes('ile de france')) return 'Paris';
    return raw;
  };

  const currentContact = () => {
    const id = new URLSearchParams(window.location.search).get('fiche');
    if (!id) return null;
    try {
      return contacts.find(contact => String(contact.id) === String(id)) || null;
    } catch (_error) {
      return null;
    }
  };

  const cleanStoredSessionLabel = select => {
    if (!select) return;
    [...select.options].forEach(option => {
      const current = option.textContent || '';
      const cleaned = current.replace(/\s+—\s+réponse META\s*$/i, '');
      if (cleaned !== current) option.textContent = cleaned;
    });
  };

  let applyingStoredLocation = false;

  const ensureStoredLocation = (select, contact) => {
    if (!select || !contact || applyingStoredLocation) return;
    const expected = canonicalLocation(contact.lieu);
    if (!expected) return;

    const matchingOption = [...select.options].find(option => (
      canonicalLocation(option.value || option.textContent) === expected
    ));

    applyingStoredLocation = true;
    try {
      if (matchingOption) {
        if (select.value !== matchingOption.value) {
          matchingOption.selected = true;
          select.value = matchingOption.value;
        }
      } else {
        // Le formulaire historique ne proposait que les centres possédant une
        // session configurée. Un lieu valide comme Paris disparaissait alors
        // et le premier centre disponible (souvent Auvergne) était affiché.
        const option = document.createElement('option');
        option.value = expected;
        option.textContent = `${expected} — lieu enregistré`;
        option.dataset.storedLocation = '1';
        option.selected = true;
        select.prepend(option);
        select.value = expected;
      }
    } finally {
      applyingStoredLocation = false;
    }
  };

  const installOnForm = () => {
    const form = document.querySelector('#contactForm');
    if (!form || form.dataset.salesforceLocationDisplayFix === '1') return;
    const contact = currentContact();
    if (!contact) return;

    form.dataset.salesforceLocationDisplayFix = '1';
    const canonical = canonicalLocation(contact.lieu);
    if (canonical && canonical !== contact.lieu) {
      contact.lieu = canonical;
    }

    const locationSelect = form.querySelector('[name="lieu"]');
    ensureStoredLocation(locationSelect, contact);
    if (locationSelect) {
      const locationObserver = new MutationObserver(() => {
        ensureStoredLocation(locationSelect, currentContact() || contact);
      });
      locationObserver.observe(locationSelect, { childList: true, subtree: true });
    }

    const sessionSelect = form.querySelector('[name="dates_formation"]');
    cleanStoredSessionLabel(sessionSelect);
    if (sessionSelect) {
      const sessionObserver = new MutationObserver(() => cleanStoredSessionLabel(sessionSelect));
      sessionObserver.observe(sessionSelect, { childList: true, subtree: true });
    }
  };

  const observer = new MutationObserver(installOnForm);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  installOnForm();
})();
