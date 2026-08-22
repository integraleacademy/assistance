(function patchDashboardOrigins() {
  'use strict';

  const originalDashboard = dashboard;
  const originalDashboardOrigin = dashboardOrigin;
  const originalDashboardKpi = dashboardKpi;

  dashboardOrigin = function normalizedDashboardOrigin(contact) {
    const gclid = String(contact.gclid || contact.formulaire?.gclid || '').trim();
    return gclid ? 'Google Ads' : originalDashboardOrigin(contact);
  };

  dashboardKpi = function dashboardKpiWithoutMeta(label, ...args) {
    return label === 'Pistes META' ? '' : originalDashboardKpi(label, ...args);
  };

  dashboard = function dashboardWithClearOriginDescription() {
    const html = originalDashboard();
    return typeof html === 'string'
      ? html.replace(
          'Les origines sont lues directement dans les fiches : META n’est plus exclu.',
          'Les origines utilisent les huit valeurs canoniques du CRM.',
        )
      : html;
  };
})();
