# 前端性能实测报告 · 2026-08-06

## 测量方法
- 工具链：Playwright 拉起 Chromium（Chrome for Testing）+ Lighthouse（Node API，desktop 模拟 1280×800）
- 目标：本地已构建（esbuild 合并）的 Linkora 管理后台首页 `http://127.0.0.1:8000/`
- 说明：**本地 localhost 直连，未做网络限速**。反映的是「资源合并后解析/加载开销」已归零；真实公网（尤其 3G）需另行限速复测。

## 实测结果（Lighthouse，构建后 bundle）
| 维度 | 分数 / 值 |
| --- | --- |
| Performance | **100** |
| Accessibility | 91 |
| Best Practices | 96 |
| FCP | 0.1 s |
| LCP | 0.2 s |
| TBT | 0 ms |
| CLS | 0 |
| SI | 0.5 s |
| 首屏总请求 | 14（JS 4 / CSS 3，含 bootstrap vendor 与字体） |
| 传输体积 | 1,640 KiB |

## 与合并前审计基线对比
| 指标 | 合并前（审计记录） | 合并后（Lighthouse 实测） |
| --- | --- | --- |
| 首屏 JS/CSS 资源请求 | ~70（40+ CSS + 30+ JS 逐文件） | 应用代码资源 ≈6（bundle.js + bundle.css + bootstrap js/css + drafts module），首屏总 14 |
| 性能分 | 未量化（瓶颈为请求数） | 100 |
| LCP | 受 ~70 请求串行/并行限制 | 0.2 s |

## 本轮附加改动
- 合并后的单 JS bundle 补 `defer`：脚本在 `DOMContentLoaded` 前按文档顺序执行，bootstrap（立即）先于它、drafts.js（module 默认 defer）后于它；全局桥接（`window.api`/`window.switchPage`）不受影响，且不阻塞 HTML 解析。收益在本地满分环境下不可见，但补齐规范、对未来脚本前置更 robust，零功能风险。

## 结论
esbuild 合并将首屏应用代码资源从 ~70 个独立文件降到个位数，请求数瓶颈已彻底消除；本地实测性能满分、CLS 0、零阻塞。后续若需公网 3G 真实数据，可加 Lighthouse `throttling`（rttMs/throughputKbps）限速复测。
