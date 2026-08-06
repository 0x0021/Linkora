/**
 * 首次使用引导 (Onboarding)
 *
 * 首次访问时检测 localStorage 标记 + 后端 LLM 配置状态，未完成则显示 3 步引导 overlay。
 * 仅覆盖最小化核心流程：配置 API Key → 创建关键词 → 发送测试。
 *
 * 显示条件（满足任一即跳过）：
 *   1. localStorage 已标记 linkora_onboarding_done
 *   2. 后端 llm.api_key 已配置（非空）
 */

const ONBOARDING_KEY = 'linkora_onboarding_done';
let _onboardingStep = 0;
/** 缓存后端检测结果，避免重复请求 */
let _backendChecked = false;
let _hasApiKey = false;

/**
 * 异步检测后端是否已配置 API Key。
 * /api/config 返回的 llm.api_key 经脱敏（如 "9311****"），非空即视为已配置。
 */
async function checkBackendConfig() {
    try {
        const res = await api.fetch('/api/config');
        if (res && res.llm && res.llm.api_key && res.llm.api_key.trim()) {
            _hasApiKey = true;
            // 后端已配好 → 视同完成引导，静默标记
            try { localStorage.setItem(ONBOARDING_KEY, '1'); } catch (_) {}
        }
    } catch (_) {
        // 网络异常等不阻断，降级为仅看 localStorage
    } finally {
        _backendChecked = true;
    }
}

/**
 * 检测是否需要显示引导（同步快路径）
 * 仅在已完成后端检测后才给出最终答案；未完成时先返回 false 避免 flash。
 */
function shouldShowOnboarding() {
    // localStorage 已标记 → 永不再显示
    try { if (localStorage.getItem(ONBOARDING_KEY)) return false; } catch (_) {}
    // 后端检测完成且发现已配 key → 不显示
    if (_backendChecked && _hasApiKey) return false;
    // 后端检测未完成 → 先不显示（等异步结果）
    if (!_backendChecked) return false;
    return true;
}

/**
 * 显示引导 overlay（异步入口）
 */
async function showOnboarding() {
    // 先查后端配置
    await checkBackendConfig();
    // 再判断
    if (!shouldShowOnboarding()) return;
    const overlay = document.getElementById('onboarding-overlay');
    if (!overlay) return;
    overlay.classList.add('active');
    renderOnboardingStep();
}

/**
 * 隐藏引导
 */
function hideOnboarding() {
    const overlay = document.getElementById('onboarding-overlay');
    if (overlay) overlay.classList.remove('active');
    try { localStorage.setItem(ONBOARDING_KEY, '1'); } catch (_) {}
}

/**
 * 渲染当前步骤
 */
function renderOnboardingStep() {
    const steps = [
        {
            title: '配置 API Key',
            desc: '打开系统配置 -> LLM 模型，填入您的 API Key 并保存，让灵桥具备 AI 能力。',
            action: '去配置',
            actionFn: function () {
                hideOnboarding();
                switchPage('config');
            }
        },
        {
            title: '创建关键词',
            desc: '打开关键词匹配页面，新建您的第一条监控规则，灵桥将自动识别并响应匹配的消息。',
            action: '去创建',
            actionFn: function () {
                hideOnboarding();
                switchPage('keywords');
            }
        },
        {
            title: '发送测试消息',
            desc: '打开模拟测试页面，输入一条测试消息，验证关键词匹配与 AI 回复是否生效。',
            action: '去测试',
            actionFn: function () {
                hideOnboarding();
                switchPage('simulate');
            }
        }
    ];

    if (_onboardingStep >= steps.length) {
        hideOnboarding();
        return;
    }

    const step = steps[_onboardingStep];
    const indicator = document.getElementById('onboarding-indicator');
    const titleEl = document.getElementById('onboarding-title');
    const descEl = document.getElementById('onboarding-desc');
    const btnEl = document.getElementById('onboarding-btn');

    if (indicator) indicator.textContent = `步骤 ${_onboardingStep + 1} / ${steps.length}`;
    if (titleEl) titleEl.textContent = step.title;
    if (descEl) descEl.textContent = step.desc;
    if (btnEl) {
        btnEl.textContent = step.action;
        btnEl.onclick = step.actionFn;
    }
}

/**
 * 下一步
 */
function onboardingNext() {
    _onboardingStep++;
    if (_onboardingStep >= 3) {
        hideOnboarding();
    } else {
        renderOnboardingStep();
    }
}

/**
 * 跳过引导
 */
function onboardingSkip() {
    hideOnboarding();
}

// 页面加载完成后自动检测
document.addEventListener('DOMContentLoaded', function () {
    setTimeout(function () {
        showOnboarding();
    }, 800);
});
