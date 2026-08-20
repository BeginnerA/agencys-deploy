// pages/devices.js — 设备总览（项目/环境切换 + 服务卡片墙 + 发布抽屉 + 服务器列表）
(function(){
  let state = { projects: [], currentProject: null, envs: [], currentEnv: null };
  let timer = null;
  let monitorCache = null;
  let tasksCache = [];
  let reqSeq = 0; // 丢弃过期响应（快速切换项目/环境时防乱序覆盖）
  let lastSig = null; // 3s 轮询数据签名：无变化时跳过卡片墙/服务器表全量重建
  let destroyed = false; // 路由切换后丢弃 in-flight 响应，防覆盖新页面/泄漏轮询

  // 服务卡片：状态灯优先级 发布中 > 探测结果（沿用旧 dotClass 语义）
  function dotClass(svc, runningTask){
    if (runningTask) return 'warn';
    const st = svc.status;
    if (st === 'online') return 'ok';
    if (st === 'error') return 'bad';
    if (st === 'probe_failed') return 'warn';
    return 'gray';
  }
  function svcLabel(st, run){
    if (run) return '发布中';
    const map = {online:'在线', error:'异常', not_deployed:'未部署', probe_failed:'探测失败'};
    return map[st.status] || '未知';
  }

  async function loadProjects(){
    state.projects = await api('/api/projects');
    // 支持全局搜索跳转指定项目（layout.js 写入 dev_filter_project）
    const filter = localStorage.getItem('dev_filter_project');
    if (filter) localStorage.removeItem('dev_filter_project');
    if (!state.projects.length) return;
    if (!state.currentProject){
      const want = filter ? state.projects.find(p => p.id === Number(filter)) : null;
      await switchProject(want ? want.id : state.projects[0].id);
    } else {
      render(); // 再次进入页面：保留当前选择，重新渲染并重建轮询
    }
  }
  async function switchProject(pid){
    const p = state.projects.find(x => x.id === pid);
    if (!p) return;
    state.currentProject = p;
    state.envs = await api(`/api/projects/${pid}/environments`);
    state.currentEnv = (state.envs.find(e => e.is_default) || state.envs[0] || {}).name || null;
    render();
  }
  function switchEnv(name){ state.currentEnv = name; render(); }

  async function loadData(){
    if (destroyed || !state.currentProject) return;
    const seq = ++reqSeq;
    const [services, monitor] = await Promise.all([
      api(`/api/projects/${state.currentProject.id}/services`),
      api(`/api/monitor/overview?project_id=${state.currentProject.id}&env=${state.currentEnv||''}`).catch(() => null),
    ]);
    if (destroyed || seq !== reqSeq) return; // 过期响应丢弃
    const tasks = await api(`/api/tasks?project_id=${state.currentProject.id}&limit=20`).catch(() => []);
    if (destroyed || seq !== reqSeq) return;
    // 3s 轮询签名：数据无变化时跳过全量重建（卡片墙/服务器表 innerHTML 是主要开销）
    const sig = JSON.stringify({s: services, m: monitor, t: tasks});
    if (lastSig === sig) return;
    lastSig = sig;
    monitorCache = monitor;
    tasksCache = tasks;
    renderCards(services);
    renderServers();
  }
  function renderCards(services){
    const svcStatus = (monitorCache && monitorCache.services) || {};
    const running = tasksCache.filter(t => t.status === 'running');
    const root = document.getElementById('pageRoot');
    const wall = root.querySelector('#svcWall');
    if (!wall) return;
    wall.innerHTML = services.map(s => {
      const st = svcStatus[s.name] || {};
      const run = running.find(r => r.type === 'project' && r.name === s.name && r.env === state.currentEnv);
      const ver = st.version || '—';
      const inst = st.instances !== undefined && st.instances !== -1 ? st.instances : '—';
      const ts = st.ts ? fmtTime(st.ts) : '';
      const dotCls = dotClass(st, run);
      return `<div class="svc-card ${run ? 'publishing' : ''}" data-id="${s.id}">
        <div class="svc-head"><span class="dot ${dotCls}"></span><b class="ellipsis">${esc(s.name)}</b>
          ${s.enabled ? '' : '<span class="weak" style="font-size:11px">(停用)</span>'}</div>
        <div class="svc-meta">
          <div><span class="weak">镜像</span> <span class="num">${esc(ver)}</span></div>
          <div><span class="weak">实例</span> <span class="num">${inst}</span>
               <span class="weak" style="margin-left:10px">状态</span> ${svcLabel(st, run)}</div>
          <div><span class="weak">端口</span> <span class="num">${s.health_port || '-'}</span>
               ${ts ? `<span class="weak" style="margin-left:10px">探测</span> ${ts}` : ''}</div>
        </div>
        <div class="svc-foot">
          ${canDeploy() ? `<button class="btn sm primary" data-pub="${s.id}">发布</button>
          <button class="btn sm ghost" data-roll="${s.id}">回滚</button>` : '<span class="weak" style="font-size:11px">只读账号</span>'}
        </div>
      </div>`;
    }).join('') || emptyState('📭', '该项目暂无服务');
    wall.querySelectorAll('[data-pub]').forEach(b => b.onclick = () => openDeployDrawer(Number(b.dataset.pub), false));
    wall.querySelectorAll('[data-roll]').forEach(b => b.onclick = () => openDeployDrawer(Number(b.dataset.roll), true));
  }
  function renderServers(){
    const root = document.getElementById('pageRoot');
    const box = root.querySelector('#serverBox');
    if (!box) return;
    const servers = (monitorCache && monitorCache.servers) || [];
    box.innerHTML = servers.length
      ? `<div class="panel"><div class="panel-title">服务器列表（${servers.length}）</div>
          <div class="tbl-wrap"><table class="tbl"><thead><tr>
            <th>主机</th><th>SSH</th><th>容器数</th><th>最后探测</th><th>备注</th></tr></thead><tbody>
            ${servers.map(s => `<tr>
              <td><span class="dot ${s.ssh_ok ? 'ok' : 'bad'}"></span><span class="num">${esc(s.host)}</span></td>
              <td>${s.ssh_ok ? '<span class="ok">连通</span>' : '<span class="err">失败</span>'}</td>
              <td class="num">${s.containers ?? '-'}</td>
              <td class="muted">${s.error ? esc(s.error) : fmtTime(s.ts)}</td>
              <td class="muted">${esc(s.remark || '')}</td></tr>`).join('')}
          </tbody></table></div></div>`
      : `<div class="panel"><div class="panel-title">服务器列表</div>
          <div class="empty-state"><div class="e-ico">🖥️</div><div>实时探测未启用或暂无数据</div></div></div>`;
  }

  // ---- 发布抽屉（规格 §9：卡片 → 抽屉表单 → 提交跳发布中心）----
  async function openDeployDrawer(sid, rollback){
    const pid = state.currentProject.id;
    let services, pipelines;
    try{
      [services, pipelines] = await Promise.all([
        api(`/api/projects/${pid}/services`),
        api(`/api/projects/${pid}/pipelines`),
      ]);
    }catch(e){ toastError('发布配置加载失败：' + e.message); return; }
    if (destroyed) return; // 路由切换后在途响应丢弃，防新页面弹出旧抽屉
    const svc = services.find(s => s.id === sid);
    const envs = state.envs;
    // 回滚入口来自服务卡片：锁定服务发布类型与目标服务，仅需回滚目标版本。
    // 版本号/自动版本号/跳过构建与回滚无关（--auto-version 与 --rollback 互斥、
    // 回滚本身跳过构建、version 由后端兜底为目标版本），回滚表单不展示。
    const body = openDrawer({ title: (rollback ? '回滚 ' : '发布 ') + (svc ? svc.name : ''), wide:false, body: rollback ? `
      <div class="form-grid">
        <div class="field full"><label>回滚服务</label>
          <input value="${esc(svc ? svc.name : '')}" readonly></div>
        <div class="field"><label>环境</label>
          <select id="dpEnv">${envs.map(e => `<option ${e.name === state.currentEnv ? 'selected' : ''}>${esc(e.name)}</option>`).join('')}</select></div>
        <div class="field"><label>回滚目标版本</label><input id="dpPrevVersion" placeholder="必填，回滚到的版本"></div>
      </div>
      <div style="display:flex;gap:10px;margin-top:20px">
        <button class="btn primary flex-1" id="dpSubmit">确认回滚</button>
      </div>` : `
      <div class="form-grid">
        <div class="field full"><label>发布类型</label>
          <select id="dpType">
            <option value="project" selected>服务发布</option>
            <option value="pipeline">流水线发布</option>
          </select></div>
        <div class="field full"><label>服务 / 流水线</label>
          <select id="dpRef"></select></div>
        <div class="field"><label>环境</label>
          <select id="dpEnv">${envs.map(e => `<option ${e.name === state.currentEnv ? 'selected' : ''}>${esc(e.name)}</option>`).join('')}</select></div>
        <div class="field"><label>版本号（可留空）</label><input id="dpVersion" placeholder="如 1.0.3"></div>
        <div class="field"><label>回滚目标版本（回滚时填写）</label><input id="dpPrevVersion" placeholder="回滚到的版本"></div>
        <div class="field check"><label><input type="checkbox" id="dpAuto"> 自动版本号</label></div>
        <div class="field check"><label><input type="checkbox" id="dpSkip"> 跳过构建</label></div>
      </div>
      <div style="display:flex;gap:10px;margin-top:20px">
        <button class="btn primary flex-1" id="dpSubmit">确认发布</button>
      </div>` });
    if (rollback){
      // 回滚提交：固定 type=project/ref_id=sid，version 留空由后端兜底为目标版本
      document.getElementById('dpSubmit').onclick = async () => {
        const env = document.getElementById('dpEnv').value;
        const previous_version = document.getElementById('dpPrevVersion').value.trim();
        try{
          if (!previous_version) throw new Error('请填写回滚目标版本');
          const resp = await api('/api/tasks', {method:'POST', body: JSON.stringify({
            project_id: pid, type: 'project', ref_id: sid, env, version: '', previous_version,
            rollback: true, auto_version: false, skip_build: false})});
          toastSuccess(`任务 #${resp.id} 已创建`);
          document.getElementById('drawerRoot').classList.add('hidden');
          location.hash = '#/deploy';
        }catch(e){ toastError(e.message); }
      };
      return;
    }
    // 发布：类型切换时联动刷新 ref 选项
    const refOpts = () => {
      const isPipe = document.getElementById('dpType').value === 'pipeline';
      return isPipe
        ? pipelines.map(p => `<option value="pipeline:${p.id}">流水线 · ${esc(p.name)}</option>`).join('')
        : services.map(s => `<option value="project:${s.id}" ${s.id === sid ? 'selected' : ''}>服务 · ${esc(s.name)}</option>`).join('');
    };
    const refSel = document.getElementById('dpRef');
    refSel.innerHTML = refOpts();
    document.getElementById('dpType').onchange = () => { refSel.innerHTML = refOpts(); };
    document.getElementById('dpSubmit').onclick = async () => {
      const typeSel = document.getElementById('dpType').value;
      const ref = document.getElementById('dpRef').value;
      const env = document.getElementById('dpEnv').value;
      const version = document.getElementById('dpVersion').value.trim();
      const previous_version = document.getElementById('dpPrevVersion').value.trim();
      const auto_version = document.getElementById('dpAuto').checked;
      const skip_build = document.getElementById('dpSkip').checked;
      try{
        if (auto_version && (version || previous_version)) throw new Error('自动版本与显式版本互斥');
        const [t, rid] = ref.split(':');
        const resp = await api('/api/tasks', {method:'POST', body: JSON.stringify({
          project_id: pid, type: t, ref_id: Number(rid), env, version, previous_version,
          rollback, auto_version, skip_build})});
        toastSuccess(`任务 #${resp.id} 已创建`);
        document.getElementById('drawerRoot').classList.add('hidden');
        location.hash = '#/deploy';
      }catch(e){ toastError(e.message); }
    };
  }

  // ---- 模块导出 ----
  window.devices = {
    render: async function(root){
      destroyed = false;
      root.innerHTML = skeletonHTML(4);
      try{ await loadProjects(); }
      catch(e){ root.innerHTML = errorState('设备数据加载失败：' + e.message, 'navigate'); return []; }
      if (destroyed) return [];
      return timer ? [timer] : [];
    },
    destroy: function(){ destroyed = true; if (timer) clearInterval(timer); timer = null; lastSig = null; },
  };

  async function render(){
    if (destroyed) return;
    lastSig = null; // 整页重建后强制首帧渲染（防止签名跳过导致新容器留空）
    const root = document.getElementById('pageRoot');
    root.innerHTML = `
      <div class="panel" style="margin-bottom:var(--sp-4);padding:12px 16px">
        <div class="flex gap-3" style="flex-wrap:wrap;align-items:center">
          <span class="weak" style="font-size:12px">项目</span>
          <select class="filter-sel" id="projSel">
            ${state.projects.map(p => `<option value="${p.id}" ${p.id === state.currentProject.id ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}
          </select>
          <span style="width:1px;height:20px;background:var(--border);margin:0 4px"></span>
          <span class="weak" style="font-size:12px">环境</span>
          ${state.envs.map(e => `<span class="chip ${e.name === state.currentEnv ? 'active' : ''}" data-env="${esc(e.name)}">${esc(e.name)}</span>`).join('')}
        </div>
      </div>
      <div id="svcWall"></div>
      <div id="serverBox"></div>`;
    const sel = document.getElementById('projSel');
    if (sel) sel.onchange = () => switchProject(Number(sel.value)).catch(e => toastError(e.message));
    root.querySelectorAll('.chip[data-env]').forEach(c => c.onclick = () => switchEnv(c.dataset.env));
    await loadData().catch(() => {});
    if (destroyed) return;
    // 3s 轮询卡片状态（发布中任务状态变化）
    if (timer) clearInterval(timer);
    timer = setInterval(async () => { try{ await loadData(); }catch(e){} }, 3000);
  }
})();
