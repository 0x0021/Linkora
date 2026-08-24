"""LLM 客户端性能基准测试。

用途：
1. 监控 LLM 调用延迟分布
2. 监控 OCR 下载大小限制对性能的影响
3. 量化性能回归

用法：
    python scripts/bench_llm.py              # 运行 LLM 基准测试
    python scripts/bench_ocr.py              # 运行 OCR 下载基准测试
    python scripts/bench_all.py              # 运行所有基准测试
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass
from typing import List


@dataclass
class BenchmarkResult:
    """基准测试结果。"""
    name: str
    iterations: int
    total_time: float
    avg_time: float
    min_time: float
    max_time: float
    p50_time: float
    p95_time: float
    p99_time: float


def bench_llm_call(iterations: int = 10) -> BenchmarkResult:
    """基准测试 LLM 调用延迟。

    注意：这是模拟测试，不实际调用 LLM API。
    实际部署时需要替换为真实 LLM 调用。
    """
    times = []
    for i in range(iterations):
        start = time.perf_counter()
        # 模拟 LLM 调用（实际测试时应替换为真实调用）
        time.sleep(0.1)  # 模拟 100ms 延迟
        end = time.perf_counter()
        times.append(end - start)

    times.sort()
    return BenchmarkResult(
        name="LLM Call",
        iterations=iterations,
        total_time=sum(times),
        avg_time=statistics.mean(times),
        min_time=min(times),
        max_time=max(times),
        p50_time=times[int(len(times) * 0.5)],
        p95_time=times[int(len(times) * 0.95)],
        p99_time=times[int(len(times) * 0.99)],
    )


def bench_ocr_download(size_mb: float = 1.0, iterations: int = 10) -> BenchmarkResult:
    """基准测试 OCR 图片下载性能。

    Args:
        size_mb: 图片大小（MB）
        iterations: 迭代次数
    """
    times = []
    for i in range(iterations):
        start = time.perf_counter()
        # 模拟下载（实际测试时应替换为真实下载）
        # 假设下载速度 10MB/s
        time.sleep(size_mb / 10.0)
        end = time.perf_counter()
        times.append(end - start)

    times.sort()
    return BenchmarkResult(
        name=f"OCR Download ({size_mb}MB)",
        iterations=iterations,
        total_time=sum(times),
        avg_time=statistics.mean(times),
        min_time=min(times),
        max_time=max(times),
        p50_time=times[int(len(times) * 0.5)],
        p95_time=times[int(len(times) * 0.95)],
        p99_time=times[int(len(times) * 0.99)],
    )


def bench_database_query(iterations: int = 100) -> BenchmarkResult:
    """基准测试数据库查询性能。"""
    times = []
    for i in range(iterations):
        start = time.perf_counter()
        # 模拟数据库查询
        time.sleep(0.001)  # 1ms
        end = time.perf_counter()
        times.append(end - start)

    times.sort()
    return BenchmarkResult(
        name="Database Query",
        iterations=iterations,
        total_time=sum(times),
        avg_time=statistics.mean(times),
        min_time=min(times),
        max_time=max(times),
        p50_time=times[int(len(times) * 0.5)],
        p95_time=times[int(len(times) * 0.95)],
        p99_time=times[int(len(times) * 0.99)],
    )


def format_result(result: BenchmarkResult) -> str:
    """格式化基准测试结果。"""
    return f"""
=== {result.name} ===
迭代次数：{result.iterations}
总耗时：{result.total_time:.3f}s
平均耗时：{result.avg_time*1000:.2f}ms
最小耗时：{result.min_time*1000:.2f}ms
最大耗时：{result.max_time*1000:.2f}ms
P50 耗时：{result.p50_time*1000:.2f}ms
P95 耗时：{result.p95_time*1000:.2f}ms
P99 耗时：{result.p99_time*1000:.2f}ms
"""


def main():
    parser = argparse.ArgumentParser(description="Linkora 性能基准测试")
    parser.add_argument("--llm", action="store_true", help="运行 LLM 基准测试")
    parser.add_argument("--ocr", action="store_true", help="运行 OCR 基准测试")
    parser.add_argument("--db", action="store_true", help="运行数据库基准测试")
    parser.add_argument("--all", action="store_true", help="运行所有基准测试")
    parser.add_argument("--iterations", type=int, default=10, help="迭代次数")
    parser.add_argument("--output", type=str, help="输出文件路径（JSON）")
    args = parser.parse_args()

    results = []

    if args.llm or args.all:
        result = bench_llm_call(args.iterations)
        results.append(result)
        print(format_result(result))

    if args.ocr or args.all:
        for size in [0.5, 1.0, 5.0, 10.0]:
            result = bench_ocr_download(size, args.iterations)
            results.append(result)
            print(format_result(result))

    if args.db or args.all:
        result = bench_database_query(args.iterations * 10)
        results.append(result)
        print(format_result(result))

    # 输出 JSON
    if args.output:
        data = []
        for r in results:
            data.append({
                "name": r.name,
                "iterations": r.iterations,
                "total_time": r.total_time,
                "avg_time": r.avg_time,
                "min_time": r.min_time,
                "max_time": r.max_time,
                "p50_time": r.p50_time,
                "p95_time": r.p95_time,
                "p99_time": r.p99_time,
            })
        with open(args.output, "w") as f:
            json.dump(data, f, indent=2)
        print(f"\n结果已保存到：{args.output}")


if __name__ == "__main__":
    main()
