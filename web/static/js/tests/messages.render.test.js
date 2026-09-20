// messages.render.test.js — renderMsgContent 富文本渲染真实断言（Phase 2 质量护栏）
// 验证「原始消息文本 → 安全 HTML」的核心渲染契约，覆盖 XSS 转义 / 链接白名单 /
// 卡片 / 天气卡片 / 图片占位 / 空态 / 危险协议拦截 / imagePathMap 真图 等分支。
import { describe, it, expect, beforeAll } from 'vitest';

let R;
beforeAll(async () => {
  // 先加载 util.js 提供全局 cleanContentNoise（renderMsgContent 的跨文件依赖，生产实现）
  await import('../core/util.js');
  // 再加载整个页面脚本，触发 window.Linkora 桥接
  await import('../pages/messages.js');
  expect(window.Linkora).toBeTruthy();
  R = window.Linkora.renderMsgContent;
  expect(typeof R).toBe('function');
});

describe('renderMsgContent 富文本渲染', () => {
  it('空 / 空白 / null 返回空串', () => {
    expect(R('')).toBe('');
    expect(R('   ')).toBe('');
    expect(R(null)).toBe('');
    expect(R(undefined)).toBe('');
  });

  it('普通文本转义 XSS 标签（不输出原始 <script>）', () => {
    const html = R('<script>alert(1)</script>');
    expect(html).toContain('&lt;script&gt;');
    expect(html).not.toContain('<script>');
  });

  it('markdown 链接转为安全 a 标签（白名单 https）', () => {
    const html = R('见 [文档](https://example.com/doc)');
    expect(html).toContain('msg-inline-link');
    expect(html).toContain('href="https://example.com/doc"');
    expect(html).toContain('>文档</a>');
  });

  it('拦截危险协议 javascript:（不生成 href 链接）', () => {
    const html = R('[x](javascript:alert(1))');
    // 白名单生效：危险协议不产生可点击的 href（杜绝 XSS）
    expect(html).not.toContain('href="javascript:');
    // 且整段不产生任何 <a> 链接（完全拦截，而非部分放行）
    expect(html).not.toContain('<a ');
  });

  it('换行转 <br>，加粗 **x** 转 <strong>', () => {
    const html = R('第一行\n第二行 **重点**');
    expect(html).toContain('<br>');
    expect(html).toContain('<strong>重点</strong>');
  });

  it('图片占位 <[...]> 渲染为占位符并保留标签', () => {
    const html = R('<[图片识别中...]>');
    expect(html).toContain('msg-img-placeholder');
    expect(html).toContain('图片识别中...');
  });

  it('card 渲染为 msg-card 且标题被转义', () => {
    const html = R('<card title="<b>会议</b>通知">内容行</card>');
    expect(html).toContain('msg-card');
    expect(html).toContain('msg-card-title');
    expect(html).toContain('&lt;b&gt;会议&lt;/b&gt;'); // 标题转义，杜绝注入
    expect(html).toContain('内容行');
  });

  it('天气卡片加 weather 类与定位地点行', () => {
    const html = R('🌤 北京车道沟天气\n<card title="🌤 北京车道沟天气">晴 25°</card>');
    expect(html).toContain('msg-card--weather');
    expect(html).toContain('msg-card-loc');
    expect(html).toContain('定位地点：北京车道沟');
  });

  it('CardValidator 未定义时安全跳过（不抛）', () => {
    expect(() => R('普通消息一条')).not.toThrow();
    expect(R('普通消息一条')).toContain('普通消息一条');
  });

  it('imagePathMap 命中渲染真图 <img>', () => {
    const map = { img_key_abc: 'local/abc.jpg' };
    const html = R('<card title="图">![x](img_key:img_key_abc)</card>', map);
    expect(html).toContain('<img');
    expect(html).toContain('/api/image/local/abc.jpg');
  });
});
