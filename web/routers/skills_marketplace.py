"""SkillHub 技能市场路由：搜索 / 安装 / 热门 / 榜单。

从 `web/api.py` 抽取（原 3898–4026 行），业务逻辑不变。
通过 `import web.api as _api` 持有模块引用，运行时取共享符号
（_ensure_skillhub_cli / _get_project_root / _fetch_market_rankings /
get_app_instance / logger），避免与 api.py 的挂载产生循环导入。
"""
from __future__ import annotations

import json as _json
import os
import re
import subprocess
from pathlib import Path

from web.dependencies import (
    _ensure_skillhub_cli,
    _fetch_market_rankings,
    _get_project_root,
    _get_raw_icon_url,
    _proxy_icon_url,
    get_app_instance,
    logger,
)
from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from src.paths import data_path

router = APIRouter()


@router.get("/api/skills/marketplace/search")
async def search_marketplace(keyword: str = ""):
    """搜索 SkillHub 技能市场。keyword 为空时返回热门推荐。"""
    try:
        ok, err = _ensure_skillhub_cli()
        if not ok:
            raise HTTPException(status_code=500, detail=f"skillhub CLI 不可用: {err}")

        env = os.environ.copy()
        local_bin = str(Path.home() / ".local" / "bin")
        env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"

        cmd = ["skillhub", "search", keyword, "--json"] if keyword.strip() else ["skillhub", "search", "--json"]
        proc = await run_in_threadpool(subprocess.run, cmd, capture_output=True, text=True, timeout=30, env=env)

        if proc.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"SkillHub 搜索失败: {proc.stderr.strip()[:200]}"
            )

        stdout = proc.stdout.strip()
        if not stdout or "no skills found" in stdout.lower():
            return {"skills": [], "total": 0}

        try:
            raw = _json.loads(stdout)
        except _json.JSONDecodeError:
            # 尝试从混合输出中提取 JSON
            match = re.search(r'\[.*\]', stdout, re.DOTALL)
            if match:
                try:
                    raw = _json.loads(match.group())
                except _json.JSONDecodeError:
                    raise HTTPException(status_code=500, detail="SkillHub 返回格式异常，无法解析")
            else:
                raise HTTPException(status_code=500, detail="SkillHub 返回格式异常，无法解析")

        # 标准化字段 — CLI 返回 {"query": ..., "results": [...]} 对象格式
        items = raw.get("results", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
        skills = []
        for item in items:
            sl = str(item.get("slug", item.get("name", "")))
            skills.append({
                "slug": sl,
                "name": str(item.get("name", item.get("skill_name", item.get("slug", "")))),
                "description": str(item.get("description", item.get("desc", ""))),
                "version": str(item.get("version", "")),
                "author": str(item.get("author", item.get("source", ""))),
                "installs": item.get("installs", item.get("downloads", 0)),
                "url": str(item.get("url", item.get("repo_url", ""))),
                "iconUrl": _proxy_icon_url(item.get("iconUrl", item.get("icon", "")), slug=sl),
            })

        return {"skills": skills, "total": len(skills)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("SkillHub 搜索错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/skills/marketplace/install")
async def install_from_marketplace(data: dict):
    """从 SkillHub 安装技能到 data/skills/ 目录。"""
    try:
        slug = (data.get("slug") or data.get("name") or "").strip()
        if not slug:
            raise HTTPException(status_code=400, detail="技能标识不能为空")

        ok, err = _ensure_skillhub_cli()
        if not ok:
            raise HTTPException(status_code=500, detail=f"skillhub CLI 不可用: {err}")

        project_root = _get_project_root()
        skills_dir = data_path("skills")
        skills_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        local_bin = str(Path.home() / ".local" / "bin")
        env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"

        cmd = ["skillhub", "install", slug, "--dir", str(skills_dir)]
        proc = await run_in_threadpool(subprocess.run, cmd, capture_output=True, text=True, timeout=120, env=env)

        if proc.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"SkillHub 安装失败: {proc.stderr.strip()[:200]}"
            )

        install_msg = proc.stdout.strip()[:300] or f"技能 {slug} 安装成功"

        # 安装成功后下载图标到本地缓存（异步，不影响安装流程）
        icon_url = _get_raw_icon_url(slug)
        if icon_url:
            try:
                from web.routers.image import _download_skill_icon
                ok = await _download_skill_icon(slug, icon_url)
                logger.info("SkillHub 图标缓存 [%s]: %s → %s", slug, icon_url, "成功" if ok else "跳过/失败")
            except Exception as e:
                logger.warning("SkillHub 图标下载 [%s] 异常: %s", slug, e)

        # reload 技能引擎
        app_instance = get_app_instance()
        if app_instance and app_instance.llm_agent and app_instance.llm_agent.skill_manager:
            count = app_instance.llm_agent.skill_manager.reload()
            return {"success": True, "message": install_msg, "loaded_count": count}

        return {"success": True, "message": install_msg}
    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="安装超时（120s）")
    except Exception as e:
        logger.error("SkillHub 安装错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/skills/marketplace/popular")
async def popular_skills():
    """获取 SkillHub 热门推荐技能。"""
    return await search_marketplace(keyword="")


@router.get("/api/skills/marketplace/rankings")
async def market_rankings(force: bool = False):
    """获取 SkillHub 技能市场榜单。

    返回 6 类排序 section：all(全部/score)、featured(推荐精选)、
    trending(近期飙升)、hot(下载量)、newest(最近上新)、stars(收藏量)。
    """
    try:
        return await _fetch_market_rankings(force=force)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("SkillHub 榜单错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/skills/migrate-icons")
async def migrate_skill_icons():
    """一次性迁移：为已安装技能批量下载图标到本地缓存。

    调用 skillhub skill rankings 获取全量图标 URL，匹配已安装的技能
    （data/skills/ 下的子目录），逐个下载到 data/skill_icons/。
    """
    from web.routers.image import _download_skill_icon

    # 1. 强制刷新排行榜数据，获取 iconUrl 映射
    try:
        rankings_data = await _fetch_market_rankings(force=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取榜单数据失败: {e}")

    # 2. 列出已安装技能
    skills_dir = data_path("skills")
    installed_slugs = set()
    if skills_dir.is_dir():
        for entry in skills_dir.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                installed_slugs.add(entry.name)

    if not installed_slugs:
        return {"ok": True, "message": "没有已安装技能", "downloaded": 0, "skipped": 0, "failed": 0}

    # 3. 构建 installed_dir → raw_icon_url 映射
    #    _ICON_URL_MAP key 如 @org/contract-review，已安装目录如 contract-review
    icon_map: dict[str, str] = {}
    sections = rankings_data.get("sections", {})
    for sec_name in sections:
        for skill in sections[sec_name]:
            s = skill.get("slug", "")
            raw = _get_raw_icon_url(s)
            if not raw:
                continue
            # 将 suffix 匹配也加入（@org/foo → foo 匹配 installed dir）
            icon_map[s] = raw
            if "/" in s:
                icon_map[s.rsplit("/", 1)[-1]] = raw
            # 名称匹配
            name = skill.get("name", "")
            if name:
                icon_map[name] = raw

    for dir_name in installed_slugs:
        matched = icon_map.get(dir_name, "")
        if matched:
            icon_map[dir_name] = matched

    downloaded = 0
    skipped = 0
    failed = 0
    details: list[dict] = []

    for slug in sorted(installed_slugs):
        raw_url = icon_map.get(slug, "")
        if not raw_url:
            skipped += 1
            details.append({"slug": slug, "status": "skipped", "reason": "排行榜中无对应图标 URL"})
            continue
        ok = await _download_skill_icon(slug, raw_url)
        if ok:
            downloaded += 1
            details.append({"slug": slug, "status": "downloaded"})
        else:
            failed += 1
            details.append({"slug": slug, "status": "failed", "reason": "下载失败或非图片响应"})

    return {
        "ok": True,
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
        "details": details,
    }
