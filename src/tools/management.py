from __future__ import annotations

import logging
import psutil
import time

from src.config import load_config
from src.paths import get_config_path
from src.dws_adapter import DwsAdapter
from src.tools.base import BaseTool
from src.memory.sqlite_store import SQLiteStore
from src.memory.platform_context import get_current_platform

logger = logging.getLogger(__name__)


class SystemStatusTool(BaseTool):
    name = "system_status"
    display_name = "检查系统状态"
    short_description = "查询系统运行健康状态，包括 DWS 登录、数据库、工具可用性、CPU、内存"
    description = (
        "检查系统运行状态，包括 DWS 登录状态、数据库连接、工具可用性、"
        "内存使用、CPU 占用等。当用户问'系统怎么样'、'运行正常吗'、'检查一下'等时使用。"
    )
    # 场景关键词统一维护在 IntentRegistry 的 domain.system_status（单一真源）
    intent_categories = ["domain.system_status"]
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, dws: DwsAdapter, store: SQLiteStore, config=None):
        self.dws = dws
        self.store = store
        self.config = config

    def execute(self, args: dict) -> str | dict:
        try:
            # 会话相关表（messages/conversations）已按账号隔离，走 per-account 会话库；
            # keyword_rules/kb_documents 为平台无关表，走主库。
            conv_cur = self.store.conv_conn(get_current_platform()).cursor()
            conv_cur.execute("SELECT COUNT(*) as cnt FROM messages")
            msg_count = conv_cur.fetchone()["cnt"]
            conv_cur.execute("SELECT COUNT(*) as cnt FROM conversations")
            conv_count = conv_cur.fetchone()["cnt"]
            cur = self.store.conn.cursor()
            cur.execute("SELECT COUNT(*) as cnt FROM keyword_rules")
            kw_count = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) as cnt FROM kb_documents")
            kb_count = cur.fetchone()["cnt"]
            db_status = "正常"
        except Exception as e:
            logger.exception("获取数据库状态失败: %s", e)
            db_status = f"异常: {e}"
            msg_count = conv_count = kw_count = kb_count = 0

        # 仅读取本地 DWS profile 获取当前账号信息（零网络调用，不触发登录检测/弹窗）
        try:
            profile = self.dws._get_current_profile_local()
            if profile:
                user_name = profile.get("userName", "")
                corp_name = profile.get("corpName", "")
                expires_at = profile.get("expiresAt") or profile.get("expires_at", "")
                auth_status = "已配置" if profile.get("status") == "active" else "未配置"
            else:
                user_name = corp_name = expires_at = ""
                auth_status = "未配置"
        except Exception as e:
            logger.debug("get profile failed: %s", e)
            user_name = corp_name = expires_at = ""
            auth_status = "未知"

        try:
            mem = psutil.virtual_memory()
            cpu = psutil.cpu_percent(interval=0.1)
        except Exception as e:
            logger.debug("psutil failed: %s", e)
            mem = None
            cpu = 0

        # 优先用注入的运行时 config；缺失时回退读取 config.yaml（可能不存在，须容错）
        cfg = self.config
        if cfg is None:
            try:
                cfg = load_config(str(get_config_path()))
            except Exception as e:
                logger.debug("load_config failed: %s", e)
                cfg = None

        if cfg is not None:
            tools_enabled = cfg.tools.enabled
            rules_enabled = cfg.rules.enabled
            embedding_enabled = cfg.embedding.enabled
            poll_interval = cfg.poller.interval_seconds
            llm_model = cfg.llm.model
            embedding_model = cfg.embedding.model
            tools_count = len(cfg.tools.available)
        else:
            tools_enabled = rules_enabled = embedding_enabled = "N/A"
            poll_interval = "N/A"
            llm_model = embedding_model = "N/A"
            tools_count = "N/A"

        result = {
            "status": {
                "dws_auth": auth_status,
                "database": db_status,
                "tools_enabled": tools_enabled,
                "rules_enabled": rules_enabled,
                "embedding_enabled": embedding_enabled,
            },
            "dws": {
                "user_name": user_name,
                "corp_name": corp_name,
                "expires_at": expires_at,
            },
            "storage": {
                "messages": msg_count,
                "conversations": conv_count,
                "keyword_rules": kw_count,
                "kb_documents": kb_count,
            },
            "config": {
                "poll_interval": f"{poll_interval}s" if poll_interval != "N/A" else "N/A",
                "llm_model": llm_model,
                "embedding_model": embedding_model,
                "tools_count": tools_count,
            },
            "system": {
                "cpu": f"{cpu}%",
                "memory": f"{mem.percent}%" if mem else "N/A",
                "memory_total": f"{mem.total / 1024 / 1024 / 1024:.1f} GB" if mem else "N/A",
                "memory_available": f"{mem.available / 1024 / 1024 / 1024:.1f} GB" if mem else "N/A",
            },
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        return result


class MessageStatsTool(BaseTool):
    name = "message_stats"
    display_name = "消息统计"
    short_description = "查询近期消息统计数据，包括处理量、趋势、高频发送者与 AI 回复占比"
    description = (
        "查询消息统计数据，包括消息趋势、类型分布、高频发送者、"
        "AI回复数等。当用户问'今天处理了多少消息'、'消息统计'、'活跃用户'等时使用。"
    )
    # 场景关键词统一维护在 IntentRegistry 的 domain.message_stats（单一真源）
    intent_categories = ["domain.message_stats"]
    parameters = {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "统计最近几天的数据，默认7天",
                "default": 7,
            },
        },
        "required": [],
    }

    def __init__(self, store: SQLiteStore):
        self.store = store

    def execute(self, args: dict) -> str | dict:
        from src.tools.utils import safe_int
        days = max(1, min(safe_int(args.get("days", 7), 7), 30))

        try:
            # 消息统计仅涉及会话表 messages（已按账号隔离），走 per-account 会话库
            cur = self.store.conv_conn(get_current_platform()).cursor()

            cur.execute(
                "SELECT DATE(timestamp) as day, COUNT(*) as cnt "
                "FROM messages WHERE timestamp >= date('now', '-{} days') "
                "GROUP BY day ORDER BY day".format(days)
            )
            trend = [dict(row) for row in cur.fetchall()]

            cur.execute("""
                SELECT
                    CASE
                        WHEN msg_type = 'system' THEN '系统消息'
                        WHEN chat_type = 'single' THEN '私信'
                        ELSE '群消息'
                    END as msg_type,
                    COUNT(*) as cnt
                FROM messages
                GROUP BY CASE
                        WHEN msg_type = 'system' THEN '系统消息'
                        WHEN chat_type = 'single' THEN '私信'
                        ELSE '群消息'
                    END
                ORDER BY cnt DESC
            """)
            msg_types = [dict(row) for row in cur.fetchall()]

            cur.execute("""
                SELECT sender_name, COUNT(*) as cnt
                FROM messages
                WHERE role = 'user' OR role = ''
                GROUP BY sender_name
                ORDER BY cnt DESC
                LIMIT 10
            """)
            top_senders = [dict(row) for row in cur.fetchall()]

            cur.execute("SELECT COUNT(*) as cnt FROM messages WHERE role = 'assistant'")
            ai_replies = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(*) as cnt FROM messages")
            total_messages = cur.fetchone()["cnt"]

            return {
                "trend": trend,
                "msg_types": msg_types,
                "top_senders": top_senders,
                "ai_replies": ai_replies,
                "total_messages": total_messages,
                "days": days,
            }

        except Exception as e:
            logger.error("消息统计查询失败: %s", e)
            return {"error": f"查询失败: {e}"}


class KeywordRulesTool(BaseTool):
    name = "keyword_rules"
    display_name = "关键词规则管理"
    short_description = "查询、添加、启用或禁用关键词规则，命中后会优先于 AI 回复"
    description = (
        "管理关键词规则，支持查询、添加、启用/禁用规则。"
        "当用户说'添加关键词规则'、'禁用某个规则'、'查看规则列表'等时使用。"
    )
    # 场景关键词统一维护在 IntentRegistry 的 domain.keyword_rules（单一真源）
    intent_categories = ["domain.keyword_rules"]
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "操作类型：'list'（查询列表）、'add'（添加规则）、'enable'（启用）、'disable'（禁用）",
                "enum": ["list", "add", "enable", "disable"],
                "default": "list",
            },
            "match_pattern": {
                "type": "string",
                "description": "匹配关键词，action为'add'时必填",
            },
            "reply_text": {
                "type": "string",
                "description": "回复内容，action为'add'时必填",
            },
            "category": {
                "type": "string",
                "description": "分类，默认'default'",
                "default": "default",
            },
            "rule_id": {
                "type": "integer",
                "description": "规则ID，action为'enable'或'disable'时必填",
            },
        },
        "required": ["action"],
    }

    def __init__(self, store: SQLiteStore):
        self.store = store

    def execute(self, args: dict) -> str | dict:
        action = args.get("action", "list")

        try:
            cur = self.store.conn.cursor()

            if action == "list":
                cur.execute(
                    "SELECT id, match_pattern, reply_text, category, enabled, hit_count, created_at "
                    "FROM keyword_rules ORDER BY category, priority DESC, hit_count DESC"
                )
                rules = [dict(row) for row in cur.fetchall()]
                return {"rules": rules, "count": len(rules)}

            elif action == "add":
                pattern = args.get("match_pattern", "").strip()
                reply = args.get("reply_text", "").strip()
                category = args.get("category", "default").strip()

                if not pattern or not reply:
                    return {"error": "match_pattern 和 reply_text 不能为空"}

                now = time.strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    "INSERT INTO keyword_rules (match_pattern, reply_text, category, enabled, hit_count, created_at, updated_at) "
                    "VALUES (?, ?, ?, 1, 0, ?, ?)",
                    (pattern, reply, category, now, now),
                )
                self.store.conn.commit()
                rule_id = cur.lastrowid
                return {"success": True, "rule_id": rule_id, "message": "规则添加成功"}

            elif action in ("enable", "disable"):
                rule_id = args.get("rule_id")
                if not rule_id:
                    return {"error": "rule_id 不能为空"}

                enabled = 1 if action == "enable" else 0
                cur.execute(
                    "UPDATE keyword_rules SET enabled = ?, updated_at = ? WHERE id = ?",
                    (enabled, time.strftime("%Y-%m-%d %H:%M:%S"), rule_id),
                )
                self.store.conn.commit()

                if cur.rowcount > 0:
                    return {"success": True, "message": "规则已{}".format("启用" if enabled else "禁用")}
                else:
                    return {"error": "未找到该规则"}

            else:
                return {"error": f"未知操作: {action}"}

        except Exception as e:
            logger.error("关键词规则操作失败: %s", e)
            return {"error": f"操作失败: {e}"}


class ConfigManageTool(BaseTool):
    name = "config_manage"
    display_name = "系统配置管理"
    short_description = "查看与修改系统配置（轮询、LLM、Embedding、功能开关），写入 config.yaml"
    description = (
        "查看和更新系统配置（写入 config.yaml 磁盘）。注意：运行中的进程不会自动热加载，"
        "修改后需重启服务才会生效。当用户说'查看配置'、'修改轮询间隔'、'开启工具'等时使用。"
    )
    # 场景关键词统一维护在 IntentRegistry 的 domain.config（单一真源）
    intent_categories = ["domain.config"]
    # 写配置落盘 config.yaml 属于高责任操作，需二次确认；但 'view' 只读直接放行
    # （通过覆写 needs_confirm 实现按参数条件确认，而非无条件拦截）。
    require_confirm = True

    def needs_confirm(self, args: dict) -> bool:
        return (args or {}).get("action") == "update"

    def build_confirmation_preview(self, args: dict) -> str:
        section = args.get("section")
        key = args.get("key")
        value = args.get("value")
        return (
            f"即将修改配置 {section}.{key} = {value!r}，并写入 config.yaml 磁盘文件"
            "（重启服务后新配置才会生效）。请确认此变更。"
        )

    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "操作类型：'view'（查看配置）、'update'（更新配置）",
                "enum": ["view", "update"],
                "default": "view",
            },
            "section": {
                "type": "string",
                "description": "配置分区：'dws'、'poller'、'llm'、'tools'、'embedding'",
            },
            "key": {
                "type": "string",
                "description": "配置项名称，如 'dry_run'、'interval_seconds'、'temperature'",
            },
            "value": {
                "type": "string",
                "description": "配置值",
            },
        },
        "required": ["action"],
    }

    def execute(self, args: dict) -> str | dict:
        action = args.get("action", "view")

        try:
            config = load_config(str(get_config_path()))

            if action == "view":
                section = args.get("section")
                if section:
                    section_data = {
                        "dws": {"dry_run": config.dws.dry_run, "retries": config.dws.retries, "timeout": config.dws.timeout},
                        "poller": {"interval_seconds": config.poller.interval_seconds, "merge_window_seconds": config.poller.merge_window_seconds},
                        "llm": {"model": config.llm.model, "temperature": config.llm.temperature, "max_tokens": config.llm.max_tokens, "base_url": config.llm.base_url},
                        "tools": {"enabled": config.tools.enabled, "available": config.tools.available},
                        "embedding": {"enabled": config.embedding.enabled, "model": config.embedding.model, "top_k": config.embedding.top_k},
                    }.get(section)
                    if section_data:
                        return {"section": section, "config": section_data}
                    else:
                        return {"error": f"未知分区: {section}"}
                else:
                    return {
                        "dws": {"dry_run": config.dws.dry_run, "retries": config.dws.retries, "timeout": config.dws.timeout},
                        "poller": {"interval_seconds": config.poller.interval_seconds, "merge_window_seconds": config.poller.merge_window_seconds},
                        "llm": {"model": config.llm.model, "temperature": config.llm.temperature, "max_tokens": config.llm.max_tokens, "base_url": config.llm.base_url},
                        "tools": {"enabled": config.tools.enabled, "available": config.tools.available},
                        "embedding": {"enabled": config.embedding.enabled, "model": config.embedding.model, "top_k": config.embedding.top_k},
                    }

            elif action == "update":
                section = args.get("section")
                key = args.get("key")
                value = args.get("value")

                if not section or not key or value is None:
                    return {"error": "section、key、value 不能为空"}

                import yaml

                config_path = get_config_path()
                with open(config_path, "r", encoding="utf-8") as f:
                    config_dict = yaml.safe_load(f)

                sections = {
                    "dws": config_dict.setdefault("dws", {}),
                    "llm": config_dict.setdefault("llm", {}),
                    "tools": config_dict.setdefault("tools", {}),
                    "embedding": config_dict.setdefault("embedding", {}),
                }
                # poller 配置已迁移到各平台块（按平台隔离）。config_manage 工具只更新
                # dingtalk 主平台 poller，落在 platforms[0].poller（即 dingtalk 平台块）。
                _plats = config_dict.setdefault("platforms", [])
                _dt = next((p for p in _plats if p.get("id") == "dingtalk"), None)
                if _dt is None:
                    # 极旧 legacy config（无 platforms 段）：退回 root poller 兼容
                    sections["poller"] = config_dict.setdefault("poller", {})
                else:
                    sections["poller"] = _dt.setdefault("poller", {})

                if section not in sections:
                    return {"error": f"未知分区: {section}"}

                if key not in sections[section]:
                    return {"error": f"未知配置项: {key}"}

                old_value = sections[section][key]

                # LLM 可能传非字符串（如 JSON bool/int），先归一化为字符串再解析，
                # 否则 value.lower() / int(value) 遇到非 str 会抛异常，用户收到无法行动的报错。
                sval = value if isinstance(value, str) else ("" if value is None else str(value))
                try:
                    if isinstance(old_value, bool):
                        sections[section][key] = sval.strip().lower() in ("true", "1", "yes", "on")
                    elif isinstance(old_value, int):
                        sections[section][key] = int(sval)
                    elif isinstance(old_value, float):
                        sections[section][key] = float(sval)
                    else:
                        sections[section][key] = value
                except (ValueError, TypeError):
                    return {"error": (
                        f"配置项 {section}.{key} 需要 {type(old_value).__name__} 类型，"
                        f"收到无法解析的值: {value!r}"
                    )}

                # 原子写：先写临时文件再 os.replace 整体替换，避免写盘中途崩溃导致
                # YAML 被截断、进程再也起不来；也避免并发两次 update 互相覆盖成半截文件。
                import os
                import tempfile

                dir_name = os.path.dirname(os.path.abspath(config_path)) or "."
                fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".config.", suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        yaml.dump(config_dict, f, default_flow_style=False, allow_unicode=True)
                    os.replace(tmp_path, config_path)  # 原子：要么完整新文件，要么旧文件不变
                except Exception:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    raise

                return {
                    "success": True,
                    "section": section,
                    "key": key,
                    "old_value": old_value,
                    "new_value": sections[section][key],
                    "message": "配置已写入 config.yaml（磁盘）。注意：运行中的进程不会自动热加载，需重启服务后新配置才会被加载生效。",
                }

            else:
                return {"error": f"未知操作: {action}"}

        except Exception as e:
            logger.error("配置管理操作失败: %s", e)
            return {"error": f"操作失败: {e}"}
