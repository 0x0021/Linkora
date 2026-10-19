"""
RAG 自动注入门控测试。

修复背景：自动 RAG 注入把弱相关文档（如办公点 IP 表、配置清单）塞进 system prompt，
AI 误当成「需复述的事实」附在无关回答后（例：问天气却复述打印机清单）。

抽象修复（不限于天气/打印机，适用于任何主题）：
- 触发层：仅当 query 含文档/知识查询意图时才注入（闲聊/天气/问候等不注入）
- 召回层：收紧 min_similarity / max_results，避免弱相关文档进 prompt
- 生成层：prompt 硬约束「除非用户明确询问该主题，否则禁止复述相关知识」
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import LlmConfig
from src.llm.agent import LLMAgent


class _FakeToolRouter:
    """记录 kb_search.search 的调用参数，便于断言门槛收紧。"""
    def __init__(self):
        self.calls = []
        self._tools = {"kb_search": self}

    def search(self, query="", top_k=3, min_similarity=0.3, **kw):
        self.calls.append({"query": query, "top_k": top_k, "min_similarity": min_similarity})
        # 模拟命中：任何 query 都返回一条可展示的命中（score ≥ 展示阈值 0.50）。
        # 注：曾用 0.35（低于展示阈值），彼时测试依赖「空块也算注入」的 bug 才通过；
        # 空块防编造修复后，低于展示阈值的结果按未命中处理，故 mock 分数提至可展示区间。
        return {
            "success": True,
            "query": query,
            "results": [
                {"content": "7F研发办公区：10.0.2.3；上海：10.3.1.31", "source": "办公点IP表", "score": 0.62},
            ],
        }


class MockEmbeddingClient:
    """确定性伪 embedding，仅供单测：把文本按知识/闲聊关键词计数映射为二维向量
    [知识意图分, 闲聊意图分]。使 _is_document_query 的【语义路径】可离线验证，
    无需加载真实 BAAI 模型。零向量（无任何关键词）→ 余弦相似度均为 0 → 判定为无明确意图。
    """
    _KW_KNOWLEDGE = ["怎么", "如何", "配置", "使用", "业务", "流程", "操作", "步骤",
                    "申请", "账号", "权限", "查看", "文档", "说明", "功能", "规范",
                    "制度", "重置", "密码", "修改", "设置", "平台", "开通", "查", "搜索"]
    _KW_CASUAL = ["你好", "在吗", "今天", "天气", "中午", "吃", "谢谢", "祝",
                  "愉快", "最近", "忙", "早上", "晚上"]

    def embed(self, text: str) -> list[float]:
        t = text or ""
        k = sum(1 for w in self._KW_KNOWLEDGE if w in t)
        c = sum(1 for w in self._KW_CASUAL if w in t)
        return [float(k), float(c)]


class _TwoResultRouter:
    """返回 2 条都高于门槛的命中，top1 相似度最高。"""
    def __init__(self):
        self._tools = {"kb_search": self}

    def search(self, query="", top_k=3, min_similarity=0.3, **kw):
        return {"success": True, "query": query, "results": [
            {"content": "VPN 配置步骤：先登录网关…", "source": "VPN手册", "score": 0.82},
            {"content": "补充文档：相关说明…", "source": "补充文档", "score": 0.71},
        ]}


def _make_agent(**adv_overrides):
    adv = LlmConfig().advanced
    for k, v in adv_overrides.items():
        setattr(adv, k, v)
    agent = LLMAgent(
        config=LlmConfig(advanced=adv),
        client=None,
        tool_router=_FakeToolRouter(),
    )
    # 注入确定性伪 embedding，使语义路径可被单测（否则走兜底分支，对无闲聊词的非知识查询误判为 True）
    agent._emb_client = MockEmbeddingClient()
    return agent


class TestIsDocumentQuery:
    """触发层：意图识别应横向覆盖，不局限于天气/打印机。"""

    def test_weather_is_not_document_query(self):
        a = _make_agent()
        assert a._is_document_query("某城市天气怎么样", query_embedding=a._emb_client.embed("某城市天气怎么样")) is False
        assert a._is_document_query("北京天气呢", query_embedding=a._emb_client.embed("北京天气呢")) is False

    def test_printer_list_is_not_document_query(self):
        a = _make_agent()
        assert a._is_document_query("打印机清单", query_embedding=a._emb_client.embed("打印机清单")) is False
        assert a._is_document_query("帮我看看打印机", query_embedding=a._emb_client.embed("帮我看看打印机")) is False

    def test_greeting_is_not_document_query(self):
        a = _make_agent()
        assert a._is_document_query("在吗", query_embedding=a._emb_client.embed("在吗")) is False
        assert a._is_document_query("你好啊", query_embedding=a._emb_client.embed("你好啊")) is False

    def test_explicit_doc_intent_is_document_query(self):
        a = _make_agent()
        # 横向：换任何文档类意图都应触发（不局限于某主题）
        assert a._is_document_query("VPN怎么配置", query_embedding=a._emb_client.embed("VPN怎么配置")) is True
        assert a._is_document_query("查一下员工手册", query_embedding=a._emb_client.embed("查一下员工手册")) is True
        assert a._is_document_query("服务器上架流程是什么", query_embedding=a._emb_client.embed("服务器上架流程是什么")) is True
        assert a._is_document_query("搜索一下考勤制度", query_embedding=a._emb_client.embed("搜索一下考勤制度")) is True

    def test_long_question_with_question_mark_is_document_query(self):
        a = _make_agent()
        assert a._is_document_query("我们公司申请服务器的完整流程和要求是什么？") is True


class TestRagGatingBehavior:
    """集成：_build_user_message 是否把 kb 结果注入 system prompt。"""

    def _system_prompt_of(self, agent, content):
        # 复刻 _build_user_message 的 system prompt 构建逻辑（隔离 history）
        class _Msg:
            pass
        m = _Msg()
        m.content = content
        m.chat_type = "single"
        m.chat_name = ""
        m.sender_name = ""
        msgs = agent._build_user_message(m, history=[])
        return msgs[0]["content"]

    def _all_system_content(self, agent, content):
        """v5：拼接所有 system 消息内容（含 RAG 独立消息），供 RAG 内容断言用。"""
        class _Msg:
            pass
        m = _Msg()
        m.content = content
        m.chat_type = "single"
        m.chat_name = ""
        m.sender_name = ""
        msgs = agent._build_user_message(m, history=[])
        return "\n".join(m["content"] for m in msgs if m["role"] == "system")

    def test_weather_does_not_inject_kb(self):
        """问天气（无文档意图）不应注入 kb 知识，即使 kb 命中弱相关内容。

        注意：system prompt 里本身含『【相关知识』字样（RAG 使用规则，
        形如「若出现【相关知识】区块」），与真正注入的知识块是两回事。
        真正注入的知识块格式为「【相关知识】\\n1. <来源>…」，即标记后紧跟换行+编号；
        护栏规则文本中【相关知识】后接的是「区块」而非换行，故用「【相关知识】\\n」
        精确判定注入区块是否出现，避免把护栏文本误判为注入。
        """
        a = _make_agent(rag_intent_only=True)
        sp = self._system_prompt_of(a, "某城市天气怎么样")
        assert "【相关知识】\n" not in sp
        # 且不应把打印机 IP 表塞进 prompt
        assert "10.0.2.3" not in sp

    def test_document_query_injects_kb(self):
        """文档类 query 应注入 kb（验证门控未误杀正常检索）。"""
        a = _make_agent(rag_intent_only=True)
        # v5：RAG 可能在独立消息中，检查全部 system 消息
        sp = self._all_system_content(a, "VPN怎么配置")
        assert "【相关知识】\n" in sp

    def test_auto_inject_disabled_never_injects(self):
        """rag_auto_inject=false 彻底关闭自动注入，任何 query 都不注入。"""
        a = _make_agent(rag_auto_inject=False, rag_intent_only=False)
        sp = self._system_prompt_of(a, "VPN怎么配置")
        # 自动注入关闭时，绝不应出现「【相关知识】\n」注入区块（护栏文本不含该模式）。
        assert "【相关知识】\n" not in sp

    def test_recall_layer_tightens_threshold_and_count(self):
        """召回层：自动注入应把门槛收紧到配置值（0.6 / 1 条），而非旧值 0.3 / 3 条。"""
        router = _FakeToolRouter()
        a = _make_agent(rag_intent_only=False, rag_min_similarity=0.6, rag_max_results=1)
        a.tool_router = router
        # 直接用文档类 query 触发（绕过意图门控）
        a._retrieve_relevant_knowledge("VPN怎么配置")
        assert len(router.calls) == 1
        call = router.calls[0]
        assert call["min_similarity"] == 0.6
        assert call["top_k"] == 1

    def test_only_top1_result_injected_when_rag_max_results_is_1(self):
        a = _make_agent(rag_intent_only=False, rag_max_results=1, rag_min_similarity=0.5)
        a.tool_router = _TwoResultRouter()
        sp = self._all_system_content(a, "VPN怎么配置")
        assert "【相关知识】\n" in sp
        assert "1. VPN手册" in sp          # 编号 1 = 相似度最高
        assert "补充文档" not in sp        # 第 2 条不得注入
        block = sp.split("【相关知识】\n", 1)[1]
        assert "2." not in block           # 区块内无编号 2


class _LongContentRouter:
    """模拟命中一个长分块，关键 IP 信息落在前 200 字之后（复现打印机 IP 被截断丢失的 bug）。"""
    def __init__(self):
        self._tools = {"kb_search": self}
        self.calls = []
        prefix = "第一章 安装配置说明\n1.1 开箱检查与硬件连接规范详见手册。\n" * 12
        self._content = prefix + "\n【七楼打印机网络参数】IP地址：192.168.10.50，网关：192.168.10.1。"

    def search(self, query="", top_k=3, min_similarity=0.3, **kw):
        self.calls.append({"query": query, "top_k": top_k, "min_similarity": min_similarity})
        return {
            "success": True,
            "query": query,
            "results": [
                {"content": self._content, "source": "打印机配置文档", "score": 0.82},
            ],
        }


class TestRagContentNotTruncated(TestRagGatingBehavior):
    """回归：注入到 system prompt 的知识必须是【完整分块】，不能硬截断到 200 字。

    复现场景：打印机 IP 在分块后段（>200 字），旧实现 content[:200] 会切掉 IP，
    导致 LLM 看不到答案而回复『未找到』。
    """

    def test_ip_after_200_chars_survives_injection(self):
        a = _make_agent(rag_intent_only=False)  # 关闭意图门控，确保注入
        a.tool_router = _LongContentRouter()
        sp = self._all_system_content(a, "我要找打印机ip")
        # 关键 IP 必须在注入内容中可见
        assert "192.168.10.50" in sp, "打印机 IP 被 200 字截断丢失，LLM 将无法据此回答"
        # 完整内容（非截断标记）应出现
        assert "【七楼打印机网络参数】" in sp

    def test_no_truncation_ellipsis_on_short_chunk(self):
        a = _make_agent(rag_intent_only=False)
        a.tool_router = _LongContentRouter()
        sp = self._all_system_content(a, "我要找打印机ip")
        # 分块 < 1200 字时不应出现截断省略号
        assert "…" not in sp.split("【七楼打印机网络参数】")[0][:50] or "192.168.10.50" in sp


class _TruncRouter:
    """单 result：content 含 (a) 命中关键词且 >800 字段落；(b) 无关键词但含 IP 的段落。
    用于验证 H8 的截断上限生效，以及无匹配时整块返回的旧分支完好（IP 不丢）。"""
    def __init__(self):
        self._tools = {"kb_search": self}
        self.calls = []
        # (a) 命中关键词"配置"且明显 > 800 字
        matched = "配置项详细说明与操作规范：" + "X" * 900
        # (b) 无关键词匹配的段落，含 IP 等关键信息（验证整块返回不丢）
        unmatched = "内网打印机IP地址：192.168.10.50，网关：192.168.10.1，用于联网打印。"
        self._content = matched + "\n" + unmatched

    def search(self, query="", top_k=3, min_similarity=0.3, **kw):
        self.calls.append({"query": query, "top_k": top_k, "min_similarity": min_similarity})
        return {
            "success": True,
            "query": query,
            "results": [{"content": self._content, "source": "测试文档", "score": 0.82}],
        }


class TestRagContentCharsCap:
    """H8 回归：rag_max_content_chars 作为片段截断上限真实生效；且旧的无匹配整块返回
    分支（防止 IP 等长内容被丢）仍完好。"""

    def test_long_matched_paragraph_truncated_at_cap(self):
        a = _make_agent(rag_intent_only=False, rag_max_content_chars=800)
        a.tool_router = _TruncRouter()
        # 触发注入（query 含"配置"命中段落 A）
        knowledge, _ = a._retrieve_relevant_knowledge("VPN 配置")
        # 提取注入片段（"  - " 前缀之后）
        snippets = [ln[4:] for ln in knowledge.split("\n") if ln.startswith("  - ")]
        assert snippets, "应至少有一个注入片段"
        truncated = [s for s in snippets if s.endswith("…")]
        assert truncated, "超长命中段落应被截断并以 … 结尾"
        for s in truncated:
            # 截断上限：正文（去掉省略号）<= 800，且确实发生了截断（原段落 > 800）
            assert len(s) - 1 <= 800, f"截断正文不应超过 800 字，实际 {len(s) - 1}"
            assert len(s) > 800, "原段落 > 800 字，应确实发生截断"

        # 无匹配段落（含 IP）在"全返回"分支中返回完整分块，不丢
        full = a._extract_relevant_snippets(a.tool_router._content, "qwertyuiop asdfghjkl")
        assert len(full) == 1
        assert full[0] == a.tool_router._content, "无匹配时应整块返回，不截断"
        assert "192.168.10.50" in full[0], "IP 等长内容不应在无匹配分支丢失"


class _MultilineTruncRouter:
    """多行长文档（换行密集分布在截断点之前），验证 _use_raw 路径按行边界截断。

    2026-10-19 周哲事故：content[:max]+"…" 按字符硬切把「常见问题排查」
    拦腰截断，模型把「解决方法：确认账号格式是 `r」半截步骤原样照抄。
    """
    def __init__(self):
        self._tools = {"kb_search": self}
        self.calls = []
        self._content = "\n".join(
            f"步骤{i}：映射网络驱动器并输入账号 acme\\pinyin 完成验证登录。" for i in range(60)
        )

    def search(self, query="", top_k=3, min_similarity=0.3, **kw):
        self.calls.append({"query": query, "top_k": top_k, "min_similarity": min_similarity})
        return {
            "success": True,
            "query": query,
            "results": [{"content": self._content, "source": "测试文档", "score": 0.9}],
        }


class TestBoundaryAwareTruncation:
    """截断绝不从半句话/半条步骤中间切：超长回退最近换行边界，尾部「…」为截断标记。"""

    def test_multiline_cut_lands_on_line_boundary(self):
        from src.llm.rag import _truncate_at_boundary
        content = "\n".join(f"解决方法第{i}步：确认账号格式是 acme\\pinyin。" for i in range(60))
        out = _truncate_at_boundary(content, 800)
        assert out.endswith("…")
        body = out[:-1]
        assert len(body) <= 800
        # 切点必须是完整行结尾：body 之后紧跟换行（或已到末尾）
        rest = content[len(body):]
        assert rest.startswith("\n") or not rest.strip(), "截断点不得落在半句话中间"

    def test_no_newline_falls_back_to_hard_cut(self):
        from src.llm.rag import _truncate_at_boundary
        out = _truncate_at_boundary("X" * 900, 800)
        assert out.endswith("…") and len(out) <= 801

    def test_short_content_untouched(self):
        from src.llm.rag import _truncate_at_boundary
        assert _truncate_at_boundary("短内容", 800) == "短内容"

    def test_raw_path_multiline_cut_at_line_boundary(self):
        a = _make_agent(rag_intent_only=False, rag_max_content_chars=800)
        a.tool_router = _MultilineTruncRouter()
        knowledge, _ = a._retrieve_relevant_knowledge("VPN 配置")
        raw = knowledge.split("\n  - ", 1)[1]
        assert raw.endswith("…"), "超限文档应带截断标记"
        body = raw[:-1]
        assert a.tool_router._content.startswith(body)
        assert a.tool_router._content[len(body):].startswith("\n"), \
            "_use_raw 路径截断点必须落在行边界，不得出现半截步骤"


class TestRagPromptConstraint:
    """生成层：system prompt 应包含『禁止复述无关相关知识』硬约束。

    per-turn H4：RAG 专属护栏块（禁止复述相关知识）不再在 _build_system_prompt 里
    常驻，而是随 『【相关知识】』区块注入时一并下发——仅当本轮真正注入 RAG 才出现。
    """

    def _system_prompt_of(self, agent, content):
        # 复刻 _build_user_message 的 system prompt 构建逻辑（隔离 history）
        class _Msg:
            pass
        m = _Msg()
        m.content = content
        m.chat_type = "single"
        m.chat_name = ""
        m.sender_name = ""
        msgs = agent._build_user_message(m, history=[])
        return msgs[0]["content"]

    def test_system_prompt_has_rag_no_repeat_rule(self):
        a = _make_agent(rag_intent_only=False)  # 关意图门控，确保注入
        a.tool_router = _FakeToolRouter()
        a._max_input_tokens = 99999  # 避免 token 截断干扰断言
        msgs = self._msgs_of(a, "VPN怎么配置")  # 走 _build_user_message 触发注入
        # v5：RAG 块从主 system prompt 抽出为独立消息（近因位），检查独立消息
        # 用完整前缀匹配避免与 system_prompt 中的例外声明文本混淆
        rag_msgs = [m for m in msgs if m["role"] == "system" and (
                    m.get("content", "").startswith("\n【★RAG 知识库答案")
                    or m.get("content", "").startswith("【★RAG 知识库答案"))]
        assert len(rag_msgs) == 1, "v5: RAG 应为独立 system 消息"
        rag_content = rag_msgs[0]["content"]
        assert "必须直接基于以下内容回答用户问题" in rag_content  # v5 新措辞
        # 治「只有标题没有步骤」忽略病：具体细节必须给出（2026-10-19 改措辞后保留）
        assert "具体步骤/地址/IP/配置/账号密码等细节必须给出" in rag_content
        # 反复述：只摘当前问题必需片段，禁整篇复述（回归 2026-10-19 事故）
        assert "严禁把整篇文档从头到尾复述一遍" in rag_content

    def _msgs_of(self, agent, content):
        """完整消息列表（含 RAG 独立消息），供 v5 断言用。"""
        class _Msg:
            pass
        m = _Msg()
        m.content = content
        m.chat_type = "single"
        m.chat_name = ""
        m.sender_name = ""
        return agent._build_user_message(m, history=[])

    def test_rag_rule_block_absent_on_non_rag_turn(self):
        """per-turn H4：不注入 RAG 的轮次（闲聊/天气）不应下发 RAG 专属护栏块，省 token。"""
        a = _make_agent(rag_intent_only=True)
        sp = self._system_prompt_of(a, "某城市天气怎么样")
        assert "【★RAG 知识库答案" not in sp, (
            "非 RAG 轮次仍下发了 RAG 护栏块，浪费 token"
        )
        # 通用质量护栏不受影响，必须仍在
        assert "【禁止补答历史问题】" in sp

    def test_rag_rule_block_absent_when_auto_inject_off(self):
        """H4：rag_auto_inject=false 时，RAG 专属护栏块（禁止复述相关知识）应从
        system prompt 移除；但通用质量护栏（禁止补答历史问题）仍无条件常驻。"""
        a = _make_agent(rag_auto_inject=False)
        sp = a._build_system_prompt()
        assert "【★RAG 知识库答案" not in sp, (
            "rag_auto_inject 关闭后仍下发了 RAG 专属护栏块，浪费 token"
        )
        # 通用质量护栏不受影响，必须仍在
        assert "【禁止补答历史问题】" in sp


class TestNoRewritingHistory:
    """生成层：system prompt 应包含『禁止补答历史问题』硬约束（修复 bot 12:01:26 把
    历史里'力拔山兮气盖世'未答好的问题主动重答 + 当前'查某城市天气'打包返回的 bug）。"""

    def test_system_prompt_has_no_rewriting_history_rule(self):
        a = _make_agent()
        sp = a._build_system_prompt()
        # 必须包含"禁止补答历史"标题
        assert "禁止补答历史问题" in sp
        # 必须明确"只回答当前这一条"
        assert "只回答用户当前这一条提问" in sp
        # 必须明确不要主动重答历史未答好的问题
        assert "不要主动重答" in sp or "禁止重答" in sp or "不得重新" in sp


class TestHistoryWindowConfig:
    """配置层：poller.history_window 上限（避免长跨度的旧问题被 LLM 复答）。

    H2-A 决策：dingtalk history_window 由 5 放宽到 6（= max_recent + 2），
    以激活后台异步摘要；feishu/wecom 维持 6。故统一断言所有平台 <= 6。
    """

    def test_history_window_le_6(self):
        from src.config import load_config
        cfg = load_config("config.yaml")
        assert cfg.poller.history_window <= 12, (
            f"history_window={cfg.poller.history_window} 太大，会把跨多轮的旧问题塞进 LLM context；"
            "已放宽至 12 以提升还原度（更多历史消息供风格学习）。"
        )

    def test_feishu_wecom_history_window_le_6(self):
        """H2-B + H2-A：所有『已配置』平台的 history_window 均应 <= 6。

        dingtalk 由 5 放宽到 6 以激活 H2-A 后台异步摘要（= max_recent + 2）。
        config.yaml 为 gitignored，各平台配置随部署不同（CI 默认仅 dingtalk，
        feishu/wecom 被注释）；故仅校验『已配置』的平台，未配置平台跳过，避免误报。
        本地完整配置（feishu/wecom 均启用）时，对应平台也会被一并校验。"""
        from src.config import load_config
        cfg = load_config("config.yaml")

        checked = []
        for p in cfg.platforms:
            hw = p.poller.history_window
            assert hw <= 12, (
                f"{p.id} history_window={hw} 太大，会把跨多轮旧问题塞进 LLM context；"
                "已放宽至 12 以提升还原度（更多历史消息供风格学习）。"
            )
            checked.append(p.id)

        # 默认平台 dingtalk 必然配置，作为最小断言锚点
        assert "dingtalk" in checked, "默认平台 dingtalk 未配置，历史窗口校验失去锚点"
