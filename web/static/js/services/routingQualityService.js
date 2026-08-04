// ============ services/routingQualityService.js ============
// P4 路由追踪域服务：统一 /api/routing-quality 数据获取与 store 切片写入
// 替代原有 raw api.fetch，支持共享时间窗与切片管理
(function (global) {
  'use strict';

  const DOMAIN = 'routingQuality';

  function _setSlice(key, value) {
    global.store.setSlice(DOMAIN, key, value);
  }

  async function loadAggregate() {
    try {
      const data = await api.fetch('/api/routing-quality/aggregate');
      _setSlice('aggregate', data);
      return data;
    } catch (e) {
      _setSlice('aggregate', { available: false, error: String(e.message || e) });
      return { available: false, error: String(e.message || e) };
    }
  }

  async function loadStats() {
    try {
      const data = await api.fetch('/api/routing-quality/stats');
      _setSlice('stats', data);
      return data;
    } catch (e) {
      _setSlice('stats', { available: false, error: String(e.message || e) });
      return { available: false, error: String(e.message || e) };
    }
  }

  async function loadPage(page = 1, size = 15) {
    try {
      const data = await api.fetch(`/api/routing-quality?page=${page}&size=${size}`);
      _setSlice(`page_${page}`, data);
      return data;
    } catch (e) {
      _setSlice(`page_${page}`, { available: false, error: String(e.message || e) });
      return { available: false, error: String(e.message || e) };
    }
  }

  global.RoutingQualityService = { loadAggregate, loadStats, loadPage };
})(window);
