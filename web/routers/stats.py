"""消息统计 / 工具调用统计 路由。

从 `web/api.py` 抽取（原 1993–2335 行），业务逻辑不变。
- get_store / load_config / CONFIG_PATH / get_app_instance / _get_cached_stats /
  _put_cached_stats 均经 `import web.api as _api` 做属性访问，
  以尊重测试对 `web.api.*` 的 monkeypatch（TestStatsMessages / TestToolStats）。
- jieba / re 为模块级依赖，本地导入。
"""

from __future__ import annotations

import re

import jieba
from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

import web.api as _api
from web.dependencies import get_current_platform, logger

router = APIRouter()


@router.get("/api/stats/messages")
async def message_stats(days: int = 7):
    # TTL 缓存：避免每次请求都全表聚合 + jieba 分词（仪表盘每 30s 刷新一次）
    cached = _api._get_cached_stats(days, get_current_platform())
    if cached is not None:
        return cached
    try:
        def _work():
            store = _api.get_store()
            platform = get_current_platform()
            msg_repo = store._message_repo

            # 消息趋势（按天统计）
            trend = msg_repo.get_daily_message_trend(days, platform=platform)

            # 消息类型分布（4 类均基于实际数据设计，避免空类别）
            # 分类优先级：系统发送者 > AI摘要 > 群聊 > 私信（兜底）
            _SYSTEM_SENDER_KEYWORDS = [
                "OA审批", "智能人事", "钉钉人事", "智能招聘", "智能会议室", "智能云打印",
                "考勤打卡", "钉钉客服", "钉钉小秘书", "钉钉管理助手", "钉钉AI表格", "钉钉-云瑞", "钉钉365会员",
                "服务小钉", "小钉", "公告", "员工服务助手", "文件小助手", "日志", "固定资产管理",
                "有成报销", "访客预约系统", "群晖告警", "七牛CDN", "MySQL连接器", "Jenkins",
                "AI助理", "AI小钉", "项目小助手", "委外通知", "OnlineDocu", "官网服务器告警",
                "呆滞物料预警", "易快报", "魔点门禁", "易盘点",
            ]
            def _is_system_sender(name):
                if not name:
                    return False
                for kw in _SYSTEM_SENDER_KEYWORDS:
                    if kw in name:
                        return True
                return False

            rows = msg_repo.get_message_type_breakdown(platform=platform)
            # 新分类：私信 / 群消息 / 系统通知 / AI摘要（均对应真实数据）
            all_categories = ["私信", "群消息", "系统通知", "AI摘要"]
            msg_types_map = {cat: 0 for cat in all_categories}
            for row in rows:
                sender_name = row["sender_name"]
                msg_type = row["msg_type"]
                chat_type = row["chat_type"]
                cnt = row["cnt"]
                # 1) AI 对话摘要（msg_type='system' 是后台生成的对话摘要）
                if msg_type == "system":
                    msg_types_map["AI摘要"] += cnt
                # 2) 系统/业务通知（OA、考勤、人事等系统发送者）
                elif _is_system_sender(sender_name):
                    msg_types_map["系统通知"] += cnt
                # 3) 群聊消息
                elif chat_type != "single":
                    msg_types_map["群消息"] += cnt
                # 4) 私信（兜底：真人一对一）
                else:
                    msg_types_map["私信"] += cnt

            msg_types = [
                {"msg_type": cat, "cnt": msg_types_map[cat]}
                for cat in all_categories
            ]

            # 高频发送者
            top_senders = msg_repo.get_top_senders(limit=10, platform=platform)

            # 高频关键词（简单分词统计）
            raw_contents = msg_repo.get_recent_user_contents(limit=500, platform=platform)

            # === 过滤非自然语言内容 ===
            def is_natural_language(text: str) -> bool:
                """判断是否为自然语言消息（过滤 JSON/结构化/系统消息）"""
                text = text.strip()
                if not text:
                    return False
                # 过滤 JSON 结构化消息（钉钉富文本、卡片等）
                if text.startswith(('[', '{')):
                    return False
                # 过滤系统/模板消息
                if text.startswith('>') or text.startswith('* 仅你和'):
                    return False
                # 过滤纯 URL
                if text.startswith('http') and ' ' not in text:
                    return False
                # 过滤纯表情/emoji
                if len(text) <= 4 and not any('\u4e00' <= c <= '\u9fff' for c in text):
                    return False
                # 过滤重复模板（天气、错误提示等）
                noise_patterns = [
                    '抱歉，我暂时无法回答',
                    '请稍后再试',
                    '来源：wttr.in',
                    'minSupportVersion',
                    'translateMap',
                    'fallbackKey',
                ]
                if any(p in text for p in noise_patterns):
                    return False
                return True

            contents = [c for c in raw_contents if is_natural_language(c)]

            # === 停用词表（大幅扩充） ===
            stop_words = {
                # 基础寒暄
                "你好", "您好", "谢谢", "请问", "这个", "那个", "什么", "怎么", "可以", "没有", "知道", "现在", "今天", "明天",
                "一下", "我们", "他们", "大家", "就是", "但是", "因为", "所以", "如果", "的话", "需要", "能够", "已经",
                "非常", "比较", "还是", "或者", "以及", "进行", "使用", "通过", "对于", "关于", "是否", "能否", "麻烦",
                "帮忙", "收到", "好的", "了解", "明白", "清楚", "请", "谢谢", "你好", "您好", "嗯", "啊", "哦",
                # 代词/助词/虚词
                "自己", "别人", "我", "你", "他", "她", "它", "的", "了", "在", "是", "有", "和", "与", "及", "等",
                "也", "都", "就", "又", "还", "被", "把", "给", "让", "向", "从", "到", "去",
                "吗", "呢", "吧", "呀", "哇", "嘛", "么", "而", "则", "且", "或",
                # 常见无意义词
                "问题", "信息", "一切", "公司", "安装", "允许", "禁止", "购买", "正版软件", "盗版软件",
                "系统", "用户", "文件", "数据", "内容", "功能", "服务", "应用", "程序", "代码",
                "时间", "地方", "方式", "情况", "结果", "原因", "目的", "方法", "步骤",
                # 技术噪音词（来自 JSON/HTML 残留）
                "text", "type", "data", "style", "bold", "size", "darkcolor", "fallbackkey",
                "version", "items", "translate", "map", "true", "false", "null", "none",
                "dingtalk", "https", "http", "www", "com", "org",
                # 技术字段名
                "progress", "success", "failed", "error", "status", "code", "msg", "result",
                "id", "name", "value", "key", "desc", "title", "summary", "detail",
                "config", "setting", "option", "param", "args", "request", "response",
                "api", "json", "html", "xml", "url", "path", "query", "cookie",
                "token", "session", "user", "admin", "login", "logout", "auth",
                "page", "view", "list", "detail", "create", "update", "delete", "add",
                "edit", "save", "cancel", "confirm", "submit", "reset", "clear",
                "download", "upload", "import", "export", "sync", "refresh",
                "enable", "disable", "active", "inactive", "online", "offline",
                "function", "callback", "async", "await", "return", "throw", "catch",
                "try", "finally", "class", "extends", "constructor", "new", "this",
                "let", "const", "var", "if", "else", "for", "while", "switch", "case",
                "break", "continue", "module", "export", "import", "require", "from", "as",
                "static", "private", "public", "protected", "abstract", "virtual",
                "override", "final", "native", "encode", "decode", "parse", "stringify",
                "format", "trim", "split", "join", "slice", "splice", "push", "pop",
                "shift", "unshift", "sort", "reverse", "filter", "find", "includes",
                "indexof", "lastindexof", "concat", "reduce", "every", "some", "foreach",
                "keys", "values", "entries", "assign", "freeze", "seal", "create",
                "defineproperty", "getownpropertynames", "getprototypeof", "setprototypeof",
                "is", "hasownproperty", "isextensible", "isfrozen", "issealed", "isarray",
                "exec", "test", "search", "replace", "tolowercase", "touppercase",
                "padstart", "padend", "normalize", "repeat", "localecompare",
                "copywithin", "fill", "findindex", "flat", "flatmap", "groupby",
                "defineproperties", "fromentries", "getownpropertydescriptor",
                "getownpropertydescriptors", "getownpropertysymbols", "preventextensions",
                "toentries", "tostring", "valueof", "apply", "construct", "deleteproperty",
                "ownkeys", "all", "allsettled", "any", "race", "reject", "resolve",
                "add", "clear", "get", "has", "size", "typeof", "instanceof", "void",
                "debugger", "eval", "arguments", "callee", "caller", "length", "name",
                "prototype", "__proto__", "constructor", "isprototypeof",
                "propertyisenumerable", "tolocalestring", "nan", "infinity", "undefined",
                # URL 编码残留词（如 3dfalse -> dfalse, 2fshowmenu -> fshowmenu）
                "dfalse", "fshowmenu", "fcorpid", "fback", "fnative", "faflow", "fdingtalk",
                "dshowmenu", "dcorpid", "dback", "dnative", "daflow", "ddingtalk",
                "corpid", "showmenu", "native", "aflow", "back",
            }

            def normalize_keyword_token(token: str) -> str:
                token = re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]+', '', token or '').strip().lower()
                return token

            # 内建技术/URL 残留子串（命中即视为机器生成噪音 token，配合 denylist 形成长效机制）
            _TECH_SUBSTRINGS = (
                'dingtalk', 'mobile', 'link', 'client', 'api', 'sdk', 'web', 'http', 'https',
                'token', 'session', 'oauth', 'callback', 'redirect', 'android', 'ios', 'app',
                'robot', 'im', 'chat', 'msg', 'message', 'send', 'recv', 'corp', 'agent',
                'dev', 'prod', 'test', 'debug', 'config', 'runtime', 'native',
                'bundle', 'min', 'umd', 'cdn', 'assets', 'static', 'upload', 'download',
                'bot', 'gpt', 'llm', 'open', 'talk', 'server', 'iframe', 'script',
                'style', 'class', 'json', 'xml', 'yaml', 'csv', 'img', 'src', 'href',
                'url', 'uri', 'uuid', 'guid', 'hash', 'md5', 'sha', 'base64',
                'encode', 'decode', 'miniapp', 'webbot', 'translate', 'fallback',
            )

            def is_valid_keyword_token(token: str, denylist: set[str] | None = None) -> bool:
                if not token:
                    return False
                if token in stop_words:
                    return False
                if denylist and token in denylist:
                    return False
                if len(token) < 2:
                    return False
                if token.isdigit():
                    return False
                # 过滤 URL 编码残留（如 3d=, 2f=/ 等十六进制序列）
                if re.match(r'^[0-9a-f]{3,}$', token, re.IGNORECASE):
                    return False
                # 过滤类似 3dfalse, 2fshowmenu 等 URL 路径片段（十六进制前缀 + 英文）
                if re.match(r'^[0-9a-f]{2}[a-zA-Z]+$', token, re.IGNORECASE):
                    return False
                # 过滤纯数字+字母混合但无中文的短词（可能是ID）
                if re.match(r'^[0-9a-zA-Z]{2,8}$', token) and not any('\u4e00' <= c <= '\u9fff' for c in token):
                    return False
                # ===== 纯 ASCII / 无中文 token 的强过滤（大多是机器生成噪音）=====
                has_cjk = any('\u4e00' <= c <= '\u9fff' for c in token)
                if not has_cjk:
                    # 短技术词（<8 字符）直接丢弃
                    if len(token) < 8:
                        return False
                    # 含长串十六进制（>=4 连续 hex），如 3dding9888ef577f7811cb 中的 9888ef577f7811cb
                    if re.search(r'[0-9a-f]{4,}', token, re.IGNORECASE):
                        return False
                    # 含内建技术/URL 残留子串（dingtalkclient / mobilelink 等拼接标识符）
                    low = token.lower()
                    if any(s in low for s in _TECH_SUBSTRINGS):
                        return False
                    # 长纯字母无数字无下划线的拼接标识符（>=12 字符），机器生成特征
                    if len(token) >= 12 and token.isalpha():
                        return False
                return True

            # 高频关键词黑名单（来自配置，免改代码即可扩展）
            try:
                _kw_cfg = _api._get_cfg()
                _keyword_denylist = set(
                    w.strip().lower() for w in (_kw_cfg.rules.keyword_denylist or [])
                )
            except Exception:
                _keyword_denylist = set()

            word_freq = {}
            for text in contents:
                cleaned = re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]+', ' ', text or '')
                for token in jieba.lcut(cleaned):
                    token = normalize_keyword_token(token)
                    if not is_valid_keyword_token(token, denylist=_keyword_denylist):
                        continue
                    word_freq[token] = word_freq.get(token, 0) + 1
            top_words = sorted(
                [{"word": k, "count": v} for k, v in word_freq.items()],
                key=lambda x: x["count"],
                reverse=True,
            )[:30]

            # 响应统计（assistant 消息数）
            ai_replies = msg_repo.count_messages_by_role("assistant", platform=platform)


            result = {
                "trend": trend,
                "msg_types": msg_types,
                "top_senders": top_senders,
                "top_words": top_words,
                "ai_replies": ai_replies,
            }
            _api._put_cached_stats(days, result, get_current_platform())
            return result
        return await run_in_threadpool(_work)
    except Exception as e:
        logger.error("获取消息统计失败: %s", e)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.get("/api/stats/tools")
async def tool_call_stats(days: int = 7, top_n: int = 12):
    """获取工具调用统计信息。数据来自 tool_execution_logs 表。

    Args:
        days: 统计周期（天）
        top_n: 返回的 TOP N 工具（按 total_calls 降序）。
    """
    try:
        def _work():
            config = _api._get_cfg()
            available_tools = config.tools.available

            # 从 ToolRouter 拿所有工具的中文别名 / 描述 (供前端展示)
            tool_info_map: dict[str, dict] = {}
            app_instance = _api.get_app_instance()
            if app_instance is not None and getattr(app_instance, "tool_router", None) is not None:
                for info in app_instance.tool_router.get_all_info():
                    tool_info_map[info["name"]] = info

            # 从 tool_execution_logs 表查询真实统计数据
            store = _api.get_store()
            db_rows = {r["tool_name"]: r for r in store.get_tool_call_stats(days)}

            # 合并：已配置的工具都有条目，无调用的显示 0
            tool_stats = []
            for tool_name in available_tools:
                rate_cfg = config.tools.rate_limit.get(tool_name, {})
                info = tool_info_map.get(tool_name, {})
                db_row = db_rows.get(tool_name, {})
                tool_stats.append({
                    "tool_name": tool_name,
                    "display_name": info.get("display_name") or tool_name,
                    "short_description": info.get("short_description") or "",
                    "total_calls": db_row.get("total_calls", 0),
                    "success_rate": db_row.get("success_rate", 0.0),
                    "avg_duration_ms": db_row.get("avg_duration_ms", 0.0),
                    "rate_limit_per_hour": rate_cfg.get("per_hour", 0),
                })

            # 按 total_calls 降序排，取 TOP N。top_n <= 0 返全量。
            tool_stats.sort(key=lambda t: t["total_calls"], reverse=True)
            if top_n > 0:
                tool_stats = tool_stats[:top_n]

            return {
                "tools": tool_stats,
                "period_days": days,
                "top_n": top_n if top_n > 0 else len(tool_stats),
            }
        return await run_in_threadpool(_work)
    except Exception as e:
        logger.error("工具统计API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
