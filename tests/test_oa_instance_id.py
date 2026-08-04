"""OA 审批实例 ID 提取正则健壮性回归（#6）。

覆盖：标准查询参数 / JSON 引用 / URL 编码 / path 风格 / 大小写 / 下划线风格 /
无匹配 / 子串误匹配守卫 / 过短 ID。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.poller_core_parse import _extract_oa_instance_id, _OA_INSTANCE_ID_RE


def test_standard_query_param():
    assert _extract_oa_instance_id({"url": "https://x/oa?procInstId=12345678"}) == "12345678"


def test_json_quoted_form():
    assert (
        _extract_oa_instance_id({"processInstanceId": "abcd-1234-efgh"})
        == "abcd-1234-efgh"
    )


def test_url_encoded_json_form():
    blob = {"url": "https://oapi.dingtalk.com?%22processInstanceId%22%3A%22xyz789%22"}
    assert _extract_oa_instance_id(blob) == "xyz789"


def test_path_style_with_slash():
    assert (
        _extract_oa_instance_id({"link": "https://x/oa/procInstId/abcdef"})
        == "abcdef"
    )


def test_case_insensitive():
    assert _extract_oa_instance_id({"ProcInstId": "112233"}) == "112233"
    assert _extract_oa_instance_id({"PROCESSINSTANCEID": "445566"}) == "445566"


def test_underscore_style():
    assert (
        _extract_oa_instance_id({"process_instance_id": "99887766"}) == "99887766"
    )


def test_no_match_returns_empty():
    assert _extract_oa_instance_id({"foo": "bar", "name": "task"}) == ""


def test_substring_false_positive_guard():
    # originProcInstId 中 procInstId 作为子串不应被匹配
    assert _extract_oa_instance_id({"originProcInstId": "shouldnotmatch"}) == ""


def test_short_id_below_threshold_returns_empty():
    assert _extract_oa_instance_id({"procInstId": "123"}) == ""


def test_short_numeric_id_above_threshold_captured():
    # 6 位纯数字短编号（老实例）应被捕获
    assert _extract_oa_instance_id({"procInstId": "123456"}) == "123456"


def test_regex_compiled_and_flagged():
    # 确保 (?i) 与前瞻已生效：直接对原始正则验证大小写与子串守卫
    assert _OA_INSTANCE_ID_RE.search("ProcInstId=abc123") is not None
    assert _OA_INSTANCE_ID_RE.search('x="originProcInstId":"nope"') is None
