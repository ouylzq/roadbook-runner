// 把页面走成一棵语义树：每个可见元素给出 role + 可访问名，
// 可交互的额外挂一个 data-qa-ref，后续「按引用点」就靠它。
// 这是「读页面结构」这项能力的实现，是 agent 的主要感知通道（截图只是留档）。
(() => {
  const PREV = document.querySelectorAll('[data-qa-ref]');
  PREV.forEach(el => el.removeAttribute('data-qa-ref'));

  let refSeq = 0;

  const INTERACTIVE_TAGS = new Set(['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'SUMMARY', 'OPTION']);
  const INTERACTIVE_ROLES = new Set([
    'button', 'link', 'checkbox', 'radio', 'textbox', 'combobox', 'listbox',
    'option', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'tab', 'switch',
    'slider', 'spinbutton', 'searchbox',
  ]);

  const TAG_ROLE = {
    A: 'link', BUTTON: 'button', NAV: 'navigation', MAIN: 'main', HEADER: 'banner',
    FOOTER: 'contentinfo', ASIDE: 'complementary', FORM: 'form', UL: 'list', OL: 'list',
    LI: 'listitem', TABLE: 'table', TR: 'row', TD: 'cell', TH: 'columnheader',
    SELECT: 'combobox', TEXTAREA: 'textbox', IMG: 'img', DIALOG: 'dialog',
    H1: 'heading', H2: 'heading', H3: 'heading', H4: 'heading', H5: 'heading', H6: 'heading',
    P: 'paragraph', SECTION: 'region', ARTICLE: 'article', LABEL: 'label',
  };

  const INPUT_TYPE_ROLE = {
    checkbox: 'checkbox', radio: 'radio', button: 'button', submit: 'button',
    reset: 'button', search: 'searchbox', range: 'slider', number: 'spinbutton',
  };

  function roleOf(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit.trim().toLowerCase();
    if (el.tagName === 'INPUT') {
      return INPUT_TYPE_ROLE[(el.type || 'text').toLowerCase()] || 'textbox';
    }
    return TAG_ROLE[el.tagName] || 'generic';
  }

  function isVisible(el) {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    if (el.hasAttribute('hidden') || el.getAttribute('aria-hidden') === 'true') return false;
    const r = el.getBoundingClientRect();
    // 位置为 0 也保留：弹层常常先进 DOM 再绘制（browser-pitfalls §「绘制之前就在 DOM 里」）
    return r.width > 0 || r.height > 0 || el.tagName === 'OPTION';
  }

  function ownText(el) {
    let s = '';
    for (const node of el.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) s += node.textContent;
    }
    return s.replace(/\s+/g, ' ').trim();
  }

  function accessibleName(el) {
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();

    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const parts = labelledby.split(/\s+/)
        .map(id => document.getElementById(id))
        .filter(Boolean)
        .map(n => n.textContent.replace(/\s+/g, ' ').trim());
      if (parts.length) return parts.join(' ');
    }

    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') {
      if (el.id) {
        const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (lab) return lab.textContent.replace(/\s+/g, ' ').trim();
      }
      const wrapping = el.closest('label');
      if (wrapping) {
        const t = wrapping.textContent.replace(/\s+/g, ' ').trim();
        if (t) return t;
      }
      if (el.placeholder) return el.placeholder.trim();
      if (el.title) return el.title.trim();
      if (el.name) return el.name.trim();
      return '';
    }

    if (el.tagName === 'IMG') return (el.alt || '').trim();

    const own = ownText(el);
    if (own) return own;

    // 按钮/链接这类，允许取整棵子树文本（图标 + 文案的常见结构）
    const role = roleOf(el);
    if (['button', 'link', 'heading', 'listitem', 'option', 'tab', 'menuitem', 'cell'].includes(role)) {
      const t = (el.textContent || '').replace(/\s+/g, ' ').trim();
      if (t.length <= 120) return t;
    }
    return '';
  }

  function stateOf(el) {
    const bits = [];
    const role = roleOf(el);
    if (el.tagName === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio')) {
      bits.push(el.checked ? 'checked' : 'unchecked');
    }
    if (el.getAttribute('aria-checked')) bits.push(`aria-checked=${el.getAttribute('aria-checked')}`);
    if (el.getAttribute('aria-selected')) bits.push(`aria-selected=${el.getAttribute('aria-selected')}`);
    if (el.getAttribute('aria-expanded')) bits.push(`aria-expanded=${el.getAttribute('aria-expanded')}`);
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') bits.push('disabled');
    if (el.getAttribute('aria-current')) bits.push(`current=${el.getAttribute('aria-current')}`);
    if (['textbox', 'searchbox', 'combobox', 'spinbutton', 'slider'].includes(role)) {
      const v = el.value;
      if (v !== undefined && v !== null && String(v) !== '') bits.push(`value=${JSON.stringify(String(v).slice(0, 80))}`);
    }
    return bits;
  }

  function isInteractive(el) {
    if (INTERACTIVE_TAGS.has(el.tagName)) return true;
    if (INTERACTIVE_ROLES.has(roleOf(el))) return true;
    if (el.hasAttribute('onclick')) return true;
    if (el.tabIndex >= 0 && el.tagName !== 'BODY') return true;
    return false;
  }

  // 只保留“有信息量”的节点：可交互、有名字、或结构性地标
  const LANDMARKS = new Set(['main', 'navigation', 'banner', 'contentinfo', 'complementary',
                             'dialog', 'form', 'list', 'listitem', 'table', 'row', 'heading', 'region']);

  function keep(el, role, name, interactive) {
    if (interactive) return true;
    if (LANDMARKS.has(role)) return true;
    if (name) return true;
    return false;
  }

  const lines = [];

  function walk(el, depth) {
    if (!(el instanceof Element)) return;
    if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'META', 'LINK'].includes(el.tagName)) return;
    if (!isVisible(el)) return;

    const role = roleOf(el);
    const name = accessibleName(el);
    const interactive = isInteractive(el);
    const kept = keep(el, role, name, interactive);

    let nextDepth = depth;
    if (kept) {
      let ref = '';
      if (interactive) {
        refSeq += 1;
        ref = `ref_${refSeq}`;
        el.setAttribute('data-qa-ref', ref);
      }
      const states = stateOf(el);
      let line = '  '.repeat(depth) + `- ${role}`;
      if (name) line += ` ${JSON.stringify(name.slice(0, 160))}`;
      if (states.length) line += ` [${states.join(' ')}]`;
      if (ref) line += ` {${ref}}`;
      lines.push(line);
      nextDepth = depth + 1;
    }

    for (const child of el.children) walk(child, nextDepth);
  }

  walk(document.body, 0);

  return {
    url: location.href,
    title: document.title,
    viewport: { w: window.innerWidth, h: window.innerHeight },
    tree: lines.join('\n'),
    ref_count: refSeq,
  };
})();
