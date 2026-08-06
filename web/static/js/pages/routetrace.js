// ============ pages/routetrace.js ============
// 「路由追踪」页：混合指挥中心 —— 聚合链路流 + 可观测面板 + 逐条路径溯源（ribbon + 决策理由 chips）。
// 数据来自 backend：/api/routing-quality（分页列表）、/stats（过滤器+均值）、/aggregate（系统级聚合）。

let _rtPage = 1;
let _rtPolling = null;

// 阶段中文名 + 图标 + 配色（仿网络路由 hop）
const RT_STAGE_META = {
    message_in:     { label: "消息进入", icon: "fa-inbox",        color: "#2563eb" },
    intent:         { label: "意图判定", icon: "fa-compass",      color: "#8b5cf6" },
    skill_routing:  { label: "技能路由", icon: "fa-puzzle-piece", color: "#06b6d4" },
    tool_exposure:  { label: "工具暴露", icon: "fa-toolbox",       color: "#f59e0b" },
    llm_inference:  { label: "LLM 推理", icon: "fa-brain",         color: "#ec4899" },
    reply:          { label: "回复生成", icon: "fa-reply",         color: "#16a34a" },
};
// 聚合链路流的固定阶段顺序（缺失阶段自动跳过）
const RT_STAGE_ORDER = ["message_in", "intent", "skill_routing", "tool_exposure", "llm_inference", "reply"];

const RT_STATUS_LABEL = {
    ok:            ["正常", "success"],
    skip:          ["跳过", "muted"],
    fail:          ["失败", "error"],
    empty:         ["空回复", "warning"],
    reconstructed: ["历史回填", "info"],
};

function rtStageMeta(stage) {
    return RT_STAGE_META[stage] || { label: stage, icon: "fa-circle", color: "#94a3b8" };
}

function rtFmtMs(ms) {
    if (ms == null) return "—";
    if (ms >= 1000) return (ms / 1000).toFixed(2) + "s";
    return Math.round(ms) + "ms";
}

function rtFmtTime(iso) {
    if (!iso) return "—";
    // created_at 为本地时间字符串如 2026-07-14 18:24:31
    return iso.replace("T", " ").slice(0, 19);
}

// 会话 ID 固定截断：首 8 + … + 末 4（max 13 字符可读），用于卡片/详情弹窗中狭长布局。
function rtTruncCid(cid) {
    if (!cid || cid === "—") return "—";
    if (cid.length <= 13) return cid;
    return cid.slice(0, 8) + "…" + cid.slice(-4);
}

// 通用长文本截断：超过 max 字符时中间插入 …。
function rtShortText(text, max = 22) {
    if (!text) return '<span class="rt-d-muted">—</span>';
    const s = String(text);
    if (s.length <= max) return escapeHtml(s);
    const half = Math.floor((max - 1) / 2);
    return `<span class="rt-cid-full" title="${escapeHtml(s)}">${escapeHtml(s.slice(0, half) + "…" + s.slice(-half))}</span>`;
}

// ============ 聚合链路流 + 可观测面板 ============

async function loadRouteTraceAggregate() {
    try {
        const agg = await RoutingQualityService.loadAggregate();
        if (!agg || agg.available === false) {
            const f = document.getElementById("rt-flow-spine");
            if (f) f.innerHTML = `<div class="rt-muted">聚合加载失败：${escapeHtml(agg.error || '')}</div>`;
            return;
        }
        // KPI：总记录数 / 平均延迟 / 健康率（改用 KpiCard）
        renderKpiCard("rt-kpi-total", {
            label: "总记录数",
            icon: "📊",
            value: agg.total_records ?? "—",
            sub: "路由追踪总条数"
        });
        renderKpiCard("rt-kpi-avg", {
            label: "平均延迟",
            icon: "⏱️",
            value: rtFmtMs(agg.avg_total_ms ?? "—"),
            sub: "全链路平均耗时"
        });
        const healthRate = ((1 - (agg.empty_rate ?? 0)) * 100).toFixed(1);
        renderKpiCard("rt-kpi-health", {
            label: "健康率",
            icon: "✅",
            value: healthRate + "%",
            sub: `空回复率 ${((agg.empty_rate??0)*100).toFixed(1)}%`,
        });
        // 补齐 llm / max 卡片：与 total/avg/health 风格一致（icon + label + value + sub）
        renderKpiCard("rt-kpi-llm", {
            label: "平均 LLM 推理",
            icon: "🧠",
            value: rtFmtMs(agg.avg_llm_ms ?? 0),
            sub: "LLM 推理耗时均值"
        });
        renderKpiCard("rt-kpi-max", {
            label: "峰值总耗时",
            icon: "📈",
            value: rtFmtMs(agg.max_total_ms ?? 0),
            sub: "全链路最长记录"
        });
        renderRouteFlow(agg);
        renderObsPanels(agg);
    } catch (e) {
        console.error('loadRouteTraceAggregate error:', e);
        const f = document.getElementById("rt-flow-spine");
        if (f) f.innerHTML = `<div class="rt-muted">聚合加载失败：${escapeHtml(String(e.message))}</div>`;
    }
}

// 系统级聚合链路：各阶段均值节点 + 瓶颈高亮
function renderRouteFlow(agg) {
    const spine = document.getElementById("rt-flow-spine");
    const botEl = document.getElementById("rt-flow-bottleneck");
    if (!spine) return;
    const stageAvg = agg.stage_avg || {};
    const stages = RT_STAGE_ORDER.filter(s => s in stageAvg);
    if (!stages.length) {
        spine.innerHTML = `<div class="rt-muted">暂无阶段记录</div>`;
        if (botEl) botEl.textContent = "";
        return;
    }
    const maxMs = Math.max(...stages.map(s => stageAvg[s]));
    const nodes = stages.map((s, i) => {
        const meta = rtStageMeta(s);
        const ms = stageAvg[s];
        const isBot = s === agg.bottleneck_stage;
        const last = i === stages.length - 1;
        const line = last ? "" : `<span class="rt-flow-line"></span>`;
        return `
            <div class="rt-flow-node ${isBot ? "rt-flow-bot" : ""}" style="--c:${meta.color}" title="${meta.label} · 均值 ${rtFmtMs(ms)}">
                <span class="rt-flow-dot"><i class="fa-solid ${meta.icon}"></i></span>
                <span class="rt-flow-name">${meta.label}</span>
                <span class="rt-flow-ms">${rtFmtMs(ms)}</span>
            </div>${line}`;
    }).join("");
    spine.innerHTML = nodes;

    if (botEl) {
        if (agg.bottleneck_stage && agg.bottleneck_share != null) {
            const meta = rtStageMeta(agg.bottleneck_stage);
            botEl.innerHTML = `<i class="fa-solid fa-triangle-exclamation"></i> 瓶颈：${meta.label} 占全链路 <b>${(agg.bottleneck_share * 100).toFixed(0)}%</b> 耗时`;
        } else {
            botEl.textContent = "";
        }
    }
}

// 可观测面板：来源构成 / 延迟分布 / 引擎信号
function renderObsPanels(agg) {
    // 来源构成
    const srcEl = document.getElementById("rt-obs-source");
    if (srcEl) {
        const src = agg.source_split || {};
        const entries = Object.entries(src);
        if (!entries.length) {
            srcEl.innerHTML = `<div class="rt-muted">暂无数据</div>`;
        } else {
            const max = Math.max(...entries.map(e => e[1]));
            srcEl.innerHTML = entries.map(([k, v]) => {
                const pct = max ? Math.round(v / max * 100) : 0;
                return `<div class="rt-obs-row">
                    <span class="rt-obs-k">${escapeHtml(k)}</span>
                    <span class="rt-obs-bar"><span class="rt-obs-fill" style="width:${pct}%"></span></span>
                    <span class="rt-obs-v">${v}</span>
                </div>`;
            }).join("");
        }
    }
    // 延迟分布（横向条）
    const latEl = document.getElementById("rt-obs-latency");
    if (latEl) {
        const hist = agg.latency_hist || [];
        const max = Math.max(1, ...hist.map(h => h.count));
        latEl.innerHTML = hist.map(h => {
            const pct = Math.round(h.count / max * 100);
            return `<div class="rt-obs-row">
                <span class="rt-obs-k">${escapeHtml(h.label)}</span>
                <span class="rt-obs-bar"><span class="rt-obs-fill rt-obs-fill-lat" style="width:${pct}%"></span></span>
                <span class="rt-obs-v">${h.count}</span>
            </div>`;
        }).join("") || `<div class="rt-muted">暂无数据</div>`;
    }
    // 引擎信号：收敛 / 组合 / 主动意图
    const engEl = document.getElementById("rt-obs-engine");
    if (engEl) {
        const conv = agg.total_convergence || 0;
        const combo = agg.total_combo || 0;
        const proactive = agg.total_proactive || 0;
        const blockedRec = agg.total_blocked_records || 0;
        engEl.innerHTML = `
            <div class="rt-obs-signal">
                <div class="rt-obs-sig-num">${conv}</div>
                <div class="rt-obs-sig-lbl">目标感知收敛介入</div>
            </div>
            <div class="rt-obs-signal">
                <div class="rt-obs-sig-num">${combo}</div>
                <div class="rt-obs-sig-lbl">组合激活触发</div>
            </div>
            <div class="rt-obs-signal rt-obs-signal-pro">
                <div class="rt-obs-sig-num">${proactive}</div>
                <div class="rt-obs-sig-lbl">主动意图触达</div>
            </div>
            <div class="rt-obs-signal rt-obs-signal-blk">
                <div class="rt-obs-sig-num">${blockedRec}</div>
                <div class="rt-obs-sig-lbl">技能停用拦截</div>
            </div>`;
    }
}

// ============ 逐条路径溯源卡片（ribbon + chips + 内联展开） ============

function renderRouteCard(rec) {
    const stages = Array.isArray(rec.stages_json) ? rec.stages_json : [];
    const totalMs = rec.total_latency_ms || 0;
    const totalTxt = rtFmtMs(totalMs);

    const intent = rec.intent_disposition || "—";
    const action = rec.intent_action || "—";
    const skill = rec.primary_skill || "无技能";
    const mode = rec.routing_mode || "—";
    const model = rec.llm_model || "—";
    const rounds = rec.llm_rounds ?? "—";
    const score = rec.primary_score ?? "—";
    const sender = rec.sender_name || "未知";
    const preview = rec.content_preview || "";
    const msgType = rec.message_type || "text";
    const status = (rec.reply_len > 0) ? "ok" : ((rec.reply_len === 0) ? "empty" : "skip");
    const [stLabel, stCls] = RT_STATUS_LABEL[status] || ["—", "muted"];
    const timeStr = rtFmtTime(rec.created_at);
    const scoreStr = typeof score === "number" ? score.toFixed(3) : score;
    const convApplied = rec.convergence_applied ? 1 : 0;
    const comboCnt = rec.combo_count || 0;

    const msgTypeMap = {
        text:   { label: "text",   color: "#3b82f6" },
        image:  { label: "image",  color: "#06b6d4" },
        mixed:  { label: "mixed",  color: "#a78bfa" },
        merged: { label: "merged", color: "#f472b6" },
    };
    const mt = msgTypeMap[msgType] || msgTypeMap.text;

    // —— 横向路径 ribbon：节点落在真实耗时上，瓶颈节点高亮 ——
    const maxMs = stages.length ? Math.max(...stages.map(s => typeof s.ms === "number" ? s.ms : 0)) : 0;
    const ribbon = stages.length ? stages.map((s, i) => {
        const meta = rtStageMeta(s.stage);
        const ms = typeof s.ms === "number" ? s.ms : 0;
        const isBot = stages.length > 1 && ms === maxMs;
        const last = i === stages.length - 1;
        const line = last ? "" : `<span class="rt-ribbon-line"></span>`;
        return `<div class="rt-ribbon-node ${isBot ? "rt-ribbon-bot" : ""}" style="--c:${meta.color}" title="${meta.label} · ${rtFmtMs(ms)}">
                    <span class="rt-ribbon-dot"><i class="fa-solid ${meta.icon}"></i></span>
                    <span class="rt-ribbon-ms">${rtFmtMs(ms)}</span>
                </div>${line}`;
    }).join("") : `<div class="rt-muted">⚠ 无阶段记录</div>`;

    // —— 决策理由 chips：把"为什么这么路由"做成可见标签 ——
    const chips = [];
    if (intent && intent !== "—") chips.push(`<span class="rt-chip">意图 ${escapeHtml(intent)}</span>`);
    if (action && action !== "—") chips.push(`<span class="rt-chip">动作 ${escapeHtml(action)}</span>`);
    chips.push(`<span class="rt-chip rt-chip-strong">技能 ${escapeHtml(skill)} <b>${scoreStr}</b></span>`);
    if (mode && mode !== "—") chips.push(`<span class="rt-chip">模式 ${escapeHtml(mode)}</span>`);
    if (convApplied) chips.push(`<span class="rt-chip-badge rt-chip-conv"><i class="fa-solid fa-bolt"></i> 收敛介入</span>`);
    if (comboCnt > 0) chips.push(`<span class="rt-chip-badge rt-chip-combo"><i class="fa-solid fa-layer-group"></i> 组合×${comboCnt}</span>`);
    // 行动层主动意图（action.monitor / action.subscribe）：紫色徽章，与处置/动作区分
    const actionCats = (rec.intent_actions || "")
        .split(",").map(s => s.trim()).filter(Boolean);
    const PROACTIVE_LABELS = { "action.monitor": "监控", "action.subscribe": "订阅" };
    const proactiveShown = new Set();
    for (const ac of actionCats) {
        if (proactiveShown.has(ac)) continue;
        proactiveShown.add(ac);
        const lbl = PROACTIVE_LABELS[ac] || ac.replace(/^action\./, "");
        chips.push(`<span class="rt-chip-badge rt-chip-proactive"><i class="fa-solid fa-satellite-dish"></i> 主动·${escapeHtml(lbl)}</span>`);
    }

    // 被停用技能级联屏蔽的工具：醒目标记，运维一眼看到"为什么这次没调某工具"
    const blocked = parseBlockedList(rec.blocked_by_disabled_skill);
    if (blocked.length) {
        chips.push(`<span class="rt-chip-badge rt-chip-blocked" title="因技能停用而被屏蔽: ${escapeHtml(blocked.join("、"))}"><i class="fa-solid fa-ban"></i> 屏蔽 ${blocked.length} 工具</span>`);
    }

    // —— 内联展开的链路瀑布（复用既有 hop/gantt 样式） ——
    const totalStageMs = Math.max(1, stages.reduce((a, s) => a + (typeof s.ms === "number" ? s.ms : 0), 0));
    const hopLines = stages.map((s, i) => {
        const meta = rtStageMeta(s.stage);
        const ms = typeof s.ms === "number" ? s.ms : 0;
        const clr = latClr(ms);
        const w = Math.max(2, Math.round((ms / totalStageMs) * 100));
        const last = i === stages.length - 1;
        const stTag = s.status ? (RT_STATUS_LABEL[s.status] ? RT_STATUS_LABEL[s.status][0] : "") : "";
        const stCls2 = s.status || "";
        return `
            <div class="rt-hop-line ${last ? "rt-hop-last" : ""}">
                <div class="rt-rail"><span class="rt-rail-dot" style="--c:${meta.color}"></span></div>
                <span class="rt-hop-idx">${String(i + 1).padStart(2, " ")}</span>
                <span class="rt-hop-ico" style="color:${meta.color}"><i class="fa-solid ${meta.icon}"></i></span>
                <span class="rt-hop-name">${meta.label}</span>
                <span class="rt-hop-bar"><span class="rt-hop-fill" style="width:${w}%;background:linear-gradient(90deg, ${clr}55, ${clr})"></span></span>
                <span class="rt-hop-ms" style="color:${clr}">${rtFmtMs(ms).padStart(8, " ")}</span>
                ${stTag ? `<span class="rt-hop-st rt-hop-st-${stCls2}">${stTag}</span>` : ""}
            </div>`;
    }).join("");

    const gantt = stages.length ? buildGantt(stages) : "";

    const tools = Array.isArray(rec.tools_exposed) ? rec.tools_exposed : [];
    const toolsTxt = tools.length ? `${tools.length} tools` : "no tools";

    const rawReply = (typeof rec.reply_text === "string" ? rec.reply_text : "").trim();
    const RT_REPLY_MAX = 110;
    const replyDisp = rawReply
        ? (rawReply.length > RT_REPLY_MAX
            ? rawReply.slice(0, Math.floor((RT_REPLY_MAX - 1) / 2)) + "…" + rawReply.slice(-Math.floor((RT_REPLY_MAX - 1) / 2))
            : rawReply)
        : "";
    const replyLine = replyDisp
        ? `<div class="rt-reply" title="${escapeHtml(rawReply)}">
              <span class="rt-reply-tag">↩ AI</span>
              <span class="rt-reply-text">${escapeHtml(replyDisp)}</span>
           </div>`
        : "";

    const card = document.createElement("div");
    card.className = `rt-card rt-modern rt-status-${status}`;
    card.id = `rt-card-${rec.id}`;
    card.innerHTML = `
        <div class="rt-card-head">
            <span class="rt-avatar" style="background:${mt.color}22;color:${mt.color}">${escapeHtml(sender.charAt(0).toUpperCase())}</span>
            <span class="rt-sender">${escapeHtml(sender)}</span>
            <span class="rt-type-badge" style="background:${mt.color}1a;color:${mt.color}">${mt.label}</span>
            <span class="rt-head-spacer"></span>
            <span class="rt-time"><i class="fa-regular fa-clock"></i> ${timeStr}</span>
            <span class="rt-status-pill rt-st-${status}">${stLabel}</span>
        </div>
        <div class="rt-preview">${escapeHtml(preview.length > 120 ? preview.slice(0, 120) + "…" : preview) || '<span class="rt-muted">(空消息)</span>'}</div>
        ${replyLine}
        <div class="rt-ribbon">${ribbon}</div>
        <div class="rt-chips">${chips.join("")}</div>
        <div class="rt-expand">
            <button class="rt-expand-btn" onclick="toggleRtExpand(this)"><i class="fa-solid fa-water"></i> 链路瀑布 <i class="fa-solid fa-chevron-down rt-expand-ico"></i></button>
            <div class="rt-expand-body" style="display:none">
                ${gantt}
                <div class="rt-hops">${hopLines || '<div class="rt-term-empty">⚠ 无阶段记录 (stages_json 为空)</div>'}</div>
            </div>
        </div>
        <div class="rt-card-foot">
            <span class="rt-total"><b>${totalTxt}</b> 总耗时</span>
            <span class="rt-foot-meta">${stages.length} 跳 · ${toolsTxt} · 分 ${scoreStr} · #${rec.id}</span>
            <button class="rt-detail-btn" onclick="openRouteTraceDetail(${rec.id})">详情 →</button>
        </div>`;
    return card;
}

function latClr(ms) {
    return ms < 100 ? "#22c55e"
         : ms < 500 ? "#3b82f6"
         : ms < 1000 ? "#fb923c"
         : ms < 3000 ? "#f87171"
         : "#f472b6";
}

// 顶部甘特时间轴
function buildGantt(stages) {
    const totalStageMs = Math.max(1, stages.reduce((a, s) => a + (typeof s.ms === "number" ? s.ms : 0), 0));
    let gacc = 0;
    const gantt = stages.map((s) => {
        const meta = rtStageMeta(s.stage);
        const ms = typeof s.ms === "number" ? s.ms : 0;
        const seg = Math.max(1.5, Math.round((ms / totalStageMs) * 1000) / 10);
        const left = gacc; gacc += seg;
        return `<span class="rt-gantt-seg" style="left:${left}%;width:${seg}%;background:${meta.color}" title="${meta.label} · ${rtFmtMs(ms)} (${seg.toFixed(1)}%)"></span>`;
    }).join("");
    return `<div class="rt-gantt">${gantt}<span class="rt-gantt-total">${rtFmtMs(totalStageMs)}</span></div>`;
}

function toggleRtExpand(btn) {
    const body = btn.parentElement.querySelector(".rt-expand-body");
    if (!body) return;
    const open = body.style.display !== "none";
    body.style.display = open ? "none" : "block";
    btn.classList.toggle("rt-expand-open", !open);
}

// ============ 数据加载 ============

async function loadRouteTrace(page) {
    if (page) _rtPage = page;
    const listEl = document.getElementById("rt-list");
    if (!listEl) return;
    listEl.classList.add("rt-list");
    try {
        const skill = document.getElementById("rt-filter-skill")?.value || "";
        const source = document.getElementById("rt-filter-source")?.value || "";
        const time = document.getElementById("rt-filter-time")?.value || "";
        const blocked = document.getElementById("rt-filter-blocked")?.value || "";
        // 原分页 fetch 改用 RoutingQualityService.loadPage，参数保持一致
        const data = await RoutingQualityService.loadPage(_rtPage, 15);
        const items = data.items || [];
        if (!items.length) {
            listEl.innerHTML = `
                <div class="rt-empty-state-modern">
                    <i class="fa-solid fa-route"></i>
                    <div class="rt-empty-title">暂无路由追踪记录</div>
                    <div class="rt-empty-desc">发一条消息给机器人后，这里会显示完整的链路追踪</div>
                </div>`;
        } else {
            listEl.innerHTML = "";
            const win = document.createElement("div");
            win.className = "rt-term-win";
            for (const rec of items) win.appendChild(renderRouteCard(rec));
            listEl.appendChild(win);
        }
        renderRouteTracePagination(data);
        await loadRouteTraceStats();
        await loadRouteTraceAggregate();
    } catch (e) {
        listEl.innerHTML = `<div class="empty-state"><i class="fa-solid fa-triangle-exclamation" style="font-size:1.75rem;opacity:.4;color:#ef4444;"></i><p style="margin-top:.5rem;">加载失败：${escapeHtml(e.message || e)}</p></div>`;
    }
}

async function loadRouteTraceStats() {
    try {
        const st = await RoutingQualityService.loadStats();
        if (!st || st.available === false) { /* 忽略 */ }
        else {
            // llm / max 由 loadRouteTraceAggregate 统一渲染（aggregate 已包含这两个字段），
            // 此处不再 setText，避免破坏 renderKpiCard 注入的 icon + sub 结构。
            fillSelectOnce("rt-filter-skill", st.skills || []);
            fillSelectOnce("rt-filter-source", st.sources || []);
        }
    } catch (_) { /* 忽略 */ }
}

function fillSelectOnce(id, options) {
    const sel = document.getElementById(id);
    if (!sel || sel.dataset.filled) return;
    for (const o of options) {
        if (!o) continue;
        const opt = document.createElement("option");
        opt.value = o; opt.textContent = o;
        sel.appendChild(opt);
    }
    sel.dataset.filled = "1";
}

function renderRouteTracePagination(data) {
    const el = document.getElementById("rt-pagination");
    if (!el) return;
    const total = data.total || 0;
    const size = data.page_size || 15;
    const cur = data.page || 1;
    const pages = Math.max(1, Math.ceil(total / size));
    if (pages <= 1) { el.innerHTML = ""; return; }

    let start = Math.max(1, cur - 2);
    let end = Math.min(pages, start + 4);
    start = Math.max(1, end - 4);
    const nums = [];
    for (let p = start; p <= end; p++) nums.push(p);

    const pageBtn = (p, label, opts = {}) => {
        const cls = ["rt-pg", opts.active ? "rt-pg-active" : "", opts.disabled ? "rt-pg-disabled" : ""].join(" ").trim();
        const attr = opts.disabled ? "" : `onclick="loadRouteTrace(${p})"`;
        const inner = label ?? String(p);
        return `<button class="${cls}" ${attr}>${inner}</button>`;
    };

    let html = `<div class="rt-pager">`;
    html += pageBtn(1, "«", { disabled: cur === 1 });
    html += pageBtn(cur - 1, "‹", { disabled: cur === 1 });
    if (start > 1) html += `<span class="rt-pg-gap">…</span>`;
    for (const p of nums) html += pageBtn(p, null, { active: p === cur });
    if (end < pages) html += `<span class="rt-pg-gap">…</span>`;
    html += pageBtn(cur + 1, "›", { disabled: cur === pages });
    html += pageBtn(pages, "»", { disabled: cur === pages });
    html += `<span class="rt-pg-info">第 ${cur}/${pages} 页 · 共 ${total} 条</span>`;
    html += `</div>`;
    el.innerHTML = html;
}

async function openRouteTraceDetail(id) {
    const modal = document.getElementById("routetrace-modal");
    const body = document.getElementById("rt-modal-body");
    if (!modal || !body) return;
    body.innerHTML = `<div class="empty-state"><i class="fa-solid fa-spinner fa-spin" style="font-size:24px;margin-bottom:10px;display:block"></i>加载中…</div>`;
    modal.classList.add("active");
    try {
        const rec = await api.fetch(`/api/routing-quality/${id}`);
        const stages = Array.isArray(rec.stages_json) ? rec.stages_json : [];
        const blockedTools = parseBlockedList(rec.blocked_by_disabled_skill);

        let hops = "";
        for (const s of stages) {
            const meta = rtStageMeta(s.stage);
            const [stTxt, stCls] = RT_STATUS_LABEL[s.status] || [s.status, "muted"];
            const detail = s.detail || {};
            hops += `
                <div class="rt-detail-hop">
                    <div class="rt-detail-hop-head">
                        <span class="rt-detail-stage-icon" style="background:${meta.color}15;color:${meta.color}">
                            <i class="fa-solid ${meta.icon}"></i>
                        </span>
                        <b>${meta.label}</b>
                        <span class="rt-hop-ms">${rtFmtMs(s.ms)}</span>
                        <span class="pill pill-${stCls}">${stTxt}</span>
                    </div>
                    <pre class="rt-json">${escapeHtml(JSON.stringify(detail, null, 2))}</pre>
                </div>`;
        }
        body.innerHTML = `
            <div class="rt-detail-summary">
                <div><span class="rt-d-label">发送者</span>${rtShortText(rec.sender_name, 18)}</div>
                <div><span class="rt-d-label">会话</span><span class="rt-cid-full" title="${escapeHtml(rec.conversation_id || "")}">${escapeHtml(rtTruncCid(rec.conversation_id))}</span></div>
                <div><span class="rt-d-label">意图</span>${rtShortText(rec.intent_disposition, 18)}</div>
                <div><span class="rt-d-label">动作</span>${rtShortText(rec.intent_action, 18)}</div>
                <div><span class="rt-d-label">行动意图</span>${(rec.intent_actions && String(rec.intent_actions).trim()) ? escapeHtml(rec.intent_actions) : '<span class="rt-muted">无</span>'}</div>
                <div><span class="rt-d-label">被屏蔽工具</span>${blockedTools.length ? `<span class="rt-d-blocked">${escapeHtml(blockedTools.join(", "))}</span>` : '<span class="rt-muted">无</span>'}</div>
                <div><span class="rt-d-label">主技能</span>${rtShortText(rec.primary_skill, 18)}</div>
                <div><span class="rt-d-label">分数</span>${rec.primary_score ?? "—"}</div>
                <div><span class="rt-d-label">路由模式</span>${rtShortText(rec.routing_mode, 18)}</div>
                <div><span class="rt-d-label">LLM 模型</span>${rtShortText(rec.llm_model, 18)}</div>
                <div><span class="rt-d-label">推理轮次</span>${rec.llm_rounds ?? "—"}</div>
                <div><span class="rt-d-label">总耗时</span>${rtFmtMs(rec.total_latency_ms)}</div>
            </div>
            <div class="rt-detail-content"><span class="rt-d-label">消息内容</span><div class="rt-content-box">${escapeHtml(rec.content_preview || "")}</div></div>
            ${(rec.reply_text && String(rec.reply_text).trim()) ? `
            <div class="rt-detail-reply">
                <span class="rt-d-label">AI 回复<span class="rt-d-reply-meta">· ${rec.reply_len ?? 0} 字符</span></span>
                <div class="rt-reply-box">${escapeHtml(rec.reply_text)}</div>
            </div>` : ""}
            <h4 style="margin:14px 0 8px;">链路瀑布</h4>
            ${hops}
            <div class="rt-detail-tools"><span class="rt-d-label">暴露工具</span>${renderToolChips(rec.tools_exposed)}</div>`;
    } catch (e) {
        body.innerHTML = `<div class="empty-state"><i class="fa-solid fa-triangle-exclamation" style="font-size:1.75rem;opacity:.4;color:#ef4444;"></i><p style="margin-top:.5rem;">加载失败：${escapeHtml(e.message || e)}</p></div>`;
    }
}

function renderToolChips(tools) {
    if (!Array.isArray(tools) || !tools.length) return `<span class="rt-muted">无</span>`;
    return tools.map(t => `<span class="tam-tag">${escapeHtml(t)}</span>`).join(" ");
}

function closeRouteTraceModal() {
    document.getElementById("routetrace-modal")?.classList.remove("active");
}

// 订阅 routingQuality.aggregate 切片：当其他页面（如 dashboard）触发数据刷新时，
// 无需重新请求即可更新 KPI 卡片，实现跨页数据共享。
let _rtAggregateUnsub = null;
function _setupRtAggregateSubscription() {
    if (_rtAggregateUnsub) return; // 避免重复订阅
    _rtAggregateUnsub = window.store.subscribeSlice('routingQuality', 'aggregate', function (agg) {
        if (!agg || agg.available === false) return;
        // 仅当 routetrace 页活跃时重渲染 KPI
        if (currentPage !== "intent" || window._activeIntentTab !== "routetrace") return;
        renderKpiCard("rt-kpi-total", {
            label: "总记录数", icon: "📊",
            value: agg.total_records ?? "—",
            sub: "路由追踪总条数"
        });
        renderKpiCard("rt-kpi-avg", {
            label: "平均延迟", icon: "⏱️",
            value: rtFmtMs(agg.avg_total_ms ?? "—"),
            sub: "全链路平均耗时"
        });
        const healthRate = ((1 - (agg.empty_rate ?? 0)) * 100).toFixed(1);
        renderKpiCard("rt-kpi-health", {
            label: "健康率", icon: "✅",
            value: healthRate + "%",
            sub: "空回复率 " + ((agg.empty_rate ?? 0) * 100).toFixed(1) + "%",
        });
        // 同步 llm / max，避免 polling 周期中两个卡片停在过期值
        renderKpiCard("rt-kpi-llm", {
            label: "平均 LLM 推理", icon: "🧠",
            value: rtFmtMs(agg.avg_llm_ms ?? 0),
            sub: "LLM 推理耗时均值"
        });
        renderKpiCard("rt-kpi-max", {
            label: "峰值总耗时", icon: "📈",
            value: rtFmtMs(agg.max_total_ms ?? 0),
            sub: "全链路最长记录"
        });
    });
}

function startRouteTracePolling() {
    if (_rtPolling) return;
    _setupRtAggregateSubscription();
    _rtPolling = setInterval(() => {
        if (currentPage === "intent" && (window._activeIntentTab === "routetrace")) loadRouteTrace(_rtPage);
    }, 5000);
    loadRouteTrace(1);
}

function stopRouteTracePolling() {
    if (_rtPolling) { clearInterval(_rtPolling); _rtPolling = null; }
}

// 解析「被停用技能级联屏蔽」的工具列表（落库为 JSON 数组字符串）
function parseBlockedList(raw) {
    if (Array.isArray(raw)) return raw;
    if (!raw) return [];
    try {
        const v = JSON.parse(raw);
        return Array.isArray(v) ? v : [];
    } catch (e) {
        return [];
    }
}

// 桥接到全局，供 index.html 内联 handler 与 app.js 调用
window.loadRouteTrace = loadRouteTrace;
window.toggleRtExpand = toggleRtExpand;
window.openRouteTraceDetail = openRouteTraceDetail;
window.closeRouteTraceModal = closeRouteTraceModal;
window.startRouteTracePolling = startRouteTracePolling;
window.stopRouteTracePolling = stopRouteTracePolling;
