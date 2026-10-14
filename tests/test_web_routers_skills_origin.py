"""web/routers/skills.py 来源字段（origin）透出测试。

覆盖「技能列表 / 详情」API 将 Skill.origin（builtin=平台内置 / user=用户安装）
透传给前端的数据契约——Web 各展示界面据此渲染来源徽标。
未加载目录（data/skills 下尚未 reload 的条目）按用户安装处理。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.skills.loader import Skill
import web.api as _web_api  # noqa: F401  # 必须先于 web.routers.skills 导入，避免循环导入
from web.routers.skills import router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _mk_skill(name: str, origin: str, source_path: str) -> Skill:
    return Skill(
        name=name,
        description=f"{name} 描述",
        body="# Body\n",
        source_path=source_path,
        origin=origin,
    )


def _fake_engine(skills: list[Skill]):
    mgr = MagicMock()
    mgr.list_all.return_value = skills
    mgr.get = lambda name: next((s for s in skills if s.name == name), None)
    agent = MagicMock()
    agent.skill_manager = mgr
    app = MagicMock()
    app.llm_agent = agent
    return app


class TestListSkillsOrigin:
    def test_loaded_skills_carry_origin(self, client, tmp_path):
        """已加载技能按 origin 透出：src/skills → builtin，data/skills → user。"""
        skills = [
            _mk_skill("demo-builtin", "builtin", "/repo/src/skills/demo-builtin/SKILL.md"),
            _mk_skill("demo-user", "user", "/repo/data/skills/demo-user/SKILL.md"),
        ]
        empty_skills_dir = tmp_path / "empty-skills"
        empty_skills_dir.mkdir()
        with patch("web.api.get_app_instance", return_value=_fake_engine(skills)), \
             patch("web.routers.skills.data_path", return_value=empty_skills_dir):
            resp = client.get("/api/skills")
        assert resp.status_code == 200
        by_name = {s["name"]: s for s in resp.json()["skills"]}
        assert by_name["demo-builtin"]["origin"] == "builtin"
        assert by_name["demo-user"]["origin"] == "user"

    def test_unloaded_entry_is_user(self, client, tmp_path):
        """data/skills 下未加载目录 → origin=user + unloaded=True。"""
        data_skills = tmp_path / "skills"
        (data_skills / "pending-install").mkdir(parents=True)
        with patch("web.api.get_app_instance", return_value=_fake_engine([])), \
             patch("web.routers.skills.data_path", return_value=data_skills):
            resp = client.get("/api/skills")
        assert resp.status_code == 200
        skills = resp.json()["skills"]
        assert len(skills) == 1
        assert skills[0]["name"] == "pending-install"
        assert skills[0]["unloaded"] is True
        assert skills[0]["origin"] == "user"


class TestSkillDetailOrigin:
    def test_detail_carries_origin(self, client, tmp_path):
        """技能详情 API 透出 origin（详情弹窗标题徽标用）。"""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: demo\n---\n\n# Body\n", encoding="utf-8")
        skill = _mk_skill("demo", "builtin", str(skill_md))
        with patch("web.api.get_app_instance", return_value=_fake_engine([skill])):
            resp = client.get("/api/skills/demo")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "demo"
        assert data["origin"] == "builtin"
        assert "raw_content" in data
