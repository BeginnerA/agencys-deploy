// logs.js 追尾/全量刷新竞态回归：旧请求晚到不得回填日志或倒退 offset
// 运行：node tests/js/logs_tail_race_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const elements = {};
function makeEl(id) {
  return {
    id, innerHTML: '', textContent: '', scrollTop: 0, scrollHeight: 100,
    style: {}, dataset: {}, disabled: false, value: '',
    options: [], selectedOptions: [],
    querySelectorAll: () => [],
    addEventListener: () => {},
    classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
  };
}
function el(id) {
  return elements[id] || (elements[id] = makeEl(id));
}

const logRequests = [];   // {url, offset, resolve}
function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}
function api(url) {
  if (url === '/api/projects') return Promise.resolve([]);
  if (url.startsWith('/api/tasks?')) return Promise.resolve([]);
  const m = /\/api\/tasks\/(\d+)\/log\?offset=(\d+)/.exec(url);
  if (m) {
    const d = deferred();
    logRequests.push({ url, taskId: Number(m[1]), offset: Number(m[2]), resolve: d.resolve });
    return d.promise;
  }
  return Promise.resolve({});
}

const timers = [];
const ctx = {
  console,
  URLSearchParams,
  Promise,
  setTimeout,
  clearTimeout,
  setInterval: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
  clearInterval: id => { if (id) timers[id - 1] = null; },
  api,
  esc: s => String(s == null ? '' : s),
  fmtTime: t => String(t),
  statusBadge: s => 'status:' + s,
  isAdmin: () => false,
  liveBind: () => () => {},
  document: {
    getElementById: el,
    querySelectorAll: () => [],
  },
  window: {},
};
vm.createContext(ctx);
const logsPath = process.env.LOGS_JS
  || path.join(__dirname, '../../webapp/static/js/pages/logs.js');
vm.runInContext(fs.readFileSync(logsPath, 'utf8'), ctx);

const flush = () => new Promise(r => setImmediate(r));
const failures = [];
function check(name, cond, detail) {
  if (!cond) failures.push(name + (detail !== undefined ? ': ' + detail : ''));
}

(async () => {
  const root = makeEl('pageRoot');
  await ctx.window.logs.render(root);

  const taskSel = el('logTaskSel');
  const refresh = el('logRefresh');
  const box = el('logBox');
  check('render binds task selector', typeof taskSel.onchange === 'function');
  check('render binds refresh', typeof refresh.onclick === 'function');

  // 启动任务 5 的全量拉取（请求 1 悬置）
  taskSel.value = '5';
  taskSel.onchange();
  await flush();
  check('first full fetch issued', logRequests.length === 1, logRequests.length);
  check('first full fetch starts at offset 0', logRequests[0] && logRequests[0].offset === 0,
        logRequests[0] && logRequests[0].offset);

  // 用户点“全量刷新”：请求 2 启动，请求 1 变成过期请求
  refresh.onclick();
  await flush();
  check('second full fetch issued', logRequests.length === 2, logRequests.length);

  // 旧请求晚到：带 OLD 内容，必须被丢弃
  logRequests[0].resolve({ log: 'OLD', offset: 3, status: 'success' });
  await flush();
  check('stale response does not render', box.textContent !== 'OLD', box.textContent);

  // 新请求返回增量，继续拉取；随后进入追尾并提交 offset=6
  logRequests[1].resolve({ log: 'NEW', offset: 3, status: 'running' });
  await flush();
  check('fresh fetch continues at old offset', logRequests.length === 3, logRequests.length);
  check('fresh fetch resumes from advanced offset',
        logRequests[2] && logRequests[2].offset === 3,
        logRequests[2] && logRequests[2].offset);

  logRequests[2].resolve({ log: '', offset: 6, status: 'running' });
  await flush();
  check('fresh log rendered', box.textContent === 'NEW', box.textContent);
  check('tail started for running task', timers.length === 1, timers.length);

  // 关键竞态：全量刷新完成后，旧请求才返回 OLD。
  // 若缺少代次校验，它会把 offset 倒退回 3 并覆盖日志。
  logRequests[0].resolve({ log: 'OLD', offset: 3, status: 'success' });
  await flush();
  check('late stale response does not overwrite log', box.textContent === 'NEW', box.textContent);

  // 触发追尾：必须从已提交的 offset=6 继续，而不是旧响应的 3
  timers[0].fn();
  await flush();
  const tailReq = logRequests[3];
  check('tail fetch issued', !!tailReq, logRequests.length);
  check('tail uses committed offset', tailReq && tailReq.offset === 6,
        tailReq && tailReq.offset);
  tailReq.resolve({ log: 'MORE', offset: 10, status: 'success' });
  await flush();
  check('tail appends increment', box.textContent === 'NEWMORE', box.textContent);
  check('status badge updated', el('logTaskStatus').innerHTML === 'status:success',
        el('logTaskStatus').innerHTML);

  if (failures.length) {
    console.error('FAIL\n' + failures.join('\n'));
    process.exit(1);
  }
  console.log('logs tail race tests passed');
})().catch(e => {
  console.error(e);
  process.exit(1);
});
