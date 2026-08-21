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

  const installOnForm = () => {
    const form = document.querySelector('#contactForm');
    if (!form || form.dataset.salesforceLocationDisplayFix === '1') return;
    const contact = currentContact();
    if (!contact) return;

    form.dataset.salesforceLocationDisplayFix = '1';
    const canonical = canonicalLocation(contact.lieu);
    if (canonical && canonical !== contact.lieu) {
      // Le formulaire historique compare les lieux caractère par caractère.
      // Canonicaliser avant de relancer son calcul évite que « Côte d'Azur »
      // soit remplacé visuellement par le premier centre, souvent Auvergne.
      contact.lieu = canonical;
      const formation = form.querySelector('[name="formation"]');
      if (formation) formation.dispatchEvent(new Event('input', { bubbles: true }));
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
