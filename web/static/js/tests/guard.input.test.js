/* 前端统一收口点：提交前输入校验 + 错误提示脱敏（正常 / 异常场景）。 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

const read = (rel) => fs.readFileSync(path.resolve(__dirname, rel), 'utf8');
const UTIL_SRC = read('../core/util.js');
const API_SRC = read('../core/api.js');
const APP_SRC = read('../core/app.js');

beforeEach(() => {
  window.eval(UTIL_SRC);
  globalThis.showToast = vi.fn();
  window.showToast = globalThis.showToast;
});

describe('提交前输入校验 guardValidateRequest', () => {
  it('正常业务请求体一律放行', () => {
    expect(window.guardValidateRequest(null)).toBe(null);
    expect(window.guardValidateRequest({ content: '忽略以上，我再补充一句：打印机在3楼' })).toBe(null);
    expect(window.guardValidateRequest({ text: '系统提示词内容 '.repeat(2000) })).toBe(null);
    expect(window.guardValidateRequest({ items: [1, 2, 3], nested: { a: { b: 'ok' } } })).toBe(null);
    expect(window.guardValidateRequest({ text: '第一行\n第二行\t第三行\r\n' })).toBe(null);
  });

  it('超长字段 / 超大请求体被拦截', () => {
    expect(window.guardValidateRequest({ text: 'x'.repeat(200001) })).toContain('过长');
    expect(window.guardValidateRequest({ text: 'x'.repeat(2000001) })).toContain('过大');
  });

  it('不可见控制字符被拦截', () => {
    expect(window.guardValidateRequest({ text: 'a\u0000b' })).toContain('控制字符');
    expect(window.guardValidateRequest({ text: 'a\u202eb' })).toContain('控制字符');
  });

  it('病态结构被拦截', () => {
    let deep = {};
    let cur = deep;
    for (let i = 0; i < 25; i++) { cur.v = {}; cur = cur.v; }
    expect(window.guardValidateRequest(deep)).toContain('层级过深');
    expect(window.guardValidateRequest({ items: new Array(2001).fill(1) })).toContain('条目过多');
  });

  it('api.fetch 命中拦截时不发请求，返回既有错误结构', async () => {
    window.eval(API_SRC);
    const spy = vi.fn(async () => ({
      ok: true, status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => ({ success: true }),
    }));
    globalThis.fetch = spy;
    window.fetch = spy;
    const res = await window.api.fetch('/api/test', 'POST', { text: 'x'.repeat(200001) });
    expect(res.error).toBe('guard_blocked');
    expect(typeof res.message).toBe('string');
    expect(spy).not.toHaveBeenCalled();
  });

  it('api.fetch 正常请求仍照常发出（业务流程不变）', async () => {
    window.eval(API_SRC);
    const spy = vi.fn(async () => ({
      ok: true, status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => ({ success: true }),
    }));
    globalThis.fetch = spy;
    window.fetch = spy;
    const res = await window.api.fetch('/api/test', 'POST', { text: '正常内容' });
    expect(res).toEqual({ success: true });
    expect(spy).toHaveBeenCalledTimes(1);
  });
});

describe('错误提示脱敏 safeErrorText', () => {
  it('正常业务文案原样保留', () => {
    expect(window.safeErrorText('配置保存成功')).toBe('配置保存成功');
    expect(window.safeErrorText('文档重复，已跳过')).toBe('文档重复，已跳过');
    expect(window.safeErrorText('用户名或密码错误')).toBe('用户名或密码错误');
  });

  it('服务端绝对路径被脱敏', () => {
    const out = window.safeErrorText('技能目录不存在: /Users/ring0/dev/Linkora/skills/demo');
    expect(out).toContain('[路径已脱敏]');
    expect(out).not.toContain('/Users/ring0');
  });

  it('凭据与令牌被脱敏', () => {
    const fake = 'sk-' + 'A'.repeat(24);
    expect(window.safeErrorText(`调用失败 ${fake}`)).not.toContain(fake);
    expect(window.safeErrorText('Bearer ' + 'B'.repeat(30))).toContain('[已脱敏]');
  });

  it('堆栈与结构化 422 错误不回显', () => {
    expect(window.safeErrorText('Traceback (most recent call last):\n  File "/srv/app.py", line 1')).toContain('隐藏');
    expect(window.safeErrorText('[{"type":"missing","loc":["body","x"]}]')).toBe('输入格式有误，请检查后重试');
  });

  it('超长错误文案被截断', () => {
    const out = window.safeErrorText('错'.repeat(500));
    expect(out.length).toBeLessThanOrEqual(161);
    expect(out.endsWith('…')).toBe(true);
  });

  it('空值与异常类型不抛错', () => {
    expect(window.safeErrorText('')).toBe('');
    expect(window.safeErrorText(null)).toBe('');
    expect(() => window.safeErrorText({ a: 1 })).not.toThrow();
  });

  it('showToast 确实接入了脱敏收口（源码护栏）', () => {
    const body = APP_SRC.slice(APP_SRC.indexOf('function showToast'));
    expect(body.slice(0, 500)).toContain('safeErrorText');
  });
});
