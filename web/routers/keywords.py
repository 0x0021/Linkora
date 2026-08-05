"""关键词规则（增删改查 / 导入导出 / 测试匹配 / 批量操作）路由。

从 `web/api.py` 抽取（原 698–1026 行），业务逻辑不变。
- get_store / load_config / CONFIG_PATH 经 `import web.api as _api` 做属性访问，
  以尊重测试对 `web.api.*` 的 monkeypatch。
- RuleKeyword / KeywordUpdate / KeywordMatchTest / KeywordBatchOp 模型自 web.api 导入
  （仅作类型注解，测试不 patch）。
- jieba / json / datetime 为模块级依赖，本地导入。
"""

from __future__ import annotations

import json

import jieba
from datetime import datetime
from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse

import web.api as _api
from fastapi.concurrency import run_in_threadpool
from web.schemas import (
    RuleKeyword,
    KeywordUpdate,
    KeywordMatchTest,
    KeywordBatchOp,
)
from web.dependencies import logger

router = APIRouter()


@router.get("/api/keywords")
async def list_keywords(category: str = "", enabled: int | None = None,
                        search: str = "", limit: int = 200, page: int = 1,
                        platform: str = ""):
    try:
        limit = max(1, min(limit, 500))
        def _work():
            store = _api.get_store()
            rules = store.list_keyword_rules(category=category, enabled=enabled, limit=limit)
            if search:
                rules = [r for r in rules if search.lower() in r["match_pattern"].lower()
                         or search.lower() in r["reply_text"].lower()]
            categories = store.keyword_categories()
            total = len(rules)

            start = (page - 1) * 50
            end = start + 50
            paged_rules = rules[start:end]

            return {
                "rules": paged_rules,
                "categories": categories,
                "total": total,
                "page": page,
                "page_size": 50,
            }
        return await run_in_threadpool(_work)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/keywords")
async def add_keyword(rule: RuleKeyword):
    try:
        def _work():
            store = _api.get_store()
            return store.add_keyword_rule(
                match_pattern=rule.match_pattern,
                reply_text=rule.reply_text,
                category=rule.category,
                match_type=rule.match_type,
                priority=rule.priority,
            )
        rule_id = await run_in_threadpool(_work)
        return {"success": True, "id": rule_id, "message": "规则添加成功"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/keywords/stats")
async def keyword_stats():
    try:
        def _work():
            return _api.get_store().keyword_rules_stats()
        return await run_in_threadpool(_work)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/keywords/{rule_id}")
async def get_keyword(rule_id: int):
    try:
        def _work():
            store = _api.get_store()
            rule = store.get_keyword_rule(rule_id)
            if not rule:
                raise HTTPException(status_code=404, detail="规则不存在")
            return {"rule": rule}
        return await run_in_threadpool(_work)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/api/keywords/{rule_id}")
async def update_keyword(rule_id: int, update: KeywordUpdate):
    try:
        def _work():
            store = _api.get_store()
            existing = store.get_keyword_rule(rule_id)
            if not existing:
                raise HTTPException(status_code=404, detail="规则不存在")
            data = {k: v for k, v in update.model_dump().items() if v is not None}
            if data:
                store.update_keyword_rule(rule_id, **data)
            return {"success": True, "message": "规则更新成功"}
        return await run_in_threadpool(_work)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/keywords/{rule_id}")
async def delete_keyword(rule_id: int):
    try:
        def _work():
            store = _api.get_store()
            store.delete_keyword_rule(rule_id)
            return {"success": True, "message": "规则删除成功"}
        return await run_in_threadpool(_work)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/keywords/import")
async def import_keywords(file: UploadFile = File(...)):
    try:
        content = await file.read()
        text = ""
        for enc in ["utf-8", "gbk", "gb2312", "gb18030", "utf-16"]:
            try:
                text = content.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if not text:
            raise HTTPException(status_code=400, detail="无法识别文件编码，请使用 UTF-8 编码")

        rules = []
        if file.filename and file.filename.endswith(".json"):
            try:
                data = json.loads(text)
                if isinstance(data, list):
                    rules = data
                elif isinstance(data, dict) and "rules" in data:
                    rules = data["rules"]
                elif isinstance(data, dict) and "keywords" in data:
                    rules = data["keywords"]
            except json.JSONDecodeError:
                pass
        elif file.filename and file.filename.endswith(".txt"):
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "\t" in line:
                    parts = line.split("\t", 1)
                    if len(parts) == 2:
                        rules.append({"match_pattern": parts[0].strip(),
                                      "reply_text": parts[1].strip(),
                                      "match_type": "fuzzy"})
                elif "=>" in line:
                    parts = line.split("=>", 1)
                    if len(parts) == 2:
                        rules.append({"match_pattern": parts[0].strip(),
                                      "reply_text": parts[1].strip(),
                                      "match_type": "fuzzy"})
                elif "->" in line:
                    parts = line.split("->", 1)
                    if len(parts) == 2:
                        rules.append({"match_pattern": parts[0].strip(),
                                      "reply_text": parts[1].strip(),
                                      "match_type": "fuzzy"})

        if not rules:
            raise HTTPException(status_code=400, detail="未解析到有效规则，请检查文件格式")

        def _work():
            # get_store() 内部含向量索引陈旧校验（同步 DB 查询），一并放进线程池。
            return _api.get_store().batch_import_keywords(rules)
        result = await run_in_threadpool(_work)
        return {
            "success": True,
            "imported": result["imported"],
            "skipped": result["skipped"],
            "message": f"导入完成：成功 {result['imported']} 条，跳过 {result['skipped']} 条",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/keywords/export")
async def export_keywords(category: str = ""):
    try:
        def _work():
            store = _api.get_store()
            return store.list_keyword_rules(category=category)
        rules = await run_in_threadpool(_work)
        export_data = [
            {
                "match_pattern": r["match_pattern"],
                "reply_text": r["reply_text"],
                "category": r["category"],
                "match_type": r["match_type"],
                "priority": r["priority"],
                "enabled": bool(r["enabled"]),
                "hit_count": r["hit_count"],
            }
            for r in rules
        ]
        return JSONResponse(
            content=export_data,
            headers={
                "Content-Disposition": f"attachment; filename=keyword_rules_{datetime.now().strftime('%Y%m%d')}.json"
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/keywords/test-match")
async def test_keyword_match(body: KeywordMatchTest):
    # 配置缺失时抛 503（放在 try 外：HTTPException 是 Exception 子类，
    # 落进下方 except 会被压平成语义错误的 500）。
    _config = _api._require_cfg()
    try:
        def _work():
            store = _api.get_store()
            return store.list_keyword_rules(enabled=1)
        rules = await run_in_threadpool(_work)
        import re
        # 从配置加载停用词（与 rule_engine.py 保持一致）
        _stop_words_cfg = set()
        for line in _config.rules.stop_words:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for w in line.split(","):
                w = w.strip()
                if w:
                    _stop_words_cfg.add(w.lower())

        def _is_sw(t: str) -> bool:
            return t not in _stop_words_cfg and len(t) > 1

        matched = []
        for rule in rules:
            pattern = rule["match_pattern"]
            match_type = rule["match_type"]
            text = body.text
            is_match = False
            if match_type == "exact":
                is_match = text.strip() == pattern.strip()
            elif match_type == "fuzzy":
                # 与 rule_engine.py 保持一致：从配置加载停用词后做 token 交集匹配
                if not text:
                    is_match = False
                else:
                    txt = text.strip()
                    keywords = [k.strip() for k in pattern.split(",") if k.strip()]
                    text_tokens = {t for t in jieba.lcut(txt) if _is_sw(t)}
                    is_match = False
                    for kw in keywords:
                        kw_lower = kw.lower()
                        txt_lower = txt.lower()
                        # 策略1: 完整短语子串匹配（高置信度）
                        if kw_lower in txt_lower:
                            is_match = True
                            break
                        # 策略2: token 交集
                        kw_tokens = {t for t in jieba.lcut(kw) if _is_sw(t)}
                        overlap = text_tokens & kw_tokens
                        if overlap:
                            is_very_short = len(txt) <= 4
                            is_single_specific = (
                                len(text_tokens) == 1
                                and len(next(iter(text_tokens))) >= 3
                            )
                            if (is_very_short or is_single_specific) and len(overlap) >= 1:
                                is_match = True
                                break
                            if len(overlap) >= 2:
                                is_match = True
                                break
            elif match_type == "regex":
                try:
                    is_match = bool(re.search(pattern, text))
                except re.error:
                    continue
            if is_match:
                matched.append({
                    "id": rule["id"],
                    "match_pattern": rule["match_pattern"],
                    "reply_text": rule["reply_text"],
                    "category": rule["category"],
                    "match_type": rule["match_type"],
                    "priority": rule["priority"],
                })
        matched.sort(key=lambda x: x["priority"], reverse=True)
        return {
            "success": True,
            "input": body.text,
            "matched": matched,
            "hit_count": len(matched),
            "top_reply": matched[0]["reply_text"] if matched else "",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/keywords/batch")
async def batch_keyword_ops(body: KeywordBatchOp):
    try:
        def _work():
            store = _api.get_store()
            count = 0
            if body.action == "enable":
                for rid in body.ids:
                    store.update_keyword_rule(rid, enabled=1)
                    count += 1
            elif body.action == "disable":
                for rid in body.ids:
                    store.update_keyword_rule(rid, enabled=0)
                    count += 1
            elif body.action == "delete":
                for rid in body.ids:
                    store.delete_keyword_rule(rid)
                    count += 1
            elif body.action == "move_category" and body.category:
                for rid in body.ids:
                    store.update_keyword_rule(rid, category=body.category)
                    count += 1
            else:
                raise HTTPException(status_code=400, detail=f"不支持的操作: {body.action}")
            return count
        count = await run_in_threadpool(_work)
        return {"success": True, "count": count, "message": f"批量操作完成，处理 {count} 条"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
