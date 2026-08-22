#!/usr/bin/env python3
"""
文件上传 — 上传文件到客户的钉盘空间。

Usage:
  python upload_file_to_dingpan.py \
    --file "/path/to/contract.docx" \
    --corp-id "dingxxxxxxxxxxxxxxxx" \
    --staff-id "012345" \
    [--base-url "https://trip.dingtalk.com"]

Required env vars: 无（也可通过环境变量设置 DINGTALK_CORP_ID）

接口说明:
  POST /common/uploadFile (multipart/form-data)
  参数:
    - file:    上传的文件 (MultipartFile)
    - corpId:  客户企业ID (String)
    - staffId: 员工ID (String)
  返回:
    LawFileDTO JSON，包含 fileName, fileType, fileSize, spaceId, fileId 等字段
"""

import argparse
import json
import os
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


def upload_file_to_dingpan(
    file_path: str,
    corp_id: str,
    staff_id: str,
    base_url: str = DEFAULT_BASE_URL,
) -> dict:
    """
    上传文件到客户的钉盘空间

    :param file_path: 本地文件路径
    :param corp_id: 客户企业ID
    :param staff_id: 员工ID
    :param base_url: 服务基础地址
    :return: 接口返回的 LawFileDTO JSON
    """
    url = f"{base_url.rstrip('/')}/common/uploadFile"

    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    file_name = os.path.basename(file_path)

    with open(file_path, "rb") as f:
        files = {
            "file": (file_name, f),
        }
        data = {
            "corpId": corp_id,
            "staffId": staff_id,
        }

        response = requests.post(url, files=files, data=data, timeout=60)

    if response.status_code != 200:
        print(f"请求失败, HTTP状态码: {response.status_code}", file=sys.stderr)
        print(f"响应内容: {response.text}", file=sys.stderr)
        sys.exit(1)

    return response.json()


def main():
    parser = argparse.ArgumentParser(description="上传文件到客户的钉盘空间")
    parser.add_argument("--file", required=True, help="本地文件路径")
    parser.add_argument(
        "--corp-id",
        default=os.environ.get("DINGTALK_CORP_ID", ""),
        help="客户企业ID（也可通过环境变量 DINGTALK_CORP_ID 设置）",
    )
    parser.add_argument("--staff-id", required=True, help="员工ID")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("UPLOAD_BASE_URL", DEFAULT_BASE_URL),
        help=f"服务基础地址，默认: {DEFAULT_BASE_URL}",
    )
    args = parser.parse_args()
    args.base_url = _validate_base_url(args.base_url)

    if not args.corp_id:
        print("错误: 必须通过 --corp-id 参数或 DINGTALK_CORP_ID 环境变量指定企业ID", file=sys.stderr)
        sys.exit(1)

    result = upload_file_to_dingpan(
        file_path=args.file,
        corp_id=args.corp_id,
        staff_id=args.staff_id,
        base_url=args.base_url,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
