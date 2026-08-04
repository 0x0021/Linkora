import { defineConfig } from 'vitest/config';

// 前端冒烟测试配置（P1-3）
// 仅覆盖 web/static/js 下的页面脚本（rag / persona / metrics），
// 不依赖任何构建步骤——源码就是直接被浏览器 <script> 加载的脚本。
export default defineConfig({
  test: {
    environment: 'jsdom',
    globals: true,
    // setupFiles 在每个测试文件之前运行，补齐浏览器全局（api / 组件 / 轻量 DOM stub）
    setupFiles: ['./web/static/js/tests/setup.js'],
    include: ['web/static/js/tests/**/*.test.js'],
    // 这些页面脚本是经典脚本（非 ESM），vite 转换时会报「无导出」，属预期，忽略即可
    silent: false,
  },
});
