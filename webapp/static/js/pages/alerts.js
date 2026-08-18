// pages/alerts.js — 告警中心（聚合 + 项目/环境筛选 + 标记已处理 + 导航角标）
(function(){
  let timer = null;
  let alerts = [];
  let levelFilter = 'all';
  let showFilter = 'unacked';   // unacked / acked / all
  let projFilter = 'all';       // all / 项目 id
  let envFilter = 'all';        // all / 环境名（仅选中项目时生效）
  let projects = [];
  let envs = [];
  let destroyed = false;        // 路由切换后丢弃 in-flight 轮询响应，防覆盖新页面

  async function ensureProjects(){
    if (projects.length) return;
    projects = await api('/api/projects').catch(() => []);
  }
  async function loadEnvs(){
    envs = [];
    if (projFilter !== 'all'){
      const p = projects.find(x => x.id === Number(projFilter));
      if (p) envs = await api(`/api/projects/${p.id}/environments`).catch(() => []);
      else projFilter = 'all';
    }
    if (envFilter !== 'all' && !envs.some(e => e.name === envFilter)) envFilter = 'all';
  }
  async function loadAlerts(){
    const q = new URLSearchParams({limit:'500'});
    if (projFilter !== 'all') q.set('project_id', projFilter);
    const [tasks, overview] = await Promise.all([
      api('/api/tasks?' + q).catch(() => []),
      api('/api/dashboard/overview').catch(() => null),
    ]);
    // 环境筛选：任务告警按 env 过滤
    if (envFilter !== 'all') tasks = tasks.filter(t => t.env === envFilter);
    // 监控数据：选中项目（全部时为所有项目默认环境）；环境筛选下仅取含该环境的项目
    const projs = projFilter === 'all' ? projects : projects.filter(p => p.id === Number(projFilter));
    const monByProject = {};
    for (const p of projs){
      try{
        const pEnvs = await api(`/api/projects/${p.id}/environments`).catch(() => []);
        let names;
        if (envFilter !== 'all'){
          if (!pEnvs.some(e => e.name === envFilter)) continue; // 无此环境 → 不产生服务告警
          names = [envFilter];
        } else if (projFilter !== 'all'){
          names = pEnvs.map(e => e.name); // 选中项目：聚合全部环境
        } else {
          const env = pEnvs.find(e => e.is_default) || pEnvs[0];
          if (!env) continue;
          names = [env.name];
        }
        const merged = {services: {}};
        for (const name of names){
          const mon = await api(`/api/monitor/overview?project_id=${p.id}&env=${name}`);
          if (mon && mon.services) Object.assign(merged.services, mon.services);
        }
        if (Object.keys(merged.services).length) monByProject[p.name] = merged;
      }catch(e){ /* 单项目监控失败跳过 */ }
    }
    alerts = computeAlerts(tasks, overview, monByProject);
    if (destroyed) return; // 页面已销毁（路由切换），丢弃过期响应
    render();
  }
  function render(){
    const root = document.getElementById('pageRoot');
    const unacked = alerts.filter(a => a.status === 'unacked').length;
    const acked = alerts.filter(a => a.status === 'acked').length;
    const filtered = alerts.filter(a =>
      (levelFilter === 'all' || a.level === levelFilter) &&
      (showFilter === 'all' || a.status === showFilter));
    root.innerHTML = `
      <div class="alert-stats">
        <div class="alert-stat unacked"><div class="as-num">${unacked}</div><div class="as-label">未处理</div></div>
        <div class="alert-stat"><div class="as-num">${acked}</div><div class="as-label">已处理</div></div>
        <div class="alert-stat"><div class="as-num">${alerts.length}</div><div class="as-label">全部</div></div>
      </div>
      <div class="deploy-toolbar">
        <span class="weak" style="font-size:12px">项目</span>
        <select class="filter-sel" id="alProjSel"></select>
        <span class="weak" style="font-size:12px">环境</span>
        <select class="filter-sel" id="alEnvSel"></select>
        <span style="width:1px;height:20px;background:var(--border);margin:0 4px"></span>
        <span class="weak" style="font-size:12px">级别</span>
        ${[['all','全部'],['critical','严重'],['warning','警告']].map(([k, v]) =>
          `<span class="chip ${k === levelFilter ? 'active' : ''}" data-lv="${k}">${v}</span>`).join('')}
        <span style="width:1px;height:20px;background:var(--border);margin:0 4px"></span>
        <span class="weak" style="font-size:12px">状态</span>
        ${[['unacked','未处理'],['acked','已处理'],['all','全部']].map(([k, v]) =>
          `<span class="chip ${k === showFilter ? 'active' : ''}" data-sh="${k}">${v}</span>`).join('')}
        <span style="flex:1"></span>
      </div>
      <div id="alertList">
        ${filtered.length ? filtered.map(a => `
          <div class="alert-row ${a.status}" data-key="${esc(a.key)}">
            <span class="badge ${a.level === 'critical' ? 'critical' : 'warning'}">${a.level === 'critical' ? '严重' : '警告'}</span>
            <div class="ar-main">
              <div class="ar-title">${esc(a.title)}</div>
              <div class="ar-detail">${esc(a.detail)}</div>
            </div>
            <span class="ar-time">${fmtTime(a.ts)}</span>
            ${a.status === 'unacked'
              ? '<button class="btn sm primary" data-ack="1">标记已处理</button>'
              : '<button class="btn sm ghost" data-ack="0">恢复未处理</button>'}
          </div>`).join('')
          : `<div class="panel">${emptyState('🎉', '暂无告警，一切正常')}</div>`}
      </div>`;
    const projSel = document.getElementById('alProjSel');
    if (projSel){
      projSel.innerHTML = '<option value="all">全部项目</option>' + projects.map(p =>
        `<option value="${p.id}" ${p.id === Number(projFilter) ? 'selected' : ''}>${esc(p.name)}</option>`).join('');
      projSel.onchange = async () => {
        projFilter = projSel.value; envFilter = 'all';
        await loadEnvs();
        loadAlerts();
      };
    }
    const envSel = document.getElementById('alEnvSel');
    if (envSel){
      if (projFilter === 'all'){
        envSel.disabled = true;
        envSel.innerHTML = '<option value="all">全部环境</option>';
      }else{
        envSel.disabled = false;
        envSel.innerHTML = '<option value="all">全部环境</option>' + envs.map(e =>
          `<option value="${esc(e.name)}" ${e.name === envFilter ? 'selected' : ''}>${esc(e.name)}</option>`).join('');
        envSel.onchange = () => { envFilter = envSel.value; loadAlerts(); };
      }
    }
    root.querySelectorAll('.chip[data-lv]').forEach(c => c.onclick = () => {
      levelFilter = c.dataset.lv;
      document.querySelectorAll('.chip[data-lv]').forEach(x => x.classList.toggle('active', x === c));
      render();
    });
    root.querySelectorAll('.chip[data-sh]').forEach(c => c.onclick = () => {
      showFilter = c.dataset.sh;
      document.querySelectorAll('.chip[data-sh]').forEach(x => x.classList.toggle('active', x === c));
      render();
    });
    root.querySelectorAll('[data-ack]').forEach(btn => btn.onclick = () => {
      const key = btn.closest('.alert-row').dataset.key;
      if (btn.dataset.ack === '1') ackAlert(key); else unackAlert(key);
      toastSuccess(btn.dataset.ack === '1' ? '已标记处理' : '已恢复未处理');
      loadAlerts();
    });
    updateAlertBadge(unacked);
  }

  // ---- 模块导出 ----
  window.alerts = {
    render: async function(root){
      destroyed = false;
      root.innerHTML = skeletonHTML(3);
      await ensureProjects();
      await loadEnvs();
      render();
      await loadAlerts();
      if (destroyed) return []; // destroy 后在途续体不再创建轮询
      timer = setInterval(loadAlerts, 15000);
      return [timer];
    },
    destroy: function(){
      destroyed = true;
      if (timer) clearInterval(timer);
      timer = null;
    },
  };
})();
