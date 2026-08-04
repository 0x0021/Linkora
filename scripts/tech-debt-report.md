# Linkora 技术债梳理报告

> 分析时间：2026-08-03
> 代码规模：139 个 Python 文件 / 43,911 行代码 / 1,096 次提交（近30天）

---

## 一、高优先级技术债（建议立即处理）

### 1. 全局状态管理混乱
**位置**：`src/shared_state.py`, `src/config.py`, `src/semantic.py`, `src/llm/client.py` 等
**问题**：全局变量 + `global` 关键字散布在 13 个文件中
**风险**：
- 多线程并发时状态竞态（虽然项目用 ContextVar 隔离部分，但未覆盖全部）
- 单测困难，需要大量 monkey-patch
- 重启后状态不一致

**建议方案**：
```python
# 现状
global _config
_config = load_config()

# 改进：使用模块级单例 + ContextVar 组合
from contextlib import contextmanager

class ConfigManager:
    _instance = None
    def __new__(cls): ...
    @contextmanager
    def scope(self): ...
```

**工作量**：中（需测试覆盖）

---

### 2. 异常处理过于宽泛
**位置**：约 40+ 处 `except Exception:` 无具体处理
**典型文件**：
- `src/approval/dingtalk.py`（4处）
- `src/im_adapter/wecom.py`（5处）
- `src/platform/lifecycle.py`（4处）
- `src/utils/logger.py`（6处）

**问题**：
- 静默吞掉关键错误，排查困难
- `# noqa: BLE001` 注释泛滥（超过 20 处），说明规则被滥用

**建议方案**：
1. 区分「可预期异常」和「未知异常」：
   ```python
   try:
       ...
   except (APIError, TimeoutError) as e:
       logger.warning(f"预期内错误: {e}")
   except Exception as e:  # 仅在此处记录并重新抛出
       logger.error(f"未知错误: {e}", exc_info=True)
       raise
   ```
2. 批量清理 `noqa: BLE001`，对每处评估是否需要
3. 为关键路径添加结构化日志

**工作量**：小（逐文件处理，约 2-3 天）

---

### 3. 数据库备份累积
**位置**：`data/backups/` 目录
**问题**：
- 已有 25 个备份文件，总计约 98MB
- 备份频率过高（每 30-60 分钟一次），但清理策略不明确
- 历史对话数据库（93MB）也未做压缩/归档

**建议方案**：
1. 实现备份轮换策略：保留最近 7 天，按天压缩
2. 历史对话数据库超过 30 天自动归档到对象存储或压缩删除
3. 备份清理加入定时任务

**工作量**：小（配置驱动，1-2 天）

---

## 二、中优先级技术债（建议逐步处理）

### 4. 大文件拆分
**位置**：6 个文件超过 1000 行
| 文件 | 行数 | 建议拆分方向 |
|------|------|-------------|
| `llm/style.py` | 1327 | 按规则类别拆分为多个 module |
| `llm/agent.py` | 1271 | 按 agent 职责拆分为 tools/chat/memory |
| `memory/sqlite_store.py` | 1263 | 按表/实体拆分为 repo 类 |
| `dws_adapter.py` | 1253 | 按 API 域拆分为 chat/contact/doc/calendar |
| `config.py` | 1095 | 按配置类型拆分为 models/schemas/validators |
| `intent.py` | 1019 | 按意图域拆分为 domain.* 子模块 |

**建议**：参考 runtime.py 的拆分模式（6 个子模块 + 组合根）

**工作量**：大（需回归测试保障）

---

### 5. IM 适配器重复代码
**位置**：`src/im_adapter/feishu.py` (884行) 与 `src/im_adapter/wecom.py` (762行)
**问题**：
- 两套适配器存在约 40% 重复逻辑
- 消息格式转换、媒体处理、错误处理逻辑高度相似
- `base.py` 和 `base_adapter.py` 存在两套基类

**建议方案**：
1. 合并 `base.py` 和 `base_adapter.py` 为单一基类
2. 提取共享逻辑为 mixin：
   ```python
   class MessageFormatMixin:
       def format_reply(self, text: str) -> dict: ...
   
   class MediaHandlerMixin:
       def download_media(self, url: str) -> bytes: ...
   ```
3. 适配器类继承共享 mixin，仅保留平台差异

**工作量**：中（需充分测试）

---

### 6. 硬编码与魔法值
**位置**：散布在多处
**典型例子**：
- `src/config.py:1019` 的 `max_pages` 默认值
- `src/llm/agent.py` 的硬编码阈值 6
- `src/dws_adapter.py` 的硬编码上限 20 → 50

**建议方案**：
1. 统一收敛到 `Config` 模型
2. 常量定义在 `src/constants.py`
3. 使用 `pydantic.Field(ge=..., le=...)` 做范围校验

**工作量**：小（2-3 天）

---

## 三、低优先级技术债（可暂缓）

### 7. 测试质量提升
**现状**：
- 199 个测试文件，3987 个测试用例
- 覆盖率良好，但缺乏集成测试和性能测试

**建议**：
1. 添加端到端测试（使用 mock 平台 API）
2. 添加负载测试（并发回复场景）
3. 添加混沌测试（网络抖动、LLM 超时）

**工作量**：中（长期投入）

---

### 8. 文档与注释
**问题**：
- 部分复杂逻辑缺乏文档字符串
- 注释风格不统一（有的用英文，有的用中文）

**建议**：
1. 为关键模块添加 docstring
2. 统一注释语言（建议中文，与项目一致）

**工作量**：小

---

### 9. 性能优化机会
**发现**：
- `time.sleep()` 散布在 7 个文件中，部分可改为异步等待
- 正则表达式编译在循环内（`src/llm/style.py` 中有 10+ 个）

**建议**：
1. 将阻塞式 sleep 改为 asyncio 原语
2. 预编译正则表达式为模块级常量（已部分实现）

**工作量**：小

---

## 四、项目健康度评分

| 维度 | 评分 | 说明 |
|------|------|------|
| 代码规模 | ⭐⭐⭐☆☆ | 43K 行，偏大但有控制 |
| 测试覆盖 | ⭐⭐⭐⭐⭐ | 3987 测试，非常充分 |
| 代码质量 | ⭐⭐⭐⭐☆ | ruff/pylint/mypy 全绿 |
| 架构设计 | ⭐⭐⭐☆☆ | 全局状态和重复代码待优化 |
| 文档完整 | ⭐⭐⭐☆☆ | 部分模块缺文档 |
| **综合** | **⭐⭐⭐☆☆** | **良好，有改进空间** |

---

## 五、推荐处理顺序

### 第一阶段（1-2 周）：低风险高收益
1. [ ] 清理 `except Exception` + `noqa: BLE001`
2. [ ] 实现备份自动清理策略
3. [ ] 统一硬编码常量为 Config 配置

### 第二阶段（2-4 周）：中等风险
4. [ ] 合并 IM 适配器重复代码
5. [ ] 大文件拆分（从最小的开始）
6. [ ] 全局状态重构（ContextVar 化）

### 第三阶段（长期）：架构优化
7. [ ] 补充集成测试
8. [ ] 添加性能基准测试
9. [ ] 完善模块文档

---

## 六、立即可做的优化

### A. 清理备份文件（节省约 70MB）
```bash
# 删除超过 7 天的备份
find /Users/user/Documents/Linkora/data/backups -name "*.db" -mtime +7 -delete
```

### B. 检查重复测试
```bash
# 找出可能重复的测试用例
cd /Users/user/Documents/Linkora
find tests -name "test_*.py" -exec grep -l "def test_" {} \; | xargs grep -h "def test_" | sort | uniq -d
```

### C. 添加代码审查规则
在 `.pre-commit-config.yaml` 中添加：
```yaml
- repo: https://github.com/PyCQA/bandit
  rev: '1.7.8'
  hooks:
  - id: bandit
    args: ["-r", "src/"]
```

---

## 总结

项目整体健康度良好，测试覆盖充分，代码质量规范。主要技术债集中在：
1. **全局状态管理**（架构层面）
2. **异常处理规范**（代码质量）
3. **备份清理策略**（运维层面）

建议优先处理第三阶段的备份清理，然后逐步推进全局状态重构。大文件拆分可在业务空档期进行，需配合完善测试保障。
