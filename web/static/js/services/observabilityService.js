// ============ services/observabilityService.js ============
// 可观测性域服务：统一成本/质量/置信度/趋势/引文的数据获取与 store 写入。
// metrics / cost-quality / routetrace 三页共享 timeRange，杜绝口径不一致。
// 依赖：api.fetch、window.store（core/store.js）
//
// 数据流：页面 load 时调用 loadAll() → 写 store 切片(data.observability.*)；
//        卡片经 store.subscribeSlice('observability', key, render) 订阅并重渲染。
//        本服务只负责「取数 + 写切片」，不触碰 DOM。

(function (global) {
  'use strict';

  const DOMAIN = 'observability';

  const _state = {
    timeRangeHours: 24,   // 可观测页共享时间窗（小时）
  };

  function _setSlice(key, value) {
    global.store.setSlice(DOMAIN, key, value);
  }

  function setTimeRange(hours) {
    _state.timeRangeHours = Number(hours) || 24;
    // UI 状态写入 store.ui，供跨可观测页共享订阅（切换时间窗三页联动）
    global.store.set('ui.observabilityTimeRange', _state.timeRangeHours);
    return _state.timeRangeHours;
  }

  function getTimeRange() {
    return _state.timeRangeHours;
  }

  async function loadSummary() {
    const hours = _state.timeRangeHours;
    try {
      const data = await api.fetch(`/api/cost-quality/summary?hours=${hours}`);
      _setSlice('summary', data);
      return data;
    } catch (e) {
      _setSlice('summary', { available: false, error: String(e.message || e) });
      return null;
    }
  }

  // 置信度分布：归一化为 [{bucket, count}]（与 summary.confidence_hist 同形）
  async function loadConfidenceHist() {
    const hours = _state.timeRangeHours;
    try {
      const data = await api.fetch(`/api/cost-quality/confidence-hist?hours=${hours}`);
      const hist = (data && data.hist) || [];
      _setSlice('confidenceHist', hist);
      return hist;
    } catch (e) {
      _setSlice('confidenceHist', []);
      return [];
    }
  }

  async function loadTrend(days) {
    const d = Number(days) || 7;
    try {
      const data = await api.fetch(`/api/cost-quality/trend?days=${d}`);
      const series = (data && data.series) || [];
      _setSlice('trend', series);
      return series;
    } catch (e) {
      _setSlice('trend', []);
      return [];
    }
  }

  async function loadCitations(limit) {
    const l = Number(limit) || 20;
    try {
      const data = await api.fetch(`/api/cost-quality/citations?limit=${l}`);
      const items = (data && data.items) || [];
      _setSlice('citations', items);
      return items;
    } catch (e) {
      _setSlice('citations', []);
      return [];
    }
  }

  // 一次性拉取所有可观测切片（页面 load 时调用）
  async function loadAll(opts) {
    opts = opts || {};
    const [summary, hist, trend, citations] = await Promise.all([
      loadSummary(),
      loadConfidenceHist(),
      loadTrend(opts.days || 7),
      loadCitations(opts.limit || 20),
    ]);
    return { summary, hist, trend, citations };
  }

  global.ObservabilityService = {
    DOMAIN,
    state: _state,
    setTimeRange, getTimeRange,
    loadSummary, loadConfidenceHist, loadTrend, loadCitations, loadAll,
  };
})(window);
