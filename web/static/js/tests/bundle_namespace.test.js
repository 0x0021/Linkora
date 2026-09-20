// 前端生产产物回归测试（Phase 0 解锁层验证）
// 断言关键机制确实进入了压缩后的生产 bundle，而非仅停留在 dev 源码。
// 不执行 bundle（避免 jsdom 缺 DOM 导致加载即崩），仅做静态内容断言。
import { readFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
// 本文件位于 web/static/js/tests/，dist 在 web/static/dist（上两级）
const DIST = join(__dirname, '..', '..', 'dist');
const manifestPath = join(DIST, 'manifest.json');

const bundle = (() => {
  if (!existsSync(manifestPath)) return null;
  try {
    const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
    const jsPath = join(DIST, manifest.js);
    return existsSync(jsPath) ? readFileSync(jsPath, 'utf8') : null;
  } catch {
    return null;
  }
})();

describe('生产 bundle 包含 Phase 0 解锁机制', () => {
  it('bundle 已构建（未构建则跳过本组断言，先 npm run build:frontend）', () => {
    if (!bundle) {
      console.warn('[bundle_namespace] dist 未构建，跳过 Phase 0 产物断言');
    }
  });

  it('window.Linkora 命名空间已注入生产 bundle', () => {
    if (!bundle) return;
    expect(bundle).toContain('window.Linkora');
  });

  it('data-action 事件委托已编译进 bundle', () => {
    if (!bundle) return;
    expect(bundle).toContain('data-action');
    expect(bundle).toContain('closest');
  });

  it('renderPager 翻页已迁到 pager-go action（不再依赖内联 onclick + window.__pagerCb）', () => {
    if (!bundle) return;
    expect(bundle).toContain('pager-go');
  });

  it('产物附 sourcemap（Phase 1 调试增强）', () => {
    if (!bundle) return;
    expect(bundle).toContain('sourceMappingURL');
  });
});
