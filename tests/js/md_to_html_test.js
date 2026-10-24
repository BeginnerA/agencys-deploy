// components.js 的 mdToHtml 回归测试（Node 环境 + 最小 DOM stub）
// 运行：node tests/js/md_to_html_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const noop = () => {};
const fakeEl = () => ({
  style: {}, dataset: {}, classList: { add: noop, remove: noop, contains: () => false },
  setAttribute: noop, appendChild: noop, addEventListener: noop,
  querySelectorAll: () => [], remove: noop, focus: noop,
});
const ctx = {
  console,
  setTimeout, clearTimeout, setInterval, clearInterval,
  document: {
    readyState: 'complete',
    body: { appendChild: noop },
    addEventListener: noop,
    removeEventListener: noop,
    getElementById: () => null,
    querySelectorAll: () => [],
    createElement: fakeEl,
    documentElement: { clientWidth: 1280, clientHeight: 800 },
  },
  window: {},
  MutationObserver: class { observe() {} disconnect() {} },
};
vm.createContext(ctx);
const source = fs.readFileSync(
  path.join(__dirname, '../../webapp/static/js/components.js'), 'utf8');
vm.runInContext(source, ctx);

const failures = [];
function check(name, cond, detail) {
  if (!cond) failures.push(name + (detail ? ': ' + detail : ''));
}

const payloads = [
  '[x](https://evil.example/"><img/src=x/onerror=alert(1)>)',
  "[x](https://evil.example/'><svg/onload=alert(1)>)",
  '[x](https://evil.example/"onmouseover="alert(1))',
];
for (const p of payloads) {
  const html = ctx.mdToHtml(p);
  check('no injected tags for ' + p, !/<(img|svg|script)\b/i.test(html), html);
  check('no event attrs for ' + p, !/\son\w+\s*=/i.test(html), html);
  check('no attr break-out for ' + p, !/["']\s*on\w+\s*=/i.test(html), html);
  check('href stays single attribute for ' + p,
        !/href="[^"]*"[^>]*\son/i.test(html), html);
}

const legit = ctx.mdToHtml('[docs](https://example.com/a?x=1&y=2)');
check('legit link preserved',
      legit.includes('href="https://example.com/a?x=1&amp;y=2"'), legit);
check('legit link text preserved', legit.includes('>docs</a>'), legit);

const jsUrl = ctx.mdToHtml('[x](javascript:alert(1))');
check('javascript url not linkified', !jsUrl.includes('<a '), jsUrl);

const raw = ctx.mdToHtml('<img src=x onerror=alert(1)>');
check('raw tag escaped', !raw.includes('<img'), raw);

if (failures.length) {
  console.error('FAIL\n' + failures.join('\n'));
  process.exit(1);
}
console.log('markdown XSS tests passed');
