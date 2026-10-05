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

// ============================================================================
// P0 存储型 XSS 回归护栏（2026-10-04 审计发现）
//
// 漏洞：_renderCardBody 收到的是**未转义**的原始消息正文，而下游
// _renderInline 只做加粗/标签替换、不转义 → 外部群成员发一条带 <card> 的
// 消息即可在管理员浏览器执行脚本，窃取 localStorage 中的 jwt_token。
// 本组用例把当时的真实 PoC 载荷固化为断言，任何回归都会立即变红。
// ============================================================================
describe('P0 存储型 XSS 回归（卡片正文）', () => {
  it('卡片正文内的 <img onerror> 被转义，不可执行', () => {
    const payload = '<card title="公告">重要通知\n<img src=x onerror="alert(document.cookie)">\n</card>';
    const html = R(payload);
    // 关键断言：绝不能出现可执行的原始标签
    expect(html).not.toMatch(/<img[^>]*onerror/i);
    expect(html).not.toContain('onerror="alert');
    // 应当以转义形态出现（内容仍可见）
    expect(html).toContain('&lt;img');
  });

  it('卡片正文内的 <script> 被转义', () => {
    const html = R('<card title="t"><script>alert(1)</script></card>');
    expect(html).not.toContain('<script>');
    expect(html).toContain('&lt;script&gt;');
  });

  it('卡片正文内 <svg onload> 被转义', () => {
    const html = R('<card title="t"><svg onload=alert(1)></svg></card>');
    expect(html).not.toMatch(/<svg/i);
    expect(html).toContain('&lt;svg');
  });

  it('卡片内 <clickable url="javascript:..."> 降级为不可点击（不生成 href）', () => {
    const html = R('<card title="t"><clickable url="javascript:alert(1)">点我</clickable></card>');
    expect(html).not.toContain('href="javascript:');
    expect(html).not.toMatch(/<a[^>]*href="javascript:/i);
    // 文字内容保留可见
    expect(html).toContain('点我');
  });

  it('卡片内 [text](javascript:...) 链接被拦截', () => {
    const html = R('<card title="t">[点我](javascript:alert(1))</card>');
    expect(html).not.toMatch(/href="javascript:/i);
  });

  it('卡片内 [text](data:text/html,...) 链接被拦截', () => {
    const html = R('<card title="t">[x](data:text/html;base64,PHNjcmlwdD4=)</card>');
    expect(html).not.toMatch(/href="data:/i);
  });

  it('折行空格不得用于绕过协议白名单（java\\nscript:）', () => {
    // 钉钉/飞书长 URL 常被折行插入空白；若在去空格**之前**验协议，
    // "java\nscript:alert(1)" 会被拼成合法 javascript: 绕过白名单
    const html = R('<card title="t">[x](java\nscript:alert(1))</card>');
    expect(html).not.toMatch(/href="javascript:/i);
  });

  it('合法 https clickable 仍正常渲染为可点击链接（防过度拦截）', () => {
    const html = R('<card title="t"><clickable url="https://example.com/a">文档</clickable></card>');
    expect(html).toContain('href="https://example.com/a"');
    expect(html).toContain('msg-link-btn');
  });

  it('合法 https 卡片链接仍可点击（防过度拦截）', () => {
    const html = R('<card title="t">[文档](https://example.com/doc)</card>');
    expect(html).toContain('href="https://example.com/doc"');
  });

  it('卡片标题同样被转义（标题来自外部）', () => {
    const html = R('<card title="<img src=x onerror=alert(1)>">正文</card>');
    expect(html).not.toMatch(/<img[^>]*onerror/i);
  });
});

describe('sanitizeUrl 协议白名单（单一真源）', () => {
  // 惰性取值：util.js 的 import 发生在 beforeAll（晚于 describe 回调执行），
  // 故不能在 describe 体内直接 const S = window.sanitizeUrl
  const S = () => window.sanitizeUrl;

  it('sanitizeUrl 已挂载到 window', () => {
    expect(typeof window.sanitizeUrl).toBe('function');
  });

  it('放行 http/https/mailto', () => {
    expect(S()('https://example.com')).toBe('https://example.com');
    expect(S()('http://example.com')).toBe('http://example.com');
    expect(S()('mailto:a@b.com')).toBe('mailto:a@b.com');
    expect(S()('HTTPS://EXAMPLE.COM')).toBe('HTTPS://EXAMPLE.COM');
  });

  it('阻断 javascript:/data:/vbscript:/file:', () => {
    expect(S()('javascript:alert(1)')).toBeNull();
    expect(S()('data:text/html,<script>alert(1)</script>')).toBeNull();
    expect(S()('vbscript:msgbox(1)')).toBeNull();
    expect(S()('file:///etc/passwd')).toBeNull();
  });

  it('阻断无协议相对 URL（避免被解析为同源相对路径）', () => {
    expect(S()('/api/image/x.jpg')).toBeNull();
    expect(S()('//evil.com/x')).toBeNull();
    expect(S()('example.com')).toBeNull();
  });

  it('非字符串与空值安全返回 null', () => {
    expect(S()('')).toBeNull();
    expect(S()('   ')).toBeNull();
    expect(S()(null)).toBeNull();
    expect(S()(undefined)).toBeNull();
    expect(S()(123)).toBeNull();
  });
});
