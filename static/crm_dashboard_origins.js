(function patchDashboardOrigins() {
  'use strict';

  const originalDashboard = dashboard;
  const originalDashboardOrigin = dashboardOrigin;
  const originalDashboardGroup = dashboardGroup;
  const originalDashboardKpi = dashboardKpi;
  const originalLeadOriginFilterOptions = leadOriginFilterOptions;
  const requiredOrigins = ['Saisie manuelle', 'Google Ads'];

  dashboardOrigin = function normalizedDashboardOrigin(contact) {
    const gclid = String(contact.gclid || contact.formulaire?.gclid || '').trim();
    if (gclid) return 'Google Ads';

    const origin = originalDashboardOrigin(contact);
    const normalized = String(origin)
      .toLocaleLowerCase('fr-FR')
      .replace(/[_-]+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
    if (normalized === 'ajout manuel' || normalized === 'saisie manuelle') {
      return 'Saisie manuelle';
    }
    return origin;
  };

  dashboardGroup = function dashboardGroupWithRequiredOrigins(list, labelFor) {
    const groups = originalDashboardGroup(list, labelFor);
    if (labelFor !== dashboardOrigin) return groups;

    requiredOrigins.forEach(label => {
      if (!groups.some(group => group.label === label)) {
        groups.push({
          label,
          contacts: [],
          count: 0,
          contacted: 0,
          appointments: 0,
          converted: 0,
        });
      }
    });
    return groups.sort(
      (first, second) =>
        second.count - first.count ||
        second.converted - first.converted ||
        first.label.localeCompare(second.label, 'fr'),
    );
  };

  dashboardKpi = function dashboardKpiWithoutMeta(label, ...args) {
    return label === 'Pistes META' ? '' : originalDashboardKpi(label, ...args);
  };

  leadOriginFilterOptions = function normalizedLeadOriginFilterOptions(list) {
    return originalLeadOriginFilterOptions(list).filter(
      origin => origin !== 'Ajout manuel',
    );
  };

  dashboard = function dashboardWithClearOriginDescription() {
    const html = originalDashboard();
    return typeof html === 'string'
      ? html.replace(
          'Les origines sont lues directement dans les fiches : META n’est plus exclu.',
          'Les origines incluent notamment META, la saisie manuelle et Google Ads.',
        )
      : html;
  };
})();
