// live.js 回归测试：绑定即刷新一次；重连 hello 触发全量 resync
// 运行：node tests/js/live_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const noop = () => {};
let readResolve = null;
const encoder = new TextEncoder();
const events = {};
const documentStub = {
  hidden: false,
  addEventListener: (ev, fn) => { (events[ev] = events[ev] || []).push(fn); },
  removeEventListener: noop,
  getElementById: () => null,
  querySelectorAll: () => [],
  createElement: () => ({style: {}, classList: {add: noop, remove: noop, toggle: noop}}),
};
const ctx = {
  console,
  setTimeout, clearTimeout, setInterval, clearInterval,
  TextDecoder, AbortController,
  localStorage: {getItem: () => 'test-token'},
  token: () => 'test-token',
  fetch: () => Promise.resolve({
    ok: true,
    body: {getReader: () => ({read: () => new Promise(r => { readResolve = r; })})},
  }),
  document: documentStub,
  window: {},
  MutationObserver: class { observe() {} disconnect() {} },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(
  path.join(__dirname, '../../webapp/static/js/live.js'), 'utf8'), ctx);

const failures = [];
function check(name, cond, detail) {
  if (!cond) failures.push(name + (detail !== undefined ? ': ' + detail : ''));
}
const tick = (ms = 0) => new Promise(r => setTimeout(r, ms));
function pushFrame(obj) {
  const text = 'data: ' + JSON.stringify(obj) + '\n\n';
  const resume = readResolve;
  readResolve = null;
  if (resume) resume({done: false, value: encoder.encode(text)});
}

(async () => {
  let calls = 0;
  const off = ctx.window.liveBind('alerts', () => { calls++; }, 60000);
  check('liveBind invokes callback immediately', calls === 1, 'calls=' + calls);
  await tick(10);                       // 等首帧读取挂起

  pushFrame({type: 'hello'});
  await tick(20);
  check('first hello does not duplicate initial refresh', calls === 1, 'calls=' + calls);

  // 模拟断线重连后的第二个 hello：必须触发一次 resync（所有绑定刷新）
  pushFrame({type: 'hello'});
  await tick(500);                      // 等合并窗口
  check('reconnect hello triggers resync', calls === 2, 'calls=' + calls);

  pushFrame({type: 'event', topic: 'tasks'});
  await tick(500);
  check('unrelated topic does not refresh alerts binding', calls === 2,
        'calls=' + calls);

  pushFrame({type: 'event', topic: 'alerts'});
  await tick(500);
  check('matching topic refreshes binding', calls === 3, 'calls=' + calls);

  off();
  ctx.window.liveStop();
  if (failures.length) {
    console.error('FAIL\n' + failures.join('\n'));
    process.exit(1);
  }
  console.log('live tests passed');
  process.exit(0);
})().catch(e => {
  console.error(e);
  process.exit(1);
});
