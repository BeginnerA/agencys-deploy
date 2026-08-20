// pages/deploy_compare.js — 发布版本对比（环境筛选 + 快照任意选择）
(function(){
  let destroyed = false;
  let projects = [];
  let selectedProject = null;
  let envs = [];
  let selectedEnv = '';
  let allSnapshots = [];   // 当前项目所有环境的快照（用于快照选择面板）
  let panelOpen = false;    // 快照选择面板是否展开

  async function loadProjects(){
    projects = await api('/api/projects').catch(() => []);
    if (projects.length && !selectedProject) selectedProject = projects[0].id;
  }

  async function loadEnvs(){
    envs = [];
    if (!selectedProject) return;
    envs = await api(`/api/projects/${selectedProject}/environments`).catch(() => []);
    if (selectedEnv && !envs.some(e => e.name === selectedEnv)) selectedEnv = '';
    if (!selectedEnv && envs.length) selectedEnv = envs[0].name;
  }

  async function loadAllSnapshots(){
    if (!selectedProject) return;
    allSnapshots = await api(`/api/deploy/snapshots/${selectedProject}`).catch(() => []);
  }

  async function doCompareDefault(){
    if (!selectedProject || !selectedEnv) return;
    const box = document.getElementById('compareBox');
    if (!box) return;
    box.innerHTML = skeletonHTML(2);
    try {
      const data = await api(`/api/deploy/compare?project_id=${selectedProject}&env=${encodeURIComponent(selectedEnv)}`);
      renderCompareResult(box, data);
    } catch(e) {
      box.innerHTML = `<div class="panel">${emptyState('📊', e.message || '对比失败')}</div>`;
    }
  }

  async function doCompareManual(oldId, newId){
    const box = document.getElementById('compareBox');
    if (!box) return;
    box.innerHTML = skeletonHTML(2);
    try {
      const data = await api(`/api/deploy/compare?old_id=${oldId}&new_id=${newId}`);
      renderCompareResult(box, data);
    } catch(e) {
      box.innerHTML = `<div class="panel">${emptyState('📊', e.message || '对比失败')}</div>`;
    }
  }

  function renderCompareResult(box, data){
    const changeTag = (c) => {
      if (c === 'upgraded') return '<span class="tag-upgrade">↑ 升级</span>';
      if (c === 'downgraded') return '<span class="tag-downgrade">↓ 回滚</span>';
      return '<span class="tag-unchanged">= 未变</span>';
    };
    const diff = data.changes || [];
    const allServices = new Set();
    if (data.old && data.old.image_versions) Object.keys(data.old.image_versions).forEach(k => allServices.add(k));
    if (data.new && data.new.image_versions) Object.keys(data.new.image_versions).forEach(k => allServices.add(k));
    const rows = [];
    allServices.forEach(svc => {
      const d = diff.find(x => x.service === svc);
      const oldV = (data.old && data.old.image_versions && data.old.image_versions[svc]) || '-';
      const newV = (data.new && data.new.image_versions && data.new.image_versions[svc]) || '-';
      const change = d ? (oldV === '-' ? 'upgraded' : 'downgraded') : 'unchanged';
      rows.push({service: svc, old: oldV, new: newV, change});
    });
    box.innerHTML = `
      <div class="panel" style="padding:16px">
        <div style="display:flex;gap:24px;margin-bottom:16px;flex-wrap:wrap">
          <div><span class="dg-label">旧版本</span> <strong>${esc(data.old.version)}</strong>
            <span class="muted"> · ${esc(data.old.env || '')} · ${fmtTime(data.old.deployed_at)}</span></div>
          <div><span class="dg-label">新版本</span> <strong>${esc(data.new.version)}</strong>
            <span class="muted"> · ${esc(data.new.env || '')} · ${fmtTime(data.new.deployed_at)}</span></div>
        </div>
        <div class="tbl-wrap"><table class="tbl compare-tbl">
          <thead><tr><th>服务</th><th>旧版本</th><th>新版本</th><th>变化</th></tr></thead>
          <tbody>${rows.map(d => `<tr class="${d.change !== 'unchanged' ? 'changed' : ''}">
            <td>${esc(d.service)}</td>
            <td class="num">${esc(d.old)}</td>
            <td class="num">${esc(d.new)}</td>
            <td>${changeTag(d.change)}</td>
          </tr>`).join('')}</tbody>
        </table></div>
      </div>`;
  }

  function renderSnapshotPanel(){
    const panel = document.getElementById('snapshotPanel');
    if (!panel) return;
    if (!panelOpen) { panel.classList.add('hidden'); return; }
    panel.classList.remove('hidden');
    // 按环境分组快照
    const envGroups = {};
    allSnapshots.forEach(s => {
      const env = s.env || 'unknown';
      if (!envGroups[env]) envGroups[env] = [];
      envGroups[env].push(s);
    });
    const renderCol = (colId, title) => {
      const envKeys = Object.keys(envGroups).sort();
      if (!envKeys.length) return `<div class="snap-col"><h4>${title}</h4><p class="muted">暂无快照</p></div>`;
      return `<div class="snap-col"><h4>${title}</h4>
        ${envKeys.map(env => `
          <div class="snap-env-group">
            <div class="snap-env-label">${esc(env)}</div>
            ${envGroups[env].map(s => `
              <label class="snap-radio">
                <input type="radio" name="${colId}" value="${s.id}" data-env="${esc(env)}">
                <span class="snap-info">
                  <strong>${esc(s.version || '未知版本')}</strong>
                  <span class="muted">${fmtTime(s.deployed_at)}</span>
                </span>
              </label>`).join('')}
          </div>`).join('')}
      </div>`;
    };
    panel.innerHTML = `
      <div class="snap-panel">
        ${renderCol('snapOld', '旧版本')}
        <div style="display:flex;align-items:center;font-size:18px;color:var(--text-2)">→</div>
        ${renderCol('snapNew', '新版本')}
      </div>
      <div style="display:flex;justify-content:flex-end;margin-top:12px">
        <button class="btn primary" id="snapCompareBtn" disabled>开始对比</button>
      </div>`;
    // 绑定 radio 变更 → 启用/禁用对比按钮
    panel.querySelectorAll('input[type=radio]').forEach(r => {
      r.onchange = () => {
        const oldSel = panel.querySelector('input[name=snapOld]:checked');
        const newSel = panel.querySelector('input[name=snapNew]:checked');
        const btn = document.getElementById('snapCompareBtn');
        if (btn) {
          const sameId = oldSel && newSel && oldSel.value === newSel.value;
          btn.disabled = !oldSel || !newSel || sameId;
          if (sameId) toast('请选择两条不同的快照', 'error');
        }
      };
    });
    const cmpBtn = document.getElementById('snapCompareBtn');
    if (cmpBtn) cmpBtn.onclick = () => {
      const oldSel = panel.querySelector('input[name=snapOld]:checked');
      const newSel = panel.querySelector('input[name=snapNew]:checked');
      if (oldSel && newSel) doCompareManual(Number(oldSel.value), Number(newSel.value));
    };
  }

  window.deploy_compare = {
    render: async function(root){
      destroyed = false;
      panelOpen = false;
      await loadProjects();
      await loadEnvs();
      await loadAllSnapshots();
      if (destroyed) return [];
      root.innerHTML = `
        <div class="deploy-toolbar">
          <span class="weak" style="font-size:12px">项目</span>
          <select class="filter-sel" id="cmpProjSel">${projects.map(p =>
            `<option value="${p.id}" ${p.id === selectedProject ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select>
          <span class="weak" style="font-size:12px">环境</span>
          <select class="filter-sel" id="cmpEnvSel">${envs.map(e =>
            `<option value="${esc(e.name)}" ${e.name === selectedEnv ? 'selected' : ''}>${esc(e.name)}</option>`).join('')}</select>
          <span style="flex:1"></span>
          <button class="btn ghost" id="togglePanelBtn">📋 选择快照对比</button>
        </div>
        <div id="snapshotPanel" class="hidden"></div>
        <div id="compareBox">
          ${envs.length === 0 ? `<div class="panel">${emptyState('📊', '该项目暂无环境')}</div>`
            : !selectedEnv ? `<div class="panel">${emptyState('📊', '请选择环境')}</div>`
            : skeletonHTML(2)}
        </div>`;
      // 项目切换
      document.getElementById('cmpProjSel').onchange = async (e) => {
        selectedProject = Number(e.target.value);
        selectedEnv = '';
        await loadEnvs();
        await loadAllSnapshots();
        if (destroyed) return [];
        // 刷新快照面板（面板打开时重绘，关闭时仅保持 hidden）
        renderSnapshotPanel();
        // 重建环境选项
        const envSel = document.getElementById('cmpEnvSel');
        if (envSel) {
          envSel.innerHTML = envs.map(e =>
            `<option value="${esc(e.name)}" ${e.name === selectedEnv ? 'selected' : ''}>${esc(e.name)}</option>`).join('');
          enhanceSelect(envSel);
        }
        if (selectedEnv) await doCompareDefault();
        else {
          const box = document.getElementById('compareBox');
          if (box) box.innerHTML = `<div class="panel">${emptyState('📊', '请选择环境')}</div>`;
        }
      };
      // 环境切换
      const envSel = document.getElementById('cmpEnvSel');
      if (envSel) {
        envSel.onchange = async (e) => {
          selectedEnv = e.target.value;
          await doCompareDefault();
        };
      }
      // 快照选择面板切换
      const toggleBtn = document.getElementById('togglePanelBtn');
      if (toggleBtn) toggleBtn.onclick = () => {
        panelOpen = !panelOpen;
        renderSnapshotPanel();
      };
      // 自动对比
      if (selectedEnv) await doCompareDefault();
      return [];
    },
    destroy: function(){ destroyed = true; }
  };
})();
