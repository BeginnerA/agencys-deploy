// pages/agent.js — 智能助手（对话 / 审批单 / 决策建议 / 故障案例 / 网关配置）
// 消费后端 /api/agent/* API；角色：viewer=只读对话；operator/admin=审批/建议/案例
(function(){
  let liveOffs = [];   // 列表页实时绑定（事件驱动；降级时按 15s 兜底轮询）
  function clearLiveOffs(){ liveOffs.forEach(off => { try{ off(); }catch(e){} }); liveOffs = []; }

  // 当前页签刷新（后台 advisor 建议与审批单执行态会持续变化）；
  // 案例页无事件源，靠同一绑定的兜底轮询兼顾
  function refreshLiveTab(){
    if (destroyed || document.hidden) return;
    if (currentTab === 'runs') loadRuns();
    else if (currentTab === 'advice') loadAdvice();
    else if (currentTab === 'cases') loadCases();
    else if (currentTab === 'chat') refreshChatTail();
  }
  let destroyed = false;
  let chatAbort = null;    // 流式对话 AbortController（切页时中止）
  let pendingTools = [];   // 流式对话中未收尾的工具行（tool → tool_result）
  let chatLastId = 0;      // 当前会话已渲染的最大消息 id（后台回写新消息时据此增量刷新）
  let currentTab = 'chat';   // chat | runs | advice | cases

  // ---- 页面状态 ----
  let gw = null;             // GET /api/agent/config 缓存
  let runs = [], runStatus = 'all';
  let adviceList = [], adviceScope = '', adviceStatus = 'pending';
  let cases = [], caseKw = '', caseSvc = '', caseTag = '';

  const role = currentUser().role || 'viewer';
  const canManage = ['admin','operator'].includes(role);
  // 当前会话 id：管理角色可在多会话间切换（localStorage 记住）；viewer 固定 web 单会话
  let currentSession = canManage
    ? (localStorage.getItem('agentCurSession') || 'web')
    : 'web';
  let sessPinned = false;   // 当前会话置顶态（refreshSessSel 同步）

  const RUN_LABEL = {
    awaiting_approval: {label:'待审批', cls:'pending', ico:'📝'},
    rejected:         {label:'已驳回', cls:'failed',   ico:'🚫'},
    executing:        {label:'执行中', cls:'running',  ico:'⚙️'},
    success:          {label:'成功',   cls:'success',  ico:'✅'},
    failed:           {label:'失败',   cls:'failed',   ico:'❌'}
  };
  const SCOPE_LABEL = {
    canary:'灰度决策', task_failed:'失败归因', alert:'告警归因', precheck:'发布预检'
  };
  const OP_LABEL = {deploy:'发布', rollback:'回滚', restart:'重启', schedule_run:'定时发布'};
  const CHAT_SUG = [
    'pay 服务现在状态如何？',
    '最近 1 小时有没有任务失败？',
    '查看未解决告警并分析原因',
    '把 order 服务回滚到上一个版本，先做计划'
  ];

  // ---- 工具 ----
  function qs(obj){
    const p = new URLSearchParams();
    Object.entries(obj).forEach(([k,v]) => { if (v !== '' && v != null) p.set(k, v); });
    const s = p.toString(); return s ? '?' + s : '';
  }
  function parsePlan(p){
    if (!p) return {};
    if (typeof p === 'string'){ try { return JSON.parse(p); }catch(e){ return {}; } }
    return (p && typeof p === 'object') ? p : {};
  }
  function setPanel(html){
    const tc = document.getElementById('tabContent');
    if (tc){ tc.innerHTML = html || '<div class="muted" style="padding:30px;text-align:center">暂无内容</div>'; }
  }
  function scrollBottom(el){ if (el) el.scrollTop = el.scrollHeight; }

  // ---- 主渲染 ----
  function renderPage(){
    const root = document.getElementById('pageRoot');
    root.innerHTML = `
      <div class="agent-tabs-bar">
        <button class="tab-btn ${currentTab==='chat'?'active':''}" data-tab="chat">💬 对话助手</button>
        <button class="tab-btn ${currentTab==='runs'?'active':''}" data-tab="runs">📝 发布审批单</button>
        <button class="tab-btn ${currentTab==='advice'?'active':''}" data-tab="advice">🤖 决策建议</button>
        <button class="tab-btn ${currentTab==='cases'?'active':''}" data-tab="cases">📚 故障案例库</button>
        <span class="spacer"></span>
        ${role === 'admin' ? '<button class="btn sm ghost" id="digestChip">📤 巡检日报</button>' : ''}
        <span class="gw-state" id="gwChip" title="查看/配置 LLM 网关"></span>
      </div>
      <div id="tabContent"></div>`;
    root.querySelectorAll('.tab-btn[data-tab]').forEach(btn => {
      btn.onclick = () => switchTab(btn.dataset.tab);
    });
    const chip = document.getElementById('gwChip');
    if (chip) chip.onclick = () => openGwModal();
    const dg = document.getElementById('digestChip');
    if (dg) dg.onclick = openDigestModal;
    switchTab(currentTab);
  }

  async function switchTab(tab){
    currentTab = tab;
    const root = document.getElementById('pageRoot');
    if (!root) return;
    root.querySelectorAll('.tab-btn[data-tab]').forEach(b =>
      b.classList.toggle('active', b.dataset.tab === tab));
    const tc = document.getElementById('tabContent');
    if (!tc) return;
    tc.innerHTML = skeletonHTML(3);
    if (tab === 'chat') renderChatTab();
    else if (tab === 'runs'){ renderRunsTab(); loadRuns(); }
    else if (tab === 'advice'){ renderAdviceTab(); loadAdvice(); }
    else if (tab === 'cases'){ renderCasesTab(); loadCases(); }
    renderGwChip();
  }

  // ==================== 对话 Tab ====================
  function renderChatTab(){
    setPanel(`
      <div class="chat-wrap">
        ${canManage ? `
        <div class="chat-sessbar">
          <select id="sessSel" class="filter-sel sess-sel" data-search="1"
                  title="切换历史会话（含悬浮助手会话；📌 为置顶；展开后可输入关键词筛选标题/末条消息）"></select>
          <button class="btn sm ghost" id="sessPin" title="置顶/取消置顶当前会话">📌</button>
          <button class="btn sm ghost" id="sessRename" title="重命名当前会话">✏️</button>
          <button class="btn sm ghost" id="sessNew" title="新建会话（从空历史开始）">＋ 新建</button>
          <button class="btn sm ghost" id="sessDel" title="删除当前会话全部消息（不可恢复）">🗑 删除</button>
        </div>` : ''}
        <div class="chat-msgs" id="chatMsgs"></div>
        <div class="chat-input">
          <textarea id="chatInput" placeholder="${canManage ? '例：把 pay 服务回滚到上一版本（生成审批单）' : '例：pay 服务现在状态如何？'}（Enter 发送 / Shift+Enter 换行）"></textarea>
          <button class="btn primary send-btn" id="chatSend">发送</button>
        </div>
      </div>`);
    const msgs = document.getElementById('chatMsgs');
    const ta = document.getElementById('chatInput');
    const btn = document.getElementById('chatSend');
    if (msgs) loadMsgs(currentSession);
    const sel = document.getElementById('sessSel');
    if (sel){
      refreshSessSel(); // 会话下拉（当前会话无消息时保留占位 option）
      sel.onchange = () => switchSession(sel.value);
    }
    const sessNew = document.getElementById('sessNew');
    if (sessNew) sessNew.onclick = newSession;
    const sessDel = document.getElementById('sessDel');
    if (sessDel) sessDel.onclick = delSession;
    const sessRename = document.getElementById('sessRename');
    if (sessRename) sessRename.onclick = renameSession;
    const sessPin = document.getElementById('sessPin');
    if (sessPin) sessPin.onclick = pinSession;
    const send = () => {
      const text = (ta.value || '').trim();
      if (!text) return;
      ta.value = '';
      sendChat(text);
    };
    if (btn) btn.onclick = send;
    if (ta){
      ta.onkeydown = (e) => {
        if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); send(); }
      };
      ta.focus();
    }
  }

  // ---- 会话管理（多会话切换 / 新建 / 删除） ----
  function loadMsgs(sessionId){
    // 渲染空态并恢复该会话服务端历史（operator+ 可见；viewer 无权限时静默降级为空会话）
    const msgs = document.getElementById('chatMsgs');
    if (!msgs) return;
    chatLastId = 0;
    msgs.innerHTML = chatEmptyHtml();
    bindSugs(msgs);
    api('/api/agent/sessions/' + encodeURIComponent(sessionId)
        + '/messages?limit=40').then(data => {
      if (destroyed || !document.getElementById('chatMsgs')) return;
      const list = (data.messages || [])
        .filter(m => ['user','assistant'].includes(m.role));
      chatLastId = list.reduce((mx, m) => Math.max(mx, m.id || 0), 0);
      if (list.length) renderChatHistory(list);
    }).catch(() => {});
  }
  async function refreshChatTail(){
    // 对话页实时收尾：审批单在后台执行完成/失败时会向发起会话回写一条助手消息，
    // 收到 agent/tasks 事件后据此拉取；流式回复进行中（chatAbort 非空）不刷新，
    // 仅当服务端出现更大 id 的新消息才整体重绘，规避无关事件引起的无谓闪烁。
    if (chatAbort) return;
    if (!document.getElementById('chatMsgs')) return;
    let list = [];
    try{
      const data = await api('/api/agent/sessions/'
        + encodeURIComponent(currentSession) + '/messages?limit=40');
      list = (data.messages || [])
        .filter(m => ['user','assistant'].includes(m.role));
    }catch(e){ return; }
    if (destroyed || !document.getElementById('chatMsgs')) return;
    const newLastId = list.reduce((mx, m) => Math.max(mx, m.id || 0), 0);
    if (newLastId > chatLastId){ renderChatHistory(list); chatLastId = newLastId; }
  }
  function bindSugs(msgs){
    msgs.querySelectorAll('[data-sug]').forEach(s => {
      s.onclick = () => sendChat(s.dataset.sug);
    });
  }
  async function refreshSessSel(q){
    // 会话下拉 = 当前用户全部会话（q 非空时后端按标题/最后消息过滤）；
    // 新会话（无消息）不在列表时补占位 option
    const sel = document.getElementById('sessSel');
    if (!sel) return;
    let rows = [];
    try{ rows = ((await api('/api/agent/sessions' + (q ? '?q=' + encodeURIComponent(q) : ''))).sessions) || []; }
    catch(e){ /* 列表加载失败：仅保留当前会话占位 */ }
    sel.innerHTML = '';
    const labelText = (r) => {
      const t = (r.title || '').replace(/\s+/g, ' ').slice(0, 30) || r.session_id;
      return `${r.pinned ? '📌 ' : ''}${t}（${r.n} 条 · ${fmtTime(r.last_at)}）`;
    };
    const opts = rows.filter(r => r.session_id !== currentSession)
      .map(r => {
        const o = document.createElement('option');
        o.value = r.session_id;
        o.text = labelText(r);
        o.title = (r.last_content || '').slice(0, 80) || r.session_id;
        o.dataset.n = r.n;
        return o;
      });
    const cur = rows.find(r => r.session_id === currentSession);
    if (cur){
      sessPinned = !!cur.pinned;
      const o = document.createElement('option');
      o.value = cur.session_id;
      o.text = labelText(cur);
      o.title = (cur.last_content || '').slice(0, 80) || cur.session_id;
      o.dataset.n = cur.n;
      opts.unshift(o); // 当前会话置顶
    }else{
      sessPinned = false; // 新会话无 meta 记录
      const o = document.createElement('option');
      o.value = currentSession;
      o.text = currentSession + '（新会话，暂无消息）';
      o.dataset.n = '0';
      opts.unshift(o);
    }
    opts.forEach(o => sel.appendChild(o));
    sel.value = currentSession;
    const pinBtn = document.getElementById('sessPin');
    if (pinBtn) pinBtn.classList.toggle('active', sessPinned);
  }
  function switchSession(sid){
    if (!sid || sid === currentSession) return;
    abortChat(); // 切换会话时中止进行中的流（其气泡已脱离文档）
    currentSession = sid;
    localStorage.setItem('agentCurSession', sid);
    loadMsgs(sid);
  }
  function newSession(){
    abortChat();
    // 新会话：随机 id 本地记住，首次对话后消息才在服务端落库
    currentSession = 'web-' + Date.now().toString(36)
      + '-' + Math.random().toString(36).slice(2, 6);
    localStorage.setItem('agentCurSession', currentSession);
    loadMsgs(currentSession);
    refreshSessSel();
  }
  async function delSession(){
    const sel = document.getElementById('sessSel');
    const opt = (sel && sel.selectedOptions && sel.selectedOptions[0]) || null;
    const n = opt ? (opt.dataset.n || '') : '';
    const ok = await confirmDialog(
      `删除会话「${currentSession}」${n && n !== '0'
        ? '（含 ' + n + ' 条消息记录）' : ''}？删除后不可恢复。`,
      '删除会话');
    if (!ok) return;
    try{
      await api('/api/agent/sessions/'
        + encodeURIComponent(currentSession), {method: 'DELETE'});
      toastSuccess('会话已删除');
      newSession(); // 当前会话已删：落到新会话，消息区同时清空
    }catch(e){
      toastError('删除失败：' + (e.message || ''));
    }
  }
  async function renameSession(){
    // 重命名当前会话：后端存 agent_session_meta，列表展示优先于自动标题
    const sel = document.getElementById('sessSel');
    const cur = sel && sel.selectedOptions && sel.selectedOptions[0];
    const curTitle = cur ? (cur.text || '').split('（')[0] : '';
    const name = await promptDialog('重命名会话', curTitle,
      '会话名称（留空恢复自动标题）');
    if (name === null) return;
    try{
      await api('/api/agent/sessions/' + encodeURIComponent(currentSession)
        + '/title', {method: 'PUT', body: JSON.stringify({title: name})});
      toastSuccess(name ? '会话已重命名' : '已恢复自动标题');
      refreshSessSel();
    }catch(e){
      toastError('重命名失败：' + (e.message || ''));
    }
  }
  async function pinSession(){
    // 置顶/取消置顶当前会话：列表排序优先于最近活动（仅本人）
    try{
      const r = await api('/api/agent/sessions/' + encodeURIComponent(currentSession)
        + '/pin', {method: 'PUT',
                   body: JSON.stringify({pinned: !sessPinned})});
      sessPinned = !!r.pinned;
      toastSuccess(sessPinned ? '会话已置顶' : '已取消置顶');
      refreshSessSel();
    }catch(e){
      toastError('置顶失败：' + (e.message || ''));
    }
  }
  function abortChat(){
    if (chatAbort){ try{ chatAbort(); }catch(e){} chatAbort = null; }
  }
  function chatEmptyHtml(){
    const sugs = CHAT_SUG.map(s => `<span data-sug="${esc(s)}">${esc(s)}</span>`).join('');
    return `<div class="chat-empty" id="chatEmpty">
      <span class="big">🤖</span>你好，我是平台内置的运维智能助手。
      可以询问服务状态、告警归因、发布记录，或描述发布意图让我生成审批单。
      <div class="chat-sug">${sugs}</div></div>`;
  }
  function renderChatHistory(list){
    const msgs = document.getElementById('chatMsgs');
    if (!msgs) return;
    msgs.innerHTML = list.map(m => bubbleHtml(m.role, m.content || '')).join('');
    const empty = msgs.querySelector('.chat-empty');
    if (empty) empty.remove();
    scrollBottom(msgs);
    bindSugs(msgs);
  }
  function bubbleHtml(role, content, extra){
    const inner = role === 'user'
      ? esc(content)
      : `<div class="md-body">${mdToHtml(content)}</div>`;
    return `
      <div class="msg-row ${role === 'user' ? 'user' : ''}">
        <div class="msg-ava">${role === 'user' ? '🧑' : '🤖'}</div>
        <div class="msg-bubble">${inner}${extra || ''}</div>
      </div>`;
  }
  function renderBubbleMd(bubble, reply){
    // 流式回复结束后把气泡内的原始 Markdown 文本节点换成渲染后的容器（工具行保留）。
    if (!bubble) return;
    const text = (reply || '').trim()
      || Array.from(bubble.childNodes)
        .filter(n => n.nodeType === 3).map(n => n.nodeValue).join('').trim();
    if (!text) return;
    Array.from(bubble.childNodes).forEach(n => { if (n.nodeType === 3) n.remove(); });
    const body = document.createElement('div');
    body.className = 'md-body';
    body.innerHTML = mdToHtml(text);
    bubble.appendChild(body);
  }
  function streamBubble(msgs){
    // 创建流式回复气泡容器（含“思考中”占位，首个事件到达时移除）
    const empty = msgs.querySelector('.chat-empty');
    if (empty) empty.remove();
    const row = document.createElement('div');
    row.className = 'msg-row';
    row.innerHTML = `<div class="msg-ava">🤖</div>
      <div class="msg-bubble"><span class="stream-hint" id="streamHint">思考中…</span></div>`;
    msgs.appendChild(row);
    scrollBottom(msgs);
    return row.querySelector('.msg-bubble');
  }
  function clearStreamHint(bubble){
    if (!bubble) return;
    const h = bubble.querySelector('.stream-hint');
    if (h) h.remove();
  }
  function addToolLine(bubble, name){
    if (!bubble) return null;
    clearStreamHint(bubble);
    const line = document.createElement('div');
    line.className = 'msg-tool';
    line.dataset.name = name || '';
    line.innerHTML = `<span class="mark">⏳</span><span>正在调用工具 ${esc(name || '')} …</span>`;
    bubble.appendChild(line);
    scrollBottom(document.getElementById('chatMsgs'));
    return line;
  }
  function finishToolLine(line, ok){
    if (!line) return;
    const name = line.dataset.name || '';
    line.classList.add(ok ? 'ok' : 'fail');
    line.innerHTML = `<span class="mark">${ok ? '✅' : '❌'}</span>` +
      `<span>${ok ? '工具调用完成：' : '工具调用失败：'}${esc(name)}</span>`;
    scrollBottom(document.getElementById('chatMsgs'));
  }
  function attachPlanTip(bubble, runId){
    if (!bubble || !runId) return;
    const tip = document.createElement('div');
    tip.className = 'plan-tip';
    const t = document.createElement('span');
    t.textContent = '📝 已生成审批单 #' + runId;
    const go = document.createElement('button');
    go.className = 'btn sm primary';
    go.textContent = '查看并审批';
    go.onclick = () => switchTab('runs');
    tip.appendChild(t);
    tip.appendChild(go);
    bubble.appendChild(tip);
    scrollBottom(document.getElementById('chatMsgs'));
  }
  function appendStreamErr(bubble, msg, hint){
    if (!bubble){ addChatBubble('assistant', msg + (hint ? '\n💡 ' + hint : '')); return; }
    clearStreamHint(bubble);
    const d = document.createElement('div');
    d.className = 'msg-err';
    d.textContent = (msg || '请求失败') + (hint ? '\n💡 ' + hint : '');
    bubble.appendChild(d);
    scrollBottom(document.getElementById('chatMsgs'));
  }
  function addChatBubble(role, content, runId){
    const msgs = document.getElementById('chatMsgs');
    if (!msgs) return;
    const empty = msgs.querySelector('.chat-empty');
    if (empty) empty.remove();
    const extra = runId ? `<div class="plan-tip">📝 已生成审批单 #${runId}
      <button class="btn sm primary" id="gotoRun">查看并审批</button></div>` : '';
    const holder = document.createElement('div');
    holder.innerHTML = bubbleHtml(role, content, extra);
    msgs.appendChild(holder.firstElementChild);
    const g = document.getElementById('gotoRun');
    if (g) g.onclick = () => switchTab('runs');
    scrollBottom(msgs);
  }
  function appendDelta(bubble, text, msgs){
    if (!bubble || !text) return false;
    clearStreamHint(bubble);
    bubble.appendChild(document.createTextNode(text));
    scrollBottom(msgs);
    return true;
  }
  function sendChat(text){
    const ta = document.getElementById('chatInput');
    const btn = document.getElementById('chatSend');
    const msgs = document.getElementById('chatMsgs');
    if (!msgs) return;
    addChatBubble('user', text);
    const bubble = streamBubble(msgs);
    if (ta) ta.disabled = true;
    if (btn){ btn.disabled = true; btn.textContent = '等待回复…'; }
    let gotText = false, ended = false;
    // 事件 → DOM 渲染（协议：text/tool/tool_result/done/error）
    const onEvent = (ev) => {
      const t = ev.type || '';
      if (t === 'text'){
        gotText = appendDelta(bubble, ev.text || '', msgs) || gotText;
      } else if (t === 'tool'){
        const line = addToolLine(bubble, ev.name);
        if (line) pendingTools.push(line);
      } else if (t === 'tool_result'){
        // 找到该工具最近的未完成行并收尾
        for (let i = pendingTools.length - 1; i >= 0; i--){
          if (pendingTools[i].dataset.name === (ev.name || '')){
            finishToolLine(pendingTools[i], !!ev.ok);
            pendingTools.splice(i, 1);
            break;
          }
        }
      } else if (t === 'done'){
        ended = true;
        clearStreamHint(bubble);
        renderBubbleMd(bubble, (ev.result || {}).reply || '');
        const runId = (ev.result || {}).plan_run_id;
        if (runId && bubble) attachPlanTip(bubble, runId);
        // 本轮消息已落库：刷新会话下拉条数（新会话从占位变为真实条目）
        refreshSessSel();
      } else if (t === 'error'){
        ended = true;
        appendStreamErr(bubble, ev.error || '对话出错', ev.hint || '');
        refreshSessSel(); // 用户消息已落库：同样更新会话条数
      }
    };
    // 流结束（正常/中止/异常均触发）：恢复输入并做中断兜底提示
    const onClose = () => {
      endChat();
      if (destroyed) return;
      if (bubble && !bubble.isConnected) return; // 会话已切换：气泡脱离文档，静默
      if (!ended && !gotText) appendStreamErr(bubble, '连接中断，未收到完整回复', '');
      else if (!ended && gotText) appendStreamErr(bubble, '\n\n⚠️ 回复中断（连接异常）', '');
    };
    const onFatal = (msg, hint) => {
      ended = true;
      appendStreamErr(bubble, msg, hint);
    };
    const cancel = streamAgentChat({
      message: text,
      sessionId: currentSession,
      on401: () => {
        localStorage.removeItem('token'); localStorage.removeItem('user');
        location.href = '/';
      },
      onEvent: onEvent,
      onFatal: onFatal,
      onClose: onClose
    });
    chatAbort = cancel;
  }
  function endChat(){
    chatAbort = null;
    pendingTools = [];
    const ta = document.getElementById('chatInput');
    const btn = document.getElementById('chatSend');
    if (ta){ ta.disabled = false; ta.focus(); }
    if (btn){ btn.disabled = false; btn.textContent = '发送'; }
  }

  // ==================== 审批单 Tab ====================
  function renderRunsTab(){
    const chips = ['all', ...Object.keys(RUN_LABEL)].map(s => `
      <button class="chip ${runStatus===s?'active':''}" data-status="${s}">
        ${s==='all' ? '全部' : RUN_LABEL[s].label}</button>`).join('');
    setPanel(`
      <div class="agent-list-panel panel">
        <div class="list-toolbar">
          ${chips}
          <span style="flex:1"></span>
          <button class="btn sm ghost" id="runsRefresh">刷新</button>
        </div>
        <div id="runsList" class="agent-list-body"></div>
      </div>`);
    document.querySelectorAll('[data-status]').forEach(c => {
      c.onclick = () => {
        runStatus = c.dataset.status;
        document.querySelectorAll('[data-status]').forEach(x => x.classList.toggle('active', x === c));
        loadRuns();
      };
    });
    const rf = document.getElementById('runsRefresh');
    if (rf) rf.onclick = loadRuns;
  }
  async function loadRuns(){
    const box = document.getElementById('runsList');
    if (!box) return;
    box.innerHTML = skeletonHTML(3);
    try{
      const data = await api('/api/agent/runs' + qs({status: runStatus === 'all' ? '' : runStatus, limit: 100}));
      runs = data.runs || [];
    }catch(e){ runs = []; }
    renderRunsList(box);
    renderGwChip();
  }
  function renderRunsList(box){
    if (!box) return;
    const keepScroll = box.scrollTop; // 15s 轮询重渲染后恢复内部滚动位置
    if (!runs.length){
      box.innerHTML = `<div class="agent-list-panel panel"><div class="panel">${emptyState('📝', '暂无审批单（可在对话中让助手生成发布计划）')}</div></div>`;
      return;
    }
    box.innerHTML = runs.map(r => {
      const st = RUN_LABEL[r.status] || {label: r.status, cls:'pending', ico:'📋'};
      const plan = parsePlan(r.plan);
      const actions = [];
      if (r.status === 'awaiting_approval' && canManage){
        actions.push(`<button class="btn sm primary" data-act="approve" data-id="${r.id}">通过</button>`);
        actions.push(`<button class="btn sm danger" data-act="reject" data-id="${r.id}">驳回</button>`);
      }
      if (role === 'admin'){
        actions.push(`<button class="btn sm danger" data-delrun="${r.id}" title="删除审批单记录（不影响已执行任务）">删除</button>`);
      }
      const title = `${OP_LABEL[plan.op] || plan.op || '操作'} · ${esc(r.name || r.intent || '#')}`
        + (r.env ? `（${esc(r.env)}）` : '');
      return `
        <div class="agent-item" data-open="${r.id}">
          <div class="run-status-ico">${st.ico}</div>
          <div class="ai-main">
            <div class="ai-title"><span>#${r.id} ${title}</span>
              <span class="badge ${st.cls}">${st.label}</span>
              ${plan.window_warning ? '<span class="tag tag-warn">⏱ 窗口外</span>' : ''}
            </div>
            <div class="ai-meta">
              <span>🕐 ${fmtTime(r.created_at)}</span>
              <span>👤 ${esc(r.created_by_name || '-')}</span>
              ${r.version ? `<span>📦 ${esc(r.version)}</span>` : ''}
              ${r.decided_by_name ? `<span>✅ ${esc(r.decided_by_name)}${r.decision_note ? '：' + esc(r.decision_note) : ''}</span>` : ''}
            </div>
          </div>
          <div class="ai-actions">${actions.join('')}</div>
        </div>`;
    }).join('');
    // 操作绑定
    box.querySelectorAll('[data-act]').forEach(b => {
      b.onclick = (ev) => { ev.stopPropagation(); onRunDecision(Number(b.dataset.id), b.dataset.act); };
    });
    box.querySelectorAll('[data-delrun]').forEach(b => {
      b.onclick = async (ev) => {
        ev.stopPropagation();
        const id = Number(b.dataset.delrun);
        const run = getRun(id);
        const executing = run && run.status === 'executing';
        const yes = await confirmDialog(
          executing
            ? `审批单 #${id} 正在执行中，删除仅移除审批记录，已提交的任务会继续执行且不可恢复。确认删除？`
            : `确认删除审批单 #${id}？删除后不可恢复（不影响已执行任务与审计）。`,
          '删除审批单');
        if (!yes) return;
        try{
          await api(`/api/agent/runs/${id}`, {method:'DELETE'});
          toastSuccess(`审批单 #${id} 已删除`);
          loadRuns();
        }catch(e){ toastError('删除失败：' + (e.message || '')); }
      };
    });
    box.querySelectorAll('[data-open]').forEach(item => {
      item.onclick = () => openRunDetail(Number(item.dataset.open));
    });
    box.scrollTop = keepScroll;
  }
  function getRun(id){ return runs.find(r => r.id === id) || null; }
  async function onRunDecision(id, act){
    const run = getRun(id);
    if (!run) return;
    const plan = parsePlan(run.plan);
    if (act === 'approve'){
      openDrawer({
        title: `审批发布单 #${id} · ${esc(run.name)}`,
        body: `
          <div class="plan-detail">
            <div class="row"><b>操作</b>${OP_LABEL[plan.op] || plan.op}</div>
            <div class="row"><b>目标</b>${esc(plan.target || run.name)}</div>
            <div class="row"><b>环境</b>${esc(run.env || '-')}</div>
            ${run.version ? `<div class="row"><b>版本</b>${esc(run.version)}</div>` : ''}
            ${plan.window_text ? `<div class="row"><b>窗口</b>${esc(plan.window_text)}</div>` : ''}
            ${plan.steps ? `<div class="row"><b>步骤</b><span style="white-space:pre-line">${esc(plan.steps)}</span></div>` : ''}
            ${run.intent ? `<div class="row"><b>意图</b><span style="white-space:pre-line">${esc(run.intent)}</span></div>` : ''}
          </div>
          ${plan.window_warning ? '<div class="warn-line">⚠️ ' + esc(plan.window_warning) + '</div>' : ''}
          <div class="field" style="margin-top:12px"><label>审批意见（选填）</label>
            <input type="text" id="decNote" maxlength="200" placeholder="补充审批意见，将写入审计"></div>
          ${plan.window_warning ? `<div style="display:flex;align-items:center;gap:8px;margin-top:10px">
            <input type="checkbox" id="decForce"><label style="color:var(--text-2);font-size:var(--fs-12)">非发布窗口期，强制提交执行</label></div>` : ''}`,
        footer: `<button class="btn" id="decCancel">取消</button>
                 <button class="btn primary" id="decOk">确认通过并提交执行</button>`,
        onClose: () => {}
      });
      const doApprove = async () => {
        const okBtn = document.getElementById('decOk');
        if (okBtn && okBtn.disabled) return; // 防重复提交
        const note = (document.getElementById('decNote') || {}).value || '';
        const force = !!(document.getElementById('decForce') || {}).checked;
        if (okBtn) { okBtn.disabled = true; okBtn.textContent = '提交中…'; }
        try{
          await api(`/api/agent/runs/${id}/decision`, {method:'POST',
            body: JSON.stringify({action:'approve', note:note, force_window:force})});
          toastSuccess(`审批单 #${id} 已通过，任务进入执行队列`);
          const dr = document.getElementById('drawerRoot'); // 关闭抽屉，否则遮罩残留看似“页面无变化”
          if (dr) { dr.classList.add('hidden'); dr.innerHTML = ''; }
          loadRuns();
        }catch(e){
          toastError('审批失败：' + (e.message || ''));
          if (okBtn) { okBtn.disabled = false; okBtn.textContent = '确认通过并提交执行'; }
        }
      };
      const ok = document.getElementById('decOk');
      const can = document.getElementById('decCancel');
      if (ok) ok.onclick = doApprove;
      if (can) can.onclick = () => { document.getElementById('drawerRoot').classList.add('hidden'); };
    }else{
      const yes = await confirmDialog('确认驳回该发布单？驳回后不可恢复。', `驳回审批单 #${id}`);
      if (!yes) return;
      try{
        await api(`/api/agent/runs/${id}/decision`, {method:'POST',
          body: JSON.stringify({action:'reject', note:'Web 端驳回'})});
        toastSuccess(`审批单 #${id} 已驳回`);
        loadRuns();
      }catch(e){ toastError('驳回失败：' + (e.message || '')); }
    }
  }
  function openRunDetail(id){
    const run = getRun(id);
    if (!run) return;
    const st = RUN_LABEL[run.status] || {label:run.status, cls:'pending'};
    const plan = parsePlan(run.plan);
    const flags = plan.flags || {};
    const flagText = Object.entries(flags)
      .filter(([,v]) => v).map(([k]) => k).join(', ') || '-';
    openDrawer({
      title: `审批单 #${id} 详情`,
      wide: true,
      body: `
        <div class="plan-detail">
          <div class="row"><b>状态</b><span class="badge ${st.cls}">${st.label}</span>
            ${run.task_id ? `关联任务 #${run.task_id}` : ''}</div>
          <div class="row"><b>操作</b>${OP_LABEL[plan.op] || plan.op}</div>
          <div class="row"><b>目标</b>${esc(plan.target || run.name)}</div>
          <div class="row"><b>环境</b>${esc(run.env || '-')}</div>
          ${run.version ? `<div class="row"><b>版本</b>${esc(run.version)}</div>` : ''}
          ${run.previous_version ? `<div class="row"><b>回滚至</b>${esc(run.previous_version)}</div>` : ''}
          <div class="row"><b>标记</b>${esc(flagText)}</div>
          ${run.intent ? `<div class="row"><b>用户意图</b><span style="white-space:pre-line">${esc(run.intent)}</span></div>` : ''}
          ${plan.window_text ? `<div class="row"><b>发布窗口</b>${esc(plan.window_text)}</div>` : ''}
          ${plan.steps ? `<div class="row"><b>执行步骤</b><span style="white-space:pre-line">${esc(plan.steps)}</span></div>` : ''}
          ${plan.cli ? `<div class="row"><b>等价 CLI</b><code>${esc(plan.cli)}</code></div>` : ''}
          ${run.summary ? `<div class="row"><b>执行摘要</b><span style="white-space:pre-line">${esc(run.summary)}</span></div>` : ''}
          <div class="row"><b>创建人</b>${esc(run.created_by_name || '-')} @ ${fmtTime(run.created_at)}</div>
          ${run.decided_by_name ? `<div class="row"><b>审批人</b>${esc(run.decided_by_name)} @ ${fmtTime(run.decided_at)}
            ${run.decision_note ? '（' + esc(run.decision_note) + '）' : ''}</div>` : ''}
        </div>`,
      footer: (canManage && (run.status === 'failed' || run.status === 'success'))
        ? `<button class="btn" id="runCaseCancel">关闭</button>
           <button class="btn primary" id="runToCase">📥 沉淀为案例</button>`
        : ''
    });
    const cc = document.getElementById('runCaseCancel');
    if (cc) cc.onclick = () => { document.getElementById('drawerRoot').classList.add('hidden'); };
    const tc = document.getElementById('runToCase');
    if (tc) tc.onclick = async () => {
      try{
        const data = await api(`/api/agent/runs/${id}/to_case`, {method:'POST'});
        toastSuccess('已生成案例草稿 #' + data.case_id + '，可在故障案例库查看');
        document.getElementById('drawerRoot').classList.add('hidden');
      }catch(e){ toastError('沉淀失败：' + (e.message || '')); }
    };
  }

  // ==================== 决策建议 Tab ====================
  const ADVICE_STATUS_LABEL = {
    pending: {label:'待处理', cls:'tag-warn'},
    adopted: {label:'已采纳', cls:'tag-ok'},
    dismissed: {label:'已忽略', cls:'tag-info'}
  };
  function renderAdviceTab(){
    const chips = ['', ...Object.keys(SCOPE_LABEL)].map(s => `
      <button class="chip ${adviceScope===s?'active':''}" data-scope="${s}">
        ${s === '' ? '全部' : SCOPE_LABEL[s]}</button>`).join('');
    const stChips = ['', ...Object.keys(ADVICE_STATUS_LABEL)].map(s => `
      <button class="chip ${adviceStatus===s?'active':''}" data-astatus="${s}">
        ${s === '' ? '全部状态' : ADVICE_STATUS_LABEL[s].label}</button>`).join('');
    setPanel(`
      <div class="agent-list-panel panel">
        <div class="list-toolbar">
          ${chips}
          <span style="flex:1"></span>
          <button class="btn sm ghost" id="adviceRefresh">刷新</button>
        </div>
        <div class="list-toolbar" style="border-top:none;padding-top:0">${stChips}</div>
        <div id="adviceList" class="agent-list-body"></div>
      </div>
      <div class="field" style="display:flex;gap:8px;margin-top:12px;align-items:center;flex-wrap:wrap">
        <b style="color:var(--text-2)">快捷工具：</b>
        <input type="text" id="preTarget" placeholder="发布预检：服务名（如 order）" style="width:200px">
        <input type="text" id="preEnv" placeholder="环境（如 prod，可空）" style="width:120px">
        <button class="btn sm" id="preGo">运行预检</button>
        <input type="text" id="alertId" placeholder="告警归因：告警 ID" style="width:130px">
        <button class="btn sm" id="alertGo">归因分析</button>
      </div>`);
    document.querySelectorAll('[data-scope]').forEach(c => {
      c.onclick = () => {
        adviceScope = c.dataset.scope;
        document.querySelectorAll('[data-scope]').forEach(x => x.classList.toggle('active', x === c));
        loadAdvice();
      };
    });
    document.querySelectorAll('[data-astatus]').forEach(c => {
      c.onclick = () => {
        adviceStatus = c.dataset.astatus;
        document.querySelectorAll('[data-astatus]').forEach(x => x.classList.toggle('active', x === c));
        loadAdvice();
      };
    });
    const rf = document.getElementById('adviceRefresh');
    if (rf) rf.onclick = loadAdvice;
    const preGo = document.getElementById('preGo');
    if (preGo) preGo.onclick = runPrecheck;
    const alertGo = document.getElementById('alertGo');
    if (alertGo) alertGo.onclick = runAlertAnalyze;
  }
  async function loadAdvice(){
    const box = document.getElementById('adviceList');
    if (!box) return;
    box.innerHTML = skeletonHTML(3);
    try{
      const data = await api('/api/agent/advice' + qs({scope: adviceScope, status: adviceStatus, limit: 100}));
      adviceList = data.advice || [];
    }catch(e){ adviceList = []; }
    renderAdviceList(box);
    refreshAdviceBadge(); // 建议状态变化后同步导航角标
  }
  async function refreshAdviceBadge(){
    if (!window.updateAdviceBadge) return;
    try{
      const data = await api('/api/agent/advice/badge');
      window.updateAdviceBadge(data.pending || 0);
    }catch(e){ /* 角标失败不影响主列表 */ }
  }
  function renderAdviceList(box){
    if (!box) return;
    const keepScroll = box.scrollTop; // 15s 轮询重渲染后恢复内部滚动位置
    if (!adviceList.length){
      box.innerHTML = `<div class="panel">${emptyState('🤖', '暂无建议（后台调度会自动分析灰度 / 失败任务 / 告警）')}</div>`;
      return;
    }
    const scopeIco = {canary:'🐤', task_failed:'🧯', alert:'🚨', precheck:'🛡️'};
    box.innerHTML = adviceList.map(a => {
      const st = ADVICE_STATUS_LABEL[a.status] || ADVICE_STATUS_LABEL.pending;
      const actions = [];
      if (canManage && a.status === 'pending' && adoptable(a)){
        actions.push(`<button class="btn sm primary" data-adopt="${a.id}">⚡ 采纳执行</button>`);
      }
      if (canManage){
        actions.push(`<button class="btn sm" data-to="${a.id}">📥 沉淀案例</button>`);
      }
      if (canManage && a.status === 'pending'){
        actions.push(`<button class="btn sm" data-dismiss="${a.id}">忽略</button>`);
      }
      if (canManage && a.status === 'dismissed'){
        actions.push(`<button class="btn sm" data-reopen="${a.id}">恢复</button>`);
      }
      if (role === 'admin'){
        actions.push(`<button class="btn sm danger" data-del="${a.id}">删除</button>`);
      }
      const stBadge = a.status === 'pending' ? ''
        : `<span class="tag ${st.cls}">${st.label}</span>`;
      const refHtml = a.action_ref ? `<span>📎 ${esc(a.action_ref)}</span>` : '';
      return `
        <div class="agent-item" data-open="${a.id}">
          <div class="run-status-ico">${scopeIco[a.scope] || '💡'}</div>
          <div class="ai-main">
            <div class="ai-title">${esc(a.title || '建议')}
              <span class="tag tag-info">${SCOPE_LABEL[a.scope] || esc(a.scope)}</span>
              ${stBadge}
              ${a.model ? `<span class="muted" style="font-size:var(--fs-11)">${esc(a.model)}</span>` : ''}
            </div>
            <div class="ai-body">${esc(a.body || '')}</div>
            <div class="ai-meta">
              <span>🕐 ${fmtTime(a.created_at)}</span>
              <span>👤 ${esc(a.created_by_name || 'advisor')}</span>
              <span>🔗 ref #${a.ref_id}</span>
              ${refHtml}
            </div>
          </div>
          <div class="ai-actions">${actions.join('')}</div>
        </div>`;
    }).join('');
    box.querySelectorAll('[data-open]').forEach(item => {
      item.onclick = () => openAdviceDetail(Number(item.dataset.open));
    });
    box.querySelectorAll('[data-adopt]').forEach(b => {
      b.onclick = async (ev) => {
        ev.stopPropagation();
        try{
          const data = await api(`/api/agent/advice/${b.dataset.adopt}/to_run`, {method:'POST'});
          if (data.action === 'run'){
            toastSuccess(`已生成回滚审批单 #${data.run_id}，请在发布审批单页确认执行`);
          }else{
            toastSuccess(`任务 #${data.task_id} 灰度决策「${data.decision}」已写入`);
          }
          loadAdvice();
        }catch(e){ toastError('采纳失败：' + (e.message || '')); }
      };
    });
    box.querySelectorAll('[data-to]').forEach(b => {
      b.onclick = async (ev) => {
        ev.stopPropagation();
        try{
          const data = await api(`/api/agent/advice/${b.dataset.to}/to_case`, {method:'POST'});
          toastSuccess('已生成案例草稿 #' + data.case_id + '，可在故障案例库查看');
        }catch(e){ toastError('沉淀失败：' + (e.message || '')); }
      };
    });
    box.querySelectorAll('[data-del]').forEach(b => {
      b.onclick = async (ev) => {
        ev.stopPropagation();
        const yes = await confirmDialog('确认删除该建议？', '删除建议');
        if (!yes) return;
        try{
          await api(`/api/agent/advice/${b.dataset.del}`, {method:'DELETE'});
          toastSuccess('已删除');
          loadAdvice();
        }catch(e){ toastError('删除失败：' + (e.message || '')); }
      };
    });
    box.querySelectorAll('[data-dismiss]').forEach(b => {
      b.onclick = async (ev) => {
        ev.stopPropagation();
        try{
          await api(`/api/agent/advice/${b.dataset.dismiss}/dismiss`, {method:'POST'});
          toastSuccess('已忽略该建议');
          loadAdvice();
        }catch(e){ toastError('操作失败：' + (e.message || '')); }
      };
    });
    box.querySelectorAll('[data-reopen]').forEach(b => {
      b.onclick = async (ev) => {
        ev.stopPropagation();
        try{
          await api(`/api/agent/advice/${b.dataset.reopen}/reopen`, {method:'POST'});
          toastSuccess('已重开该建议');
          loadAdvice();
        }catch(e){ toastError('操作失败：' + (e.message || '')); }
      };
    });
    box.scrollTop = keepScroll;
  }
  function parseEvidence(a){
    let ev = a.evidence;
    if (typeof ev === 'string'){ try { ev = JSON.parse(ev); }catch(e){ return {}; } }
    return (ev && typeof ev === 'object') ? ev : {};
  }
  // 建议是否可一键采纳：失败归因（有回滚版本）或灰度决策建议
  function adoptable(a){
    const ev = parseEvidence(a);
    if (a.scope === 'task_failed') return !!(ev.task || {}).previous_version;
    if (a.scope === 'canary') return !!(ev.decision && (ev.task || {}).id);
    return false;
  }
  function openAdviceDetail(id, adviceObj){
    const a = adviceObj || adviceList.find(x => x.id === id);
    if (!a){
      // 缓存未命中（如从案例详情互链跳入）：异步取单条详情
      api('/api/agent/advice/' + id).then(d => {
        if (d.advice) openAdviceDetail(id, d.advice);
      }).catch(() => { toastError('建议 #' + id + ' 不存在或加载失败'); });
      return;
    }
    let evHtml = '';
    try{
      const ev = typeof a.evidence === 'string' ? JSON.parse(a.evidence) : a.evidence;
      if (ev) evHtml = `<div class="row"><b>证据</b><code style="font-size:11px">${esc(JSON.stringify(ev, null, 2))}</code></div>`;
    }catch(e){}
    const st = ADVICE_STATUS_LABEL[a.status] || ADVICE_STATUS_LABEL.pending;
    const statusHtml = a.status === 'pending'
      ? `<span class="tag ${st.cls}">${st.label}</span>`
      : `<span class="tag ${st.cls}">${st.label}</span>` +
        (a.status_by ? ` <span class="muted" style="font-size:var(--fs-11)">${esc(a.status_by)} @ ${fmtTime(a.status_at)}</span>` : '');
    openDrawer({
      title: '决策建议 #' + id,
      wide: true,
      body: `
        <div class="plan-detail">
          <div class="row"><b>标题</b>${esc(a.title || '-')}</div>
          <div class="row"><b>类型</b>${SCOPE_LABEL[a.scope] || esc(a.scope)}（ref #${a.ref_id}）</div>
          <div class="row"><b>状态</b>${statusHtml}</div>
          ${a.action_ref ? `<div class="row"><b>采纳产出</b><code>${esc(a.action_ref)}</code></div>` : ''}
          <div class="row"><b>内容</b><span style="white-space:pre-line">${esc(a.body || '-')}</span></div>
          ${evHtml}
          <div class="row"><b>产出</b>${esc(a.model || '规则引擎')} · ${esc(a.created_by_name || 'advisor')} @ ${fmtTime(a.created_at)}</div>
          ${canManage ? `<div class="row"><b>关联案例</b><span id="advCaseBox" class="muted">加载中…</span></div>` : ''}
        </div>`
    });
    if (canManage){
      (async () => {
        try{
          const data = await api('/api/agent/cases' + qs({advice_id: id, limit: 20}));
          const box = document.getElementById('advCaseBox');
          if (!box) return;
          const cs = data.cases || [];
          box.innerHTML = cs.length
            ? cs.map(c => `<span class="tag tag-info" data-case="${c.id}" style="cursor:pointer;margin-right:4px" title="查看案例详情">#${c.id} ${esc(c.title || '')}</span>`).join('')
            : '无（可点击列表中「沉淀案例」生成）';
          box.querySelectorAll('[data-case]').forEach(el => {
            el.onclick = () => {
              const cc = cs.find(x => x.id === Number(el.dataset.case));
              if (cc) openCaseDetail(cc.id, cc);
            };
          });
        }catch(e){
          const box = document.getElementById('advCaseBox');
          if (box) box.textContent = '读取失败';
        }
      })();
    }
  }
  async function runPrecheck(){
    const target = (document.getElementById('preTarget') || {}).value || '';
    const env = (document.getElementById('preEnv') || {}).value || '';
    if (!target){ toastError('请输入要预检的服务名'); return; }
    try{
      const data = await api('/api/agent/precheck', {method:'POST',
        body: JSON.stringify({target: target, env: env})});
      const checks = (data.checks || []).map(c =>
        `<div class="row"><b>${c.ok ? '✅' : '❌'}</b>${esc(c.name || '')}：${esc(c.detail || c.message || '')}</div>`).join('');
      openDrawer({
        title: `发布预检 · ${esc(target)}${env ? '（' + esc(env) + '）' : ''}`,
        wide: true,
        body: `
          <div style="margin-bottom:10px">
            ${data.passed
              ? '<span class="badge success">预检通过，可以进入发布</span>'
              : '<span class="badge failed">预检未通过，建议先处理阻断项</span>'}
          </div>
          <div class="plan-detail">${checks || '<div class="row"><b>ℹ️</b>无检查项输出</div>'}</div>
          <div class="plan-detail" style="margin-top:10px"><div class="row"><b>结论</b>
            <span style="white-space:pre-line">${esc(data.body || '')}</span></div></div>`
      });
      loadAdvice();
    }catch(e){ toastError('预检失败：' + (e.message || '')); }
  }
  async function runAlertAnalyze(){
    const id = (document.getElementById('alertId') || {}).value || '';
    if (!id || !/^\d+$/.test(id)){ toastError('请输入正确的告警 ID'); return; }
    try{
      const data = await api(`/api/agent/analyze/alert/${id}`, {method:'POST'});
      const a = data.advice || {};
      openDrawer({
        title: '告警归因分析 #' + id,
        wide: true,
        body: `<div class="plan-detail">
          <div class="row"><b>标题</b>${esc(a.title || '-')}</div>
          <div class="row"><b>内容</b><span style="white-space:pre-line">${esc(a.body || '-')}</span></div>
          <div class="row"><b>产出</b>${esc(a.model || '规则引擎')} @ ${fmtTime(a.created_at)}</div>
        </div>`
      });
      toastSuccess('归因分析完成（建议 #' + a.id + '）');
      loadAdvice();
    }catch(e){ toastError('归因失败：' + (e.message || '')); }
  }

  // ==================== 故障案例 Tab ====================
  const CASE_SRC_LABEL = { manual: '✍️ 手工', advice: '📥 建议沉淀', agent_run: '🤖 执行沉淀' };
  function renderCasesTab(){
    setPanel(`
      <div class="agent-list-panel panel">
        <div class="list-toolbar">
          <input type="text" id="caseKw" placeholder="搜索症状 / 根因 / 教训关键字" value="${esc(caseKw)}">
          <input type="text" id="caseSvc" placeholder="服务名（可空）" value="${esc(caseSvc)}" style="width:140px">
          <input type="text" id="caseTag" placeholder="标签（可空）" value="${esc(caseTag)}" style="width:120px">
          <button class="btn sm" id="caseSearch">搜索</button>
          <span style="flex:1"></span>
          ${canManage ? '<button class="btn sm primary" id="caseNew">＋ 新案例</button>' : ''}
        </div>
        <div id="caseGrid" class="agent-list-body pad"></div>
      </div>`);
    const search = () => {
      caseKw = (document.getElementById('caseKw') || {}).value || '';
      caseSvc = (document.getElementById('caseSvc') || {}).value || '';
      caseTag = (document.getElementById('caseTag') || {}).value || '';
      loadCases();
    };
    const sb = document.getElementById('caseSearch');
    if (sb) sb.onclick = search;
    const kw = document.getElementById('caseKw');
    if (kw){ kw.onkeydown = (e) => { if (e.key === 'Enter') search(); }; }
    const cn = document.getElementById('caseNew');
    if (cn) cn.onclick = () => openCaseForm(null);
  }
  async function loadCases(){
    const box = document.getElementById('caseGrid');
    if (!box) return;
    box.innerHTML = skeletonHTML(4);
    try{
      const data = await api('/api/agent/cases' + qs({keyword: caseKw, service: caseSvc, tag: caseTag, limit: 100}));
      cases = data.cases || [];
    }catch(e){ cases = []; }
    renderCases(box);
  }
  function renderCases(box){
    if (!box) return;
    const keepScroll = box.scrollTop; // 15s 轮询重渲染后恢复内部滚动位置
    if (!cases.length){
      box.innerHTML = emptyState('📚', '暂无故障案例。可在「决策建议」中将 AI 建议一键沉淀为案例');
      return;
    }
    box.innerHTML = `<div class="case-grid">${cases.map(c => `
      <div class="case-card" data-open="${c.id}">
        <div class="cc-title">${esc(c.title)}</div>
        <div class="cc-body">${esc(c.symptom || c.root_cause || c.lesson || '')}</div>
        <div class="cc-foot">
          <span class="tag tag-${c.result === 'resolved' || c.result === '成功' ? 'ok' : 'warn'}">${esc(c.result || '待验证')}</span>
          ${c.source ? `<span>${CASE_SRC_LABEL[c.source] || esc(c.source)}</span>` : ''}
          ${c.service ? `<span>🔧 ${esc(c.service)}</span>` : ''}
          ${c.env ? `<span>🌐 ${esc(c.env)}</span>` : ''}
          <span style="margin-left:auto">🕐 ${fmtTime(c.updated_at || c.created_at)}</span>
        </div>
      </div>`).join('')}</div>`;
    box.querySelectorAll('[data-open]').forEach(card => {
      card.onclick = () => openCaseDetail(Number(card.dataset.open));
    });
    box.scrollTop = keepScroll;
  }
  function getCase(id){ return cases.find(c => c.id === id) || null; }
  function openCaseDetail(id, caseObj){
    const c = caseObj || getCase(id);
    if (!c){
      // 缓存未命中（如从建议详情互链跳入）：异步取单条详情
      api('/api/agent/cases/' + id).then(d => {
        if (d.case) openCaseDetail(id, d.case);
      }).catch(() => { toastError('案例 #' + id + ' 不存在或加载失败'); });
      return;
    }
    const tags = (c.tags || []).map(t =>
      `<span class="tag tag-info" data-tag="${esc(t)}" style="cursor:pointer" title="按该标签过滤案例">${esc(t)}</span>`).join(' ');
    openDrawer({
      title: `故障案例 #${id}`,
      wide: true,
      body: `
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px">
          <b style="font-size:var(--fs-16)">${esc(c.title)}</b>
          <span class="tag tag-${c.result === 'resolved' || c.result === '成功' ? 'ok' : 'warn'}">${esc(c.result || '待验证')}</span>
        </div>
        <div class="plan-detail">
          ${c.service ? `<div class="row"><b>服务</b>${esc(c.service)}</div>` : ''}
          ${c.env ? `<div class="row"><b>环境</b>${esc(c.env)}</div>` : ''}
          ${c.version ? `<div class="row"><b>版本</b>${esc(c.version)}</div>` : ''}
          <div class="row"><b>症状</b><span style="white-space:pre-line">${esc(c.symptom || '-')}</span></div>
          <div class="row"><b>根因</b><span style="white-space:pre-line">${esc(c.root_cause || '-')}</span></div>
          <div class="row"><b>处置</b><span style="white-space:pre-line">${esc(c.action || '-')}</span></div>
          <div class="row"><b>教训</b><span style="white-space:pre-line">${esc(c.lesson || '-')}</span></div>
          ${c.source ? `<div class="row"><b>来源</b>${CASE_SRC_LABEL[c.source] || esc(c.source)}${c.advice_id ? `（<span data-adv="${c.advice_id}" style="cursor:pointer;color:var(--primary)" title="查看建议详情">建议 #${c.advice_id}</span>）` : ''}</div>` : ''}
          <div class="row"><b>维护</b>${esc(c.created_by_name || '-')} @ ${fmtTime(c.created_at)}</div>
        </div>
        ${tags ? `<div style="margin-top:10px">${tags}</div>` : ''}
        <div style="margin-top:14px">
          <b style="font-size:var(--fs-13)">🔗 相似案例</b>
          <div id="simBox" class="muted" style="margin-top:6px;font-size:var(--fs-12)">加载中…</div>
        </div>`,
      footer: (canManage ? `<button class="btn sm primary" id="caseEdit">编辑</button>` : '') +
        (role === 'admin' ? `<button class="btn sm danger" id="caseDel">删除</button>` : '')
    });
    document.querySelectorAll('#drawerRoot [data-adv]').forEach(el => {
      el.onclick = () => openAdviceDetail(Number(el.dataset.adv));
    });
    document.querySelectorAll('#drawerRoot [data-tag]').forEach(el => {
      el.onclick = () => {
        caseTag = el.dataset.tag;
        document.getElementById('drawerRoot').classList.add('hidden');
        switchTab('cases');
      };
    });
    const simBox = document.getElementById('simBox');
    if (simBox){
      api('/api/agent/cases' + qs({
        keyword: ((c.title || '') + ' ' + (c.symptom || '')).slice(0, 80),
        exclude_id: c.id, limit: 5})).then(data => {
        const box = document.getElementById('simBox');
        if (!box) return;   // 抽屉已关闭/切换
        const sims = (data.cases || []).filter(s => s.id !== c.id);
        if (!sims.length){ box.textContent = '暂无相似案例'; return; }
        box.classList.remove('muted');
        box.innerHTML = sims.map(s => `
          <div data-sim="${s.id}" style="cursor:pointer;padding:6px 8px;border:1px solid var(--border);border-radius:6px;margin-bottom:6px">
            <b style="color:var(--text-1)">${esc(s.title)}</b>
            <span class="muted" style="margin-left:8px">相关度 ${(Math.min(1, s.score || 0) * 100).toFixed(0)}%</span>
          </div>`).join('');
        box.querySelectorAll('[data-sim]').forEach(el => {
          el.onclick = () => {
            const s = sims.find(x => x.id === Number(el.dataset.sim));
            if (s) openCaseDetail(s.id, s);
          };
        });
      }).catch(() => {
        const box = document.getElementById('simBox');
        if (box) box.textContent = '相似案例加载失败';
      });
    }
    const eb = document.getElementById('caseEdit');
    if (eb) eb.onclick = () => { openCaseForm(c); };
    const db2 = document.getElementById('caseDel');
    if (db2) db2.onclick = async () => {
      const yes = await confirmDialog('删除后不可恢复，确认删除该案例？', '删除案例');
      if (!yes) return;
      try{
        await api(`/api/agent/cases/${c.id}`, {method:'DELETE'});
        toastSuccess('已删除');
        document.getElementById('drawerRoot').classList.add('hidden');
        loadCases();
      }catch(e){ toastError('删除失败：' + (e.message || '')); }
    };
  }
  function openCaseForm(c){
    const isEdit = !!c;
    const F = (k, label, val, ph, ta) => `
      <div class="field"><label>${label}</label>
        ${ta
          ? `<textarea id="f_${k}" placeholder="${esc(ph || '')}">${esc(val || '')}</textarea>`
          : `<input type="text" id="f_${k}" value="${esc(val || '')}" placeholder="${esc(ph || '')}">`}
      </div>`;
    const d = openDrawer({
      title: (isEdit ? '编辑' : '新建') + '故障案例',
      wide: true,
      body: `
        ${F('title','标题 *', c && c.title, '如：order 服务磁盘写满导致发布失败', false)}
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px">
          ${F('service','服务', c && c.service, 'order')}
          ${F('env','环境', c && c.env, 'prod')}
          ${F('version','版本', c && c.version, 'v1.2.3')}
        </div>
        ${F('symptom','症状描述 *', c && c.symptom, '故障现场与报错', true)}
        ${F('root_cause','根因分析', c && c.root_cause, '最终定位的原因', true)}
        ${F('action','处置动作', c && c.action, '回滚 / 扩容 / 修配置…', true)}
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
          ${F('result','处置结果', c && c.result, '成功 / 失败 / 待验证', false)}
          ${F('tags','标签（逗号分隔）', c && (c.tags || []).join(','), 'mysql, 磁盘, 回滚', false)}
        </div>
        ${F('lesson','经验教训', c && c.lesson, '下次如何避免 / 快速恢复', true)}`,
      footer: `<button class="btn" id="caseFormCancel">取消</button>
               <button class="btn primary" id="caseFormOk">${isEdit ? '保存修改' : '创建'}</button>`
    });
    const cancel = document.getElementById('caseFormCancel');
    if (cancel) cancel.onclick = () => { document.getElementById('drawerRoot').classList.add('hidden'); };
    const ok = document.getElementById('caseFormOk');
    if (ok) ok.onclick = async () => {
      const val = id => ((document.getElementById('f_' + id) || {}).value || '').trim();
      const body = {
        title: val('title'), service: val('service'), env: val('env'),
        version: val('version'), symptom: val('symptom'), root_cause: val('root_cause'),
        action: val('action'), result: val('result'), lesson: val('lesson'),
        tags: val('tags') ? val('tags').split(/[,，]/).map(s => s.trim()).filter(Boolean) : []
      };
      if (!body.title || !body.symptom){ toastError('标题与症状为必填'); return; }
      ok.disabled = true;
      try{
        if (isEdit){
          await api(`/api/agent/cases/${c.id}`, {method:'PUT', body: JSON.stringify(body)});
          toastSuccess('案例已更新');
        }else{
          await api('/api/agent/cases', {method:'POST', body: JSON.stringify(body)});
          toastSuccess('案例已创建');
        }
        document.getElementById('drawerRoot').classList.add('hidden');
        loadCases();
      }catch(e){
        toastError('保存失败：' + (e.message || ''));
        ok.disabled = false;
      }
    };
    return d;
  }

  // ==================== 网关配置 ====================
  async function renderGwChip(){
    const chip = document.getElementById('gwChip');
    if (!chip) return;
    try{ gw = await api('/api/agent/config'); }catch(e){ gw = null; }
    if (!chip) return;
    const ok = gw && gw.available;
    chip.innerHTML = `<span class="dot ${ok ? 'ok' : 'warn'}"></span>` +
      (ok ? `LLM 网关就绪${gw.model ? ' · ' + esc(gw.model) : ''}`
          : '未配置 LLM 网关（点击配置）');
  }
  function openGwModal(){
    if (!gw){
      toastError('网关状态读取失败，请刷新页面');
      return;
    }
    const isAdmin = role === 'admin';
    openDrawer({
      title: 'LLM 网关配置',
      body: `
        <div style="margin-bottom:14px" class="plan-detail">
          <div class="row"><b>状态</b>
            ${gw.available ? '<span class="badge success">可用</span>' : '<span class="badge warning">未配置</span>'}</div>
          <div class="row"><b>服务地址</b>${esc(gw.base_url_host || '-')}</div>
          <div class="row"><b>模型</b>${esc(gw.model || '（服务端默认）')}</div>
          <div class="row"><b>当前角色</b>${esc(gw.role || role)}</div>
        </div>
        <div class="muted" style="font-size:var(--fs-12);line-height:1.8;margin-bottom:12px">
          支持任意 OpenAI 兼容 /chat/completions 网关。viewer/operator 仅可查看；由 admin 在下方维护，也可通过 AGENT_LLM_BASE_URL / AGENT_LLM_API_KEY / AGENT_LLM_MODEL 环境变量启动时注入。
        </div>
        ${isAdmin ? `
          <div class="field"><label>Base URL（如 https://api.deepseek.com/v1）</label>
            <input type="text" id="gwBase" placeholder="https://…/v1"></div>
          <div class="field"><label>API Key</label>
            <input type="password" id="gwKey" placeholder="sk-…（留空表示不修改）"></div>
          <div class="field"><label>模型名（可空，由服务端决定）</label>
            <input type="text" id="gwModel" placeholder="deepseek-chat"></div>`
          : '<div class="muted">请联系管理员（admin）配置或通过环境变量注入。</div>'}`,
      footer: isAdmin
        ? `<button class="btn" id="gwCancel">关闭</button><button class="btn primary" id="gwSave">保存</button>`
        : `<button class="btn" id="gwCancel">关闭</button>`
    });
    const cancel = document.getElementById('gwCancel');
    if (cancel) cancel.onclick = () => { document.getElementById('drawerRoot').classList.add('hidden'); };
    const save = document.getElementById('gwSave');
    if (save) save.onclick = async () => {
      const body = {
        base_url: (document.getElementById('gwBase') || {}).value || '',
        api_key: (document.getElementById('gwKey') || {}).value || '',
        model: (document.getElementById('gwModel') || {}).value || ''
      };
      if (!body.base_url && !body.api_key && !body.model){ toastError('请至少填写一项'); return; }
      try{
        await api('/api/agent/config', {method:'POST', body: JSON.stringify(body)});
        toastSuccess('网关配置已保存');
        document.getElementById('drawerRoot').classList.add('hidden');
        renderGwChip();
      }catch(e){ toastError('保存失败：' + (e.message || '')); }
    };
  }

  function openDigestModal(){
    openDrawer({
      title: '巡检日报订阅',
      body: '<div class="muted" style="padding:20px;text-align:center">加载中…</div>',
      footer: role === 'admin'
        ? `<button class="btn" id="dgCancel">关闭</button>
           <button class="btn" id="dgPreview">👁 预览内容</button>
           <button class="btn primary" id="dgSave">保存配置</button>`
        : `<button class="btn" id="dgCancel">关闭</button>`
    });
    const cancel = document.getElementById('dgCancel');
    if (cancel) cancel.onclick = () => { document.getElementById('drawerRoot').classList.add('hidden'); };
    const closeIt = () => document.getElementById('drawerRoot').classList.add('hidden');
    (async () => {
      let cfg = {};
      try{ cfg = (await api('/api/agent/digest/config')).config || {}; }
      catch(e){ toastError('日报配置读取失败：' + (e.message || '')); }
      const chans = [['email','邮件'],['wecom','企微'],['webhook','Webhook'],
        ['sms_aliyun','阿里云短信'],['feishu','飞书'],['dingtalk','钉钉']]; // 与告警页 CHANNELS / alert_channels.CHANNELS 同源
      const sel = cfg.channels || [];
      const bc = document.querySelector('#drawerRoot .drawer-body')
        || document.getElementById('drawerRoot');
      if (bc) bc.innerHTML = `
        <div class="muted" style="font-size:var(--fs-12);line-height:1.8;margin-bottom:10px">
          定时聚合服务健康 / 未解决告警 / 近 24h 发布 / 待审审批单 / 定时计划，
          推送到已配置的通知渠道。发送失败的原因会显示在下方。
        </div>
        ${role === 'admin' ? `
        <div class="field"><label>启用定时推送</label>
          <select id="dgEnabled">
            <option value="0" ${cfg.enabled ? '' : 'selected'}>关闭</option>
            <option value="1" ${cfg.enabled ? 'selected' : ''}>开启</option>
          </select></div>
        <div class="field"><label>推送时间（cron 表达式，默认每天 09:00）</label>
          <input type="text" id="dgCron" value="${esc(cfg.cron || '0 9 * * *')}" placeholder="0 9 * * *"></div>
        <div class="field"><label>日报标题</label>
          <input type="text" id="dgTitle" value="${esc(cfg.title || '巡检日报')}" maxlength="40"></div>
        <div class="field full"><label>通知渠道（可多选；渠道配置在告警通知页维护）</label>
          <div class="rule-ch-box">
            ${chans.map(([k, v]) => `<label class="rule-ch-cb">
              <input type="checkbox" value="${k}" ${sel.includes(k) ? 'checked' : ''}> ${v}</label>`).join('')}
          </div></div>`
        : '<div class="muted">仅管理员可修改配置；可点击预览查看当前日报内容。</div>'}
        ${cfg.last_sent_at ? `<div class="plan-detail" style="margin-top:10px">
          <div class="row"><b>上次发送</b>${fmtTime(cfg.last_sent_at)}</div>
          <div class="row"><b>结果</b>${esc(cfg.last_result || '-')}</div></div>` : ''}
        ${canManage ? `<div id="dgLogsBox" class="plan-detail" style="margin-top:12px">
          <div class="row"><b>发送历史</b><span class="muted">加载中…</span></div></div>` : ''}
        <div id="dgPreviewBox" class="plan-detail" style="margin-top:12px"></div>`;
      const pv = document.getElementById('dgPreview');
      if (pv) pv.onclick = async () => {
        const box = document.getElementById('dgPreviewBox');
        try{
          const data = await api('/api/agent/digest/preview');
          if (box) box.innerHTML = `
            <div class="row"><b>预览（不发送）</b></div>
            <pre style="white-space:pre-wrap;font-size:12px;line-height:1.7;margin:6px 0 0">${esc(data.text || '')}</pre>`;
        }catch(e){ toastError('预览失败：' + (e.message || '')); }
      };
      if (canManage){
        (async () => {
          const box = document.getElementById('dgLogsBox');
          if (!box) return;
          try{
            const data = await api('/api/agent/digest/logs' + qs({limit: 10}));
            const logs = data.logs || [];
            box.innerHTML = logs.length
              ? `<div class="row"><b>发送历史（最近 ${logs.length} 次）</b></div>` + logs.map(l => `
                <div class="row" style="font-size:var(--fs-12)">
                  <span>${l.ok_count === l.total_count ? '✅' : '⚠️'} ${fmtTime(l.sent_at)}</span>
                  <span class="muted">${esc(l.summary || '')}</span>
                </div>`).join('')
              : '<div class="row"><b>发送历史</b><span class="muted">暂无记录</span></div>';
          }catch(e){
            box.innerHTML = '<div class="row"><b>发送历史</b><span class="muted">读取失败</span></div>';
          }
        })();
      }
      const save = document.getElementById('dgSave');
      if (save) save.onclick = async () => {
        const chansSel = Array.from(
          document.querySelectorAll('#drawerRoot input[type=checkbox]:checked'))
          .map(x => x.value);
        const body = {
          enabled: (document.getElementById('dgEnabled') || {}).value === '1',
          cron: (document.getElementById('dgCron') || {}).value || '',
          title: (document.getElementById('dgTitle') || {}).value || '',
          channels: chansSel
        };
        if (body.enabled && !chansSel.length){ toastError('请至少选择一个通知渠道'); return; }
        try{
          await api('/api/agent/digest/config', {method:'POST', body: JSON.stringify(body)});
          toastSuccess('日报配置已保存');
          closeIt();
        }catch(e){ toastError('保存失败：' + (e.message || '')); }
      };
    })();
  }

  // ==================== 模块导出 ====================
  window.agent = {
    render: async function(root){
      destroyed = false;
      renderPage();
      clearLiveOffs();
      // 事件源：agent（新建议 + agent_runs/agent_advice 状态流转）与
      // tasks（审批单关联的发布任务状态）；通道降级时回 15s 轮询（与旧口径一致）
      liveOffs = [liveBind(['agent', 'tasks'], refreshLiveTab, 15000)];
      return [];
    },
    destroy: function(){
      destroyed = true;
      abortChat();
      clearLiveOffs();
    }
  };
})();
