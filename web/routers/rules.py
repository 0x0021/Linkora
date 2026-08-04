"""遗留规则配置读取路由。

从 `web/api.py` 抽取（原 2118–2129 行），业务逻辑不变。
- 配置经 `_api._get_cfg()` 读取（单例优先 + 磁盘兜底，统一真源）；
  tests 会 monkeypatch 该全局，必须取实时值而非导入期绑定。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

import web.api as _api

router = APIRouter()


@router.get("/api/rules")
async def rules():
    try:
        config = _api._get_cfg()
        return {
            "blacklist": config.rules.blacklist,
            "whitelist": config.rules.whitelist,
            "keywords": [{"match": k.match, "reply": k.reply} for k in config.rules.keywords],
            "enabled": config.rules.enabled,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
