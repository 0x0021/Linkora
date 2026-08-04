// ============ services/dashboardReliabilityService.js ============
// P5 Dashboard 可靠性域服务：集中获取 backpressure/debounce/poller 状态
(function (global) {
  'use strict';

  async function loadReliability() {
    try {
      const [bp, db, poller] = await Promise.all([
        api.fetch('/api/backpressure-metrics'),
        api.fetch('/api/debounce-metrics'),
        api.fetch('/api/poller-status')
      ]);
      return { bp, db, poller, available: true };
    } catch (e) {
      return { available: false, error: String(e.message || e), bp:null, db:null, poller:null };
    }
  }

  global.DashboardReliabilityService = { loadReliability };
})(window);
