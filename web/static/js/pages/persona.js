// ============ pages/persona.js ============
// 主人风格画像页面：
// - 加载自动画像（AI 生成 / 统计生成）+ 手动覆盖 + few-shot
// - SVG 雷达图（5 维科学风格维度：表达长度 / 正式程度 / 口语程度 / 情感温度 / 互动提问）
// - 手动覆盖开关 + 编辑
// - Few-shot 样例增删改（整批提交）
// - 重新分析按钮

(function () {
  'use strict';

  // ---------- 5 维雷达（科学沟通风格维度，映射到 compute_style_profile 输出字段） ----------
  // 维度选取原则：彼此正交、可独立量化，覆盖「量 / 正式度 / 口语度 / 情感 / 互动」五类语言学特征。
  // 原「开场白」(top_openers 计数) 非连续风格维度，已改为在描述区以标签展示。
  const RADAR_DIMS = [
    { key: 'avg_len',      label: '表达长度', max: 80 },    // 平均字数 → 表达量
    { key: 'polite_rate',  label: '正式程度', max: 1.0 },   // 敬语/礼貌词率 → 正式度
    { key: 'casual_rate',  label: '口语程度', max: 1.0 },   // 口语词率 → 口语化
    { key: 'emoji_rate',   label: '情感温度', max: 1.0 },   // emoji 率 → 情感表达
    { key: 'question_rate',label: '互动提问', max: 1.0 },   // 提问率 → 互动性
  ];

  // ---------- 入口 ----------
  async function loadPersonaPage() {
    try {
      const data = await api.fetch('/api/persona');
      if (data.error) {
        const msg = data.error === 'unauthorized' ? '未登录或会话已过期，请重新登录' : ('API 返回错误：' + data.error + (data.status ? ' (HTTP ' + data.status + ')' : ''));
        showErrorBanner(msg);
        renderEmpty();
        return;
      }
      hideErrorBanner();
      renderAuto(data.auto_profile || {});
      renderOverride(data.override || { enabled: false, prompt: '' });
      renderFewShot(data.few_shot_examples || []);
      renderStatus(data);
      renderQuality(data);
      renderColdStart(data);
      renderPlatformBadge();
      renderSource(data.source || {}, data.auto_profile || {});
    } catch (e) {
      console.error('[persona] 加载失败', e);
      showErrorBanner('加载失败：' + (e && e.message ? e.message : e));
      renderEmpty();
    }
  }

  // ---------- 抽取来源面板 ----------
  function renderSource(src, profile) {
    const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = (v === null || v === undefined || v === '') ? '—' : v; };
    const platName = (window.store && window.store.getPlatform) ? (window._platformNames && window._platformNames[src.platform] || src.platform) : src.platform;
    set('ps-src-platform', platName || src.platform || '—');
    set('ps-src-owner', src.owner || '未配置');
    set('ps-src-total', src.total_messages == null ? '—' : src.total_messages);
    set('ps-src-owner-msg', src.owner_messages == null ? '—' : src.owner_messages);
    set('ps-src-limit', src.sample_limit == null ? '—' : src.sample_limit);
    // 清洗状态：展示清洗前候选 / 清洗后实际抽样
    const raw = (profile && profile.raw_count != null) ? profile.raw_count : null;
    const cleaned = (profile && profile.cleaned_count != null) ? profile.cleaned_count : null;
    const cleanEl = document.getElementById('ps-src-clean');
    if (cleanEl) {
      if (raw == null || cleaned == null) {
        cleanEl.textContent = '—';
      } else if (raw === cleaned) {
        cleanEl.textContent = `已清洗（${cleaned}）`;
      } else {
        cleanEl.textContent = `是 ${raw}→${cleaned}`;
      }
    }
    const o2 = document.getElementById('ps-src-owner-2');
    if (o2) o2.textContent = (src.owner && src.owner !== '未配置') ? src.owner : '主人';
    // 无历史消息时给出明确提示
    const hint = document.getElementById('ps-source-hint');
    if (hint) {
      if (src.total_messages == null) {
        hint.textContent = '无法读取历史消息计数';
      } else if (src.total_messages === 0) {
        hint.textContent = '该平台暂无历史消息，无法抽取';
      } else if (src.owner_messages == null || src.owner_messages === 0) {
        hint.textContent = '未找到该主人发出的消息（请检查平台主人身份配置）';
      } else {
        hint.textContent = `已基于 ${src.owner_messages} 条主人历史消息抽取`;
      }
    }
  }

  // ---------- 错误条 / 空状态 ----------
  function showErrorBanner(msg) {
    const el = document.getElementById('ps-error-banner');
    if (!el) return;
    el.innerHTML = '<i class="fa-solid fa-triangle-exclamation"></i><span>画像加载失败：</span><code>' + escapeHtml(msg) + '</code><span style="margin-left:auto;color:#6b7280;font-size:12px;">请检查 F12 Console 或刷新页面</span>';
    el.style.display = 'flex';
  }
  function hideErrorBanner() {
    const el = document.getElementById('ps-error-banner');
    if (el) el.style.display = 'none';
  }
  function renderEmpty() {
    // 全部指标返回 —、描述返回占位
    ['ps-m-avg','ps-m-emoji','ps-m-polite','ps-m-casual','ps-m-short','ps-m-question','ps-m-sample'].forEach(id => {
      const e = document.getElementById(id); if (e) e.textContent = '—';
    });
    const p = document.getElementById('ps-auto-prompt'); if (p) p.textContent = '— 尚未生成 —';
    const u = document.getElementById('ps-auto-updated'); if (u) u.textContent = '—';
    const src = document.getElementById('ps-prompt-source'); if (src) { src.textContent = '—'; src.className = 'ps-source-badge'; }
    const ops = document.getElementById('ps-openers');
    if (ops) ops.innerHTML = '<span class="ps-openers-empty"><i class="fa-regular fa-comments"></i> 暂无典型开场样本</span>';
    const bar = document.getElementById('ps-bar-chart');
    if (bar) bar.innerHTML = '<div class="ps-bar-empty">—</div>';
    const mu = document.getElementById('ps-metric-updated'); if (mu) mu.textContent = '最后分析：—';
    const cleanEl = document.getElementById('ps-src-clean'); if (cleanEl) cleanEl.textContent = '—';
    ['ps-src-platform','ps-src-owner','ps-src-total','ps-src-owner-msg','ps-src-limit'].forEach(id => {
      const e = document.getElementById(id); if (e) e.textContent = '—';
    });
    const confEl = document.getElementById('ps-confidence'); if (confEl) { confEl.textContent = '置信度 —'; confEl.className = 'ps-status-badge'; }
    const compEl = document.getElementById('ps-src-completeness'); if (compEl) compEl.textContent = '—';
    const frEl = document.getElementById('ps-freshness'); if (frEl) { frEl.innerHTML = '<i class="fa-regular fa-clock"></i> 时效：—'; frEl.className = 'ps-meta-item'; }
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  // ---------- 状态徽章 ----------
  function renderStatus(data) {
    const el = document.getElementById('ps-auto-status');
    if (!el) return;
    const auto = data.auto_profile || {};
    const hasAuto = auto && auto.prompt;
    const hasOverride = !!(data.override && data.override.enabled && (data.override.prompt || '').trim());
    el.className = 'ps-status-badge';
    if (hasOverride) {
      const ov = data.override || {};
      const scopeText = ov.is_platform_specific ? '（本平台）' : '（全局）';
      el.textContent = '手动覆盖已启用' + scopeText;
      el.classList.add('active');
    } else if (hasAuto) {
      el.textContent = '自动画像已启用';
      el.classList.add('active');
    } else {
      el.textContent = '尚无画像';
      el.classList.add('empty');
    }
  }

  // ---------- 画像质量（置信度 / 完整度 / 时效）----------
  function renderQuality(data) {
    const profile = data.auto_profile || {};
    const freshness = data.freshness || {};

    // 置信度：样本量 + 高低
    const confEl = document.getElementById('ps-confidence');
    if (confEl) {
      const conf = profile.confidence;
      const n = profile.cleaned_count;
      if (!conf) {
        confEl.textContent = '置信度 —';
        confEl.className = 'ps-status-badge';
      } else {
        const label = conf === 'high' ? '高' : conf === 'medium' ? '中' : '低';
        confEl.textContent = `样本量 ${n == null ? '?' : n} 条 · 置信度${label}`;
        confEl.className = 'ps-status-badge conf-' + conf;
      }
    }

    // 完整度评分 0~100
    const compEl = document.getElementById('ps-src-completeness');
    if (compEl) {
      const c = profile.completeness;
      compEl.textContent = (c == null) ? '—' : (c + ' / 100');
    }

    // 时效：距今天数 + 是否建议重算
    const frEl = document.getElementById('ps-freshness');
    if (frEl) {
      const days = freshness.days_since_update;
      if (days == null) {
        frEl.innerHTML = '<i class="fa-regular fa-clock"></i> 时效：—';
        frEl.className = 'ps-meta-item';
      } else if (freshness.stale) {
        frEl.innerHTML = `<i class="fa-solid fa-triangle-exclamation"></i> 已 ${days} 天未更新，建议重算`;
        frEl.className = 'ps-meta-item ps-freshness-stale';
      } else {
        frEl.innerHTML = `<i class="fa-regular fa-clock"></i> ${days} 天前更新`;
        frEl.className = 'ps-meta-item ps-freshness-ok';
      }
    }
  }

  // ---------- #5 冷启动引导（低置信度画像时展示进度与积累建议）----------
  function renderColdStart(data) {
    const panel = document.getElementById('ps-coldstart-panel');
    const body = document.getElementById('ps-coldstart-body');
    const hint = document.getElementById('ps-coldstart-hint');
    if (!panel || !body) return;
    const cs = data.cold_start || {};
    if (!cs.is_cold) { panel.style.display = 'none'; return; }

    panel.style.display = '';
    const cc = cs.cleaned_count || 0;
    const ownerMsg = (cs.owner_messages == null) ? null : cs.owner_messages;
    const pMed = cs.progress_to_medium || 0;
    const pHigh = cs.progress_to_high || 0;
    const needMed = cs.needed_for_medium || 0;
    const needHigh = cs.needed_for_high || 0;

    let headText, subText;
    if (!cs.has_profile) {
      headText = ownerMsg != null
        ? `已发现 ${ownerMsg} 条主人历史消息，点击「重新分析」即可生成首份画像`
        : '尚未生成画像，多发送一些消息后点击「重新分析」';
      subText = '样本量达到 30 条后画像进入「中等置信度」，150 条后进入「高置信度」。';
    } else if (needMed > 0) {
      headText = `再积累约 ${needMed} 条消息即可达到「中等置信度」`;
      subText = '当前样本量偏少，画像代表性有限；继续正常沟通可逐步提升还原度。';
    } else {
      headText = `再积累约 ${needHigh} 条消息即可达到「高置信度」`;
      subText = '已接近高置信度，再补充一些样本即可让克隆口吻更稳定。';
    }

    if (hint) hint.textContent = headText;

    body.innerHTML = `
      <div class="ps-cs-sub">${escapeHtml(subText)}</div>
      <div class="ps-cs-bars">
        <div class="ps-cs-bar-row">
          <span class="ps-cs-bar-label">中等置信度（30 条）</span>
          <div class="ps-cs-track"><div class="ps-cs-fill ps-cs-fill-med" style="width:${pMed}%"></div></div>
          <span class="ps-cs-bar-val">${pMed}%</span>
        </div>
        <div class="ps-cs-bar-row">
          <span class="ps-cs-bar-label">高置信度（150 条）</span>
          <div class="ps-cs-track"><div class="ps-cs-fill ps-cs-fill-high" style="width:${pHigh}%"></div></div>
          <span class="ps-cs-bar-val">${pHigh}%</span>
        </div>
      </div>
      <div class="ps-cs-foot">
        <span class="ps-cs-stat">已清洗可用：<b>${cc}</b> 条</span>
        <button class="btn btn-outline btn-sm" onclick="reanalyzePersona()"><i class="fa-solid fa-rotate"></i> 重新分析</button>
      </div>`;
  }

  // ---------- 自动画像渲染（雷达 + 指标卡 + 描述） ----------
  function renderAuto(profile) {
    _lastAutoProfile = profile || null;
    const has = profile && profile.prompt;
    document.getElementById('ps-auto-prompt').textContent = has ? profile.prompt : '— 尚未生成 —';
    const updatedTxt = has ? formatTime(profile.updated_at) : '—';
    document.getElementById('ps-auto-updated').textContent = updatedTxt;
    const mu = document.getElementById('ps-metric-updated');
    if (mu) mu.textContent = '最后分析：' + updatedTxt;

    const avg = profile.avg_len;
    const emoji = profile.emoji_rate;
    const polite = profile.polite_rate;
    const casual = profile.casual_rate;
    const shortR = profile.short_rate;
    const questionR = profile.question_rate;
    const sample = profile.sample_count;

    // P4 KpiCard 替换：用统一 KPI 组件替代 setMetric/setChip（renderAuto 函数内）
if (sample) {
  renderKpiCard('ps-m-avg', {
    label: '表达长度',
    icon: '📝',
    value: Math.round(avg) + '字',
    sub: '平均字数'
  });
  renderKpiCard('ps-m-emoji', {
    label: '情感温度',
    icon: '😊',
    value: Math.round(emoji * 100) + '%',
    sub: 'emoji 使用率'
  });
  renderKpiCard('ps-m-polite', {
    label: '正式程度',
    icon: '📜',
    value: Math.round(polite * 100) + '%',
    sub: '礼貌词率'
  });
  renderKpiCard('ps-m-casual', {
    label: '口语程度',
    icon: '💬',
    value: Math.round(casual * 100) + '%',
    sub: '口语词率'
  });
  renderKpiCard('ps-m-short', {
    label: '短句率',
    icon: '⚡',
    value: Math.round((shortR||0) * 100) + '%',
    sub: '短句子比例'
  });
  renderKpiCard('ps-m-question', {
    label: '互动提问',
    icon: '❓',
    value: Math.round((questionR||0) * 100) + '%',
    sub: '提问率'
  });
} else {
  ['ps-m-avg','ps-m-emoji','ps-m-polite','ps-m-casual','ps-m-short','ps-m-question'].forEach(id => {
    renderKpiCard(id, { label: id.replace('ps-m-', ''), icon: '—', value: '—', sub: '暂无数据' });
  });
}

// prompt 来源徽章：AI 生成 vs 统计生成（保持不变）
    const srcEl = document.getElementById('ps-prompt-source');
    if (srcEl) {
      const src = (profile.prompt_source || 'stats');
      if (src === 'llm') {
        srcEl.textContent = 'AI 生成画像';
        srcEl.className = 'ps-source-badge llm';
      } else {
        srcEl.textContent = '统计生成画像';
        srcEl.className = 'ps-source-badge stats';
      }
    }

    // 典型开场（top_openers）作为标签云展示，不再作为雷达维度（保持不变）
    const opsEl = document.getElementById('ps-openers');
    if (opsEl) {
      const ops = (profile.top_openers || []);
      opsEl.innerHTML = ops.length
        ? '<span class="ps-openers-label"><i class="fa-solid fa-quote-right"></i> 典型开场</span>' +
          '<div class="ps-opener-cloud">' +
          ops.map((o, i) => `<span class="ps-opener-tag" style="animation-delay:${i * 40}ms">${escapeHtml(o)}</span>`).join('') +
          '</div>'
        : '<span class="ps-openers-empty"><i class="fa-regular fa-comments"></i> 暂无典型开场样本</span>';
    }
    // ps-meta-* pill 已移除（与上方「风格指标」KpiCard 重复），数值仅由 renderKpiCard 写入 ps-m-* 容器

    // 雷达：5 维科学风格维度（表达长度 / 正式程度 / 口语程度 / 情感温度 / 互动提问）
    const values = {
      avg_len:       sample ? Math.min(1, (avg || 0) / RADAR_DIMS[0].max) : 0,
      polite_rate:   sample ? (polite || 0) : 0,
      casual_rate:   sample ? (casual || 0) : 0,
      emoji_rate:    sample ? (emoji || 0) : 0,
      question_rate: sample ? (questionR || 0) : 0,
    };
    drawRadar(values);
    drawBarChart(values);
  }

  // ---------- 可解释抽屉（5 维权重条 + 代表性短语）----------
  function openExplainDrawer() {
    const profile = _lastAutoProfile || {};
    renderWeightBars(profile);
    renderRepSamples(profile);
    const drawer = document.getElementById('ps-explain-drawer');
    const mask = document.getElementById('ps-explain-mask');
    if (drawer) { drawer.classList.add('open'); drawer.setAttribute('aria-hidden', 'false'); }
    if (mask) mask.classList.add('open');
    document.body.style.overflow = 'hidden';
  }
  window.openExplainDrawer = openExplainDrawer;

  function closeExplainDrawer() {
    const drawer = document.getElementById('ps-explain-drawer');
    const mask = document.getElementById('ps-explain-mask');
    if (drawer) { drawer.classList.remove('open'); drawer.setAttribute('aria-hidden', 'true'); }
    if (mask) mask.classList.remove('open');
    document.body.style.overflow = '';
  }
  window.closeExplainDrawer = closeExplainDrawer;

  function renderWeightBars(profile) {
    const wrap = document.getElementById('ps-weight-bars');
    if (!wrap) return;
    const sample = profile.sample_count;
    const vals = {
      avg_len:       sample ? Math.min(1, (profile.avg_len || 0) / RADAR_DIMS[0].max) : 0,
      polite_rate:   sample ? (profile.polite_rate || 0) : 0,
      casual_rate:   sample ? (profile.casual_rate || 0) : 0,
      emoji_rate:    sample ? (profile.emoji_rate || 0) : 0,
      question_rate: sample ? (profile.question_rate || 0) : 0,
    };
    if (!sample) {
      wrap.innerHTML = '<div class="ps-drawer-empty">暂无样本，无法计算权重</div>';
      return;
    }
    wrap.innerHTML = RADAR_DIMS.map((d) => {
      const raw = vals[d.key] || 0;
      const pct = Math.round(raw * 100);
      const color = _PS_PLATFORM_COLORS.dingtalk;
      return `
        <div class="ps-weight-row">
          <span class="ps-weight-label">${escapeHtml(d.label)}</span>
          <div class="ps-weight-track">
            <div class="ps-weight-fill" style="width:${pct}%;background:${color}"></div>
          </div>
          <span class="ps-weight-val">${pct}%</span>
        </div>`;
    }).join('');
  }

  function renderRepSamples(profile) {
    const wrap = document.getElementById('ps-rep-samples');
    if (!wrap) return;
    const samples = (profile.representative_samples || []).filter(Boolean);
    if (!samples.length) {
      wrap.innerHTML = '<div class="ps-drawer-empty">未提取到代表性短语</div>';
      return;
    }
    wrap.innerHTML = samples.map((s) =>
      `<div class="ps-rep-chip"><i class="fa-solid fa-quote-left"></i>${escapeHtml(s)}</div>`
    ).join('');
  }

  function setMetric(id, val, unit) {
    const el = document.getElementById(id);
    if (el) el.textContent = val == null ? '—' : (val + (unit || ''));
  }

  // ---------- SVG 雷达 ----------
  function drawRadar(values) {
    const svg = document.getElementById('ps-radar-svg');
    if (!svg) return;
    const cx = 180, cy = 180, r = 110;
    const n = RADAR_DIMS.length;
    const angle = (i) => -Math.PI / 2 + (i * 2 * Math.PI / n);

    const parts = [];

    // 网格：5 圈
    for (let level = 1; level <= 4; level++) {
      const rr = (r * level) / 4;
      const pts = [];
      for (let i = 0; i < n; i++) {
        pts.push((cx + rr * Math.cos(angle(i))).toFixed(1) + ',' + (cy + rr * Math.sin(angle(i))).toFixed(1));
      }
      parts.push(`<polygon class="ps-radar-grid" points="${pts.join(' ')}" />`);
    }

    // 轴
    for (let i = 0; i < n; i++) {
      const x2 = cx + r * Math.cos(angle(i));
      const y2 = cy + r * Math.sin(angle(i));
      parts.push(`<line class="ps-radar-axis" x1="${cx}" y1="${cy}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" />`);
    }

    // 数据多边形
    const dataPts = [];
    const vertexPts = [];
    for (let i = 0; i < n; i++) {
      const v = Math.max(0, Math.min(1, values[RADAR_DIMS[i].key] || 0));
      const x = cx + r * v * Math.cos(angle(i));
      const y = cy + r * v * Math.sin(angle(i));
      dataPts.push(x.toFixed(1) + ',' + y.toFixed(1));
      vertexPts.push(`<circle class="ps-radar-vertex" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3.5" />`);
    }
    parts.push(`<polygon class="ps-radar-shape" points="${dataPts.join(' ')}" />`);
    parts.push(...vertexPts);

    // 标签
    for (let i = 0; i < n; i++) {
      const lr = r + 18;
      const lx = cx + lr * Math.cos(angle(i));
      const ly = cy + lr * Math.sin(angle(i));
      let anchor = 'middle';
      if (Math.abs(Math.cos(angle(i))) > 0.4) anchor = Math.cos(angle(i)) > 0 ? 'start' : 'end';
      parts.push(`<text class="ps-radar-label" x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" text-anchor="${anchor}" dominant-baseline="middle">${RADAR_DIMS[i].label}</text>`);
    }

    svg.innerHTML = parts.join('');
  }

  // ---------- 柱状图（雷达右侧） ----------
  const _BAR_COLORS = [
    { fill: '#6366f1', bg: '#e0e7ff' },   // 表达长度
    { fill: '#10b981', bg: '#d1fae5' },   // 正式程度
    { fill: '#06b6d4', bg: '#cffafe' },   // 口语程度
    { fill: '#f59e0b', bg: '#fef3c7' },   // 情感温度
    { fill: '#8b5cf6', bg: '#ede9fe' },   // 互动提问
  ];
  function drawBarChart(values) {
    const container = document.getElementById('ps-bar-chart');
    if (!container) return;
    const hasData = RADAR_DIMS.some(d => (values[d.key] || 0) > 0);
    if (!hasData) {
      container.innerHTML = '<div class="ps-bar-empty">—</div>';
      return;
    }
    const maxH = 140; // px
    const bars = RADAR_DIMS.map((d, i) => {
      const v = Math.max(0, Math.min(1, values[d.key] || 0));
      const pct = Math.round(v * 100);
      const h = Math.max(4, Math.round(v * maxH));
      const c = _BAR_COLORS[i % _BAR_COLORS.length];
      return `
        <div class="ps-bar-item">
          <div class="ps-bar-track" style="height:${maxH}px;background:${c.bg}">
            <div class="ps-bar-fill" style="height:${h}px;background:${c.fill};--bar-h:${h}px"></div>
          </div>
          <div class="ps-bar-value" style="color:${c.fill}">${pct}%</div>
          <div class="ps-bar-label">${escapeHtml(d.label)}</div>
        </div>
      `;
    }).join('');
    container.innerHTML = bars;
  }

  // ---------- 手动覆盖 ----------
  function renderOverride(ov) {
    const cb = document.getElementById('ps-override-enabled');
    const ta = document.getElementById('ps-override-text');
    if (!cb || !ta) return;
    cb.checked = !!ov.enabled;
    ta.value = ov.prompt || '';
    ta.disabled = !ov.enabled;
    // 作用域选择：平台专属 > 全局（与后端优先级一致）
    const scopeSel = document.getElementById('ps-override-scope');
    if (scopeSel) scopeSel.value = (ov.is_platform_specific) ? 'platform' : 'global';
    _refreshOverrideScopeBadge();
  }

  function onOverrideToggle(checked) {
    const ta = document.getElementById('ps-override-text');
    if (ta) ta.disabled = !checked;
  }

  window.onOverrideToggle = onOverrideToggle;

  function onOverrideScopeChange(val) {
    _refreshOverrideScopeBadge();
  }
  window.onOverrideScopeChange = onOverrideScopeChange;

  function _getCurrentPlatform() {
    try {
      if (window.store && window.store.getPlatform) {
        const p = window.store.getPlatform();
        if (p) return p;
      }
    } catch (e) {}
    try {
      const params = new URLSearchParams(location.search);
      const p = params.get('platform');
      if (p) return p;
    } catch (e) {}
    return 'dingtalk';
  }

  function _refreshOverrideScopeBadge() {
    const sel = document.getElementById('ps-override-scope');
    const badge = document.getElementById('ps-override-scope-badge');
    if (!sel || !badge) return;
    const plat = _getCurrentPlatform();
    const platName = (window._platformNames && window._platformNames[plat]) || plat;
    if (sel.value === 'platform') {
      badge.textContent = '将应用于：' + (platName || plat);
      badge.style.background = 'rgba(99,102,241,0.1)';
      badge.style.color = '#6366f1';
      badge.style.borderColor = 'rgba(99,102,241,0.25)';
    } else {
      badge.textContent = '将应用于：全部平台';
      badge.style.background = 'rgba(16,185,129,0.1)';
      badge.style.color = '#10b981';
      badge.style.borderColor = 'rgba(16,185,129,0.25)';
    }
  }

  async function saveOverride() {
    const cb = document.getElementById('ps-override-enabled');
    const ta = document.getElementById('ps-override-text');
    if (!cb || !ta) return;
    const enabled = cb.checked;
    const prompt = ta.value || '';
    if (enabled && !prompt.trim()) {
      showToast('已启用手动覆盖时需要填写内容；如要禁用，请关闭开关。', 'warning');
      return;
    }
    const scopeSel = document.getElementById('ps-override-scope');
    const scope = scopeSel ? scopeSel.value : 'platform';
    // 作用域：global 写入全局覆盖；platform 按当前平台写入 persona_style_prompts
    const platformArg = (scope === 'global') ? 'global' : _getCurrentPlatform();
    try {
      const data = await api.fetch('/api/persona/override', 'POST', { enabled, prompt, platform: platformArg });
      if (data.error) throw new Error(data.error === 'unauthorized' ? '未登录' : ('HTTP ' + (data.status || '')));
      showToast(enabled ? '已保存手动覆盖' : '已清除手动覆盖');
      loadPersonaPage();
    } catch (e) {
      showToast('保存失败：' + e.message, 'error');
    }
  }

  function resetOverride() {
    const cb = document.getElementById('ps-override-enabled');
    const ta = document.getElementById('ps-override-text');
    if (cb) cb.checked = false;
    if (ta) { ta.value = ''; ta.disabled = true; }
    saveOverride();
  }

  window.saveOverride = saveOverride;
  window.resetOverride = resetOverride;

  // ---------- Few-shot ----------
  function renderFewShot(list) {
    const wrap = document.getElementById('ps-fewshot-list');
    if (!wrap) return;
    if (!list || list.length === 0) {
      wrap.innerHTML = '<div class="ps-fewshot-empty">尚无样例，点右上「添加样例」开始</div>';
      return;
    }
    wrap.innerHTML = list.map((ex, i) => `
      <div class="ps-fewshot-item" data-idx="${i}">
        <button class="ps-fs-del" title="删除" onclick="removeFewShot(${i})"><i class="fa-solid fa-trash"></i></button>
        <div class="ps-fs-row">
          <span class="ps-fs-tag fs-tag-user">用户</span>
          <input type="text" class="ps-fs-input fs-input-user" data-field="user" value="${escapeAttr(ex.user || '')}" placeholder="用户问的...">
        </div>
        <div class="ps-fs-row">
          <span class="ps-fs-tag fs-tag-owner">主人</span>
          <input type="text" class="ps-fs-input fs-input-owner" data-field="assistant" value="${escapeAttr(ex.assistant || '')}" placeholder="主人回的...">
        </div>
      </div>
    `).join('');
  }

  function collectFewShot() {
    const items = document.querySelectorAll('.ps-fewshot-item');
    const out = [];
    items.forEach((it) => {
      const u = it.querySelector('[data-field="user"]').value.trim();
      const a = it.querySelector('[data-field="assistant"]').value.trim();
      if (u && a) out.push({ user: u, assistant: a });
    });
    return out;
  }

  function addFewShot() {
    const wrap = document.getElementById('ps-fewshot-list');
    if (!wrap) return;
    const empty = wrap.querySelector('.ps-fewshot-empty');
    if (empty) empty.remove();
    const div = document.createElement('div');
    div.className = 'ps-fewshot-item';
    div.dataset.idx = String(wrap.children.length);
    div.innerHTML = `
      <button class="ps-fs-del" title="删除" onclick="removeFewShot(this)"><i class="fa-solid fa-trash"></i></button>
      <div class="ps-fs-row">
        <span class="ps-fs-tag fs-tag-user">用户</span>
        <input type="text" class="ps-fs-input fs-input-user" data-field="user" placeholder="用户问的...">
      </div>
      <div class="ps-fs-row">
        <span class="ps-fs-tag fs-tag-owner">主人</span>
        <input type="text" class="ps-fs-input fs-input-owner" data-field="assistant" placeholder="主人回的...">
      </div>
    `;
    wrap.appendChild(div);
  }

  function removeFewShot(ref) {
    const wrap = document.getElementById('ps-fewshot-list');
    if (!wrap) return;
    let item;
    if (typeof ref === 'number') {
      item = wrap.querySelector(`.ps-fewshot-item[data-idx="${ref}"]`);
    } else {
      item = ref && ref.closest ? ref.closest('.ps-fewshot-item') : null;
    }
    if (item) item.remove();
    if (!wrap.children.length) renderFewShot([]);
  }

  // 把内存中的样例整体落盘（按当前平台隔离写入）。
  async function saveFewShot() {
    try {
      const examples = collectFewShot();
      const platform = (typeof _getCurrentPlatform === 'function') ? _getCurrentPlatform() : '';
      const data = await api.fetch('/api/persona/few-shot', 'POST', { examples, platform });
      if (data.error) throw new Error(data.error === 'unauthorized' ? '未登录' : ('HTTP ' + (data.status || '')));
      showToast(`已保存 ${examples.length} 条样例（本平台）`);
      loadPersonaPage();
    } catch (e) {
      showToast('保存失败：' + e.message, 'error');
    }
  }

  window.addFewShot = addFewShot;
  window.removeFewShot = removeFewShot;
  window.saveFewShot = saveFewShot;

  // ---------- 从主人历史推荐 few-shot 样例 ----------
  async function recommendFewShot() {
    try {
      const data = await api.fetch('/api/persona/recommend-few-shot?limit=6');
      if (data.error) throw new Error(data.error === 'unauthorized' ? '未登录' : ('HTTP ' + (data.status || '')));
      const examples = data.examples || [];
      if (!examples.length) { showToast('未从历史中找到合适的样例对', 'error'); return; }
      const wrap = document.getElementById('ps-fewshot-list');
      if (wrap) {
        const empty = wrap.querySelector('.ps-fewshot-empty');
        if (empty) empty.remove();
        examples.forEach((ex) => {
          const div = document.createElement('div');
          div.className = 'ps-fewshot-item ps-fewshot-recommend';
          div.dataset.idx = String(wrap.children.length);
          div.dataset.user = ex.user || '';
          div.dataset.assistant = ex.assistant || '';
          div.innerHTML = `
            <div class="ps-fs-rec-head">
              <span class="ps-fs-rec-tag"><i class="fa-solid fa-wand-magic-sparkles"></i> 推荐样例</span>
              <button class="ps-fs-adopt" onclick="adoptFewShot(this)"><i class="fa-solid fa-check"></i> 一键采纳</button>
            </div>
            <button class="ps-fs-del" title="删除" onclick="removeFewShot(this)"><i class="fa-solid fa-trash"></i></button>
            <div class="ps-fs-row">
              <span class="ps-fs-tag fs-tag-user">用户</span>
              <input type="text" class="ps-fs-input fs-input-user" data-field="user" value="${escapeAttr(ex.user || '')}" placeholder="用户问的...">
            </div>
            <div class="ps-fs-row">
              <span class="ps-fs-tag fs-tag-owner">主人</span>
              <input type="text" class="ps-fs-input fs-input-owner" data-field="assistant" value="${escapeAttr(ex.assistant || '')}" placeholder="主人回的...">
            </div>
          `;
          wrap.appendChild(div);
        });
      }
      showToast(`已推荐 ${examples.length} 条样例，可逐条「一键采纳」或检查后点「保存」`);
    } catch (e) {
      showToast('推荐失败：' + e.message, 'error');
    }
  }
  window.recommendFewShot = recommendFewShot;

  // ---------- 一键采纳推荐样例（直接落库，去重）----------
  async function adoptFewShot(btn) {
    const item = btn.closest('.ps-fewshot-item');
    if (!item) return;
    const user = item.dataset.user || '';
    const assistant = item.dataset.assistant || '';
    if (!user || !assistant) { showToast('样例内容不完整，无法采纳', 'warning'); return; }
    btn.disabled = true;
    const original = btn.innerHTML;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 采纳中...';
    try {
      const platform = (typeof _getCurrentPlatform === 'function') ? _getCurrentPlatform() : '';
      const data = await api.fetch('/api/persona/few-shot/adopt', 'POST', { example: { user, assistant }, platform });
      if (data.error) throw new Error(data.error === 'unauthorized' ? '未登录' : ('HTTP ' + (data.status || '')));
      if (data.adopted) {
        item.classList.add('ps-fs-adopted');
        btn.innerHTML = '<i class="fa-solid fa-check"></i> 已采纳';
        showToast('已采纳并保存');
      } else {
        btn.innerHTML = '<i class="fa-solid fa-check"></i> 已存在';
        showToast(data.message || '该样例已存在', 'warning');
      }
    } catch (e) {
      btn.disabled = false;
      btn.innerHTML = original;
      showToast('采纳失败：' + e.message, 'error');
    }
  }
  window.adoptFewShot = adoptFewShot;

  // ---------- 重新分析 ----------
  async function reanalyzePersona() {
    const btn = document.getElementById('ps-btn-reanalyze');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 分析中...'; }
    try {
      const data = await api.fetch('/api/persona/reanalyze', 'POST');
      if (data.error) throw new Error(data.error === 'unauthorized' ? '未登录' : ('HTTP ' + (data.status || '')));
      if (!data.success) throw new Error(data.message || '未生成画像');
      showToast('已重新生成 AI 画像');
      loadPersonaPage();
    } catch (e) {
      showToast('分析失败：' + e.message, 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-rotate"></i> 重新分析'; }
    }
  }

  window.reanalyzePersona = reanalyzePersona;

  // ---------- 还原度回测（克隆回复 vs 主人真实回复，LLM 评委打分）----------
  async function runBacktest() {
    const modal = document.getElementById('ps-backtest-modal');
    const scoreEl = document.getElementById('ps-backtest-score');
    const subEl = document.getElementById('ps-backtest-sub');
    const listEl = document.getElementById('ps-backtest-list');
    if (modal) modal.classList.add('active');
    if (scoreEl) scoreEl.textContent = '…';
    if (subEl) subEl.textContent = '回测中（克隆 + 评委打分，请稍候）…';
    if (listEl) listEl.innerHTML = '';
    try {
      // 回测在服务端要做 N×2 次 LLM 调用（克隆+评委），默认 30s 超时太短，
      // 显式给 180s；后续若返回错误也把字段拼出来，避免再丢出无意义的 "HTTP "。
      const data = await api.fetch('/api/persona/backtest?limit=6', 'POST', {}, { timeoutMs: 180000 });
      if (data.error) {
        if (data.error === 'unauthorized') throw new Error('未登录');
        if (data.error === 'timeout') throw new Error(data.message || '请求超时');
        if (data.error === 'non_json_response') {
          const preview = (data._raw || '').slice(0, 120).replace(/\s+/g, ' ');
          throw new Error(`后端返回了非 JSON 响应（HTTP ${data.status}）: ${preview || '(空)'}`);
        }
        throw new Error(data.message || `网络错误（${data.error}）`);
      }
      if (!data.success) throw new Error(data.message || '回测未产出结果');
      if (scoreEl) scoreEl.textContent = data.mean_score + ' 分';
      if (subEl) subEl.textContent = `基于 ${data.count} 条 (用户→主人) 配对的平均还原度`;
      // 趋势：拉取本平台回测基线历史并画趋势线
      try {
        const hist = await api.fetch('/api/persona/backtest/history?limit=20');
        if (hist && hist.history && hist.history.length) {
          renderBacktestTrend(hist.history);
        } else {
          const tEl = document.getElementById('ps-backtest-trend');
          if (tEl) tEl.innerHTML = '<div class="ps-bt-trend-empty">暂无历史基线，多次回测后可看趋势</div>';
        }
      } catch (e) { /* 趋势非关键，忽略 */ }
      const items = (data.details || []);
      if (listEl) {
        listEl.innerHTML = items.map((d) => {
          const cls = d.score >= 80 ? 'high' : (d.score >= 60 ? 'mid' : 'low');
          return `
            <div class="ps-bt-item">
              <div class="ps-bt-head">
                <span class="ps-bt-score ${cls}">${d.score}</span>
                <span class="ps-bt-reason">${escapeHtml(d.reason || '')}</span>
              </div>
              <div class="ps-bt-rows">
                <div class="ps-bt-row"><span class="ps-bt-k">用户</span><span class="ps-bt-v">${escapeHtml(d.user || '')}</span></div>
                <div class="ps-bt-row"><span class="ps-bt-k">真实</span><span class="ps-bt-v truth">${escapeHtml(d.truth || '')}</span></div>
                <div class="ps-bt-row"><span class="ps-bt-k">克隆</span><span class="ps-bt-v clone">${escapeHtml(d.clone || '')}</span></div>
              </div>
            </div>`;
        }).join('');
      }
    } catch (e) {
      if (scoreEl) scoreEl.textContent = '✕';
      if (subEl) subEl.textContent = '回测失败：' + e.message;
    }
  }
  window.runBacktest = runBacktest;

  // 用内联 SVG 画回测还原度趋势线（随样本量/画像成熟度增长）。无外部依赖。
  function renderBacktestTrend(history) {
    const el = document.getElementById('ps-backtest-trend');
    if (!el) return;
    const W = 320, H = 120, pad = 26;
    const n = history.length;
    const scores = history.map((h) => Number(h.mean_score) || 0);
    const x = (i) => pad + (W - pad * 2) * (n === 1 ? 0.5 : i / (n - 1));
    const y = (s) => (H - pad) - (H - pad * 2) * (Math.max(0, Math.min(100, s)) / 100);
    const pts = scores.map((s, i) => `${x(i).toFixed(1)},${y(s).toFixed(1)}`).join(' ');
    const areaPts = `${x(0).toFixed(1)},${(H - pad).toFixed(1)} ${pts} ${x(n - 1).toFixed(1)},${(H - pad).toFixed(1)}`;
    const labels = history.map((h, i) => {
      const d = (h.ts || '').slice(5, 10); // MM-DD
      return `<text x="${x(i).toFixed(1)}" y="${H - 8}" class="ps-bt-axis">${d}</text>`;
    }).join('');
    const dots = scores.map((s, i) => `<circle cx="${x(i).toFixed(1)}" cy="${y(s).toFixed(1)}" r="2.6" class="ps-bt-dot"/>`).join('');
    const last = scores[n - 1];
    const first = scores[0];
    const delta = (last - first).toFixed(1);
    const trendCls = last >= first ? 'up' : 'down';
    el.innerHTML = `
      <div class="ps-bt-trend-head">
        <span class="ps-bt-trend-title">还原度趋势（${n} 次基线）</span>
        <span class="ps-bt-trend-delta ${trendCls}">${delta >= 0 ? '▲' : '▼'} ${Math.abs(delta)}</span>
      </div>
      <svg viewBox="0 0 ${W} ${H}" class="ps-bt-trend-svg" preserveAspectRatio="xMidYMid meet">
        <line x1="${pad}" y1="${H - pad}" x2="${W - pad}" y2="${H - pad}" class="ps-bt-axis"/>
        <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${H - pad}" class="ps-bt-axis"/>
        <text x="2" y="${pad + 4}" class="ps-bt-axis">100</text>
        <text x="2" y="${H - pad + 2}" class="ps-bt-axis">0</text>
        <polygon points="${areaPts}" class="ps-bt-area"/>
        <polyline points="${pts}" class="ps-bt-line"/>
        ${dots}
        ${labels}
      </svg>`;
  }
  window.renderBacktestTrend = renderBacktestTrend;

  function closeBacktest() {
    const modal = document.getElementById('ps-backtest-modal');
    if (modal) modal.classList.remove('active');
  }
  window.closeBacktest = closeBacktest;

  // ---------- #3 画像版本管理（列表 / 查看 / 回滚）----------
  const _VERSION_TRIGGER_LABEL = {
    'baseline': '基线',
    'manual': '手动分析',
    'auto': '自动重算',
    'auto-archive': '自动归档',
    'rollback': '回滚生成',
  };
  function _versionTriggerLabel(t) {
    return _VERSION_TRIGGER_LABEL[t] || (t || '未知');
  }
  function _confLabel(c) {
    return c === 'high' ? '高' : c === 'medium' ? '中' : c === 'low' ? '低' : c || '—';
  }

  // 画像历史：点击「画像历史」按钮打开弹窗，拉取并渲染版本列表（原页面内卡片已移除）
  async function openVersionModal() {
    const modal = document.getElementById('ps-version-modal');
    const body = document.getElementById('ps-version-body');
    const noEl = document.getElementById('ps-version-modal-no');
    if (noEl) noEl.textContent = '';
    if (body) body.innerHTML = '<div class="ps-version-empty">加载中…</div>';
    if (modal) modal.classList.add('active');
    try {
      const data = await api.fetch('/api/persona/versions?limit=20');
      if (data.error) throw new Error(data.error === 'unauthorized' ? '未登录' : ('HTTP ' + (data.status || '')));
      renderVersionList(data.versions || []);
    } catch (e) {
      showToast('加载失败：' + e.message, 'error');
      if (body) body.innerHTML = '<div class="ps-version-empty">加载失败：' + escapeHtml(e.message) + '</div>';
    }
  }
  window.openVersionModal = openVersionModal;

  function renderVersionList(versions) {
    const body = document.getElementById('ps-version-body');
    if (!body) return;
    if (!versions || !versions.length) {
      body.innerHTML = '<div class="ps-version-empty">暂无历史版本（重新分析或自动重算后会自动归档）</div>';
      return;
    }
    body.innerHTML = '<div class="ps-version-list">' + versions.map((v) => `
      <div class="ps-version-item" data-vid="${v.id}">
        <div class="ps-version-main">
          <span class="ps-version-no">v${v.version_no}</span>
          <span class="ps-version-trigger trigger-${escapeHtml(v.trigger)}">${escapeHtml(_versionTriggerLabel(v.trigger))}</span>
          <span class="ps-version-meta">置信度 ${escapeHtml(_confLabel(v.confidence))} · 清洗 ${v.cleaned_count || 0} 条</span>
          <span class="ps-version-time">${escapeHtml(formatTime(v.created_at))}</span>
        </div>
        <div class="ps-version-actions">
          <button class="btn btn-outline btn-xs" onclick="viewVersion(${v.id})"><i class="fa-solid fa-eye"></i> 查看</button>
          <button class="btn btn-outline btn-xs" onclick="rollbackVersion(${v.id})"><i class="fa-solid fa-clock-rotate-left"></i> 回滚到此版</button>
        </div>
      </div>`).join('') + '</div>';
  }

  async function viewVersion(vid) {
    try {
      const data = await api.fetch('/api/persona/versions/' + vid);
      if (data.error) throw new Error(data.error === 'unauthorized' ? '未登录' : ('HTTP ' + (data.status || '')));
      const p = data.profile || {};
      const modal = document.getElementById('ps-version-modal');
      const noEl = document.getElementById('ps-version-modal-no');
      const body = document.getElementById('ps-version-body');
      if (noEl) noEl.textContent = 'v' + (data.version_id || vid);
      if (body) {
        const has = p && p.prompt;
        body.innerHTML = `
          <div class="ps-ver-quote">
            <i class="fa-solid fa-quote-left ps-quote-icon"></i>
            <div class="ps-ver-prompt">${has ? escapeHtml(p.prompt) : '— 该版本无画像描述 —'}</div>
          </div>
          <div class="ps-ver-grid">
            <div class="ps-ver-cell"><span class="ps-ver-k">置信度</span><span class="ps-ver-v">${escapeHtml(_confLabel(p.confidence))}</span></div>
            <div class="ps-ver-cell"><span class="ps-ver-k">完整度</span><span class="ps-ver-v">${p.completeness == null ? '—' : (p.completeness + ' / 100')}</span></div>
            <div class="ps-ver-cell"><span class="ps-ver-k">清洗样本</span><span class="ps-ver-v">${p.cleaned_count == null ? '—' : (p.cleaned_count + ' 条')}</span></div>
            <div class="ps-ver-cell"><span class="ps-ver-k">表达长度</span><span class="ps-ver-v">${p.avg_len == null ? '—' : Math.round(p.avg_len)}</span></div>
            <div class="ps-ver-cell"><span class="ps-ver-k">情感温度</span><span class="ps-ver-v">${p.emoji_rate == null ? '—' : Math.round(p.emoji_rate * 100) + '%'}</span></div>
            <div class="ps-ver-cell"><span class="ps-ver-k">正式程度</span><span class="ps-ver-v">${p.polite_rate == null ? '—' : Math.round(p.polite_rate * 100) + '%'}</span></div>
          </div>
          <div class="ps-ver-actions">
            <button class="btn btn-outline btn-sm" onclick="openVersionModal()"><i class="fa-solid fa-arrow-left"></i> 返回列表</button>
            <button class="btn btn-primary btn-sm" onclick="rollbackVersion(${vid})"><i class="fa-solid fa-clock-rotate-left"></i> 回滚到此版本</button>
          </div>`;
      }
      if (modal) modal.classList.add('active');
    } catch (e) {
      showToast('查看失败：' + e.message, 'error');
    }
  }
  window.viewVersion = viewVersion;

  function closeVersionModal() {
    const modal = document.getElementById('ps-version-modal');
    if (modal) modal.classList.remove('active');
  }
  window.closeVersionModal = closeVersionModal;

  async function rollbackVersion(vid) {
    if (!confirm('确认回滚到该历史版本？当前版本会自动归档，不会丢失。')) return;
    try {
      const data = await api.fetch('/api/persona/versions/rollback', 'POST', { version_id: vid });
      if (data.error) throw new Error(data.error === 'unauthorized' ? '未登录' : ('HTTP ' + (data.status || '')));
      if (!data.success) throw new Error('回滚失败');
      showToast('已回滚到历史版本');
      closeVersionModal();
      loadPersonaPage();
    } catch (e) {
      showToast('回滚失败：' + e.message, 'error');
    }
  }
  window.rollbackVersion = rollbackVersion;

  // ---------- 平台隔离徽章（钉钉 / 微信 / 飞书）----------
  // 主人风格画像按平台独立存储（独立 DB），此徽章直观标识「当前查看的是哪个平台的画像」。
  const _PS_PLATFORM_COLORS = { dingtalk: '#1677ff', feishu: '#18C08F', wecom: '#2ba245' };
  let _psPlatformMeta = null;
  let _lastAutoProfile = null;   // 最近一次加载的自动画像，供可解释抽屉使用
  async function _loadPlatformMeta() {
    if (_psPlatformMeta) return _psPlatformMeta;
    try {
      const r = await api.fetch('/api/platforms');
      _psPlatformMeta = (r && r.platforms) || [];
    } catch (e) {
      _psPlatformMeta = [];
    }
    return _psPlatformMeta;
  }
  async function renderPlatformBadge() {
    const badge = document.getElementById('ps-platform-badge');
    if (!badge) return;
    const pid = (window.store && window.store.getPlatform) ? window.store.getPlatform() : 'dingtalk';
    const meta = await _loadPlatformMeta();
    const p = meta.find(x => x.id === pid) || { id: pid, display_name: pid };
    const color = _PS_PLATFORM_COLORS[p.adapter_type] || _PS_PLATFORM_COLORS[pid] || 'var(--brand-primary)';
    const dot = badge.querySelector('.pf-dot');
    const label = badge.querySelector('.pf-label');
    if (dot) dot.style.background = color;
    if (label) label.textContent = p.display_name || pid;
    badge.title = '当前平台：' + (p.display_name || pid) + '（画像数据按平台隔离）';
  }
  function _onPlatformChange() {
    renderPlatformBadge();
    loadPersonaPage();
  }

  // 页面初始化时订阅平台切换：切换器切平台 → 刷新徽章 + 重新拉取对应平台画像
  // ---------- 工具 ----------
  function escapeAttr(s) {
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function formatTime(s) {
    if (!s) return '—';
    // 服务端是 ISO，去掉 T 之前的部分 + 截到分钟
    const m = String(s).match(/(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/);
    return m ? `${m[1]} ${m[2]}` : s;
  }
  function showToast(msg, type) {
    // 复用 app.js 暴露的全局 toast；未加载时 fallback 到 alert 保证用户可见
    if (typeof window.showToast === 'function') { window.showToast(msg, type); return; }
    try { alert(msg); } catch (_) {}
  }

  window.loadPersonaPage = loadPersonaPage;

  // 订阅全局平台变化（initPlatformSwitcher 切换时触发），保证 persona 页跟着隔离刷新
  if (window.store && typeof window.store.subscribe === 'function') {
    window.store.subscribe('platform', _onPlatformChange);
  }
})();
