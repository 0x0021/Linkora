#!/usr/bin/env python3
"""
智能法务 — 查询审查结果（支持轮询）

Usage:
  # 单次查询
  python query_review_result.py --corp-id "xxx" --review-type "AI_REVIEW" --task-id "taskId123"

  # 轮询模式（默认 30s 一次，最长 10 分钟）
  python query_review_result.py --corp-id "xxx" --review-type "AI_REVIEW" --task-id "taskId123" --poll

接口说明:
  POST /common/queryReviewResult (application/json)
  参数:
    - dingCorpId:   企业CorpId (String, 必填)
    - reviewType:   审查方式 (String, 必填)
                    AI_REVIEW: AI审查
                    HUMAN_RECHECK: AI审查+人工复审
                    HUMAN_REVIEW: 人工法务审查
    - taskId:       审查任务ID (String, 必填)
  返回:
    DingOpenResult<IntelligentLegalContractReviewDetailClientResponse> JSON
"""

import argparse
import json
import os
import sys
import time

import requests

DEFAULT_BASE_URL = "https://trip.dingtalk.com"

_ALLOWED_BASE_HOSTS = ("trip.dingtalk.com",)


def _validate_base_url(base_url: str) -> str:
    """SSRF 防护：contract-review 仅允许访问钉钉官方服务。

    拒绝非 https scheme、非钉钉域名、或解析到私网/回环/链路本地的地址，
    防止 --base-url 被注入指向内网/云元数据（169.254.169.254）。
    """
    import ipaddress
    import socket
    import sys
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    if parsed.scheme != "https":
        sys.exit(f"[安全] base-url 必须为 https: {base_url}")
    if parsed.hostname not in _ALLOWED_BASE_HOSTS:
        sys.exit(f"[安全] 拒绝非钉钉域名 base-url: {parsed.hostname}")
    try:
        for info in socket.getaddrinfo(parsed.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global or ip.is_multicast:
                sys.exit(f"[安全] base-url 解析到非公网地址（SSRF 防护）: {ip}")
    except socket.gaierror:
        sys.exit(f"[安全] base-url 解析失败: {base_url}")
    return base_url
POLL_INTERVAL = 30
POLL_MAX_DURATION_SECONDS = 10 * 60  # 10 分钟
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "ABOLISH"}

# 本技能标准产出：审查结果 JSON 写入 output 目录，文件名与用户上传文件对应，供流程中模型识别并生成报告
SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(SKILL_ROOT, "output")
DEFAULT_OUTPUT_JSON = os.path.join(OUTPUT_DIR, "review_result.json")


def _safe_basename(file_name: str) -> str:
    """从用户上传文件名得到安全的基础名（去扩展名、去路径、非法字符替换为下划线）。"""
    base = os.path.splitext(os.path.basename(file_name.strip()))[0]
    # 保留中英文、数字、点、下划线、横线
    safe = "".join(
        c if (c.isalnum() or c in "._-" or "\u4e00" <= c <= "\u9fff") else "_"
        for c in base
    )
    return safe.strip("_") or "review_result"


def _post_json(url: str, payload: dict, timeout: int = 60, silent: bool = False) -> dict:
    """
    发送 POST JSON 请求并返回响应

    :param url: 请求地址
    :param payload: 请求体
    :param timeout: 超时时间（秒）
    :param silent: 为 True 时不打印响应，仅返回
    :return: 响应 JSON
    """
    headers = {"Content-Type": "application/json"}
    response = requests.post(url, json=payload, headers=headers, timeout=timeout)

    if response.status_code != 200:
        print(f"请求失败, HTTP状态码: {response.status_code}", file=sys.stderr)
        print(f"响应内容: {response.text}", file=sys.stderr)
        sys.exit(1)

    result = response.json()
    if not silent:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def _get_status(result: dict) -> str:
    """从接口返回中解析审查状态。支持 result.result.status 或 result.status。"""
    inner = result.get("result", result)
    if isinstance(inner, dict):
        return inner.get("status", "") or ""
    return ""


def query_review_result_once(args) -> dict:
    """单次查询审查结果"""
    url = f"{args.base_url.rstrip('/')}/common/queryReviewResult"
    payload = {
        "dingCorpId": args.corp_id,
        "reviewType": args.review_type,
        "taskId": args.task_id,
    }
    return _post_json(url, payload, silent=True)


def _write_output_json(result: dict, output_path: str) -> None:
    """将接口返回的完整数据写入技能标准产出 JSON 文件。"""
    out_dir = os.path.dirname(output_path)
    os.makedirs(out_dir, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"审查结果已写入: {output_path}", file=sys.stderr)


def query_review_result_poll(args) -> dict:
    """轮询查询审查结果：每 30 秒一次，最长 10 分钟。"""
    max_attempts = POLL_MAX_DURATION_SECONDS // POLL_INTERVAL  # 20 次
    for attempt in range(1, max_attempts + 1):
        result = query_review_result_once(args)
        status = _get_status(result)
        print(f"[{attempt}/{max_attempts}] status={status}", file=sys.stderr)

        if status in TERMINAL_STATUSES:
            _write_output_json(result, args.output)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return result

        if attempt < max_attempts:
            time.sleep(POLL_INTERVAL)

    print("轮询超时（10 分钟），返回最后一次结果", file=sys.stderr)
    last = query_review_result_once(args)
    _write_output_json(last, args.output)
    print(json.dumps(last, ensure_ascii=False, indent=2))
    return last


def main():
    parser = argparse.ArgumentParser(
        description="智能法务 — 查询审查结果",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"服务基础地址，默认: {DEFAULT_BASE_URL}",
    )
    parser.add_argument("--corp-id", required=True, help="客户企业ID (dingCorpId)")
    parser.add_argument(
        "--review-type",
        required=True,
        help="审查方式: AI_REVIEW(AI审查) / HUMAN_RECHECK(AI审查+人工复审) / HUMAN_REVIEW(人工法务审查)",
    )
    parser.add_argument("--task-id", required=True, help="审查任务ID")
    parser.add_argument(
        "--poll",
        action="store_true",
        help="启用轮询：每 30 秒查询一次，最长 10 分钟",
    )
    parser.add_argument(
        "--file-name",
        default=None,
        help="用户上传的合同文件名（如 Step 2 返回的 fileName），用于生成产出文件名 output/<文件名基础>_review_result.json，便于识别是哪份合同的报告",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="审查结果 JSON 输出路径；未传时若传了 --file-name 则用 output/<文件名基础>_review_result.json，否则用 output/review_result.json",
    )

    args = parser.parse_args()
    args.base_url = _validate_base_url(args.base_url)

    if args.output:
        args.output = os.path.abspath(args.output)
    elif args.file_name:
        base = _safe_basename(args.file_name)
        args.output = os.path.join(OUTPUT_DIR, f"{base}_review_result.json")
    else:
        args.output = DEFAULT_OUTPUT_JSON

    if args.poll:
        query_review_result_poll(args)
    else:
        result = query_review_result_once(args)
        _write_output_json(result, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
