"""技能管理路由：列表 / 详情 / 配置读写 / 安装 / 删除 / 重载 / 热加载看门狗 / 元数据更新。

从 `web/api.py` 抽取（原 713–1118 行），业务逻辑不变。
- get_app_instance 经 `import web.api as _api` 做属性访问，尊重测试 monkeypatch。
- SkillInstallRequest 模型随迁（仅本路由使用）。
- Path / yaml 等依赖：Path 顶层导入，yaml 在各 handler 内局部导入。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

import web.api as _api
from web.dependencies import logger
from src.shared_state import get_config as _get_shared_config
from src.paths import data_path


def _project_root() -> Path:
    """项目根目录（data/skills 所在目录）。

    与 SkillManager 初始化基准保持一致：main.py 以 os.getcwd() 初始化
    SkillManager，故此处同样用 os.getcwd() 推导，避免从本文件 __file__
    （web/routers/）向上推导只到 web/ 而错位。
    """
    return Path(os.getcwd()).resolve()

router = APIRouter()


class SkillInstallRequest(BaseModel):
    repo: str      # 如 aaaaqwq/claude-code-skills@web-search
    name: str = "" # 可选，指定技能名（多技能仓库时必填）
    platform: str = ""  # 可选，所属平台：空=通用，dingtalk/feishu/wecom

@router.get("/api/skills")
async def list_skills(platform: str = ""):
    """列出所有已加载的技能及其元数据。

    可选查询参数 ``platform``：
    - 空字符串：返回全部技能
    - "common"：仅返回通用技能（platforms 为空或 ["all"]）
    - "dingtalk" / "feishu" / "wecom"：仅返回该平台专属技能
    """
    try:
        app_instance = _api.get_app_instance()
        if not app_instance or not app_instance.llm_agent or not app_instance.llm_agent.skill_manager:
            return {"skills": [], "message": "技能引擎未启用"}
        
        mgr = app_instance.llm_agent.skill_manager
        skills = []
        for skill in mgr.list_all():
            skills.append({
                "name": skill.name,
                "description": skill.description,
                "allowed_tools": skill.allowed_tools,
                "intent_keywords": skill.intent_keywords,
                "intent_categories": skill.intent_categories,
                "effective_intent_keywords": skill.effective_intent_keywords,
                "weight": skill.weight,
                "enabled": skill.enabled,
                "source_path": skill.source_path,
                "has_config": skill.has_config,
                "platforms": getattr(skill, "platforms", []) or [],
            })
        
        # 也列出 data/skills 目录下未加载的（可能是安装后尚未 reload）
        data_skills_dir = data_path("skills")
        loaded_names = {s["name"] for s in skills}
        if data_skills_dir.is_dir():
            for entry in data_skills_dir.iterdir():
                if entry.is_dir() and not entry.name.startswith("."):
                    if entry.name not in loaded_names:
                        skills.append({
                            "name": entry.name,
                            "description": "(未加载，请点击刷新或等待热加载)",
                            "allowed_tools": [],
                            "intent_keywords": [],
                            "intent_categories": [],
                            "effective_intent_keywords": [],
                            "weight": 0,
                            "source_path": str(entry),
                            "unloaded": True,
                            "platforms": [],
                        })
        
        # 平台过滤
        if platform:
            skills = [s for s in skills if _skill_supports_platform(s, platform)]

        return {"skills": skills, "platform": platform}
    except Exception as e:
        logger.error("技能列表API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _skill_supports_platform(skill: dict, platform: str) -> bool:
    """判断技能是否允许在指定平台使用。

    - platform="common"：仅通用技能（platforms 为空或含 "all"）
    - platform="dingtalk"/"feishu"/"wecom"：该平台专属技能 + 通用技能，
      排除其他平台专属技能（隔离原则：钉钉专用不暴露给飞书/企微）

    技能元数据含 ``platforms`` 字段（list[str]）时按白名单过滤；
    缺省视为通用（全平台可用）。
    """
    platforms: list = skill.get("platforms") or []

    if platform == "common":
        # 仅返回通用技能：platforms 为空，或含 "all"
        if not platforms:
            return True
        return "all" in platforms

    # 平台查询：显示该平台专属 + 通用技能，排除其他平台专属
    if not platforms:
        return True  # 空 = 通用，所有平台可见
    if "all" in platforms:
        return True  # "all" = 通用，所有平台可见
    # 有明确平台标记：仅当包含当前平台时可见（隔离）
    return platform in platforms


@router.get("/api/skills/watcher")
async def get_watcher_status():
    """查询技能热加载监控状态。"""
    try:
        app_instance = _api.get_app_instance()
        if not app_instance or not app_instance.llm_agent or not app_instance.llm_agent.skill_manager:
            return {"enabled": False, "reason": "技能引擎未启用"}
        mgr = app_instance.llm_agent.skill_manager
        info = _watcher_status(mgr)
        info["enabled"] = True
        return info
    except Exception as e:
        logger.error("查询 watcher 状态异常: %s", e)
        return {"enabled": False, "error": str(e)}


@router.get("/api/skills/{skill_name}")
async def get_skill_detail(skill_name: str):
    """获取单个技能的 SKILL.md 原始内容和元数据。"""
    try:
        app_instance = _api.get_app_instance()
        if not app_instance or not app_instance.llm_agent or not app_instance.llm_agent.skill_manager:
            raise HTTPException(status_code=503, detail="技能引擎未启用")
        
        mgr = app_instance.llm_agent.skill_manager
        skill = mgr.get(skill_name)
        if not skill:
            raise HTTPException(status_code=404, detail=f"技能 '{skill_name}' 未找到")
        
        # 读取原始文件内容
        raw_content = ""
        try:
            raw_content = Path(skill.source_path).read_text(encoding="utf-8")
        except Exception:
            pass
        
        return {
            "name": skill.name,
            "description": skill.description,
            "allowed_tools": skill.allowed_tools,
            "intent_keywords": skill.intent_keywords,
            "weight": skill.weight,
            "source_path": skill.source_path,
            "body": skill.body,
            "raw_content": raw_content,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("技能详情API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/skills/{skill_name}/config")
async def get_skill_config(skill_name: str):
    """读取技能的 config.yaml 内容。"""
    try:
        app_instance = _api.get_app_instance()
        if not app_instance or not app_instance.llm_agent or not app_instance.llm_agent.skill_manager:
            raise HTTPException(status_code=503, detail="技能引擎未启用")

        mgr = app_instance.llm_agent.skill_manager
        skill = mgr.get(skill_name)
        if not skill:
            raise HTTPException(status_code=404, detail=f"技能 '{skill_name}' 未找到")

        skill_dir = Path(skill.source_path).parent
        config_file = skill_dir / "config.yaml"
        if not config_file.is_file():
            return {"has_config": False, "config": None, "raw_yaml": ""}

        raw = config_file.read_text(encoding="utf-8")
        return {"has_config": True, "config": skill.config, "raw_yaml": raw}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("读取技能配置错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/skills/{skill_name}/config")
async def update_skill_config(skill_name: str, data: dict):
    """更新技能的 config.yaml 内容（完整覆盖）。"""
    try:
        app_instance = _api.get_app_instance()
        if not app_instance or not app_instance.llm_agent or not app_instance.llm_agent.skill_manager:
            raise HTTPException(status_code=503, detail="技能引擎未启用")

        mgr = app_instance.llm_agent.skill_manager
        skill = mgr.get(skill_name)
        if not skill:
            raise HTTPException(status_code=404, detail=f"技能 '{skill_name}' 未找到")

        raw_yaml = (data.get("raw_yaml") or "").strip()
        if not raw_yaml:
            raise HTTPException(status_code=400, detail="raw_yaml 不能为空")

        skill_dir = Path(skill.source_path).parent
        config_file = skill_dir / "config.yaml"
        config_file.write_text(raw_yaml, encoding="utf-8")

        # 重新加载技能以使 config 生效
        mgr.reload()
        return {"success": True, "message": "配置已保存并已重载技能"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("更新技能配置错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/skills/install")
async def install_skill(req: SkillInstallRequest):
    """安装技能到 data/skills/ 目录。

    支持 npx skills CLI 和手动 git clone 两种方式。
    安装后自动 reload 技能引擎。
    可选 platform 参数会写入 SKILL.md frontmatter。
    """
    try:
        import subprocess, shutil

        project_root = _project_root()
        data_skills = data_path("skills")
        data_skills.mkdir(parents=True, exist_ok=True)

        repo = req.repo.strip()
        if not repo:
            raise HTTPException(status_code=400, detail="repo 不能为空")

        platform = req.platform.strip() if req.platform else ""
        installed_name = ""

        # 尝试 npx skills add（cwd 设为数据目录父，使其落到 data/skills）
        cmd = ["npx", "skills", "add", repo, "-y"]
        proc = await run_in_threadpool(subprocess.run, cmd, cwd=str(data_path("skills").parent), capture_output=True, text=True, timeout=60)
        
        if proc.returncode != 0:
            # npx 方式失败，尝试手动 git clone
            # 解析 repo 格式：owner/repo@skill_name 或 owner/repo
            if "@" in repo:
                repo_url_part, skill_filter = repo.rsplit("@", 1)
                repo_url = f"https://github.com/{repo_url_part}.git"
            else:
                repo_url = f"https://github.com/{repo}.git"
                skill_filter = ""
            
            tmp_dir = data_path("skills", ".tmp_clone")
            if tmp_dir.exists():
                await run_in_threadpool(shutil.rmtree, str(tmp_dir))

            clone_proc = await run_in_threadpool(
                subprocess.run,
                ["git", "clone", "--depth", "1", repo_url, str(tmp_dir)],
                capture_output=True, text=True, timeout=120,
            )
            
            if clone_proc.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"npx: {proc.stderr.strip()}\ngit: {clone_proc.stderr.strip()}"
                )
            
            # 搜索 SKILL.md
            skill_dirs = []
            for skill_md in tmp_dir.rglob("SKILL.md"):
                skill_dir = skill_md.parent
                # 解析 name
                raw = skill_md.read_text(encoding="utf-8")
                name = skill_dir.name
                if skill_filter and name != skill_filter:
                    continue
                skill_dirs.append((name, skill_dir))
            
            if not skill_dirs:
                await run_in_threadpool(shutil.rmtree, str(tmp_dir))
                raise HTTPException(status_code=400, detail="仓库中未找到 SKILL.md")
            
            # 安装第一个匹配的技能
            name, skill_dir = skill_dirs[0]
            dest = data_skills / name
            if dest.exists():
                await run_in_threadpool(shutil.rmtree, str(dest))
            await run_in_threadpool(shutil.move, str(skill_dir), str(dest))
            await run_in_threadpool(shutil.rmtree, str(tmp_dir))
            
            installed_name = name
            install_msg = f"已从 git 安装技能: {name}"
        else:
            install_msg = f"npx skills add 成功: {proc.stdout.strip()[:200]}"
            # 尝试从安装输出中提取技能名
            for entry in sorted(data_skills.iterdir(), key=lambda e: e.stat().st_mtime, reverse=True):
                if entry.is_dir() and not entry.name.startswith(".") and (entry / "SKILL.md").exists():
                    installed_name = entry.name
                    break
        
        # 写入 platforms 到 SKILL.md frontmatter
        if platform and installed_name:
            _write_platforms_to_skill_md(data_skills / installed_name / "SKILL.md", platform)
        
        # reload 技能引擎
        app_instance = _api.get_app_instance()
        if app_instance and app_instance.llm_agent and app_instance.llm_agent.skill_manager:
            count = app_instance.llm_agent.skill_manager.reload()
            return {"success": True, "message": install_msg, "loaded_count": count, "platform": platform}
        
        return {"success": True, "message": install_msg, "platform": platform}
    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="安装超时（60s）")
    except Exception as e:
        logger.error("技能安装API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _write_platforms_to_skill_md(skill_md_path: Path, platform: str) -> None:
    """将平台信息写入 SKILL.md frontmatter。"""
    import yaml as _yaml
    if not skill_md_path.exists():
        return
    try:
        content = skill_md_path.read_text(encoding="utf-8")
        if not content.startswith("---"):
            return
        second_delim = content.find("---", 3)
        if second_delim == -1:
            return
        frontmatter_str = content[3:second_delim]
        body = content[second_delim + 3:]
        fm = _yaml.safe_load(frontmatter_str) or {}
        if not isinstance(fm, dict):
            return
        fm["platforms"] = [platform]
        new_frontmatter = _yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False).strip()
        new_content = f"---\n{new_frontmatter}\n---{body}"
        skill_md_path.write_text(new_content, encoding="utf-8")
        logger.info("[SkillInstall] 已写入 platforms: [%s] → %s", platform, skill_md_path)
    except Exception as e:
        logger.warning("[SkillInstall] 写入 platforms 失败: %s", e)


@router.delete("/api/skills/{skill_name}")
async def uninstall_skill(skill_name: str):
    """卸载技能：删除 data/skills/{name}/ 目录并 reload。"""
    try:
        import shutil

        # 复用 SkillManager 实际使用的项目根（其以 os.getcwd() 初始化），
        # 而非从本文件 __file__ 推导（web/routers/ → parent.parent 仅为 web/，路径错位）。
        app_instance = _api.get_app_instance()
        skill_manager = (
            app_instance.llm_agent.skill_manager
            if (app_instance and app_instance.llm_agent) else None
        )
        skill_dir = data_path("skills", skill_name)

        if not skill_dir.exists():
            raise HTTPException(status_code=404, detail=f"技能目录不存在: {skill_dir}")

        # 彻底删除目录
        await run_in_threadpool(shutil.rmtree, str(skill_dir))

        # reload 内存中的技能列表
        if skill_manager:
            count = skill_manager.reload()
            return {"success": True, "message": f"已卸载技能 {skill_name}", "loaded_count": count}

        return {"success": True, "message": f"已卸载技能 {skill_name}"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("技能卸载API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/skills/reload")
async def reload_skills():
    """手动 reload 所有技能（重新扫描 data/skills/）。"""
    try:
        app_instance = _api.get_app_instance()
        if not app_instance or not app_instance.llm_agent or not app_instance.llm_agent.skill_manager:
            raise HTTPException(status_code=503, detail="技能引擎未启用")

        mgr = app_instance.llm_agent.skill_manager
        count = mgr.reload()
        # 收集 watcher 状态
        watcher_info = _watcher_status(mgr)
        return {
            "success": True,
            "loaded_count": count,
            "skills": mgr.list_names(),
            "watcher": watcher_info,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("技能reload API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


class GenerateIntentsRequest(BaseModel):
    name: str = ""      # 指定单个技能；空字符串=全部
    force: bool = False # 是否覆盖已有意图词


@router.post("/api/skills/generate-intents")
async def generate_intents(req: GenerateIntentsRequest):
    """用 AI 分析每个技能的 SKILL.md 并生成意图词（写回 SKILL.md）。

    - name 为空：处理全部技能（默认仅填充空意图词的技能，force=True 覆盖全部）。
    - 通过 asyncio.to_thread 在后台线程运行同步 LLM 调用，避免阻塞事件循环。
    """
    try:
        app_instance = _api.get_app_instance()
        if not app_instance or not app_instance.llm_agent or not app_instance.llm_agent.skill_manager:
            raise HTTPException(status_code=503, detail="技能引擎未启用")
        if not getattr(app_instance, "llm_client", None):
            raise HTTPException(status_code=503, detail="LLM 客户端未就绪")

        # 安全闸门：ai_intent_generation_enabled 默认关闭，避免意外烧 LLM 额度
        cfg = _get_shared_config()
        skills_cfg = getattr(cfg, "skills", None)
        if not getattr(skills_cfg, "ai_intent_generation_enabled", False):
            raise HTTPException(
                status_code=403,
                detail="未启用 AI 意图词生成（skills.ai_intent_generation_enabled=false）。"
                       "请在 config.yaml / config.py 中开启后再调用。",
            )

        mgr = app_instance.llm_agent.skill_manager
        client = app_instance.llm_client

        names = [req.name] if req.name else None

        def _run():
            # 手动触发生成不套用后台批量限速器（BackgroundLLMThrottle 会强制最小间隔睡眠，
            # 导致多技能请求长时间挂起）。失败由生成器优雅降级处理。
            return mgr.generate_intents(
                client=client,
                names=names,
                force=req.force,
                throttle=None,
                persist=True,
            )

        result = await asyncio.to_thread(_run)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("AI 意图词生成API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


class GenerateIntentTraceRequest(BaseModel):
    name: str            # 目标技能名（必填，单技能）
    force: bool = False  # 是否覆盖已有意图词


@router.post("/api/skills/generate-intent-trace")
async def generate_intent_trace(req: GenerateIntentTraceRequest):
    """为单个技能生成 AI 意图词，并返回完整交互过程（前端可视化用）。

    与批量端点不同：这是**显式单技能**操作（用户点按钮触发），不消耗后台
    批量限速器，也不受 `skills.ai_intent_generation_enabled` 闸门约束——该闸门
    仅用于防止「意外批量烧额度」，单条显式生成属预期内的可控调用。
    """
    try:
        app_instance = _api.get_app_instance()
        if not app_instance or not app_instance.llm_agent or not app_instance.llm_agent.skill_manager:
            raise HTTPException(status_code=503, detail="技能引擎未启用")
        if not getattr(app_instance, "llm_client", None):
            raise HTTPException(status_code=503, detail="LLM 客户端未就绪")

        mgr = app_instance.llm_agent.skill_manager
        client = app_instance.llm_client

        def _run():
            return mgr.generate_intents_trace(
                client=client,
                name=req.name,
                force=req.force,
                throttle=None,
                persist=True,
            )

        result = await asyncio.to_thread(_run)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("AI 意图词 trace API 错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _watcher_status(skill_manager) -> dict:
    """返回热加载 watcher 当前状态。"""
    if not hasattr(skill_manager, "_watcher_thread"):
        return {"supported": False, "reason": "旧版 SkillManager 无热加载"}
    t = skill_manager._watcher_thread
    return {
        "supported": True,
        "running": t is not None and t.is_alive(),
        "poll_interval": getattr(skill_manager, "_poll_interval", 0),
    }


@router.put("/api/skills/{skill_name}")
async def update_skill_meta(skill_name: str, data: dict):
    """更新技能的 weight / enabled / intent_keywords / platforms，回写 SKILL.md frontmatter 后触发 reload。"""
    import yaml as _yaml
    try:
        app_instance = _api.get_app_instance()
        if not app_instance or not app_instance.llm_agent or not app_instance.llm_agent.skill_manager:
            raise HTTPException(status_code=503, detail="技能引擎未启用")

        mgr = app_instance.llm_agent.skill_manager
        skill = mgr.get(skill_name)
        if not skill:
            raise HTTPException(status_code=404, detail=f"技能 {skill_name} 不存在")

        skill_md_path = Path(skill.source_path)
        if not skill_md_path.exists():
            raise HTTPException(status_code=404, detail=f"SKILL.md 不存在: {skill_md_path}")

        content = skill_md_path.read_text(encoding="utf-8")
        if not content.startswith("---"):
            raise HTTPException(status_code=400, detail="SKILL.md 缺少 frontmatter")

        # 找到 frontmatter 边界
        second_delim = content.find("---", 3)
        if second_delim == -1:
            raise HTTPException(status_code=400, detail="SKILL.md frontmatter 格式异常")
        frontmatter_str = content[3:second_delim]
        body = content[second_delim + 3:]

        # 解析 frontmatter YAML
        try:
            fm = _yaml.safe_load(frontmatter_str) or {}
        except _yaml.YAMLError:
            raise HTTPException(status_code=400, detail="SKILL.md frontmatter YAML 格式错误")

        # 合并更新
        updates = {}
        if "weight" in data:
            try:
                w = float(data["weight"])
                w = max(0.0, min(1.0, w))
                fm["weight"] = w
                updates["weight"] = w
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="weight 必须是 0.0-1.0 的数值")

        if "enabled" in data:
            fm["enabled"] = bool(data["enabled"])
            updates["enabled"] = fm["enabled"]

        if "intent_keywords" in data:
            raw_kw = data["intent_keywords"]
            if not isinstance(raw_kw, list):
                raise HTTPException(status_code=400, detail="intent_keywords 必须是字符串数组")
            seen = set()
            cleaned = []
            for k in raw_kw:
                k = str(k).strip()
                if k and k not in seen:
                    seen.add(k)
                    cleaned.append(k)
            fm["intent_keywords"] = cleaned
            updates["intent_keywords"] = cleaned

        if "system_prompt" in data:
            sp = str(data["system_prompt"]).strip()
            if sp is None:
                raise HTTPException(status_code=400, detail="system_prompt 必须是非空字符串")
            fm["system_prompt"] = sp
            updates["system_prompt"] = "updated"

        if "platforms" in data:
            raw_pf = data["platforms"]
            if raw_pf is None or (isinstance(raw_pf, list) and len(raw_pf) == 0):
                # 空列表 = 通用技能，移除 platforms 字段
                fm.pop("platforms", None)
            elif isinstance(raw_pf, list):
                fm["platforms"] = [str(p).strip() for p in raw_pf if str(p).strip()]
            else:
                raise HTTPException(status_code=400, detail="platforms 必须是字符串数组或 null")
            updates["platforms"] = fm.get("platforms", [])

        if not updates:
            raise HTTPException(status_code=400, detail="无有效更新字段（支持 weight / enabled / intent_keywords / system_prompt / platforms）")

        # 重新序列化 frontmatter，尽量保持格式
        new_frontmatter = _yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False).strip()
        new_content = f"---\n{new_frontmatter}\n---{body}"

        skill_md_path.write_text(new_content, encoding="utf-8")

        # 触发 reload
        count = mgr.reload()
        logger.info("[SkillAPI] 更新技能 %s: %s，reload 加载 %s 个技能", skill_name, updates, count)

        return {"success": True, "updated": updates, "reloaded_count": count}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("更新技能元数据失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

