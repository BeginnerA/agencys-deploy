// 409 active_task（发布目标被占用）的前端取文案链路回归测试
// 运行：node tests/js/api_task_busy_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ctx = {
  console,
  Promise,
  TextEncoder,
  localStorage: { getItem: () => 'tok', removeItem: () => {} },
  location: { href: '' },
  Map,
  Object,
  JSON,
  String,
};
vm.createContext(ctx);
vm.runInContext(
  fs.readFileSync(path.join(__dirname, '../../webapp/static/js/api.js'), 'utf8'), ctx);

const failures = [];
function check(name, cond, detail) {
  if (!cond) failures.push(name + (detail !== undefined ? ': ' + detail : ''));
}

const BUSY = { error: 'active_task', message: '「auth」已有活动任务 #7（状态 running），请等其结束或先终止该任务',
               active_task: { task_id: 7, status: 'running', name: 'auth' } };

// 1) 后端回 409 active_task 时，err 上必须同时带得到机器码与可读说明
ctx.fetch = () => Promise.resolve({
  ok: false, status: 409, json: async () => BUSY,
});
ctx._apiCall('/api/tasks', { method: 'POST' }).then(
  () => check('409 rejects', false, 'resolved instead of rejected'),
  (e) => {
    check('machine code kept on e.message', e.message === 'active_task', e.message);
    check('body attached as e.data', !!(e.data && e.data.active_task), JSON.stringify(e.data));

    // 2) 入口展示的是中文说明，不是 "active_task"
    const text = ctx.taskBusyText(e);
    check('busy text is the human message', text === BUSY.message, text);
    check('busy text never leaks machine code', !/active_task/.test(text), text);

    // 3) 其它错误码不占用该映射（window_warn 仍走原有确认弹窗分支）
    const warn = { message: 'window_warn', data: { error: 'window_warn', window: { name: '封网' } } };
    check('window_warn not treated as busy', ctx.taskBusyText(warn) === '');
    check('plain http error not treated as busy',
          ctx.taskBusyText({ message: 'HTTP 500', data: {} }) === '');
    check('missing detail body degrades to empty text',
          ctx.taskBusyText({ message: 'active_task', data: {} }) === '');
    check('undefined error tolerated', ctx.taskBusyText(undefined) === '');

    if (failures.length) {
      console.error('FAIL\n' + failures.join('\n'));
      process.exit(1);
    }
    console.log('task busy text tests passed');
    process.exit(0);
  },
).catch((e) => { console.error(e); process.exit(1); });
