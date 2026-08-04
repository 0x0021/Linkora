#!/usr/bin/env python3
"""临时验证 LLM 密钥是否有效（不写入 config.yaml，纯靠环境变量）。

用法：
  # 验证主服务商（kenari.id）
  LLM_BASE_URL=https://kenari.id/v1 \
  LLM_API_KEY=你的真实key \
  LLM_MODEL=mimo-v2-5:free \
  .venv/bin/python scripts/test_llm_key.py

  # 验证备用服务商（NVIDIA）
  LLM_TEST_MODE=fallback \
  LLM_BASE_URL=https://integrate.api.nvidia.com/v1 \
  LLM_API_KEY=你的NVIDIAkey \
  LLM_MODEL=deepseek-ai/deepseek-v4-flash \
  .venv/bin/python scripts/test_llm_key.py

成功输出：  [OK] <model> 鉴权通过，返回的 finish_reason=stop
失败输出：  [FAIL] <错误摘要>
"""
import os
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()  # 若存在 .env，自动注入环境变量（不影响命令行已 export 的值）
except ImportError:
    pass  # 没有 python-dotenv 也能跑，只要手动 export 了变量

try:
    from openai import OpenAI
except ImportError:
    print("[FAIL] 未安装 openai 库（应在项目 .venv 中）")
    sys.exit(2)


def main() -> int:
    base_url = os.environ.get("LLM_BASE_URL")
    api_key = os.environ.get("LLM_API_KEY")
    model = os.environ.get("LLM_MODEL") or "gpt-4o"
    mode = os.environ.get("LLM_TEST_MODE", "primary")

    if not base_url or not api_key:
        print("[FAIL] 必须设置 LLM_BASE_URL 和 LLM_API_KEY 环境变量")
        return 2

    label = "备用(fallback)" if mode == "fallback" else "主(primary)"
    print(f"测试 {label} 服务商  base_url={base_url}  model={model}  key_len={len(api_key)}")

    if len(api_key) < 16:
        print("  ⚠️  密钥长度过短（<16），很可能是占位符/无效密钥")

    try:
        client = OpenAI(base_url=base_url.rstrip("/"), api_key=api_key)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            timeout=30,
        )
        fr = getattr(resp.choices[0], "finish_reason", None)
        print(f"[OK] {model} 鉴权通过，finish_reason={fr}")
        return 0
    except Exception as e:  # noqa: BLE001
        err = str(e)
        # 截断超长错误，避免泄露响应体
        if len(err) > 300:
            err = err[:300] + "..."
        print(f"[FAIL] {err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
