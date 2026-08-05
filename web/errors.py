"""Web 层安全错误消息工具。

统一收敛「异常信息泄露给客户端」问题（CodeQL py/stack-trace-exposure）：
路由里 ``except Exception as e: raise HTTPException(500, detail=str(e))`` 会把
内部异常文本（可能含堆栈片段、内部路径、第三方库错误）直接回传给前端。

正确做法：真实错误只进服务端日志（含 traceback），返回给客户端的永远是
不暴露内部结构的常量文案。``safe_detail`` 即用于此——无论传入什么异常，
都返回同一句通用文案，从数据流上切断「异常对象 → 响应体」的链路。
"""

from __future__ import annotations

from typing import Optional

# 返回给客户端的通用错误文案（不含任何内部信息）。
SAFE_INTERNAL_ERROR = "请求处理失败，请稍后重试"

# 业务校验失败但不含内部细节时的通用文案。
SAFE_OPERATION_FAILED = "操作失败，请检查输入或稍后重试"


def safe_detail(_exc: Optional[BaseException] = None) -> str:
    """返回安全的错误详情文案。

    故意忽略入参 ``_exc``（仅保留签名以便调用处 ``safe_detail(e)`` 自然书写），
    始终返回常量。这样即便调用方传入异常对象，响应体也不会包含其内部文本，
    CodeQL 的 stack-trace-exposure 数据流因此被阻断。
    """
    return SAFE_INTERNAL_ERROR
