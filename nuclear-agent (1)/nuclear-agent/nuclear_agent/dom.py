"""JavaScript snippets run inside the page to inspect it."""

# Finds visible interactive elements, numbers them and tags each with
# data-nuclear-id so later actions can target them reliably.
# Only elements inside (or near) the current viewport are returned, so the
# model sees what a person would see and has to scroll for the rest.
COLLECT_ELEMENTS_JS = r"""
(maxItems) => {
  const SELECTOR = [
    'a[href]', 'button', 'input:not([type=hidden])', 'select', 'textarea', 'summary',
    '[role=button]', '[role=link]', '[role=checkbox]', '[role=radio]', '[role=tab]',
    '[role=menuitem]', '[role=option]', '[role=switch]', '[role=combobox]', '[role=textbox]',
    '[contenteditable=""]', '[contenteditable=true]', '[onclick]', '[tabindex]:not([tabindex="-1"])'
  ].join(',');
  document.querySelectorAll('[data-nuclear-id]').forEach(el => el.removeAttribute('data-nuclear-id'));
  const vh = window.innerHeight, vw = window.innerWidth;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const labelFor = el => {
    if (el.id) {
      try {
        const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (l) return clean(l.innerText);
      } catch (e) {}
    }
    const parentLabel = el.closest('label');
    if (parentLabel) return clean(parentLabel.innerText);
    return '';
  };
  const out = [];
  let id = 0;
  for (const el of document.querySelectorAll(SELECTOR)) {
    if (out.length >= maxItems) break;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    if (r.bottom < -50 || r.top > vh + 50 || r.right < 0 || r.left > vw) continue;
    const st = window.getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity) === 0) continue;
    id += 1;
    el.setAttribute('data-nuclear-id', String(id));
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    let text = clean(el.innerText || el.value || '');
    if (tag === 'input' && ['password'].includes(type)) text = '';
    out.push({
      id,
      tag,
      type,
      role: el.getAttribute('role') || '',
      text: text.slice(0, 80),
      label: clean(el.getAttribute('aria-label') || labelFor(el) || el.getAttribute('title') || el.getAttribute('alt') || '').slice(0, 80),
      name: el.getAttribute('name') || '',
      placeholder: el.getAttribute('placeholder') || '',
      autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
      href: tag === 'a' ? (el.href || '').slice(0, 150) : '',
      in_form: !!el.closest('form'),
      disabled: !!el.disabled,
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2)
    });
  }
  return out;
}
"""

PAGE_TEXT_JS = r"""
(maxChars) => {
  const body = document.body;
  if (!body) return '';
  let t = body.innerText || '';
  t = t.replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
  return t.slice(0, maxChars);
}
"""

SCROLL_INFO_JS = r"""
() => ({
  y: Math.round(window.scrollY),
  height: Math.round(document.documentElement.scrollHeight),
  viewport: window.innerHeight
})
"""

# Detects CAPTCHA / bot-check pages. The agent never tries to solve these;
# it hands control back to the user.
CAPTCHA_JS = r"""
() => {
  const frames = Array.from(document.querySelectorAll('iframe')).map(f => (f.src || '').toLowerCase());
  const hit = frames.some(s => /recaptcha|hcaptcha|challenges\.cloudflare|turnstile|arkoselabs|funcaptcha/.test(s));
  const sel = document.querySelector('.g-recaptcha, .h-captcha, .cf-turnstile, #challenge-form, [data-sitekey]');
  const text = ((document.body && document.body.innerText) || '').slice(0, 4000).toLowerCase();
  const phrases = ["verify you are human", "i'm not a robot", "i am not a robot", "checking your browser",
                   "are you a robot", "complete the security check", "unusual traffic", "press and hold"];
  const phrase = phrases.find(p => text.includes(p)) || '';
  return { detected: !!(hit || sel || phrase), reason: phrase || (hit ? 'captcha iframe' : (sel ? 'captcha widget' : '')) };
}
"""

FOCUSED_JS = r"""
() => {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  return {
    tag: el.tagName.toLowerCase(),
    type: (el.getAttribute('type') || '').toLowerCase(),
    role: el.getAttribute('role') || '',
    name: el.getAttribute('name') || '',
    id: el.id || '',
    placeholder: el.getAttribute('placeholder') || '',
    label: clean(el.getAttribute('aria-label') || ''),
    autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
    editable: !!el.isContentEditable,
    in_form: !!el.closest('form')
  };
}
"""
