// pages/deploy_schedules.js — 定时发布计划管理
(function(){
  let timer = null;
  let destroyed = false;
  let schedules = [];
  let projects = [];

  async function loadData(){
    projects = await api('/api/projects').catch(() => []);
    schedules = await api('/api/deploy/schedules').catch(() => []);
  }

  // 加载指定项目的服务列表（带缓存）
  const svcCache = {};
  async function loadServicesForProj(projId){
    if (!projId) return [];
    if (svcCache[projId]) return svcCache[projId];
    const svcs = await api(`/api/projects/${projId}/services`).catch(() => []);
    svcCache[projId] = svcs;
    return svcs;
  }

  // 构建服务名映射（用于表格展示）
  async function buildSvcMap(){
    const projIds = [...new Set(schedules.filter(s => s.ref_type === 'service').map(s => s.project_id))];
    for (const pid of projIds) await loadServicesForProj(pid);
    const svcMap = {};
    schedules.filter(s => s.ref_type === 'service').forEach(s => {
      const svcs = svcCache[s.project_id] || [];
      const svc = svcs.find(v => v.id === s.ref_id);
      if (svc) svcMap[s.id] = svc.name;
    });
    return svcMap;
  }

  async function renderTable(){
    const root = document.getElementById('pageRoot');
    const box = root.querySelector('#schedBox');
    if (!box) return;
    const projMap = {};
    projects.forEach(p => projMap[p.id] = p.name);
    const svcMap = await buildSvcMap();
    box.innerHTML = schedules.length ? `
      <div class="panel" style="padding:0"><div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>#</th><th>名称</th><th>类型</th><th>项目</th><th>服务</th><th>环境</th><th>版本</th><th>下次执行</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>${schedules.map(s => `<tr>
          <td class="num">${s.id}</td>
          <td>${esc(s.name)}</td>
          <td>${s.type === 'cron' ? '🔄 周期' : '⏰ 一次'}</td>
          <td>${esc(projMap[s.project_id] || '-')}</td>
          <td>${s.ref_type === 'service' ? '<span class="tag tag-ok">' + esc(svcMap[s.id] || '指定服务') + '</span>' : '<span class="muted">全量</span>'}</td>
          <td>${esc(s.env || '-')}</td>
          <td class="num">${esc(s.version || '-')}</td>
          <td class="num muted">${s.next_run_at ? fmtTime(s.next_run_at) : (s.type === 'cron' ? esc(s.cron_expr) : '-')}</td>
          <td>${s.enabled ? '<span class="tag tag-ok">启用</span>' : '<span class="tag">停用</span>'}</td>
          <td>
            <button class="btn ghost sm" data-toggle="${s.id}">${s.enabled ? '停用' : '启用'}</button>
            <button class="btn ghost sm" data-run="${s.id}">执行</button>
            <button class="btn danger ghost sm" data-del="${s.id}">删除</button>
          </td>
        </tr>`).join('')}</tbody></table></div></div>`
      : `<div class="panel">${emptyState('📅', '暂无定时计划')}</div>`;
    box.querySelectorAll('[data-toggle]').forEach(btn => btn.onclick = async () => {
      await api(`/api/deploy/schedules/${btn.dataset.toggle}/toggle`, {method:'POST'});
      await loadData(); renderTable();
    });
    box.querySelectorAll('[data-run]').forEach(btn => btn.onclick = async () => {
      if (!await confirmDialog('确认立即执行此计划？')) return;
      try {
        const r = await api(`/api/deploy/schedules/${btn.dataset.run}/run`, {method:'POST'});
        toastSuccess(`任务 #${r.id} 已创建`);
      } catch(e) { toastError(e.message); }
    });
    box.querySelectorAll('[data-del]').forEach(btn => btn.onclick = async () => {
      if (!await confirmDialog('确认删除此计划？')) return;
      await api(`/api/deploy/schedules/${btn.dataset.del}`, {method:'DELETE'});
      await loadData(); renderTable();
      toastSuccess('已删除');
    });
  }

  async function loadEnvsForProj(projId){
    if (!projId) return [];
    return await api(`/api/projects/${projId}/environments`).catch(() => []);
  }

  async function openCreateForm(){
    const defaultProjId = projects.length ? projects[0].id : null;
    const initEnvs = defaultProjId ? await loadEnvsForProj(defaultProjId) : [];
    const initSvcs = defaultProjId ? await loadServicesForProj(defaultProjId) : [];
    openDrawer({ title: '新建定时计划', body: `
      <div class="form-grid">
        <div class="field full"><label>名称</label><input id="schedName" placeholder="如：每日凌晨发布"></div>
        <div class="field"><label>类型</label>
          <select id="schedType">${[['once','一次性'],['cron','周期性']].map(o=>`<option value="${o[0]}">${o[1]}</option>`).join('')}</select></div>
        <div class="field"><label>项目</label>
          <select id="schedProj">${projects.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select></div>
        <div class="field"><label>服务</label>
          <select id="schedSvc">
            <option value="">全量发布</option>
            ${initSvcs.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join('')}
          </select></div>
        <div class="field"><label>环境</label>
          <select id="schedEnv">${initEnvs.map(e => `<option value="${esc(e.name)}">${esc(e.name)}</option>`).join('')}</select></div>
        <div class="field"><label>版本</label><input id="schedVer" placeholder="1.0.0"></div>
        <div class="field full" id="schedTimeWrap"><label>执行时间</label>
          <input id="schedAt" type="datetime-local"></div>
        <div class="field full" id="schedCronWrap" style="display:none"><label>CRON 表达式</label>
          <input id="schedCron" placeholder="0 2 * * *（每天凌晨2点）"></div>
      </div>
      <div style="display:flex;gap:10px;margin-top:20px"><button class="btn primary flex-1" id="schedSave">保存</button></div>`});
    // 项目切换时刷新环境和服务下拉
    document.getElementById('schedProj').onchange = async (e) => {
      const projId = Number(e.target.value);
      const [envs, svcs] = await Promise.all([loadEnvsForProj(projId), loadServicesForProj(projId)]);
      document.getElementById('schedEnv').innerHTML = envs.map(e => `<option value="${esc(e.name)}">${esc(e.name)}</option>`).join('');
      document.getElementById('schedSvc').innerHTML = '<option value="">全量发布</option>' + svcs.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join('');
    };
    document.getElementById('schedType').onchange = (e) => {
      document.getElementById('schedTimeWrap').style.display = e.target.value === 'once' ? '' : 'none';
      document.getElementById('schedCronWrap').style.display = e.target.value === 'cron' ? '' : 'none';
    };
    document.getElementById('schedSave').onclick = async () => {
      const type = document.getElementById('schedType').value;
      const svcVal = document.getElementById('schedSvc').value;
      const body = {
        project_id: Number(document.getElementById('schedProj').value),
        type, name: document.getElementById('schedName').value,
        env: document.getElementById('schedEnv').value,
        version: document.getElementById('schedVer').value,
      };
      // 指定服务时传 service_id
      if (svcVal) body.service_id = Number(svcVal);
      if (type === 'once') {
        const dt = document.getElementById('schedAt').value;
        if (!dt) { toastError('请选择执行时间'); return; }
        body.schedule_at = Math.floor(new Date(dt).getTime() / 1000);
      } else {
        body.cron_expr = document.getElementById('schedCron').value;
        if (!body.cron_expr) { toastError('请输入 CRON 表达式'); return; }
      }
      try {
        await api('/api/deploy/schedules', {method:'POST', body:JSON.stringify(body)});
        toastSuccess('计划已创建');
        document.getElementById('dwClose').click();
        await loadData(); renderTable();
      } catch(e) { toastError(e.message); }
    };
  }

  window.deploy_schedules = {
    render: async function(root){
      destroyed = false;
      await loadData();
      if (destroyed) return [];
      root.innerHTML = `
        <div class="deploy-toolbar">
          <span style="flex:1"></span>
          ${canDeploy() ? '<button class="btn primary" id="newSchedBtn">+ 新建计划</button>' : ''}
        </div>
        <div id="schedBox"></div>`;
      const nb = document.getElementById('newSchedBtn');
      if (nb) nb.onclick = openCreateForm;
      renderTable();
      timer = setInterval(async () => { await loadData(); if (!destroyed) renderTable(); }, 10000);
      return [timer];
    },
    destroy: function(){ destroyed = true; if (timer) clearInterval(timer); timer = null; }
  };
})();
