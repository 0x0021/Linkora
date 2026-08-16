# config_models.py 拆分实施指南

## 拆分方案

### 目标结构
```
src/
├── config_models/
│   ├── __init__.py      # 统一导出
│   ├── base.py          # 基础模型 (DwsConfig, AdapterOverrideConfig, PollerConfig)
│   ├── platform.py      # 平台相关模型 (PlatformRagConfig, PlatformLLMConfig, PlatformToolsConfig)
│   ├── core.py          # 核心配置 (RulesConfig, StorageConfig, LlmConfig, ToolsConfig)
│   └── advanced.py      # 高级配置 (EmbeddingConfig, MemoryConfig, SkillsConfig, etc.)
└── config_models.py     # 保留为向后兼容的导入入口
```

### 模块划分

| 模块 | 类列表 | 行数估算 |
|------|--------|---------|
| `base.py` | DwsConfig, AdapterOverrideConfig, PollerConfig | ~180 |
| `platform.py` | PlatformRagConfig, PlatformLLMConfig, PlatformToolsConfig, PlatformConfig | ~70 |
| `core.py` | RulesConfig, KeywordRule, StorageConfig, LlmConfig, LlmAdvancedConfig, LlmThrottleConfig, ToolsConfig | ~350 |
| `advanced.py` | EmbeddingConfig, MemoryConfig, SkillsConfig, SkillHubConfig, SafetyConfig, DeadLetterConfig, RagConfig, WebConfig, OaApprovalConfig, AppConfig | ~350 |

### 实施步骤

1. **创建模块目录和文件**
   ```bash
   mkdir -p src/config_models
   ```

2. **提取 base.py**
   - 将 DwsConfig, AdapterOverrideConfig, PollerConfig 移到 base.py
   - 保持原有类型注解和文档字符串

3. **提取 platform.py**
   - 将 PlatformRagConfig, PlatformLLMConfig, PlatformToolsConfig, PlatformConfig 移到 platform.py

4. **提取 core.py**
   - 将 RulesConfig, KeywordRule, StorageConfig, LlmConfig 等移到 core.py

5. **提取 advanced.py**
   - 将剩余的高级配置模型移到 advanced.py

6. **更新 __init__.py**
   - 统一从各子模块导入并重新导出

7. **保持向后兼容**
   - 原 config_models.py 改为从新模块导入
   - 确保 `from src.config_models import AppConfig` 仍然有效

8. **运行测试**
   ```bash
   python -m pytest tests/test_*.py -v
   ```

### 注意事项

- 保持原有的 model_validator 逻辑
- 确保循环导入不会发生
- 测试所有导入路径
- 更新相关文档

## 优先级

1. **config_models.py 拆分** - 高优先级（影响全局）
2. **weather.py 拆分** - 中优先级（独立功能）
3. **其他文件** - 低优先级（耦合度高）

---
*生成日期: 2026-08-14*
