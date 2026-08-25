"""文档站点生成器测试。

验证文档生成脚本能正常运行并返回正确结果。
"""
from __future__ import annotations

import tempfile
import os
from scripts.gen_site import generate_api_docs, generate_test_docs, generate_changelog_docs, generate_html_site


class TestDocGeneration:
    """文档生成测试。"""

    def test_generate_api_docs(self):
        """验证 API 文档生成。"""
        docs = generate_api_docs()
        assert "classes" in docs
        assert "functions" in docs
        assert "total_classes" in docs
        assert "total_functions" in docs
        assert docs["total_classes"] > 0

    def test_generate_test_docs(self):
        """验证测试文档生成。"""
        docs = generate_test_docs()
        assert "files" in docs
        assert "total_files" in docs
        assert "total_tests" in docs
        assert docs["total_files"] > 0

    def test_generate_changelog_docs(self):
        """验证变更历史生成。"""
        docs = generate_changelog_docs()
        assert isinstance(docs, list)
        # 应该至少有一个版本
        assert len(docs) > 0

    def test_generate_html_site(self):
        """验证 HTML 站点生成。"""
        api_docs = {"classes": [], "functions": [], "total_classes": 0, "total_functions": 0}
        test_docs = {"files": [], "total_files": 0, "total_tests": 0}
        changelog_docs = []

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = generate_html_site(api_docs, test_docs, changelog_docs, tmpdir)
            assert os.path.exists(output_path)
            assert output_path.endswith("index.html")

            # 检查文件内容
            with open(output_path, "r") as f:
                content = f.read()
                assert "<!DOCTYPE html>" in content
                assert "Linkora 文档站点" in content


class TestDocGenerationIntegration:
    """文档生成集成测试。"""

    def test_full_pipeline(self):
        """验证完整文档生成流程。"""
        api_docs = generate_api_docs()
        test_docs = generate_test_docs()
        changelog_docs = generate_changelog_docs()

        assert api_docs["total_classes"] > 0
        assert test_docs["total_files"] > 0
        assert len(changelog_docs) > 0

        # 验证数据一致性
        assert api_docs["total_classes"] == len(api_docs["classes"])
        assert test_docs["total_tests"] == sum(f["tests"] for f in test_docs["files"])
