// pages/deploy.js — 发布中心（任务列表 + 新建 + 详情日志流 + 终止）
(function(){
  let timer = null;      // 列表 5s 轮询
  let logTimer = null;   // 当前日志流的停止函数（destroy 兜底；流自身在关抽屉/终态时自停）
  let statusFilter = 'all';
  let projFilter = 'all'; // all / 项目 id
  let envFilter = 'all';  // all / 环境名
  let actionFilter = 'all'; // all / deploy / rollback
  let projects = [];
  let envs = [];
  let tasks = [];
  let destroyed = false; // 路由切换后丢弃 in-flight 响应，防泄漏轮询

  // 项目/环境筛选数据：切换项目时重建环境选项，并重置失效的环境筛选
  async function loadFilters(){
    projects = await api('/api/projects').catch(() => []);
    envs = [];
    if (projFilter !== 'all'){
      const p = projects.find(x => x.id === Number(projFilter));
      if (p) envs = await api(`/api/projects/${p.id}/environments`).catch(() => []);
      else projFilter = 'all';
    }
    if (envFilter !== 'all' && !envs.some(e => e.name === envFilter)) envFilter = 'all';
  }
  let lastSig = null; // 5s 轮询数据签名：无变化时跳过全量 innerHTML 重建
  async function loadTasks(){
    try{
      const q = new URLSearchParams({limit:'200'});
      if (projFilter !== 'all') q.set('project_id', projFilter);
      const data = await api('/api/tasks?' + q);
      if (destroyed) return;
      // 轮询无变化时跳过重建（任务列表高频轮询下 innerHTML 全量重建是主要开销）
      const sig = JSON.stringify(data);
      if (lastSig === sig) return;
      lastSig = sig;
      tasks = data;
      renderTable();
    }catch(e){ /* 轮询失败静默（列表保留旧数据，与 alerts 轮询一致） */ }
  }
  function renderTable(){
    const root = document.getElementById('pageRoot');
    const list = tasks.filter(t => statusFilter === 'all' || t.status === statusFilter)
      .filter(t => (projFilter === 'all' || t.project_id === Number(projFilter)) &&
                   (envFilter === 'all' || t.env === envFilter))
      .filter(t => actionFilter === 'all' || (t.action || 'deploy') === actionFilter);
    const box = root.querySelector('#taskBox');
    if (!box) return;
    box.innerHTML = list.length ? `
      <div class="panel" style="padding:0">
        <div class="tbl-wrap"><table class="tbl task-tbl"><thead><tr>
          <th>#</th><th>任务</th><th>操作</th><th>类型</th><th>环境</th><th>版本</th><th>状态</th><th>启动时间</th><th>耗时</th></tr></thead><tbody>
          ${list.map(t => `<tr>
            <td class="num">${t.id}</td>
            <td><span class="task-name" data-task="${t.id}">${esc(t.name)}</span></td>
            <td>${t.action === 'rollback' ? '<span class="tag tag-warn">回滚</span>' : t.action === 'restart' ? '<span class="tag tag-info">重启</span>' : '<span class="tag tag-ok">发布</span>'}</td>
            <td>${t.type === 'pipeline' ? '流水线' : t.type === 'restart' ? '重启' : '服务'}</td>
            <td>${esc(t.env || '-')}</td>
            <td class="num">${esc(t.version || t.previous_version || '-')}</td>
            <td>${statusBadge(t.status)}</td>
            <td class="num muted">${fmtTime(t.started_at)}</td>
            <td class="num muted">${t.finished_at && t.started_at ? Math.max(0, Math.round(t.finished_at - t.started_at)) + 's' : (t.status === 'running' ? '进行中' : '-')}</td>
          </tr>`).join('')}
        </tbody></table></div></div>`
      : `<div class="panel">${emptyState('📋', '暂无任务' + (statusFilter !== 'all' ? '（当前筛选下）' : ''))}</div>`;
    box.querySelectorAll('.task-name').forEach(el => el.onclick = () => openTaskDetail(Number(el.dataset.task)));
  }

  // ---- 任务详情 + 日志流（offset 增量追尾，running→终态停止）----
  // force=true 为跨页入口（全局搜索/dashboard 经 openTaskLogDrawer），不受 destroyed 限制
  async function openTaskDetail(id, force){
    let task;
    try{ task = await api('/api/tasks/' + id); }
    catch(e){ toastError('任务详情获取失败：' + e.message); return; }
    if (!force && destroyed) return; // 页面内点击在途时路由已切换 → 丢弃响应
    let stopStream = null; // streamLog 在 openDrawer 之后调用（依赖 #taskLog DOM），闭包持有
    const body = openDrawer({ title: `任务 #${id} ${task.name}`, wide: true, onClose: () => { if (stopStream) stopStream(); }, body: `
      <div class="detail-grid">
        <div><div class="dg-label">状态</div><div style="margin-top:2px" id="taskStatusBadge">${statusBadge(task.status)}</div></div>
        <div><div class="dg-label">操作</div><div style="margin-top:2px">${task.action === 'rollback' ? '<span class="tag tag-warn">回滚</span>' : task.action === 'restart' ? '<span class="tag tag-info">重启</span>' : '<span class="tag tag-ok">发布</span>'}</div></div>
        <div><div class="dg-label">类型</div><div style="margin-top:2px">${task.type === 'pipeline' ? '流水线' : task.type === 'restart' ? '重启' : '服务'}</div></div>
        <div><div class="dg-label">环境</div><div class="num" style="margin-top:2px">${esc(task.env || '-')}</div></div>
        <div><div class="dg-label">发布版本</div><div class="num" style="margin-top:2px">${task.action === 'restart' ? '-' : task.action === 'rollback' ? esc(task.version || task.previous_version || '-') : esc(task.previous_version || '-') + ' → ' + esc(task.version || '-')}</div></div>
        <div><div class="dg-label">启动</div><div class="num" style="margin-top:2px">${fmtTime(task.started_at)}</div></div>
      </div>
      <div class="log-console" id="taskLog">加载中...</div>
      <div id="taskActionBtns">${['running','pending'].includes(task.status) && canDeploy()
        ? `<div class="task-drawer-actions"><button class="btn danger" id="killBtn">终止任务</button></div>` : ''}
      ${['failed','killed'].includes(task.status) && canDeploy()
        ? `<div class="task-drawer-actions"><button class="btn primary" id="rerunBtn">重新运行</button></div>` : ''}
      ${['success','failed','killed'].includes(task.status) && canDeploy()
        ? `<div class="task-drawer-actions" style="justify-content:flex-end"><button class="btn danger ghost" id="delTaskBtn">删除任务</button></div>` : ''}</div>
    `});
    stopStream = streamLog(id, task);
    logTimer = stopStream;
    const killBtn = document.getElementById('killBtn');
    if (killBtn) killBtn.onclick = async () => {
      if (!await confirmDialog(`确认终止任务 #${id}？终止后不可恢复。`)) return;
      try{
        const r = await api(`/api/tasks/${id}/kill`, {method:'POST'});
        toastSuccess(r.killed ? '已发送终止信号' : '任务无法终止（可能已结束）');
        loadTasks().catch(() => {});
      }catch(e){ toastError(e.message); }
    };
    const rerunBtn = document.getElementById('rerunBtn');
    if (rerunBtn) rerunBtn.onclick = () => rerunTask(task);
    const delTaskBtn = document.getElementById('delTaskBtn');
    if (delTaskBtn) delTaskBtn.onclick = () => deleteTask(task);
  }
  // 日志追尾轮询：interval 为流内局部变量，返回 stop() 供关抽屉/destroy 清理，防多流交叉误杀
  function streamLog(id, task){
    const logBox = document.getElementById('taskLog');
    if (!logBox) return null;
    let offset = 0;
    let interval = null;
    let buf = '';            // 累积待渲染文本，rAF 统一刷入 DOM
    let flushing = false;    // 防止多个 rAF 回调堆叠
    const stop = () => { if (interval){ clearInterval(interval); interval = null; } };
    // 批量 DOM 写入：每帧最多一次 textContent 赋值 + scrollTop，避免高频重排
    function flushDOM(){
      if (!buf || !logBox) { flushing = false; return; }
      logBox.textContent += buf;
      logBox.scrollTop = logBox.scrollHeight;
      buf = '';
      flushing = false;
    }
    const tick = async () => {
      try{
        const d = await api(`/api/tasks/${id}/log?offset=${offset}`);
        if (d.log){
          // 首次拉取且后端截断：前置提示，用户看到的是最新日志而非从头追赶
          if (offset === 0 && d.truncated){
            buf += '── 日志较长，仅显示最近输出 ──\n';
          }
          buf += d.log;
          if (!flushing){ flushing = true; requestAnimationFrame(flushDOM); }
        }
        offset = d.offset;
        if (['success','failed','killed'].includes(d.status)){
          // 终态：继续拉取直到日志全部读完（大日志需多个 8000 字符块）
          while (true){
            try{
              const d2 = await api(`/api/tasks/${id}/log?offset=${offset}`);
              if (d2.log) buf += d2.log;
              offset = d2.offset;
              if (!d2.log) break; // 后端无更多内容
            }catch(e){ break; }
          }
          // 刷完缓冲，再追加结束标记
          if (buf && logBox){ logBox.textContent += buf; buf = ''; }
          logBox.innerHTML += `<span class="tail">\n── 任务已结束（${esc(d.status)}）──</span>`;
          logBox.scrollTop = logBox.scrollHeight;
          stop();
          if (task.status !== d.status){
            task.status = d.status;
            // 刷新抽屉状态徽标和操作按钮（原快照已过期）
            const badge = document.getElementById('taskStatusBadge');
            if (badge) badge.innerHTML = statusBadge(d.status);
            const actBox = document.getElementById('taskActionBtns');
            if (actBox && canDeploy()){
              let html = '';
              if (['failed','killed'].includes(d.status))
                html += `<div class="task-drawer-actions"><button class="btn primary" id="rerunBtn">重新运行</button></div>`;
              if (['success','failed','killed'].includes(d.status))
                html += `<div class="task-drawer-actions" style="justify-content:flex-end"><button class="btn danger ghost" id="delTaskBtn">删除任务</button></div>`;
              actBox.innerHTML = html;
              const rb = document.getElementById('rerunBtn');  if (rb) rb.onclick = () => rerunTask(task);
              const db = document.getElementById('delTaskBtn'); if (db) db.onclick = () => deleteTask(task);
            }
            loadTasks().catch(() => {});
          }
        }
      }catch(e){ logBox.textContent += '\n[日志拉取失败] ' + e.message; stop(); }
    };
    tick();
    if (['running','pending'].includes(task.status)){
      interval = setInterval(tick, 2000);
    }
    return stop;
  }

  // ---- 重新运行：用原任务参数创建新任务 ----
  async function rerunTask(task){
    if (!await confirmDialog(`确认使用相同参数重新运行任务 #${task.id}（${task.name}）？`)) return;
    const extra = Array.isArray(task.extra) ? task.extra : [];
    const doRerun = async (forceWindow) => {
      return await api('/api/tasks', {method:'POST', body: JSON.stringify({
        project_id: task.project_id,
        type: task.type,
        ref_id: task.ref_id,
        env: task.env || '',
        version: task.version || '',
        previous_version: task.previous_version || '',
        rollback: extra.includes('--rollback'),
        auto_version: extra.includes('--auto-version') || (!task.version && !task.previous_version),
        skip_build: extra.includes('--skip-build'),
        force_window: forceWindow,
      })});
    };
    try{
      const resp = await doRerun(false);
      toastSuccess(`任务 #${resp.id} 已创建，开始重新运行`);
      await loadTasks();
      openTaskDetail(resp.id, true);
    }catch(e){
      if (e.message === 'window_warn' && e.data && e.data.window){
        if (!await confirmDialog(`当前处于发布窗口「${e.data.window.name}」（软警告），确认仍要继续重新运行？`)) return;
        try{
          const resp2 = await doRerun(true);
          toastSuccess(`任务 #${resp2.id} 已创建，开始重新运行（已忽略窗口警告）`);
          await loadTasks();
          openTaskDetail(resp2.id, true);
        }catch(e2){ toastError('重新运行失败：' + e2.message); }
      } else { toastError('重新运行失败：' + e.message); }
    }
  }

  // ---- 删除任务 ----
  async function deleteTask(task){
    if (!await confirmDialog(`确认删除任务 #${task.id}（${task.name}）？删除后不可恢复。`)) return;
    try{
      await api(`/api/tasks/${task.id}`, {method:'DELETE'});
      toastSuccess(`任务 #${task.id} 已删除`);
      // 关闭抽屉
      const dwClose = document.getElementById('dwClose');
      if (dwClose) dwClose.click();
      await loadTasks();
    }catch(e){ toastError('删除失败：' + e.message); }
  }

  // ---- 新建发布（复用 devices 的抽屉逻辑，保持单一入口）----
  function openNewDeploy(){
    toast('请在「设备总览」选择服务发起发布', '');
    location.hash = '#/devices';
  }

  // ---- 模块导出 ----
  window.deploy = {
    render: async function(root){
      destroyed = false;
      lastSig = null; // 重置签名：确保重渲染后首次 loadTasks 必定执行 renderTable
      if (timer) clearInterval(timer); // 项目筛选重渲染时先清理旧轮询
      timer = null;
      const statuses = [['all','全部'],['running','运行中'],['pending','等待中'],['success','成功'],['failed','失败']];
      const subTabs = `
        <div class="sub-tabs" style="display:flex;gap:4px;margin-bottom:12px">
          <a class="chip ${location.hash.replace(/^#\/?/,'').split('?')[0] === 'deploy' ? 'active' : ''}" href="#/deploy">任务列表</a>
          <a class="chip ${location.hash.startsWith('#/deploy/schedules') ? 'active' : ''}" href="#/deploy/schedules">📅 定时计划</a>
          <a class="chip ${location.hash.startsWith('#/deploy/windows') ? 'active' : ''}" href="#/deploy/windows">🔒 发布窗口</a>
          <a class="chip ${location.hash.startsWith('#/deploy/canary') ? 'active' : ''}" href="#/deploy/canary">🐤 灰度策略</a>
          <a class="chip ${location.hash.startsWith('#/deploy/topology') ? 'active' : ''}" href="#/deploy/topology">🕸️ 依赖拓扑</a>
          <a class="chip ${location.hash.startsWith('#/deploy/compare') ? 'active' : ''}" href="#/deploy/compare">📊 发布对比</a>
        </div>`;
      root.innerHTML = subTabs + `
        <div class="deploy-toolbar">
          <span class="weak" style="font-size:12px">项目</span>
          <select class="filter-sel" id="dpProjSel"></select>
          <span class="weak" style="font-size:12px">环境</span>
          <select class="filter-sel" id="dpEnvSel"></select>
          <span style="width:1px;height:20px;background:var(--border);margin:0 4px"></span>
          <span class="weak" style="font-size:12px">状态</span>
          ${statuses.map(([k, v]) => `<span class="chip ${k === statusFilter ? 'active' : ''}" data-st="${k}">${v}</span>`).join('')}
          <span style="width:1px;height:20px;background:var(--border);margin:0 4px"></span>
          <span class="weak" style="font-size:12px">操作</span>
          ${[['all','全部'],['deploy','发布'],['rollback','回滚'],['restart','重启']].map(([k, v]) => `<span class="chip ${k === actionFilter ? 'active' : ''}" data-act="${k}">${v}</span>`).join('')}
          <span style="flex:1"></span>
          ${canDeploy() ? '<button class="btn primary" id="newTaskBtn">+ 新建发布</button>' : ''}
        </div>
        <div id="taskBox"></div>`;
      root.querySelectorAll('.chip[data-st]').forEach(c => c.onclick = () => {
        statusFilter = c.dataset.st;
        document.querySelectorAll('.chip[data-st]').forEach(x => x.classList.toggle('active', x === c));
        renderTable();
      });
      root.querySelectorAll('.chip[data-act]').forEach(c => c.onclick = () => {
        actionFilter = c.dataset.act;
        document.querySelectorAll('.chip[data-act]').forEach(x => x.classList.toggle('active', x === c));
        renderTable();
      });
      const nb = document.getElementById('newTaskBtn');
      if (nb) nb.onclick = openNewDeploy;
      await loadFilters();
      const projSel = document.getElementById('dpProjSel');
      if (projSel){
        projSel.innerHTML = '<option value="all">全部项目</option>' + projects.map(p =>
          `<option value="${p.id}" ${p.id === Number(projFilter) ? 'selected' : ''}>${esc(p.name)}</option>`).join('');
        projSel.onchange = async () => {
          projFilter = projSel.value; envFilter = 'all';
          await loadFilters();
          // 重建环境选项并重拉列表（经模块导出函数重渲染）
          window.deploy.render(document.getElementById('pageRoot')).catch(() => {});
        };
      }
      const envSel = document.getElementById('dpEnvSel');
      if (envSel){
        envSel.innerHTML = '<option value="all">全部环境</option>' + envs.map(e =>
          `<option value="${esc(e.name)}" ${e.name === envFilter ? 'selected' : ''}>${esc(e.name)}</option>`).join('');
        envSel.onchange = () => { envFilter = envSel.value; renderTable(); };
      }
      await loadTasks().catch(() => {});
      if (destroyed) return []; // destroy 后在途续体不再创建轮询
      timer = setInterval(loadTasks, 5000);
      return [timer];
    },
    destroy: function(){ destroyed = true; if (timer) clearInterval(timer); timer = null; lastSig = null; if (logTimer){ logTimer(); logTimer = null; } },
  };
  // 跨页复用：dashboard 最近发布 / layout 全局搜索 → 任务日志抽屉（无路由依赖）
  window.deployPage = { openTaskDetailSafe: function(id){ return openTaskDetail(id, true); } };
  window.openTaskLogDrawer = function(id){
    if (window.deployPage) deployPage.openTaskDetailSafe(id);
  };
})();
