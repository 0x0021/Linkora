"""
Linkora 文档站点生成器

用途：
1. 从源码注释自动生成 API 文档
2. 从测试文件自动生成测试文档
3. 从 CHANGELOG 自动生成版本历史

用法：
    python scripts/gen_site.py              # 生成完整文档站点
    python scripts/gen_site.py --api        # 仅生成 API 文档
    python scripts/gen_site.py --tests      # 仅生成测试文档
    python scripts/gen_site.py --changelog  # 仅生成变更历史
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ClassDoc:
    """类文档。"""
    name: str
    module: str
    description: str
    methods: list[dict] = None
    attributes: list[dict] = None

    def __post_init__(self):
        if self.methods is None:
            self.methods = []
        if self.attributes is None:
            self.attributes = []


@dataclass
class FunctionDoc:
    """函数文档。"""
    name: str
    module: str
    description: str
    params: list[dict] = None
    returns: str = ""

    def __post_init__(self):
        if self.params is None:
            self.params = []


def extract_class_docs(filepath: str) -> list[ClassDoc]:
    """从 Python 文件提取类文档。"""
    docs = []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())

        module_name = filepath.replace("/", ".").replace(".py", "")

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                docstring = ast.get_docstring(node)
                class_doc = ClassDoc(
                    name=node.name,
                    module=module_name,
                    description=docstring or "",
                )

                # 提取方法
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        method_docstring = ast.get_docstring(item)
                        params = []
                        for arg in item.args.args:
                            if arg.arg != "self":
                                params.append({
                                    "name": arg.arg,
                                    "type": getattr(arg, "annotation", None),
                                })
                        class_doc.methods.append({
                            "name": item.name,
                            "description": method_docstring or "",
                            "params": params,
                        })

                docs.append(class_doc)
    except Exception as e:
        print(f"解析 {filepath} 失败：{e}")

    return docs


def extract_function_docs(filepath: str) -> list[FunctionDoc]:
    """从 Python 文件提取函数文档。"""
    docs = []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())

        module_name = filepath.replace("/", ".").replace(".py", "")

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.FunctionDef):
                docstring = ast.get_docstring(node)
                params = []
                for arg in node.args.args:
                    params.append({
                        "name": arg.arg,
                        "type": getattr(arg, "annotation", None),
                    })
                docs.append(FunctionDoc(
                    name=node.name,
                    module=module_name,
                    description=docstring or "",
                    params=params,
                ))
    except Exception as e:
        print(f"解析 {filepath} 失败：{e}")

    return docs


def generate_api_docs(src_dir: str = "src") -> dict:
    """生成 API 文档。"""
    all_classes = []
    all_functions = []

    for root, dirs, files in os.walk(src_dir):
        # 跳过 __pycache__
        dirs[:] = [d for d in dirs if d != "__pycache__"]

        for file in files:
            if file.endswith(".py") and not file.startswith("__"):
                filepath = os.path.join(root, file)
                relpath = filepath.replace(src_dir + "/", "")

                all_classes.extend(extract_class_docs(filepath))
                all_functions.extend(extract_function_docs(filepath))

    return {
        "classes": all_classes,
        "functions": all_functions,
        "total_classes": len(all_classes),
        "total_functions": len(all_functions),
    }


def generate_test_docs(tests_dir: str = "tests") -> dict:
    """生成测试文档。"""
    test_files = []
    test_count = 0

    for root, dirs, files in os.walk(tests_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]

        for file in files:
            if file.startswith("test_") and file.endswith(".py"):
                filepath = os.path.join(root, file)
                relpath = filepath.replace(tests_dir + "/", "")

                # 统计测试用例数
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        content = f.read()
                        count = content.count("def test_")
                        test_count += count
                except:
                    count = 0

                test_files.append({
                    "path": relpath,
                    "tests": count,
                })

    return {
        "files": test_files,
        "total_files": len(test_files),
        "total_tests": test_count,
    }


def generate_changelog_docs(changelog_file: str = "docs/CHANGELOG.md") -> list:
    """生成变更历史文档。"""
    versions = []
    try:
        with open(changelog_file, "r", encoding="utf-8") as f:
            content = f.read()

        # 解析版本
        pattern = r"##\s+v?(\d+\.\d+\.\d+)\s*\((\d{4}-\d{2}-\d{2})\)"
        matches = re.findall(pattern, content)

        for version, date in matches:
            versions.append({
                "version": version,
                "date": date,
            })
    except Exception as e:
        print(f"解析 CHANGELOG 失败：{e}")

    return versions


def generate_html_site(api_docs: dict, test_docs: dict, changelog_docs: list, output_dir: str = "docs/site") -> str:
    """生成 HTML 文档站点。"""
    os.makedirs(output_dir, exist_ok=True)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Linkora 文档站点</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; padding: 20px; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        h1 {{ color: #333; }}
        h2 {{ color: #666; border-bottom: 2px solid #eee; padding-bottom: 10px; }}
        .stats {{ background: #f5f5f5; padding: 20px; border-radius: 8px; margin: 20px 0; }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; }}
        .stat-card {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .stat-value {{ font-size: 32px; font-weight: bold; color: #007acc; }}
        .stat-label {{ color: #666; margin-top: 5px; }}
        table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
        th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #eee; }}
        th {{ background: #f5f5f5; font-weight: 600; }}
        tr:hover {{ background: #f9f9f9; }}
        .nav {{ background: #333; padding: 15px; margin: -20px -20px 20px; }}
        .nav a {{ color: white; text-decoration: none; margin-right: 20px; }}
        .nav a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="nav">
            <a href="#overview">概览</a>
            <a href="#api">API 文档</a>
            <a href="#tests">测试文档</a>
            <a href="#changelog">变更历史</a>
        </div>

        <h1>Linkora 文档站点</h1>
        <p>自动生成文档站点，最后更新：{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>

        <div class="stats" id="overview">
            <h2>项目统计</h2>
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-value">{api_docs['total_classes']}</div>
                    <div class="stat-label">API 类</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{api_docs['total_functions']}</div>
                    <div class="stat-label">API 函数</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{test_docs['total_files']}</div>
                    <div class="stat-label">测试文件</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{test_docs['total_tests']}</div>
                    <div class="stat-label">测试用例</div>
                </div>
            </div>
        </div>

        <div id="api">
            <h2>API 文档</h2>
            <table>
                <tr>
                    <th>类名</th>
                    <th>模块</th>
                    <th>方法数</th>
                    <th>描述</th>
                </tr>
"""

    for cls in api_docs["classes"][:20]:  # 限制显示前 20 个
        html += f"""                <tr>
                    <td><code>{cls.name}</code></td>
                    <td><code>{cls.module}</code></td>
                    <td>{len(cls.methods)}</td>
                    <td>{cls.description[:80]}...</td>
                </tr>
"""

    html += f"""            </table>
        </div>

        <div id="tests">
            <h2>测试文档</h2>
            <table>
                <tr>
                    <th>测试文件</th>
                    <th>用例数</th>
                </tr>
"""

    for test_file in test_docs["files"][:20]:  # 限制显示前 20 个
        html += f"""                <tr>
                    <td><code>{test_file['path']}</code></td>
                    <td>{test_file['tests']}</td>
                </tr>
"""

    html += f"""            </table>
        </div>

        <div id="changelog">
            <h2>变更历史</h2>
            <table>
                <tr>
                    <th>版本</th>
                    <th>发布日期</th>
                </tr>
"""

    for version in changelog_docs[:10]:  # 限制显示最近 10 个版本
        html += f"""                <tr>
                    <td><code>v{version['version']}</code></td>
                    <td>{version['date']}</td>
                </tr>
"""

    html += """            </table>
        </div>
    </div>
</body>
</html>
"""

    output_path = os.path.join(output_dir, "index.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    return output_path


def main():
    parser = argparse.ArgumentParser(description="Linkora 文档站点生成器")
    parser.add_argument("--api", action="store_true", help="仅生成 API 文档")
    parser.add_argument("--tests", action="store_true", help="仅生成测试文档")
    parser.add_argument("--changelog", action="store_true", help="仅生成变更历史")
    parser.add_argument("--all", action="store_true", help="生成所有文档")
    parser.add_argument("--output", default="docs/site", help="输出目录")
    args = parser.parse_args()

    print("=== Linkora 文档站点生成器 ===\n")

    # 生成 API 文档
    print("[1/3] 生成 API 文档...")
    api_docs = generate_api_docs()
    print(f"  → 发现 {api_docs['total_classes']} 个类，{api_docs['total_functions']} 个函数")

    # 生成测试文档
    print("[2/3] 生成测试文档...")
    test_docs = generate_test_docs()
    print(f"  → 发现 {test_docs['total_files']} 个测试文件，{test_docs['total_tests']} 个测试用例")

    # 生成变更历史
    print("[3/3] 生成变更历史...")
    changelog_docs = generate_changelog_docs()
    print(f"  → 发现 {len(changelog_docs)} 个版本")

    # 生成 HTML 站点
    print("\n生成 HTML 站点...")
    output_path = generate_html_site(api_docs, test_docs, changelog_docs, args.output)
    print(f"  → 已生成：{output_path}")

    print("\n=== 完成 ===")
    print(f"API 类：{api_docs['total_classes']} 个")
    print(f"API 函数：{api_docs['total_functions']} 个")
    print(f"测试文件：{test_docs['total_files']} 个")
    print(f"测试用例：{test_docs['total_tests']} 个")
    print(f"历史版本：{len(changelog_docs)} 个")


if __name__ == "__main__":
    main()
