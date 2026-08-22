#!/usr/bin/env python3
"""
智能法务 — 创建合同审查

Usage:
  python create_contract_review.py \
    --corp-id "dingxxxxxxxxxxxxxxxx" \
    --user-id "012345" \
    --review-type "AI_REVIEW" \
    --review-position "甲方" \
    --review-result-type "CONTRACT_REVIEW" \
    --file-name "合同.docx" \
    --file-id "fileId123" \
    --file-size "1024" \
    --space-id "spaceId123" \
    --file-type "docx"

接口说明:
  POST /common/createContractReview (application/json)
  参数:
    - dingCorpId:          企业CorpId (String, 必填)
    - originatorUserId:    发起人用户ID (String, 必填)
    - reviewType:          审查方式 (String, 必填)
                           AI_REVIEW: AI审查
                           HUMAN_RECHECK: AI审查+人工复审
                           HUMAN_REVIEW: 人工法务审查
    - reviewPosition:      审查立场 (String, 必填)
    - reviewResultType:    审查交付形式 (String, 必填)
                           CONTRACT_REVIEW: 合同审查
                           RISK_STATEMENT: 风险提示
    - fileInfo:            文件信息 (Object, 必填)
    - companyList:         审查立场对应公司名称列表 (List<String>, 可选)
    - customReviewRules:   自定义审查规则 (String, 可选)
  返回:
    DingOpenResult<IntelligentContractReviewClientResponse> JSON
"""

import argparse
import json
import sys

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


def _post_json(url: str, payload: dict, timeout: int = 60) -> dict:
    """
    发送 POST JSON 请求并返回响应

    :param url: 请求地址
    :param payload: 请求体
    :param timeout: 超时时间（秒）
    :return: 响应 JSON
    """
    headers = {"Content-Type": "application/json"}
    response = requests.post(url, json=payload, headers=headers, timeout=timeout)

    if response.status_code != 200:
        print(f"请求失败, HTTP状态码: {response.status_code}", file=sys.stderr)
        print(f"响应内容: {response.text}", file=sys.stderr)
        sys.exit(1)

    result = response.json()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def create_contract_review(args):
    """创建合同审查"""
    url = f"{args.base_url.rstrip('/')}/common/createContractReview"
    payload = {
        "dingCorpId": args.corp_id,
        "originatorUserId": args.user_id,
        "reviewType": args.review_type,
        "reviewPosition": args.review_position,
        "reviewResultType": args.review_result_type,
        "fileInfo": {
            "fileName": args.file_name,
            "fileId": args.file_id,
            "fileSize": args.file_size,
            "spaceId": args.space_id,
            "fileType": args.file_type,
        },
        "source":"OPEN_CLAW"
    }
    if args.company_list:
        payload["companyList"] = args.company_list.split(",")
    if args.custom_review_rules:
        payload["customReviewRules"] = args.custom_review_rules
    _post_json(url, payload)


def main():
    parser = argparse.ArgumentParser(
        description="智能法务 — 创建合同审查",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"服务基础地址，默认: {DEFAULT_BASE_URL}",
    )
    parser.add_argument("--corp-id", required=True, help="客户企业ID (dingCorpId)")
    parser.add_argument("--user-id", required=True, help="发起人用户ID (originatorUserId)")
    parser.add_argument(
        "--review-type",
        required=True,
        help="审查方式: AI_REVIEW(AI审查) / HUMAN_RECHECK(AI审查+人工复审) / HUMAN_REVIEW(人工法务审查)",
    )
    parser.add_argument("--review-position", required=True, help="审查立场")
    parser.add_argument(
        "--review-result-type",
        required=True,
        help="审查交付形式: CONTRACT_REVIEW(合同审查) / RISK_STATEMENT(风险提示)",
    )
    parser.add_argument("--file-name", required=True, help="文件名")
    parser.add_argument("--file-id", required=True, help="文件ID")
    parser.add_argument("--file-size", required=True, help="文件大小（字节）")
    parser.add_argument("--space-id", required=True, help="钉盘空间ID")
    parser.add_argument("--file-type", required=True, help="文件类型（如 docx, pdf）")
    parser.add_argument("--company-list", default=None, help="审查立场对应公司名称，多个用逗号分隔")
    parser.add_argument("--custom-review-rules", default=None, help="自定义审查规则")

    args = parser.parse_args()
    args.base_url = _validate_base_url(args.base_url)
    create_contract_review(args)


if __name__ == "__main__":
    main()
