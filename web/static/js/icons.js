(function() {
  function icon(name, opts) {
    opts = opts || {};
    const prefix = opts.prefix || 'fa-solid';
    const size = opts.size ? `fa-${opts.size}` : '';
    const spin = opts.spin ? 'fa-spin' : '';
    const classes = ['fa', `fa-${name}`, prefix, size, spin].filter(Boolean).join(' ');
    return `<i class="${classes}" aria-hidden="true"></i>`;
  }

  window.icon = icon;

  const EMOJI_MAP = {
    '💬': 'message',       '🔍': 'search',          '🔎': 'search',
    '📄': 'file-lines',     '💾': 'floppy-disk',     '🤖': 'robot',
    '📊': 'chart-bar',      '💡': 'lightbulb',       '📚': 'book',
    '✏': 'pen-to-square',   '✏️': 'pen-to-square',   '📁': 'folder',
    '🧩': 'puzzle-piece',   '❌': 'xmark',           '🧠': 'brain',
    '🔄': 'rotate-right',   '📥': 'download',        '➕': 'plus',
    '🧪': 'flask',  '🔑': 'key',             '👥': 'users',
    '⚙': 'gear',            '⚙️': 'gear',            '🔬': 'microscope',
    '✅': 'check-circle',    '🏢': 'building',        '👤': 'user',
    '📤': 'upload',         '📋': 'file-lines',      '📂': 'folder-open',
    '🏷': 'tag',            '🆕': 'sparkles',        '➤': 'chevron-right',
    '✕': 'xmark',           '✓': 'check',           '✗': 'xmark',
    '😕': 'face-meh',       '🔔': 'bell',            '📞': 'phone',
    '💼': 'briefcase',       '🔥': 'fire',           '🚀': 'rocket',
    '⚡': 'zap',             '🔒': 'lock',            '🔓': 'unlock',
    '🗑': 'trash',          '▶': 'chevron-right',    '▼': 'chevron-down',
    '⏳': 'hourglass',
  };

  function iconize(s) {
    if (typeof s !== 'string') return s;
    let out = s;
    for (const [emo, slug] of Object.entries(EMOJI_MAP)) {
      if (!emo) continue;
      if (out.includes(emo)) {
        out = out.split(emo).join(icon(slug, { spin: emo === '⏳' }));
      }
    }
    return out;
  }
  window.iconize = iconize;
})();