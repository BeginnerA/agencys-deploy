// components.js 自绘下拉键盘导航回归：↑↓ 移动、Enter 选择、Esc 关闭
// 运行：node tests/js/select_keyboard_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const all = [];
const noop = () => {};
function makeEl(tag) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(),
    nodeType: 1,
    children: [], style: {}, dataset: {}, attrs: {},
    textContent: '', value: '', disabled: false,
    offsetWidth: 200, offsetHeight: 100,
    classList: {
      _s: new Set(),
      add(...c) { c.forEach(x => this._s.add(x)); },
      remove(...c) { c.forEach(x => this._s.delete(x)); },
      toggle(c, on) {
        const v = on === undefined ? !this._s.has(c) : !!on;
        v ? this._s.add(c) : this._s.delete(c);
      },
      contains(c) { return this._s.has(c); },
    },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    insertBefore(c, ref) {
      const i = this.children.indexOf(ref);
      this.children.splice(i < 0 ? this.children.length : i, 0, c);
      c.parentNode = this; return c;
    },
    remove() {
      if (this.parentNode) {
        const i = this.parentNode.children.indexOf(this);
        if (i >= 0) this.parentNode.children.splice(i, 1);
      }
    },
    addEventListener(ev, fn) { this['on' + ev] = fn; },
    removeEventListener() {},
    getBoundingClientRect() { return {top: 20, left: 10, bottom: 60, width: 200}; },
    focus() { this.focused = true; },
    click() { if (this.onclick) this.onclick({stopPropagation: noop}); },
    contains() { return false; },
    dispatchEvent(ev) { (this.events = this.events || []).push(ev.type); },
    querySelectorAll(sel) {
      const out = [];
      for (const c of this.children) {
        const cls = String(c.className || '');
        if (sel.includes('sel-opt') && !cls.split(/\s+/).includes('sel-opt')) continue;
        if (sel.includes('not(.sel-empty)') && cls.split(/\s+/).includes('sel-empty')) continue;
        out.push(c);
      }
      return out;
    },
    querySelector(sel) {
      const r = this.querySelectorAll(sel);
      return r.length ? r[0] : null;
    },
  };
  Object.defineProperty(el, 'className', {
    get() { return [...this.classList._s].join(' '); },
    set(v) { this.classList._s = new Set(String(v || '').split(/\s+/).filter(Boolean)); },
  });
  all.push(el);
  return el;
}

const ctx = {
  console, setTimeout, clearTimeout,
  Event: class { constructor(type, opts) { this.type = type; this.bubbles = !!(opts && opts.bubbles); } },
  document: {
    readyState: 'complete',
    body: makeEl('body'),
    documentElement: {clientWidth: 1280, clientHeight: 800},
    getElementById(id) { return all.find(e => e.id === id) || null; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    createElement: makeEl,
    addEventListener: noop,
    removeEventListener: noop,
  },
  window: {},
  MutationObserver: class { observe() {} disconnect() {} },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(
  path.join(__dirname, '../../webapp/static/js/components.js'), 'utf8'), ctx);

const failures = [];
function check(name, cond, detail) {
  if (!cond) failures.push(name + (detail !== undefined ? ': ' + detail : ''));
}

const parent = makeEl('div');
const sel = makeEl('select');
sel.options = [
  {value: 'a', text: 'Alpha', selected: true, title: ''},
  {value: 'b', text: 'Beta', selected: false, title: 'second'},
  {value: 'c', text: 'Gamma', selected: false, title: ''},
];
sel.value = 'a';
sel.dataset.search = '1';
Object.defineProperty(sel, 'selectedOptions', {
  get() { return this.options.filter(o => o.selected); },
});
parent.appendChild(sel);

ctx.enhanceSelect(sel);
const face = all.find(e => String(e.className).includes('sel-face'));
const pop = all.find(e => String(e.className).includes('sel-pop'));
check('face created', !!face);
check('pop created', !!pop);

face.onclick({stopPropagation: noop});
check('panel opens', pop && !pop.classList.contains('hidden'));
const search = all.find(e => String(e.className).includes('sel-search'));
check('search input created', !!search);

search.onkeydown({key: 'ArrowDown', preventDefault: noop});
let active = all.filter(e => String(e.className).includes('sel-opt'))
  .find(e => e.classList.contains('kb-active'));
check('ArrowDown highlights an option', !!active, active && active.textContent);
const firstActive = active;

search.onkeydown({key: 'ArrowDown', preventDefault: noop});
active = all.filter(e => String(e.className).includes('sel-opt'))
  .find(e => e.classList.contains('kb-active'));
check('ArrowDown moves to next option', active && active !== firstActive,
      active && active.textContent);

search.onkeydown({key: 'Enter', preventDefault: noop});
check('Enter selects highlighted option', sel.value === 'b', sel.value);
check('change event dispatched', (sel.events || []).includes('change'),
      String(sel.events));
check('panel closes after selection', pop.classList.contains('hidden'));

// 重新打开：↑ 从末项开始/回绕，Esc 关闭
face.onclick({stopPropagation: noop});
const search2 = all.filter(e => String(e.className).includes('sel-search')).pop();
search2.onkeydown({key: 'ArrowUp', preventDefault: noop});
active = all.filter(e => String(e.className).includes('sel-opt'))
  .find(e => e.classList.contains('kb-active'));
check('ArrowUp highlights previous option', !!active, active && active.textContent);
search2.onkeydown({key: 'Escape', stopPropagation: noop});
check('Escape closes panel', pop.classList.contains('hidden'));

if (failures.length) {
  console.error('FAIL\n' + failures.join('\n'));
  process.exit(1);
}
console.log('select keyboard tests passed');
