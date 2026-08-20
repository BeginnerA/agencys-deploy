// pages/deploy_canary.js — 灰度策略配置
(function(){
  let destroyed = false;
  let configs = [];
  let projects = [];

  async function loadData(){
    projects = await api('/api/projects').catch(() => []);
    configs = await api('/api/deploy/canary-configs').catch(() => []);
  }

  function renderSteps(steps){
    if (!Array.isArray(steps)) return '-';
    return steps.map((s, i) => `
      ${i > 0 ? '<span class="canary-arrow">\u2192</span>' : ''}
      <div class="canary-step">
        <div class="step-num">${s.servers === 'all' ? '全量' : s.servers + '台'}</div>
        <div class="step-label">观察 ${s.observe_minutes}min</div>
        <div class="step-label">${s.auto_advance ? '自动推进' : '人工确认'}</div>
      </div>`).join('');
  }

  function renderList(){
    const root = document.getElementById('pageRoot');
    const box = root.querySelector('#canaryBox');
    if (!box) return;
    const projMap = {};
    projects.forEach(p => projMap[p.id] = p.name);
    box.innerHTML = configs.length ? configs.map(c => `
      <div class="panel" style="padding:16px;margin-bottom:12px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
          <div><strong>${esc(c.name)}</strong> <span class="muted">\u00b7 ${esc(projMap[c.project_id] || '全局')}</span>
          ${c.is_default ? ' <span class="tag tag-ok">默认</span>' : ''}</div>
          <button class="btn danger ghost sm" data-del="${c.id}">删除</button>
        </div>
        <div class="canary-steps">${renderSteps(c.steps)}</div>
      </div>`).join('')
      : `<div class="panel">${emptyState('🐤', '暂无灰度策略')}</div>`;
    box.querySelectorAll('[data-del]').forEach(btn => btn.onclick = async () => {
      if (!await confirmDialog('确认删除此策略？')) return;
      await api(`/api/deploy/canary-configs/${btn.dataset.del}`, {method:'DELETE'});
      await loadData(); renderList();
    });
  }

  function openCreateForm(){
    openDrawer({ title: '新建灰度策略', wide: true, body: `
      <div class="form-grid">
        <div class="field"><label>策略名称</label><input id="canaryName" placeholder="如：标准三批"></div>
        <div class="field"><label>所属项目</label>
          <select id="canaryProj">${projects.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select></div>
        <div class="field full"><label>批次配置 (JSON)</label>
          <textarea id="canarySteps" rows="6" style="font-family:var(--font-num)">[
  {"batch":1, "servers":1, "observe_minutes":5, "auto_advance":true},
  {"batch":2, "servers":2, "observe_minutes":10, "auto_advance":true},
  {"batch":3, "servers":"all", "observe_minutes":0, "auto_advance":false}
]</textarea></div>
        <div class="field full check"><label><input type="checkbox" id="canaryDefault"> 设为默认策略</label></div>
      </div>
      <div style="display:flex;gap:10px;margin-top:20px"><button class="btn primary flex-1" id="canarySave">保存</button></div>`});
    document.getElementById('canarySave').onclick = async () => {
      let steps;
      try { steps = JSON.parse(document.getElementById('canarySteps').value); }
      catch(e) { toastError('JSON 格式错误'); return; }
      try {
        await api('/api/deploy/canary-configs', {method:'POST', body:JSON.stringify({
          project_id: Number(document.getElementById('canaryProj').value),
          name: document.getElementById('canaryName').value,
          steps, is_default: document.getElementById('canaryDefault').checked
        })});
        toastSuccess('策略已创建');
        document.getElementById('dwClose').click();
        await loadData(); renderList();
      } catch(e) { toastError(e.message); }
    };
  }

  window.deploy_canary = {
    render: async function(root){
      destroyed = false;
      await loadData();
      if (destroyed) return [];
      root.innerHTML = `
        <div class="deploy-toolbar">
          <span style="flex:1"></span>
          ${canDeploy() ? '<button class="btn primary" id="newCanaryBtn">+ 新建策略</button>' : ''}
        </div>
        <div id="canaryBox"></div>`;
      const nb = document.getElementById('newCanaryBtn');
      if (nb) nb.onclick = openCreateForm;
      renderList();
      return [];
    },
    destroy: function(){ destroyed = true; }
  };
})();
