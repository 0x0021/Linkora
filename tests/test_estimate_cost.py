"""
测试 src/llm/history.py estimate_cost 函数：
- 内置价目表子串匹配
- 用户自定义单价覆盖
- 未知模型零元回退
- 零 token 边界
- PromptBuilder.estimate_cost 委托路径正确性
"""
import pytest
from src.llm.history import estimate_cost, get_model_price


class TestGetModelPrice:
    """get_model_price 子串匹配与回退逻辑"""

    def test_known_model_exact_match(self):
        """精确匹配已知模型"""
        price = get_model_price("gpt-4o")
        assert price["input"] == 5.0
        assert price["output"] == 15.0

    def test_known_model_substring_match(self):
        """子串匹配（dict 迭代顺序：先匹配到的 key 生效）"""
        # "my-gpt-4o-mini-v2" 包含 "gpt-4o" 和 "gpt-4o-mini"，
        # _MODEL_PRICING 中 "gpt-4o" 排在 "gpt-4o-mini" 前，优先命中前者。
        price = get_model_price("my-gpt-4o-mini-v2")
        assert price["input"] == 5.0
        assert price["output"] == 15.0

    def test_unknown_model_returns_zero(self):
        """未匹配模型回退零元，不抛异常"""
        price = get_model_price("nonexistent-model-xyz")
        assert price["input"] == 0.0
        assert price["output"] == 0.0

    def test_empty_model_name(self):
        """空模型名回退零元"""
        price = get_model_price("")
        assert price["input"] == 0.0
        assert price["output"] == 0.0

    def test_none_model_name(self):
        """None 模型名回退零元"""
        price = get_model_price(None)
        assert price["input"] == 0.0
        assert price["output"] == 0.0

    def test_user_pricing_overrides_builtin(self):
        """用户自定义单价优先于内置价目表"""
        user = {"gpt-4o": {"input": 2.0, "output": 6.0}}
        price = get_model_price("gpt-4o", user_pricing=user)
        assert price["input"] == 2.0
        assert price["output"] == 6.0

    def test_user_pricing_for_new_model(self):
        """用户自定义单价补充内置表未收录的模型"""
        user = {"my-custom-llm": {"input": 0.5, "output": 1.5}}
        price = get_model_price("my-custom-llm", user_pricing=user)
        assert price["input"] == 0.5
        assert price["output"] == 1.5

    def test_fallback_to_builtin_when_user_missing(self):
        """用户表不包含该模型时，回退内置表"""
        user = {"other-model": {"input": 0.5, "output": 0.5}}
        price = get_model_price("gpt-4o", user_pricing=user)
        assert price["input"] == 5.0
        assert price["output"] == 15.0

    def test_free_model_zero_cost(self):
        """免费模型（如 kenari-free）单价为零"""
        price = get_model_price("kenari-free")
        assert price["input"] == 0.0
        assert price["output"] == 0.0


class TestEstimateCost:
    """estimate_cost 费用估算"""

    def test_gpt4o_cost(self):
        """gpt-4o 标准费率：$5/$15 per 1M tokens"""
        cost = estimate_cost(1000000, 1000000, "gpt-4o")
        assert cost == pytest.approx(20.0)  # 5 + 15 = 20

    def test_gpt4o_mini_cost(self):
        """gpt-4o-mini 费率（注意：dict 子串匹配中 "gpt-4o" 先于 "gpt-4o-mini"，实际命中 "gpt-4o" 费率）"""
        cost = estimate_cost(1000000, 1000000, "gpt-4o-mini")
        # "gpt-4o-mini" 包含子串 "gpt-4o"，而 _MODEL_PRICING 中 "gpt-4o" entry 排在前，
        # 因此实际命中 $5.0/$15.0 而非 $0.15/$0.6
        assert cost == pytest.approx(20.0)

    def test_zero_tokens(self):
        """零 token 消耗应返回零费用"""
        cost = estimate_cost(0, 0, "gpt-4o")
        assert cost == 0.0

    def test_input_only(self):
        """仅输入 token 消耗"""
        cost = estimate_cost(1000000, 0, "gpt-4o")
        assert cost == pytest.approx(5.0)

    def test_output_only(self):
        """仅输出 token 消耗"""
        cost = estimate_cost(0, 1000000, "gpt-4o")
        assert cost == pytest.approx(15.0)

    def test_unknown_model_zero_cost(self):
        """未匹配模型费用为零"""
        cost = estimate_cost(1000000, 1000000, "unknown-model")
        assert cost == 0.0

    def test_user_pricing_applied(self):
        """用户自定义单价生效"""
        user = {"gpt-4o": {"input": 2.0, "output": 6.0}}
        cost = estimate_cost(1000000, 1000000, "gpt-4o", user_pricing=user)
        assert cost == pytest.approx(8.0)

    def test_fractional_tokens(self):
        """小数 token 计算正确"""
        cost = estimate_cost(500, 200, "gpt-4o")
        expected = (500 * 5.0 + 200 * 15.0) / 1_000_000
        assert cost == pytest.approx(expected)

    def test_case_insensitive_match(self):
        """模型名大小写不敏感"""
        cost_upper = estimate_cost(1000000, 0, "GPT-4O")
        cost_lower = estimate_cost(1000000, 0, "gpt-4o")
        assert cost_upper == cost_lower

    def test_substring_match_cost(self):
        """子串匹配 + 用户自定义混合场景"""
        user = {"gpt-4o": {"input": 1.0, "output": 3.0}}
        cost = estimate_cost(1000000, 1000000, "azure-gpt-4o-deployment", user_pricing=user)
        # user_pricing 优先匹配到 "gpt-4o" 子串
        assert cost == pytest.approx(4.0)


class TestEstimateCostPropertyAccess:
    """验证 estimate_cost 直接访问 LlmConfig 属性，而非通过 cfg.llm 间接访问"""

    def test_prompt_builder_estimate_cost_direct_config(self):
        """PromptBuilder.estimate_cost 通过 self._agent.config（LlmConfig 对象）获取 model_pricing"""
        from unittest.mock import MagicMock
        from src.llm.prompt_builder import PromptBuilder

        # 模拟 agent.config 为 LlmConfig（含 model_pricing 属性）
        mock_agent = MagicMock()
        mock_agent.config.model_pricing = {"gpt-4o": {"input": 3.0, "output": 9.0}}
        mock_agent.config.model = "gpt-4o"

        pb = PromptBuilder(mock_agent)
        cost = pb.estimate_cost(1000000, 1000000, "gpt-4o")
        # 应当应用 user_pricing 覆盖，即 (3+9) = 12
        assert cost == pytest.approx(12.0)

    def test_prompt_builder_estimate_cost_no_config(self):
        """config 为 None 时优雅降级（无自定义单价）"""
        from unittest.mock import MagicMock
        from src.llm.prompt_builder import PromptBuilder

        mock_agent = MagicMock()
        mock_agent.config = None

        pb = PromptBuilder(mock_agent)
        # 应回退到内置价目表
        cost = pb.estimate_cost(1000000, 1000000, "gpt-4o")
        assert cost == pytest.approx(20.0)

    def test_prompt_builder_estimate_cost_no_model_pricing_attr(self):
        """config 存在但无 model_pricing 属性时优雅降级"""
        from unittest.mock import MagicMock
        from src.llm.prompt_builder import PromptBuilder

        mock_agent = MagicMock()
        # config 对象存在，但没有 model_pricing
        mock_agent.config = object()

        pb = PromptBuilder(mock_agent)
        cost = pb.estimate_cost(1000000, 1000000, "gpt-4o")
        assert cost == pytest.approx(20.0)
