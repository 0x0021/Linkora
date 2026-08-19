# Linkora 品牌宣传图使用说明

本目录存放 Linkora 项目用于 README 顶部横幅和 GitHub Pages 首页的宽幅宣传图，按深色 / 浅色两套主题各 3 张生成。

## 设计系统

- **品牌色**：靛蓝 `#6366F1` → 青色 `#22D3EE` 渐变
- **深色背景**：`#05070d` / `#0a0f1c`
- **浅色背景**：`#f8fafc` / `#ffffff`
- **字体气质**：现代无衬线、干净克制、玻璃拟态
- **视觉母版**：桥形 Logo（三平台线条汇聚到中央发光节点）+ Linkora 字标

## 图片清单

| 文件 | 尺寸 | 用途 |
| --- | --- | --- |
| `linkora-hero-dark.png` | 1536×768 | README / Pages 首页 Hero 横幅（深色模式） |
| `linkora-hero-light.png` | 1536×768 | README / Pages 首页 Hero 横幅（浅色模式） |
| `linkora-features-dark.png` | 1536×768 | 核心能力概览图（深色模式） |
| `linkora-features-light.png` | 1536×768 | 核心能力概览图（浅色模式） |
| `linkora-scenarios-dark.png` | 1536×768 | 使用场景流程图（深色模式） |
| `linkora-scenarios-light.png` | 1536×768 | 使用场景流程图（浅色模式） |

## README.md 引用代码

GitHub 支持按当前主题自动切换图片：

```markdown
<!-- Hero 横幅 -->
![Linkora · 灵桥](docs/assets/brand/linkora-hero-light.png#gh-light-mode-only)
![Linkora · 灵桥](docs/assets/brand/linkora-hero-dark.png#gh-dark-mode-only)

<!-- 核心能力概览 -->
![Linkora Core Capabilities](docs/assets/brand/linkora-features-light.png#gh-light-mode-only)
![Linkora Core Capabilities](docs/assets/brand/linkora-features-dark.png#gh-dark-mode-only)

<!-- 使用场景示意 -->
![Linkora Scenarios](docs/assets/brand/linkora-scenarios-light.png#gh-light-mode-only)
![Linkora Scenarios](docs/assets/brand/linkora-scenarios-dark.png#gh-dark-mode-only)
```

## GitHub Pages 引用代码

Pages 站点（源目录 `docs/`）中按当前主题切换：

```html
<!-- Hero -->
<picture>
  <source srcset="/assets/brand/linkora-hero-dark.png" media="(prefers-color-scheme: dark)">
  <img src="/assets/brand/linkora-hero-light.png" alt="Linkora · 灵桥 — 多平台 AI 智能连接中枢">
</picture>

<!-- 核心能力 -->
<picture>
  <source srcset="/assets/brand/linkora-features-dark.png" media="(prefers-color-scheme: dark)">
  <img src="/assets/brand/linkora-features-light.png" alt="Linkora Core Capabilities">
</picture>

<!-- 使用场景 -->
<picture>
  <source srcset="/assets/brand/linkora-scenarios-dark.png" media="(prefers-color-scheme: dark)">
  <img src="/assets/brand/linkora-scenarios-light.png" alt="Linkora Scenarios">
</picture>
```

## 注意事项

- 图片右下角带有生成平台 AI 内容标识。若用于正式对外发布，建议由设计师在 Figma / Photoshop 中做最终精修覆盖，或基于本套视觉重制纯矢量版。
- 本组素材为概念宣传图，界面图标与第三方平台 Logo 仅作示意，正式版本应替换为对应品牌的官方 Logo 资产。
