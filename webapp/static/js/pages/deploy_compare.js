// pages/deploy_compare.js — 发布版本对比
(function(){
  let destroyed = false;
  let projects = [];
  let selectedProject = null;
  let snapshots = [];

  async function loadProjects(){
    projects = await api('/api/projects').catch(() => []);
    if (projects.length && !selectedProject) selectedProject = projects[0].id;
  }

  async function loadSnapshots(){
    if (!selectedProject) return;
    snapshots = await api(`/api/deploy/snapshots/${selectedProject}`).catch(() => []);
  }

  async function doCompare(){
    if (snapshots.length < 2) return;
    const data = await api(`/api/deploy/compare?project_id=${selectedProject}`).catch(() => null);
    if (!data) return;
    const root = document.getElementById('pageRoot');
    const box = root.querySelector('#compareBox');
    if (!box) return;
    const changeTag = (c) => {
      if (c === 'upgraded') return '<span class="tag-upgrade">\u2191 升级</span>';
      if (c === 'downgraded') return '<span class="tag-downgrade">\u2193 回滚</span>';
      return '<span class="tag-unchanged">= 未变</span>';
    };
    box.innerHTML = `
      <div class="panel" style="padding:16px">
        <div style="display:flex;gap:24px;margin-bottom:16px">
          <div><span class="dg-label">旧版本</span> <strong>${esc(data.old.version)}</strong>
            <span class="muted"> \u00b7 ${fmtTime(data.old.deployed_at)}</span></div>
          <div><span class="dg-label">新版本</span> <strong>${esc(data.new.version)}</strong>
            <span class="muted"> \u00b7 ${fmtTime(data.new.deployed_at)}</span></div>
        </div>
        <div class="tbl-wrap"><table class="tbl compare-tbl">
          <thead><tr><th>服务</th><th>旧版本</th><th>新版本</th><th>变化</th></tr></thead>
          <tbody>${data.diff.map(d => `<tr class="${d.change !== 'unchanged' ? 'changed' : ''}">
            <td>${esc(d.service)}</td>
            <td class="num">${esc(d.old || '-')}</td>
            <td class="num">${esc(d.new || '-')}</td>
            <td>${changeTag(d.change)}</td>
          </tr>`).join('')}</tbody>
        </table></div>
        <div style="margin-top:12px;display:flex;gap:16px">
          <span>${data.nacos_changed ? '\u26a0\ufe0f Nacos 配置已变更' : '\u2713 Nacos 配置未变'}</span>
          <span>${data.env_changed ? '\u26a0\ufe0f 环境变量已变更' : '\u2713 环境变量未变'}</span>
        </div>
      </div>`;
  }

  window.deploy_compare = {
    render: async function(root){
      destroyed = false;
      await loadProjects();
      await loadSnapshots();
      if (destroyed) return [];
      root.innerHTML = `
        <div class="deploy-toolbar">
          <select class="filter-sel" id="cmpProjSel">${projects.map(p =>
            `<option value="${p.id}" ${p.id === selectedProject ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select>
          <span style="flex:1"></span>
          <button class="btn primary" id="cmpBtn" ${snapshots.length < 2 ? 'disabled' : ''}>对比最近两次发布</button>
        </div>
        <div id="compareBox">
          ${snapshots.length < 2 ? `<div class="panel">${emptyState('📊', '至少需要 2 条发布记录')}</div>` : ''}
        </div>`;
      document.getElementById('cmpProjSel').onchange = async (e) => {
        selectedProject = Number(e.target.value);
        await loadSnapshots();
        const btn = document.getElementById('cmpBtn');
        if (btn) btn.disabled = snapshots.length < 2;
        if (snapshots.length >= 2) await doCompare();
      };
      const btn = document.getElementById('cmpBtn');
      if (btn) btn.onclick = doCompare;
      if (snapshots.length >= 2) await doCompare();
      return [];
    },
    destroy: function(){ destroyed = true; }
  };
})();
