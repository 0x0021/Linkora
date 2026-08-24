"""性能基准测试套件。

验证基准测试脚本能正常运行并返回正确结果。
"""
from __future__ import annotations

import pytest
from scripts.bench_llm import bench_llm_call, bench_ocr_download, bench_database_query


class TestBenchmarkFunctions:
    """基准测试函数验证。"""

    def test_bench_llm_call_returns_result(self):
        """验证 LLM 基准测试返回正确结果。"""
        result = bench_llm_call(iterations=5)
        assert result.name == "LLM Call"
        assert result.iterations == 5
        assert result.avg_time > 0
        assert result.min_time <= result.avg_time <= result.max_time

    def test_bench_ocr_download_returns_result(self):
        """验证 OCR 下载基准测试返回正确结果。"""
        result = bench_ocr_download(size_mb=1.0, iterations=5)
        assert result.name == "OCR Download (1.0MB)"
        assert result.iterations == 5
        assert result.avg_time > 0

    def test_bench_database_query_returns_result(self):
        """验证数据库查询基准测试返回正确结果。"""
        result = bench_database_query(iterations=10)
        assert result.name == "Database Query"
        assert result.iterations == 10
        assert result.avg_time > 0

    def test_ocr_download_time_scales_with_size(self):
        """验证 OCR 下载时间与文件大小成正比。"""
        result_1mb = bench_ocr_download(size_mb=1.0, iterations=3)
        result_5mb = bench_ocr_download(size_mb=5.0, iterations=3)
        # 5MB 应该比 1MB 慢约 5 倍
        assert result_5mb.avg_time > result_1mb.avg_time * 4

    def test_percentiles_are_correct(self):
        """验证百分位数计算正确。"""
        result = bench_llm_call(iterations=10)
        assert result.p50_time <= result.p95_time <= result.p99_time
        assert result.min_time <= result.p50_time
        assert result.p99_time <= result.max_time
