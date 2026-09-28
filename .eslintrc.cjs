// Linkora 前端 ESLint 配置（纯 Vanilla JS 经典 <script> 共享全局作用域，无框架/TS）。
//
// 设计取舍：
//  - 全局函数（switchPage / window.Linkora / 各 page 顶层函数）跨文件互相调用，
//    关闭 no-undef，避免把「自定义全局」误报成 undefined。
//  - 错误级（error）只卡真正危险的写法：no-eval / no-debugger / 语法；
//    风格类（no-console / prefer-const / no-var / no-unused-vars）一律 warn，
//    不阻塞 verify:frontend，但持续暴露给开发者。
//  - 与 Prettier 共存：extends 'prettier' 关闭所有会与 prettier 冲突的格式规则。
//  - 测试目录（web/static/js/tests，ESM import/export + vitest）不纳入 ESLint，
//    由 vitest 负责；drafts.js 是全仓唯一 type=module，单独 override。
module.exports = {
  root: true,
  env: {
    browser: true,
    es2021: true,
  },
  parserOptions: {
    ecmaVersion: 2021,
    sourceType: 'script',
  },
  extends: ['eslint:recommended', 'prettier'],
  rules: {
    'no-undef': 'off',
    'no-unused-vars': ['warn', { args: 'after-used', varsIgnorePattern: '^_' }],
    'no-console': 'warn',
    'no-debugger': 'error',
    'no-eval': 'error',
    'no-alert': 'warn',
    'prefer-const': 'warn',
    'no-var': 'warn',
    // 防御性 catch {} 吞错是本项目的既定容错模式（api.js / 各 page 容错分支），
    // 设为 warn 仅曝光、不阻塞门禁。
    'no-empty': 'warn',
    // markdown 渲染管线刻意用 \u0000 作代码块占位哨兵分隔符，属安全的有意用法，
    // 关闭以免每次 lint 误报。
    'no-control-regex': 'off',
  },
  overrides: [
    {
      files: ['**/drafts.js'],
      parserOptions: { sourceType: 'module' },
    },
  ],
  ignorePatterns: ['dist/', 'node_modules/', 'tests/'],
};
