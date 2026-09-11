// core/util.js 消息内容清洗（仪表盘噪声治理）冒烟测试
// 用例取自真实落库样例（data/conversations/dingtalk__*.db），确保清洗规则贴合实际格式。
import { describe, it, expect, beforeEach } from 'vitest';

describe('core/util.js 消息内容清洗', () => {
  beforeEach(async () => {
    vi.resetModules();
    await import('../core/util.js');
  });

  it('暴露 cleanMsgPreview 与 msgTypeLabel', () => {
    expect(typeof window.cleanMsgPreview).toBe('function');
    expect(typeof window.msgTypeLabel).toBe('function');
  });

  it('image：mediaId 占位符 + 本地缓存路径 → [图片]', () => {
    const raw =
      '[图片消息](mediaId=$iwEcAqNwbmcDAQTRBtgF0QLGBrA_f9VuwflDXwp1bDKjjMYAB9IAssT5CAAJomltCgAL0gAEDQs)' +
      '\n[本地图片] data/tmp_images/dingtalk/ding9888ef577f7811cb/cidZKhr2YZHKQpe2fQpqT4yvMU0UKDULGsyQ_hxmIU6Nyc/ocr__iwEcAqNwbmcDAQTRBtgF0QLGBrA_f9VuwflDXwp1bDKjjMYAB9IAssT5CAA.png';
    expect(window.cleanMsgPreview(raw, 'image')).toBe('[图片]');
  });

  it('mixed：OCR 区块标记被剥离，保留尾部可读文字', () => {
    const raw =
      '———— 图片识别内容 ————\n' +
      '[图片消息](mediaId=$iwEcAqNwbmcDAQTRBjsF0QHqBrAlH0uHL1uefwp1bHH25kcAB9IJFUOkCAAJomltCgAL0gADwPU)\n' +
      '[图片识别中...]\n' +
      '———— 图片识别内容结束 ————\n' +
      '可以了';
    expect(window.cleanMsgPreview(raw, 'mixed')).toBe('[图片]\n可以了');
  });

  it('voice：保留语音转文字，去掉 mediaId 与 dws 下载提示', () => {
    const raw =
      '收到，我看看\n' +
      '[语音消息](mediaId=@lR_PJw1Ez5NamV8AALAgx4w9F9kJnAakTYkNbXgA) 注意：如需下载使用dws chat message download-media命令下载';
    const out = window.cleanMsgPreview(raw, 'voice');
    expect(out).toContain('收到，我看看');
    expect(out).toContain('[语音]');
    expect(out).not.toContain('mediaId');
    expect(out).not.toContain('download-media');
  });

  it('video：mediaId/fileName/url + 本地文件路径 → [视频]', () => {
    const raw =
      '[视频消息](mediaId=@lQbPKHamnKNBPhsAALCTpktEcHivBApeC42MY9kA) fileName=video url: @lQbPKHamnKNBPhsAALCTpktEcHivBApeC42MY9kA 注意：如需下载使用dws chat message download-media命令下载\n' +
      '[本地文件] data/recv_files/dingtalk/ding9888ef577f7811cb/cidYJ3w6lxx7n97VUXmrUxN1zeeCo4WhclfZq8EDsrHlUI_/video';
    expect(window.cleanMsgPreview(raw, 'video')).toBe('[视频]');
  });

  it('app：裸 JSON 清洗为空时按类型兜底为 [应用消息]', () => {
    expect(window.cleanMsgPreview('{"textContent":{"text":""},"contentType":1101}', 'app')).toBe('[应用消息]');
  });

  it('card：标题提取为 [卡片] 前缀，标签剥离', () => {
    const raw = '<card title="应用审批通过，已发布成功">\n你的应用已通过管理员审批并发布成功！\n</card>';
    const out = window.cleanMsgPreview(raw, 'interactive');
    expect(out).toContain('[卡片] 应用审批通过，已发布成功');
    expect(out).toContain('你的应用已通过管理员审批并发布成功！');
    expect(out).not.toContain('<card');
    expect(out).not.toContain('</card>');
  });

  it('file 标签：<file .../> → [文件]', () => {
    const raw = '<file key="file_v3_0013r_47057b1f" name="dingtalk-ai-main-2.zip"/>';
    const out = window.cleanMsgPreview(raw, 'file');
    expect(out).toContain('[文件]');
    expect(out).not.toContain('<file');
  });

  it('纯文本原样保留', () => {
    expect(window.cleanMsgPreview('我再试一下', 'text')).toBe('我再试一下');
  });

  it('null/空值安全返回', () => {
    expect(window.cleanMsgPreview(null, 'image')).toBe('[图片]');
    expect(window.cleanMsgPreview('', 'text')).toBe('');
    expect(window.cleanMsgPreview('   ', '')).toBe('');
  });

  it('msgTypeLabel 英文枚举 → 中文，未知原样', () => {
    expect(window.msgTypeLabel('image')).toBe('图片');
    expect(window.msgTypeLabel('mixed')).toBe('图文');
    expect(window.msgTypeLabel('MIXED')).toBe('图文');
    expect(window.msgTypeLabel('text')).toBe('文本');
    expect(window.msgTypeLabel('')).toBe('文本');
    expect(window.msgTypeLabel('unknown_type')).toBe('unknown_type');
  });
});

// cleanContentNoise：会话详情页富文本渲染前预处理（区别于 cleanMsgPreview 的紧凑列表占位）
describe('core/util.js · cleanContentNoise（会话详情页）', () => {
  beforeEach(async () => {
    vi.resetModules();
    await import('../core/util.js');
  });

  it('暴露 cleanContentNoise', () => {
    expect(typeof window.cleanContentNoise).toBe('function');
  });

  it('mixed：OCR 区块标记剥离，保留尾部真实文字', () => {
    const raw =
      '———— 图片识别内容 ————\n' +
      '[图片消息](mediaId=$iwEcAqNwbmcDAQTRBjsF0QHqBrAlH0uHL1uefwp1bHH25kcAB9IJFUOkCAAJomltCgAL0gADwPU)\n' +
      '[图片识别中...]\n' +
      '———— 图片识别内容结束 ————\n' +
      '可以了';
    const out = window.cleanContentNoise(raw);
    expect(out).toBe('可以了');
    expect(out).not.toContain('mediaId');
    expect(out).not.toContain('图片识别内容');
    expect(out).not.toContain('图片识别中');
  });

  it('voice：保留语音转文字，去掉 mediaId 与 dws 下载提示', () => {
    const raw =
      '老徐，你和师傅说一下，就这些东西\n' +
      '[语音消息](mediaId=@lR_PJw1Ez5NamV8AALAgx4w9F9kJnAakTYkNbXgA) 注意：如需下载使用dws chat message download-media命令下载';
    const out = window.cleanContentNoise(raw);
    expect(out).toContain('老徐，你和师傅说一下，就这些东西');
    expect(out).not.toContain('mediaId');
    expect(out).not.toContain('download-media');
  });

  it('image-only：占位符全清后返回空串（图片由媒体渲染器单独渲染）', () => {
    const raw =
      '[图片消息](mediaId=$iwEcAqNwbmcDAQTRBtgF0QLGBrA_f9VuwflDXwp1bDKjjMYAB9IAssT5CAAJomltCgAL0gAEDQs)' +
      '\n[本地图片] data/tmp_images/dingtalk/ding9888/cid/ocr__x.png';
    expect(window.cleanContentNoise(raw)).toBe('');
  });

  it('video：整行媒体占位符移除', () => {
    const raw =
      '[视频消息](mediaId=@lQbPKHamnKNBPhsAALCTpktEcHivBApeC42MY9kA) fileName=video url: @lQbPKHamnKNBPhsAALCTpktEcHivBApeC42MY9kA 注意：如需下载使用dws chat message download-media命令下载';
    const out = window.cleanContentNoise(raw);
    expect(out).toBe('');
    expect(out).not.toContain('mediaId');
  });

  it('纯文本原样保留', () => {
    expect(window.cleanContentNoise('我再试一下')).toBe('我再试一下');
  });

  it('不破坏 <card> 卡片与 <[图片识别中]> 占位（交给 renderMsgContent 处理）', () => {
    expect(window.cleanContentNoise('<card title="应用审批通过">\n你的应用已发布成功\n</card>'))
      .toContain('应用审批通过');
    expect(window.cleanContentNoise('<[图片识别中...]>')).toContain('<[图片识别中');
  });

  it('null/空值安全返回', () => {
    expect(window.cleanContentNoise(null)).toBe(null);
    expect(window.cleanContentNoise('')).toBe('');
  });
});
