#!/usr/bin/env python3
"""修复 routing_quality 表历史空数据。

背景：
- 旧版 agent.py 在 record_routing_quality 时不传 disposition / intent_action /
  message_type / stages_json 等字段，导致 38 条记录这些字段都是默认值。
- 数据库不能删，因此采用「保守回填 + 修复日志 + 状态标记」方案。

修复策略（保守诚实，绝不编造真实 LLM 指标）：
1. 回填 message_type：纯文本/含图片标记/合并消息，三种高置信度
2. 回填 intent_disposition + intent_action：基于 primary_skill 与内容长度
3. 回填 stages_json：构造 1 跳「message_in(reconstructed)」+ N 跳已知信息
4. 不回填：llm_model / llm_rounds / llm_latency_ms / total_latency_ms / reply_len
   （这些是真实的 LLM 推理指标，无法从历史数据反推；保留 0 是诚实标记）
5. 写入修复日志表 routing_quality_repair_log，可审计可回滚

运行：python scripts/repair_routing_quality_history.py [--db-path PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402

# 默认数据库路径从配置派生（主库 linkora.db），不再硬编码已弃用的 dingtalk-ai.db
try:
    DEFAULT_DB_PATH = load_config().db.path
except Exception:
    DEFAULT_DB_PATH = str(ROOT / "data" / "linkora.db")

# 短礼貌/确认类关键词（用于判断 social/skip）
POLITE_KEYWORDS = (
    "好的", "收到", "谢谢", "感谢", "ok", "OK", "好", "可以", "行", "嗯", "ok了",
    "好，谢谢", "收到，谢谢", "ok了。", "好,谢谢", "好的,谢谢", "ok",
)
# 图片/合并消息标记
# 注意：poller/main.py 在 content_preview 中使用的标记是中文方括号【】
# 而不是英文方括号[]，需要同时覆盖两种情况以保证历史数据推断正确
IMAGE_MARKERS = (
    "【图片内容】", "【图片识别中...】",     # poller/main.py 实际使用的中文标记
    "[图片内容]", "[图片识别中...]",         # 兼容英文写法
)
MERGED_MARKERS = ("[消息1]", "[消息2]", "【消息1】", "【消息2】")


def infer_message_type(content_preview: str) -> str:
    """从内容预览推断消息类型。"""
    if not content_preview:
        return "text"
    has_image = any(m in content_preview for m in IMAGE_MARKERS)
    has_merged = any(m in content_preview for m in MERGED_MARKERS)
    if has_image and has_merged:
        return "mixed"
    if has_image:
        return "image"
    if has_merged:
        return "merged"
    return "text"


def infer_intent(content_preview: str, primary_skill: str) -> tuple[str, str]:
    """推断 (intent_disposition, intent_action)。"""
    # 已有主技能 → 明确走过 LLM 路径
    if primary_skill:
        return ("business", "llm")
    # 短礼貌/确认类 → 走规则引擎跳过
    s = (content_preview or "").strip()
    if len(s) <= 10:
        for kw in POLITE_KEYWORDS:
            if s.startswith(kw) or s == kw:
                return ("social", "skip")
    # 默认走 LLM
    return ("business", "llm")


def build_reconstructed_stages(
    content_preview: str,
    message_type: str,
    primary_skill: str,
    primary_source: str,
    tools_exposed: list[str],
    intent_disposition: str,
    intent_action: str,
    routing_mode: str,
) -> list[dict]:
    """构造一条"已重建"的全链路瀑布，ms 全部 0.0，状态为 reconstructed。

    前端识别 status=reconstructed 时会显示特殊标签（"历史回填"）。
    """
    stages = [
        {
            "stage": "message_in",
            "ms": 0.0,
            "status": "reconstructed",
            "detail": {
                "type": message_type,
                "len": len(content_preview or ""),
                "sender_reconstructed": True,
            },
        },
        {
            "stage": "intent",
            "ms": 0.0,
            "status": "reconstructed",
            "detail": {
                "disposition": intent_disposition,
                "action": intent_action,
                "routing_mode": routing_mode,
                "note": "intent 字段为 2026-07-14 修复时基于内容回填",
            },
        },
    ]
    if primary_skill:
        stages.append({
            "stage": "skill_routing",
            "ms": 0.0,
            "status": "reconstructed",
            "detail": {
                "primary": primary_skill,
                "source": primary_source,
                "note": "primary_skill 来自原始记录（未改动）",
            },
        })
    stages.append({
        "stage": "tool_exposure",
        "ms": 0.0,
        "status": "reconstructed",
        "detail": {
            "count": len(tools_exposed),
            "tools": tools_exposed,
            "note": "tools_exposed 来自原始记录（未改动）",
        },
    })
    stages.append({
        "stage": "llm_inference",
        "ms": 0.0,
        "status": "reconstructed",
        "detail": {
            "note": "LLM 真实指标（模型/轮次/耗时）无法从历史数据反推，保留为 0",
            "repaired": True,
        },
    })
    stages.append({
        "stage": "reply",
        "ms": 0.0,
        "status": "reconstructed",
        "detail": {
            "note": "真实回复长度无法反推，保留 reply_len=0",
            "repaired": True,
        },
    })
    return stages


def main(db_path: Path, dry_run: bool = False, fix_image_markers: bool = False) -> int:
    if not db_path.exists():
        print(f"❌ DB 不存在: {db_path}")
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 1. 创建修复日志表（如不存在）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS routing_quality_repair_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rq_id INTEGER NOT NULL,
            field_name TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            repaired_at TEXT NOT NULL,
            repair_reason TEXT
        )
    """)
    conn.commit()

    # 2. 找出待修复记录
    if fix_image_markers:
        # 幂等：仅重判含【图片内容】/【图片识别中...】中文标记但 message_type 不正确的记录
        cur.execute("""
            SELECT id, sender_name, content_preview, primary_skill, primary_source,
                   routing_mode, tools_exposed, message_type, intent_disposition,
                   intent_action, stages_json, llm_rounds, reply_len
            FROM routing_quality
            WHERE (content_preview LIKE '%【图片内容】%' OR content_preview LIKE '%【图片识别中...】%')
              AND message_type NOT IN ('image', 'mixed')
            ORDER BY id
        """)
    else:
        cur.execute("""
            SELECT id, sender_name, content_preview, primary_skill, primary_source,
                   routing_mode, tools_exposed, message_type, intent_disposition,
                   intent_action, stages_json, llm_rounds, reply_len
            FROM routing_quality
            WHERE (stages_json='[]' OR stages_json='') AND llm_rounds=0
            ORDER BY id
        """)
    rows = cur.fetchall()
    mode_label = "中文图片标记重判" if fix_image_markers else "全量空数据修复"
    print(f"📊 待修复记录: {len(rows)} 条 ({mode_label})")

    if not rows:
        print("✅ 无需修复")
        return 0

    repaired_at = datetime.now().isoformat(timespec="seconds")
    stats = {"message_type": 0, "intent_disposition": 0, "intent_action": 0, "stages_json": 0}

    for r in rows:
        rq_id = r["id"]
        cp = r["content_preview"] or ""
        ps = r["primary_skill"] or ""
        psrc = r["primary_source"] or ""
        rm = r["routing_mode"] or "smart"
        try:
            tools = json.loads(r["tools_exposed"]) if r["tools_exposed"] else []
        except Exception:
            tools = []

        updates: dict = {}
        logs: list[tuple[str, str | None, str | None, str]] = []

        # ── message_type ──
        # 全量模式：仅当字段为空才更新（避免幂等重复）
        # 标记重判模式：总是更新（目的是修正中文【】标记未被识别的情况）
        if fix_image_markers:
            new_mt = infer_message_type(cp)
            if new_mt != r["message_type"]:
                updates["message_type"] = new_mt
                logs.append(("message_type", r["message_type"], new_mt,
                             "中文【图片内容】/【图片识别中...】标记重判"))
        elif not r["message_type"]:
            new_mt = infer_message_type(cp)
            updates["message_type"] = new_mt
            logs.append(("message_type", r["message_type"], new_mt, "基于 content_preview 文本特征推断"))

        # ── intent_disposition + intent_action ──
        new_disp, new_action = infer_intent(cp, ps)
        if not r["intent_disposition"] and new_disp:
            updates["intent_disposition"] = new_disp
            logs.append(("intent_disposition", r["intent_disposition"], new_disp,
                         f"基于 primary_skill={ps!r} 与内容长度推断"))
        if not r["intent_action"] and new_action:
            updates["intent_action"] = new_action
            logs.append(("intent_action", r["intent_action"], new_action,
                         f"基于 primary_skill={ps!r} 与内容长度推断"))

        # ── stages_json（重建瀑布）──
        if not fix_image_markers:
            stages = build_reconstructed_stages(
                content_preview=cp,
                message_type=updates.get("message_type", r["message_type"] or "text"),
                primary_skill=ps,
                primary_source=psrc,
                tools_exposed=tools,
                intent_disposition=updates.get("intent_disposition", r["intent_disposition"] or "business"),
                intent_action=updates.get("intent_action", r["intent_action"] or "llm"),
                routing_mode=rm,
            )
            updates["stages_json"] = json.dumps(stages, ensure_ascii=False)
            logs.append(("stages_json", r["stages_json"] or "[]", updates["stages_json"],
                         "重建为 6 跳 reconstructed 瀑布（ms=0，标注修复）"))
        elif "message_type" in updates:
            # fix_image_markers 模式：message_type 有更新则同步更新 stages_json 里 message_in 跳的 type
            try:
                stages = json.loads(r["stages_json"])
                for s in stages:
                    if s.get("stage") == "message_in":
                        s.setdefault("detail", {})["type"] = updates["message_type"]
                        break
                updates["stages_json"] = json.dumps(stages, ensure_ascii=False)
                logs.append(("stages_json", r["stages_json"], updates["stages_json"],
                             "同步 message_in.detail.type 为修正后的 message_type"))
            except Exception as e:
                print(f"  ⚠️  id={rq_id} stages_json 解析失败，跳过同步: {e}")

        # ── 写入 ──
        if dry_run:
            print(f"  [DRY] id={rq_id:3d} | {len(logs)} fields | msg='{cp[:30]}'")
            for f, old, new, _reason in logs:
                old_s = (old or "")[:40] if isinstance(old, str) else str(old)
                new_s = (new or "")[:40] if isinstance(new, str) else str(new)
                print(f"       {f}: {old_s!r} → {new_s!r}")
            continue

        # UPDATE
        set_clause = ", ".join(f"{k}=?" for k in updates)
        cur.execute(
            f"UPDATE routing_quality SET {set_clause} WHERE id=?",
            list(updates.values()) + [rq_id],
        )
        # INSERT log
        for field, old, new, reason in logs:
            cur.execute(
                "INSERT INTO routing_quality_repair_log (rq_id, field_name, old_value, new_value, repaired_at, repair_reason) VALUES (?, ?, ?, ?, ?, ?)",
                (rq_id, field, old, new, repaired_at, reason),
            )
            stats[field] = stats.get(field, 0) + 1

    if not dry_run:
        conn.commit()
        print("✅ 修复完成:")
        for k, v in stats.items():
            print(f"   - {k}: {v} 次")
        total_logs = sum(stats.values())
        print(f"   修复日志: routing_quality_repair_log ({len(rows)} 条 rq_id × 共 {total_logs} 条字段变更)")
    else:
        print("\n💡 DRY-RUN 模式，未实际写入。移除 --dry-run 执行。")

    conn.close()
    return 0


def logs_per_row(rows) -> int:
    """估算每条 rq_id 涉及的修复字段数（用于日志统计）。"""
    n = 0
    for r in rows:
        if not r["message_type"]:
            n += 1
        if not r["intent_disposition"]:
            n += 1
        if not r["intent_action"]:
            n += 1
        n += 1  # stages_json
    return n


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="修复 routing_quality 表历史空数据")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH,
                        help=f"数据库路径 (默认: {DEFAULT_DB_PATH})")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不实际写入")
    parser.add_argument("--fix-image-markers", action="store_true",
                        help="重判含中文【图片内容】标记的 message_type")
    args = parser.parse_args()
    sys.exit(main(db_path=args.db_path, dry_run=args.dry_run, fix_image_markers=args.fix_image_markers))
