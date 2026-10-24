// agent.js 会话切换竞态回归：旧会话的消息/追尾响应不得写进新会话
// 运行：node tests/js/agent_stale_session_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const elements = {};
const all = [];
function makeEl(tag) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(),
    nodeType: 1,
    children: [], style: {}, dataset: {}, attrs: {},
    textContent: '', innerHTML: '', value: '', disabled: false,
    options: [], selectedOptions: [],
    scrollTop: 0, scrollHeight: 100,
    classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    setAttribute(){}, getAttribute(){ return null; }, removeAttribute(){},
    appendChild(c){ this.children.push(c); c.parentNode = this; return c; },
    insertBefore(c){ this.children.push(c); return c; },
    remove(){}, focus(){}, addEventListener(){}, removeEventListener(){},
    querySelector(){ return null; },
    querySelectorAll(){ return []; },
    getBoundingClientRect(){ return {top:0,left:0,bottom:0,width:100}; },
    dispatchEvent(){},
  };
  all.push(el);
  return el;
}
function el(id) { return elements[id] || (elements[id] = makeEl('div')); }

const msgRequests = [];
function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}
function api(url) {
  if (url === '/api/agent/sessions') return Promise.resolve({ sessions: [] });
  if (url === '/api/agent/config') return Promise.resolve({ available: false });
  const m = /\/api\/agent\/sessions\/([^/]+)\/messages\?limit=40/.exec(url);
  if (m) {
    const d = deferred();
    msgRequests.push({ sessionId: decodeURIComponent(m[1]), resolve: d.resolve });
    return d.promise;
  }
  return Promise.resolve({});
}

const liveFns = [];
const ctx = {
  console, URLSearchParams, Promise, setTimeout, clearTimeout,
  setInterval: () => 1, clearInterval: () => {},
  api,
  esc: s => String(s == null ? '' : s),
  mdToHtml: s => String(s == null ? '' : s),
  fmtTime: t => String(t),
  emptyState: (ico, text) => text,
  skeletonHTML: () => 'loading',
  liveBind: (topics, fn) => { liveFns.push(fn); return () => {}; },
  currentUser: () => ({ role: 'operator' }),
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  document: {
    hidden: false,
    body: makeEl('body'),
    getElementById: el,
    createElement: makeEl,
    querySelectorAll: () => [],
  },
  window: {},
};
vm.createContext(ctx);
const agentPath = process.env.AGENT_JS
  || path.join(__dirname, '../../webapp/static/js/pages/agent.js');
vm.runInContext(fs.readFileSync(agentPath, 'utf8'), ctx);

const flush = () => new Promise(r => setImmediate(r));
const failures = [];
function check(name, cond, detail) {
  if (!cond) failures.push(name + (detail !== undefined ? ': ' + detail : ''));
}

(async () => {
  const root = makeEl('div');
  await ctx.window.agent.render(root);
  await flush();

  const msgs = el('chatMsgs');
  const sessSel = el('sessSel');
  check('initial session fetched', msgRequests.length === 1 && msgRequests[0].sessionId === 'web',
        JSON.stringify(msgRequests.map(r => r.sessionId)));
  check('session switch bound', typeof sessSel.onchange === 'function');

  // 切到 s2：s2 的消息请求悬置，此时 web 的旧请求才返回
  sessSel.value = 's2';
  sessSel.onchange();
  await flush();
  check('new session fetched', msgRequests.some(r => r.sessionId === 's2'));

  msgRequests[0].resolve({ messages: [{ id: 1, role: 'assistant', content: 'WEB-OLD' }] });
  await flush();
  check('stale session response ignored', !msgs.innerHTML.includes('WEB-OLD'), msgs.innerHTML);

  const s2Req = msgRequests.find(r => r.sessionId === 's2' && !r.done);
  s2Req.done = true;
  s2Req.resolve({ messages: [{ id: 5, role: 'assistant', content: 'S2-CONTENT' }] });
  await flush();
  check('current session rendered', msgs.innerHTML.includes('S2-CONTENT'), msgs.innerHTML);

  // 追尾请求发出后用户再次切会话：旧追尾响应必须丢弃
  check('live refresh bound', liveFns.length > 0);
  liveFns[0]();
  await flush();
  const tailReq = msgRequests[msgRequests.length - 1];
  check('tail request issued', tailReq && tailReq.sessionId === 's2', tailReq && tailReq.sessionId);

  sessSel.value = 's3';
  sessSel.onchange();
  await flush();
  tailReq.resolve({ messages: [
    { id: 5, role: 'assistant', content: 'S2-CONTENT' },
    { id: 9, role: 'assistant', content: 'S2-TAIL-STALE' },
  ] });
  await flush();
  check('stale tail response ignored', !msgs.innerHTML.includes('S2-TAIL-STALE'), msgs.innerHTML);

  const s3Req = msgRequests.find(r => r.sessionId === 's3');
  s3Req.resolve({ messages: [{ id: 6, role: 'assistant', content: 'S3-CONTENT' }] });
  await flush();
  check('latest session rendered after tail race',
        msgs.innerHTML.includes('S3-CONTENT') && !msgs.innerHTML.includes('S2-TAIL-STALE'),
        msgs.innerHTML);

  if (failures.length) {
    console.error('FAIL\n' + failures.join('\n'));
    process.exit(1);
  }
  console.log('agent stale session tests passed');
})().catch(e => {
  console.error(e);
  process.exit(1);
});
