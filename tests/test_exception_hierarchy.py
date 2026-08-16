"""异常体系继承收敛守护测试（T-B1）。

验证：
1. ``src.im_adapter.errors`` 全套 IM 异常都正确并入 ``LinkoraError`` 体系；
2. ``src.llm.exceptions`` 的两个 LLM 异常都继承 ``LLMError``，且彼此平级
   （守住「限频不回复」语义——``LLMRateLimitExhaustedError`` 不是 ``LLMProcessingError``）；
3. 反冲突守护：``src.im_adapter.errors`` 与 ``src.exceptions`` 的类名交集只允许是
   ``{"IMAdapterError"}``，防 wecom.py 那类「同名不同族」事故复发；
4. 构造行为不变：``str(IMAdapterError("网络超时")) == "网络超时"``，
   ``LLMProcessingError`` 的 ``str``/``args`` 形状与裸 ``Exception`` 一致。
"""
from __future__ import annotations

import src.exceptions as exc_mod
import src.im_adapter.errors as im_errors
from src.exceptions import LinkoraError, LLMError
from src.llm.exceptions import LLMProcessingError, LLMRateLimitExhaustedError


def _defined_classes(module):
    """取模块内「真正定义」的类名集合（排除从别处导入进来的类）。"""
    return {
        name
        for name, obj in module.__dict__.items()
        if isinstance(obj, type) and obj.__module__ == module.__name__
    }


def test_im_adapter_error_is_linkora_error():
    assert issubclass(im_errors.IMAdapterError, LinkoraError)


def test_im_adapter_subclasses_are_linkora_errors():
    for cls in (
        im_errors.IMAdapterRetryableError,
        im_errors.IMAdapterNonRetryableError,
        im_errors.IMAdapterPermissionError,
        im_errors.IMAdapterResourceNotFoundError,
        im_errors.IMAdapterRateLimitError,
        im_errors.IMAdapterUnsupportedTypeError,
        im_errors.IMAdapterShutdownError,
    ):
        assert issubclass(cls, LinkoraError)


def test_llm_errors_inherit_llm_error():
    assert issubclass(LLMProcessingError, LLMError)
    assert issubclass(LLMRateLimitExhaustedError, LLMError)


def test_rate_limit_not_processing_error():
    # 守住「限频不回复」语义：限频异常不能落入 main 的
    # isinstance(e, LLMProcessingError) 通用兜底分支（否则会套 default_fallback 回复）。
    assert not issubclass(LLMRateLimitExhaustedError, LLMProcessingError)
    assert not isinstance(LLMRateLimitExhaustedError("x"), LLMProcessingError)


def test_no_duplicate_im_class_names_between_modules():
    # 反冲突守护：同名类只允许在族根 IMAdapterError 上交集，
    # 杜绝「从 src.exceptions 导入一个永不 raise 的同名类」导致 except 静默失效。
    conflicts = _defined_classes(exc_mod) & _defined_classes(im_errors)
    assert conflicts == {"IMAdapterError"}


def test_im_adapter_error_str_behaves_like_exception():
    err = im_errors.IMAdapterError("网络超时")
    assert str(err) == "网络超时"


def test_llm_processing_error_str_and_args_unchanged():
    err = LLMProcessingError("推理失败", stage="tool_exec")
    # __str__ 行为不变
    assert str(err) == "推理失败"
    assert err.stage == "tool_exec"
    # 单参数下 e.args 形状与裸 Exception 一致（(message,)），
    # 继承 LinkoraError 后不引入第二个位置参数 / 不改写 args。
    assert err.args == ("推理失败",)
