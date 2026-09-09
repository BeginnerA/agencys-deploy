// floating-agent.js — 全局悬浮智能助手（screen.html 全站注入，右下角入口）
// 独立会话 float（按用户隔离，服务端 mine 语义），可随时在任意页面询问；
// 流式渲染复用 agent.css 的 .msg-* 全局样式；网络层复用 agent-stream.js 的 streamAgentChat。
// 会话历史/审批单等完整能力在智能助手页（#/agent）提供。
(function(){
  const SESSION = 'float'; // 悬浮助手固定会话（每用户独立，见后端 mine 过滤）
  const role = (typeof currentUser === 'function' ? currentUser().role : '') || 'viewer';
  const canManage = ['admin', 'operator'].includes(role);
  const SUGS = canManage ? [
    'pay 服务现在状态如何？',
    '查看未解决告警并分析原因',
    '把 order 服务回滚到上一版本，先做计划'
  ] : [
    'pay 服务现在状态如何？',
    '最近 1 小时有没有任务失败？',
    '查看未解决告警并分析原因'
  ];
  let open = false;
  let busy = false;
  let cancelReq = null;   // 当前流的取消函数（streamAgentChat 返回）
  let seq = 0;            // 请求序号：收起/重开后旧流回调不再生效
  let pendingTools = [];  // 未收尾的工具行

  // ---- 骨架 ----
  function build(){
    const root = document.createElement('div');
    root.id = 'floatAgentUi';
    root.innerHTML = `
      <button class="fab-btn" id="fabBtn" title="智能运维助手（全站可用）">🤖</button>
      <div class="fab-panel hidden" id="fabPanel">
        <div class="fab-head">
          <span class="fab-title">🤖 智能运维助手</span>
          <span class="fab-sp"></span>
          <a class="fab-full" id="fabFull" href="#/agent" title="打开完整页：多会话管理 / 审批单 / 历史">完整版 ↗</a>
          <button class="fab-x" id="fabX" title="收起">✕</button>
        </div>
        <div class="fab-body" id="fabMsgs"></div>
        <div class="fab-input">
          <textarea id="fabInput" placeholder="${canManage
            ? '例：把 pay 服务回滚到上一版本，先做计划'
            : '例：pay 服务现在状态如何？'}（Enter 发送 / Shift+Enter 换行）"></textarea>
          <button class="btn primary fab-send" id="fabSend">发送</button>
        </div>
      </div>`;
    document.body.appendChild(root);
    document.getElementById('fabBtn').onclick = () => open ? collapse() : expand();
    document.getElementById('fabX').onclick = collapse;
    document.getElementById('fabFull').onclick = collapse;
    const ta = document.getElementById('fabInput');
    const send = () => { const t = (ta.value || '').trim(); if (!t) return; ta.value = ''; sendMsg(t); };
    document.getElementById('fabSend').onclick = send;
    ta.onkeydown = (e) => {
      if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); send(); }
    };
  }

  // ---- 开合 ----
  function expand(){
    open = true;
    const panel = document.getElementById('fabPanel');
    panel.classList.remove('hidden');
    loadHistory();
    const ta = document.getElementById('fabInput');
    if (ta) setTimeout(() => ta.focus(), 60);
  }
  function collapse(){
    open = false;
    const panel = document.getElementById('fabPanel');
    if (panel) panel.classList.add('hidden');
    // 中止进行中的流并作废其回调（seq 防串扰）
    if (cancelReq){ try{ cancelReq(); }catch(e){} }
    seq++;
    cancelReq = null;
    pendingTools = [];
    busy = false;
    updateBusy();
  }

  // ---- 历史（重开面板时加载，服务端按用户隔离） ----
  function loadHistory(){
    const box = document.getElementById('fabMsgs');
    if (!box) return;
    box.innerHTML = emptyHtml();
    bindSugs(box);
    if (!canManage) return; // viewer 无消息回溯权限：保持空会话提示
    api('/api/agent/sessions/' + SESSION + '/messages?limit=30').then(d => {
      if (!open || !document.getElementById('fabMsgs')) return;
      const list = (d.messages || [])
        .filter(m => ['user', 'assistant'].includes(m.role));
      if (list.length){
        box.innerHTML = list.map(m => {
          const isU = m.role === 'user';
          return `<div class="msg-row ${isU ? 'user' : ''}">
            <div class="msg-ava">${isU ? '🧑' : '🤖'}</div>
            <div class="msg-bubble">${esc(m.content || '')}</div></div>`;
        }).join('');
        scrollToBottom();
      }
    }).catch(() => {});
  }
  function emptyHtml(){
    const sugs = SUGS.map(s =>
      `<span data-sug="${esc(s)}">${esc(s)}</span>`).join('');
    return `<div class="chat-empty"><span class="big">🤖</span>
      全站可用的运维智能助手：询问服务状态、告警归因、发布记录等，
      ${canManage ? '描述发布意图可生成审批单' : '仅只读查询'}。
      <div class="chat-sug">${sugs}</div></div>`;
  }
  function bindSugs(box){
    box.querySelectorAll('[data-sug]').forEach(s => {
      s.onclick = () => sendMsg(s.dataset.sug);
    });
  }
  function scrollToBottom(){
    const box = document.getElementById('fabMsgs');
    if (box) box.scrollTop = box.scrollHeight;
  }

  // ---- 消息渲染（气泡文本一律 createTextNode，避免覆盖工具行元素） ----
  function addBubble(role, content){
    const box = document.getElementById('fabMsgs');
    if (!box) return null;
    const empty = box.querySelector('.chat-empty');
    if (empty) empty.remove();
    const row = document.createElement('div');
    row.className = 'msg-row' + (role === 'user' ? ' user' : '');
    row.innerHTML = `<div class="msg-ava">${role === 'user' ? '🧑' : '🤖'}</div>
      <div class="msg-bubble"></div>`;
    const bub = row.querySelector('.msg-bubble');
    bub.appendChild(document.createTextNode(content || ''));
    box.appendChild(row);
    scrollToBottom();
    return bub;
  }
  function streamBubble(){
    const box = document.getElementById('fabMsgs');
    if (!box) return null;
    const empty = box.querySelector('.chat-empty');
    if (empty) empty.remove();
    const row = document.createElement('div');
    row.className = 'msg-row';
    row.innerHTML = `<div class="msg-ava">🤖</div>
      <div class="msg-bubble"><span class="stream-hint" id="fabHint">思考中…</span></div>`;
    box.appendChild(row);
    scrollToBottom();
    return row.querySelector('.msg-bubble');
  }
  function clearHint(bubble){
    const h = bubble.querySelector('.stream-hint');
    if (h) h.remove();
  }
  function addToolLine(bubble, name){
    if (!bubble) return null;
    clearHint(bubble);
    const line = document.createElement('div');
    line.className = 'msg-tool';
    line.dataset.name = name || '';
    line.innerHTML = `<span class="mark">⏳</span><span>正在调用工具 ${esc(name || '')} …</span>`;
    bubble.appendChild(line);
    scrollToBottom();
    return line;
  }
  function finishToolLine(line, ok){
    if (!line) return;
    line.classList.add(ok ? 'ok' : 'fail');
    const name = line.dataset.name || '';
    line.innerHTML = `<span class="mark">${ok ? '✅' : '❌'}</span>` +
      `<span>${ok ? '工具调用完成：' : '工具调用失败：'}${esc(name)}</span>`;
    scrollToBottom();
  }
  function appendDelta(bubble, text){
    if (!bubble || !text) return false;
    clearHint(bubble);
    bubble.appendChild(document.createTextNode(text));
    scrollToBottom();
    return true;
  }
  function appendErr(bubble, msg, hint){
    if (!bubble) return;
    clearHint(bubble);
    const d = document.createElement('div');
    d.className = 'msg-err';
    d.textContent = (msg || '请求失败') + (hint ? '\n💡 ' + hint : '');
    bubble.appendChild(d);
    scrollToBottom();
  }
  function attachPlanTip(bubble, runId){
    if (!bubble || !runId) return;
    const tip = document.createElement('div');
    tip.className = 'plan-tip';
    const t = document.createElement('span');
    t.textContent = '📝 已生成审批单 #' + runId;
    const go = document.createElement('button');
    go.className = 'btn sm primary';
    go.textContent = '去审批';
    go.onclick = () => { collapse(); location.hash = '#/agent'; };
    tip.appendChild(t);
    tip.appendChild(go);
    bubble.appendChild(tip);
    scrollToBottom();
  }

  // ---- 发送（SSE 流式，与智能助手页同一事件协议） ----
  function sendMsg(text){
    if (busy) return;
    const my = ++seq;
    const box = document.getElementById('fabMsgs');
    const ta = document.getElementById('fabInput');
    if (!box) return;
    addBubble('user', text);
    const bubble = streamBubble();
    busy = true;
    updateBusy();
    let gotText = false, ended = false;
    const onEvent = (ev) => {
      const t = ev.type || '';
      if (t === 'text'){
        gotText = appendDelta(bubble, ev.text || '') || gotText;
      } else if (t === 'tool'){
        const line = addToolLine(bubble, ev.name);
        if (line) pendingTools.push(line);
      } else if (t === 'tool_result'){
        for (let i = pendingTools.length - 1; i >= 0; i--){
          if (pendingTools[i].dataset.name === (ev.name || '')){
            finishToolLine(pendingTools[i], !!ev.ok);
            pendingTools.splice(i, 1);
            break;
          }
        }
      } else if (t === 'done'){
        ended = true;
        clearHint(bubble);
        const runId = (ev.result || {}).plan_run_id;
        if (runId) attachPlanTip(bubble, runId);
      } else if (t === 'error'){
        ended = true;
        appendErr(bubble, ev.error || '对话出错', ev.hint || '');
      }
    };
    const onClose = () => {
      if (my !== seq) return; // 已被收起/新请求取代：旧流回调作废
      busy = false;
      cancelReq = null;
      pendingTools = [];
      updateBusy();
      if (ta) ta.focus();
      if (bubble && !bubble.isConnected) return; // 面板已重载历史：静默
      if (!ended && !gotText) appendErr(bubble, '连接中断，未收到完整回复', '');
      else if (!ended && gotText) appendErr(bubble, '\n\n⚠️ 回复中断（连接异常）', '');
    };
    const onFatal = (msg, hint) => {
      ended = true;
      appendErr(bubble, msg, hint);
    };
    cancelReq = streamAgentChat({
      message: text,
      sessionId: SESSION,
      on401: () => {
        localStorage.removeItem('token'); localStorage.removeItem('user');
        location.href = '/';
      },
      onEvent: onEvent,
      onFatal: onFatal,
      onClose: onClose
    });
  }
  function updateBusy(){
    const b = document.getElementById('fabSend');
    if (!b) return;
    b.disabled = busy;
    b.textContent = busy ? '回复中…' : '发送';
  }

  // ---- 初始化（script 位于 body 尾部，DOM 已就绪） ----
  build();
})();
