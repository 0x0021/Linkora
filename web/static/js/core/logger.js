// ============ core/logger.js ============
// 统一日志层：替代散落的 console.*。
// - 开发态（localhost / 127.0.0.1 / *.local / 非标准端口）输出 debug/info，方便排错；
// - 生产态（标准域名/端口部署）仅输出 warn/error，避免调试噪音与潜在敏感信息泄露。
// warn/error 始终输出（排错必需），debug/info/log 在生产静默。
// 以 IIFE 挂到 global（浏览器即 window；vitest 即 globalThis），兼容经典脚本与 ESM 测试。
(function (root) {
  'use strict';

  var DEV = (typeof location !== 'undefined') && (
    location.hostname === 'localhost' ||
    location.hostname === '127.0.0.1' ||
    location.hostname.endsWith('.local') ||
    (location.port && location.port !== '80' && location.port !== '443')
  );

  function emit(consoleLevel, args) {
    var a = ['[Linkora]'].concat(Array.prototype.slice.call(args));
    if (consoleLevel === 'warn' || consoleLevel === 'error') {
      console[consoleLevel].apply(console, a);
    } else if (DEV) {
      // debug / info / log 仅开发态
      (console[consoleLevel] || console.log).apply(console, a);
    }
  }

  var logger = {
    log: function () { emit('log', arguments); },
    debug: function () { emit('debug', arguments); },
    info: function () { emit('info', arguments); },
    warn: function () { emit('warn', arguments); },
    error: function () { emit('error', arguments); },
  };

  root.logger = logger;
  if (typeof module !== 'undefined' && module.exports) module.exports = logger;
})(typeof globalThis !== 'undefined' ? globalThis : this);
