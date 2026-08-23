(function patchDashboardOrigins() {
  'use strict';

  const originalDashboard = dashboard;
  const originalDashboardKpi = dashboardKpi;

  // Origin ordering is now normalized by crmOriginLabels. A late GCLID is an
  // attribution detail and must not replace an earlier primary origin.

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
