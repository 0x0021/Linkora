"""弱模型回复泄漏回归护城河（weak-model regression moat）。

★ 目的
把这两周真实发生的「弱模型(agnes/kenari)obey 率不足 → 思考链/独白/owner 口吻泄进回复」
生产事件，固化成一份**事件回放语料**。任何对 `src/llm/style.py`（sanitize_reply /
gate_reply）或 system_prompt 的改动，都必须让本套件全绿，否则视为回归。

为什么单独成文件、不在 test_sanitize_prompt_leak.py 里加：
- 那份是「按清洗机制分类」的单元测试；这份是「按生产事故分类」的回归基线。
- 本文件可经 `pytest -m weak_model_regression` 单独跑，作为**部署前质量闸门**
  （见 scripts/run_weak_model_regression.py），无需跑全部 sanitize 用例。

覆盖的真实事件（时间线）：
- 2026-07-27：system prompt 整段回声（身份/规则/风格/few-shot/数字分身）。
- 2026-07-27：owner 名字出现在评估/审批/协助口吻 + 带箭头的编造流程路径 → 整句闸门。
- 2026-07-29：Jenkins 5 句连续杂糅独白当回复开头（首句「我知道…」被旧白名单故意放过，
  5 句形态都不在既有模式 → 全漏）。
- 2026-07-29（回归防御）：0.12 前瞻误吞「让我帮您看一下…」正常答案（答案不可被吞）。

────────────────────────────────────────────────────────────────────────────
⚠️ 已知未覆盖缺口（TestKnownWeakModelGaps，标记 xfail）
建护城河时探测发现的 4 类，先登记为 xfail 以便追踪；其中 G3（真答案被吞）已于
2026-07-30 修复并转为常规断言，余下 G1/G2/G4 仍为 xfail；
将来修掉后这些用例会从 XFAIL 变 XPASS（pytest 会高亮），届时再把 xfail 去掉即可。
这些是「新发现的坑」，不属于这两周已修的事件，故不阻塞护城河。
────────────────────────────────────────────────────────────────────────────
"""

import pytest

from src.llm.style import sanitize_reply, gate_reply

pytestmark = pytest.mark.weak_model_regression


# ─────────────────────────────────────────────────────────────────────────────
# 事件 1：Jenkins 5 句杂糅独白（2026-07-29 截图实证）
# ─────────────────────────────────────────────────────────────────────────────
JENKINS_MONOLOGUE = (
    "我知道Jenkins发版服务器是 http://jenkins.dev.acme.com/，"
    "调试服务器是 https://build.dev.acme.com/。"
    "用户提到的问题是在Jenkins流水线构建中，用户名被加上了@acme.com后缀导致解析错误。"
    "我需要直接回答这个问题，基于我的技术知识来处理。"
    "这是一个典型的Jenkins流水线中用户名解析的问题，通常与身份认证或环境变量有关。"
    "让我给出一个专业的技术回复。"
    "收到截图了，看到两个问题：一是流水线里 userName 被加了后缀，二是构建缓存没清理。"
)


class TestJenkinsMonologueIncident:
    """★ 2026-07-29 核心事故：5 句思考全清，仅留真答案。"""

    def test_full_5_sentence_monologue_stripped(self):
        result = sanitize_reply(JENKINS_MONOLOGUE)
        # 5 句思考标记全部清除
        assert "我知道Jenkins" not in result
        assert "用户提到的问题" not in result
        assert "我需要直接回答" not in result
        assert "这是一个典型的Jenkins" not in result
        assert "让我给出一个专业的技术回复" not in result
        # 真实答案保留
        assert "收到截图了" in result
        assert "userName 被加了后缀" in result
        assert "构建缓存没清理" in result

    def test_pattern_self_narrate_url(self):
        """⑨ 自述已知具体事实（专名 + 事实动词）。"""
        leak = "我知道Jenkins发版服务器是 http://jenkins.dev.acme.com/。请检查流水线配置。"
        result = sanitize_reply(leak)
        assert "我知道Jenkins" not in result
        assert "请检查流水线配置" in result

    def test_pattern_user_mentions_question(self):
        """⑩ 问题复述循环化（连续『用户提到…』）。"""
        leak = "用户提到流水线超时了。用户说已经重试过三次。建议增大超时时间。"
        result = sanitize_reply(leak)
        assert "用户提到流水线超时" not in result
        assert "用户说已经重试过" not in result
        assert "建议增大超时时间" in result

    def test_pattern_need_direct_action(self):
        """⑥ 我需要 + 直接 + 行为动词。"""
        leak = "我需要直接回答这个问题，基于我的技术知识来处理。排查步骤：1. 看日志。"
        result = sanitize_reply(leak)
        assert "我需要直接回答" not in result
        assert "排查步骤" in result

    def test_pattern_typical_problem_usually(self):
        """⑦ 这是 + 问题 + 通常/往往。"""
        leak = "这是一个典型的Jenkins用户名解析问题，通常与身份认证有关。请检查 userName 变量。"
        result = sanitize_reply(leak)
        assert "这是一个典型的Jenkins" not in result
        assert "请检查 userName" in result

    def test_pattern_give_professional_reply(self):
        """⑧ 让我给 + 一个/一下 + 回复/答案（数量词后无你/您）。"""
        leak = "让我给出一个专业的技术回复。解决方案如下。"
        result = sanitize_reply(leak)
        assert "让我给出一个专业的技术回复" not in result
        assert "解决方案如下" in result


# ─────────────────────────────────────────────────────────────────────────────
# 事件 2：0.12 前瞻误吞正常答案（回归防御）
# ─────────────────────────────────────────────────────────────────────────────
class TestFrontLookaheadFalsePositive:
    """★ 2026-07-29 修复 0.12 时引入的回归：裸「您」前瞻误吞正常答案。
    护城河断言：正常答案的实义部分不被吞（前导思考前缀被剥可接受，真答案必须留存）。
    """

    def test_lets_help_you_kept(self):
        """「让我帮您看一下配置」是正常答案，完整保留。"""
        good = "让我帮您看一下配置，稍等。"
        assert sanitize_reply(good) == good

    def test_real_answer_not_eaten_after_preamble(self):
        """0.12 核心回归：『我需要您先确认…』作前导被剥，但后续真问题『请问您…』必须留存。"""
        good = "我需要您先确认一下身份，请问您方便提供工单号吗？"
        result = sanitize_reply(good)
        assert "请问您方便提供工单号吗" in result

    def test_normal_i_know_your_question_kept(self):
        """「我知道你的问题是想问 VPN」——专名后无『是』，⑨ 不命中，保留。"""
        good = "我知道你的问题是想问 VPN 怎么连。"
        assert sanitize_reply(good) == good


# ─────────────────────────────────────────────────────────────────────────────
# 事件 3：owner 名字整句闸门 + 编造流程路径（B 末端闸门）
# ─────────────────────────────────────────────────────────────────────────────
OWNER_NAME = "主人"
OWNER_TITLE = "IT工程师"


class TestOwnerNameGateIncident:
    """★ 2026-07-27 owner 身份匹配变量化 + 整句闸门。"""

    def test_owner_name_in_evaluate_voice_gated(self):
        bad = "由主人(IT工程师)评估后走正规采购流程即可。"
        out, triggered = gate_reply(bad, OWNER_NAME, OWNER_TITLE)
        assert triggered is True
        assert "主人" not in out  # 坏原句自引用不残留

    def test_owner_name_assist_voice_gated(self):
        bad = "主人会帮您协助处理这个审批单。"
        out, triggered = gate_reply(bad, OWNER_NAME, OWNER_TITLE)
        assert triggered is True
        assert "主人" not in out

    def test_fabricated_path_with_arrow_gated(self):
        bad = "通过钉钉→工作台→申请 即可完成。"
        out, triggered = gate_reply(bad, OWNER_NAME, OWNER_TITLE)
        assert triggered is True
        assert "→" not in out  # 编造箭头路径不残留

    def test_normal_dingtalk_guidance_not_falsely_gated(self):
        good = "通过钉钉工作台走 OA 审批流程。"
        out, triggered = gate_reply(good, OWNER_NAME, OWNER_TITLE)
        assert triggered is False
        assert "通过钉钉工作台走 OA 审批流程" in out

    def test_clean_reply_passes_through(self):
        good = "已为您提交工单，预计 2 小时内处理。"
        out, triggered = gate_reply(good, OWNER_NAME, OWNER_TITLE)
        assert triggered is False
        assert out == good


# ─────────────────────────────────────────────────────────────────────────────
# 事件 4：system prompt 整段回声（2026-07-27）
# ─────────────────────────────────────────────────────────────────────────────
class TestSystemPromptEchoIncident:
    """★ 2026-07-27 截图：注入段整行回声。"""

    def test_identity_line_echo(self):
        leak = "身份:主人的数字分身。部门:IT部。\n打印机的IP是192.168.1.10"
        result = sanitize_reply(leak)
        assert "数字分身" not in result
        assert "192.168.1.10" in result

    def test_rules_line_echo(self):
        leak = "规则:闲聊≤100字|禁止内心独白。\n请先重启打印机服务"
        result = sanitize_reply(leak)
        assert "禁止内心独白" not in result
        assert "请先重启打印机服务" in result

    def test_few_shot_block_echo(self):
        leak = (
            "样例(主人原话风格参考):\n"
            "- 用户: 打印机坏了\n  主人: 重启下 Print Spooler\n"
            "打印机驱动重装步骤如下："
        )
        result = sanitize_reply(leak)
        assert "主人原话风格参考" not in result
        assert "打印机驱动重装步骤" in result


# ─────────────────────────────────────────────────────────────────────────────
# 护城河正向护栏：正常业务回复必须一字不改（防止过度清除回归）
# ─────────────────────────────────────────────────────────────────────────────
class TestCleanReplyZeroChange:
    @pytest.mark.parametrize("good", [
        "我已经帮您重启了服务，请刷新页面。",
        "建议您先备份再升级，避免数据丢失。",
        "这个问题我之前处理过，根因是证书过期。",
        "请按以下三步操作：1. 登录 2. 进入设置 3. 保存。",
        "让我帮您看一下配置，稍等。",
    ])
    def test_clean_reply_zero_change(self, good):
        assert sanitize_reply(good) == good


# ─────────────────────────────────────────────────────────────────────────────
# ⚠️ 已知未覆盖缺口（建护城河时探测发现，当前代码仍会泄漏）
# 标记为 xfail：不阻塞护城河，但 CI 可见、可追踪；修掉后变 XPASS 报警即移除 xfail。
# ─────────────────────────────────────────────────────────────────────────────
class TestKnownWeakModelGaps:
    @pytest.mark.xfail(reason="G1: 首人称回想『让我回忆一下/让我回想』独白未剥离", strict=False)
    def test_first_person_recall_not_stripped(self):
        leak = "用户问的是怎么重置密码。让我回忆一下，重置入口在设置页。请去设置页操作。"
        result = sanitize_reply(leak)
        assert "让我回忆一下" not in result
        assert "请去设置页操作" in result

    def test_named_digital_avatar_leak(self):
        """G2: 具名+数字分身身份泄漏『作为主人的数字分身』须剥离（2026-08-03 修复）。

        主语候选扩展为含 2-4 字中文姓名（与风格泄漏处理一致），覆盖具名主人变体；
        仅在含『数字分身』时触发，不影响『我是XX的数字分身』正常身份披露。
        """
        leak = "作为主人的数字分身，我将以他的风格回复。会议室已预定三楼。"
        result = sanitize_reply(leak)
        assert "数字分身" not in result
        assert "会议室已预定三楼" in result

    # G3 已于 2026-07-30 修复（推理片段尾巴由「吃到行尾」收敛为「吃到本句末」），
    # xfail 已移除，转为常规回归断言。
    def test_prefix_causes_whole_reply_cleared(self):
        leak = "我应该先检查日志里有没有报错，再决定下一步。日志路径在 /var/log/app。"
        result = sanitize_reply(leak)
        # 期望：保留真答案，至少不应清空
        assert result != ""
        assert "/var/log/app" in result
        # 反泄漏不得削弱：推理句本身仍须整句清除
        assert "我应该先检查" not in result

    @pytest.mark.xfail(reason="G4: 『根据我的分析…/基于以上…』无用户指向尾句时不剥离（温和推理前缀泄漏）", strict=False)
    def test_reasoning_prefix_without_user_tail_not_stripped(self):
        leak = "根据我的分析，这个报错是端口被占用导致的。建议换 8081 端口。"
        result = sanitize_reply(leak)
        # 期望：推理前缀剥离、保留真答案（当前整条未处理）
        assert "根据我的分析" not in result
        assert "建议换 8081 端口" in result


# ─────────────────────────────────────────────────────────────────────────────
# G3 修复点定向回归（2026-07-30）：推理片段只吃到「本句末」，不跨句吞真答案
# ─────────────────────────────────────────────────────────────────────────────
class TestReasoningTailSentenceBoundary:
    """★ G3 根因回归：`_REASONING_TAIL` 的句边界收敛。

    旧实现两条尾巴都是「吃到行尾」（`.*$` / `[^\\n]*$`），推理句与真答案同行时
    答案被连坐删除，最坏整条清空。现收敛为「吃到本句句末标点」，并对已自带句末
    标点的分支（如「我需要…以便…。」）用 `(?<![。！？])` 守卫强制尾巴为空。
    """

    @pytest.mark.parametrize("leak, gone, kept", [
        # ① 我应该 + 认知动词：尾巴吃到句末标点即止
        ("我应该先检查日志里有没有报错，再决定下一步。日志路径在 /var/log/app。",
         "我应该先检查", "/var/log/app"),
        # ② 我需要 + 认知动词 + 「。」终止分支（body 自身已吃掉句号 → 尾巴须为空）
        ("我需要先确认一下你的网络环境是否正常。你可以先 ping 一下网关。",
         "我需要先确认", "ping 一下网关"),
        # ③ 我需要…以便…。 目的从句：同样不得吃掉紧随其后的答案句
        ("我需要询问具体是哪一个位置的打印机，以便提供正确的IP地址和连接方式。"
         "7F 研发办公区打印机（10.0.2.3）。",
         "我需要询问", "10.0.2.3"),
        # ④ 提示词泄漏前缀 + 真答案同句后：泄漏整句清除、答案留存
        ("根据系统提示，我需要以主人的数字分身身份来回答。会议室在三楼。",
         "数字分身", "会议室在三楼"),
        # ⑤ 行内（非行首）推理：只删推理句，前后正常句都留
        ("好的。我应该用主人的风格来回复。已经帮你重启了服务。",
         "主人的风格", "已经帮你重启了服务"),
    ])
    def test_answer_survives_reasoning_strip(self, leak, gone, kept):
        result = sanitize_reply(leak)
        assert gone not in result       # 反泄漏未削弱
        assert kept in result           # 真答案未被吞
        assert result != ""

    def test_pure_reasoning_line_still_fully_cleared(self):
        """行为保持：整行都是推理时仍整行清除（不因收敛尾巴而残留）。"""
        assert sanitize_reply("我应该先检查日志里有没有报错，再决定下一步。") == ""

    def test_multiline_reasoning_line_removed_without_blank_line(self):
        """行为保持：整行推理被删后不残留空行，下一行正文顶格保留。"""
        result = sanitize_reply("我应该先分析这个报错的根因。\n结论：证书过期，请重新签发。")
        assert result == "结论：证书过期，请重新签发。"
