// pages/deploy_windows.js — 发布窗口/封网期管理
(function(){
  let destroyed = false;
  let windows = [];
  // ---- 作用域选择状态 ----
  let scopeSelectedProjects = new Set();
  let scopeSelectedEnvs = new Set();
  let scopeSelectedTargets = new Set();
  let scopeNameCache = null;
  const SCOPE_TYPES = [['all','全部'],['project','项目'],['env','环境'],['server','服务器'],['service','服务']];

  async function loadWindows(){
    windows = await api('/api/deploy/windows').catch(() => []);
  }

  // ---- 作用域辅助函数 ----

  function parseScope(raw){
    if (!raw) return {type:'all', ids:[], env_ids:[]};
    if (typeof raw === 'string') {
      try { raw = JSON.parse(raw); } catch(e) { return {type:'all', ids:[], env_ids:[]}; }
    }
    return { type: raw.type || 'all', ids: raw.ids || [], env_ids: raw.env_ids || [] };
  }

  async function ensureScopeNames() {
    if (scopeNameCache) return scopeNameCache;
    try {
      const envs = await api('/api/alerts/scope/environments');
      const envIds = envs.map(e => e.id).join(',');
      const [servers, services] = await Promise.all([
        envIds ? api(`/api/alerts/scope/servers?env_ids=${envIds}`) : [],
        envIds ? api(`/api/alerts/scope/services?env_ids=${envIds}`) : []
      ]);
      const projects = {};
      envs.forEach(e => { projects[e.project_id] = e.project_name; });
      scopeNameCache = {
        projects,
        envs: Object.fromEntries(envs.map(e => [e.id, e.name])),
        servers: Object.fromEntries(servers.map(s => [s.id, s.host])),
        services: Object.fromEntries(services.map(s => [s.id, s.name]))
      };
    } catch(e) {
      scopeNameCache = {projects: {}, envs: {}, servers: {}, services: {}};
    }
    return scopeNameCache;
  }

  function scopeSummary(scopeRaw, names) {
    const s = parseScope(scopeRaw);
    const typeLabel = {all:'全部',project:'项目',env:'环境',server:'服务器',service:'服务'};
    const label = typeLabel[s.type] || s.type;
    if (s.type === 'all' || !s.ids || !s.ids.length) return label;
    const nameMap = (s.type === 'server') ? names.servers
      : (s.type === 'service') ? names.services
      : (s.type === 'project') ? names.projects
      : (s.type === 'env') ? names.envs
      : null;
    const resolved = (s.ids || []).map(id => nameMap?.[id] || `#${id}`);
    return `${label}(${s.ids.length}): ${resolved.slice(0, 3).join(', ')}${resolved.length > 3 ? '…' : ''}`;
  }

  function renderCalendar(){
    const now = new Date();
    const year = now.getFullYear(), month = now.getMonth();
    const firstDay = new Date(year, month, 1).getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const today = now.getDate();
    let cells = '';
    ['日','一','二','三','四','五','六'].forEach(d =>
      cells += `<div style="text-align:center;font-size:11px;color:var(--text-2);padding:4px">${d}</div>`);
    for (let i = 0; i < firstDay; i++)
      cells += '<div class="cal-cell" style="visibility:hidden"></div>';
    for (let d = 1; d <= daysInMonth; d++) {
      const dayStart = new Date(year, month, d).getTime() / 1000;
      const dayEnd = dayStart + 86400;
      let cls = 'cal-cell' + (d === today ? ' today' : '');
      let titles = [];
      let hasBlock = false, hasWarn = false;
      for (const w of windows) {
        let ws = w.start_at, we = w.end_at;
        if (w.repeat_yearly) {
          const wy = new Date(ws * 1000).getFullYear();
          const offset = (year - wy) * 365.25 * 86400;
          ws += offset; we += offset;
        }
        if (ws < dayEnd && we > dayStart) {
          titles.push(w.name);
          if (w.block_level === 'block') hasBlock = true; else hasWarn = true;
        }
      }
      if (hasBlock) cls += ' cal-block';
      else if (hasWarn) cls += ' cal-warn';
      const titleAttr = titles.join('、');
      const titleHtml = titles.length
        ? titles.map(t => `<span class="cal-title">${esc(t)}</span>`).join('')
        : '';
      cells += `<div class="${cls}" title="${esc(titleAttr)}"><span class="cal-day">${d}</span><div class="cal-titles">${titleHtml}</div></div>`;
    }
    return cells;
  }

  async function renderList(){
    const root = document.getElementById('pageRoot');
    const box = root.querySelector('#winBox');
    if (!box) return;
    const names = await ensureScopeNames();
    box.innerHTML = `
      <div class="panel" style="padding:16px">
        <div style="font-weight:600;margin-bottom:8px">${new Date().toLocaleDateString('zh-CN', {year:'numeric',month:'long'})}</div>
        <div class="cal-grid">${renderCalendar()}</div>
      </div>
      <div class="panel" style="padding:0;margin-top:12px"><div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>#</th><th>名称</th><th>作用域</th><th>开始</th><th>结束</th><th>级别</th><th>每年重复</th><th>操作</th></tr></thead>
        <tbody>${windows.map(w => `<tr>
          <td class="num">${w.id}</td>
          <td>${esc(w.name)}</td>
          <td><span class="rule-scope-summary" title="${esc(scopeSummary(w.scope, names))}">${esc(scopeSummary(w.scope, names))}</span></td>
          <td class="num muted">${fmtTime(w.start_at)}</td>
          <td class="num muted">${fmtTime(w.end_at)}</td>
          <td>${w.block_level === 'block' ? '<span class="tag tag-warn">硬拦截</span>' : '<span class="tag">软警告</span>'}</td>
          <td>${w.repeat_yearly ? '✓' : '-'}</td>
          <td>
            <button class="btn ghost sm" data-edit="${w.id}">编辑</button>
            <button class="btn danger ghost sm" data-del="${w.id}">删除</button>
          </td>
        </tr>`).join('')}</tbody></table></div></div>`;
    box.querySelectorAll('[data-del]').forEach(btn => btn.onclick = async () => {
      if (!await confirmDialog('确认删除此窗口？')) return;
      await api(`/api/deploy/windows/${btn.dataset.del}`, {method:'DELETE'});
      await loadWindows(); renderList();
      toastSuccess('已删除');
    });
    box.querySelectorAll('[data-edit]').forEach(btn => btn.onclick = () => {
      const w = windows.find(x => x.id === Number(btn.dataset.edit));
      if (w) openForm(w);
    });
  }

  function openForm(win){
    const editing = !!win;
    const title = editing ? '编辑发布窗口' : '新建发布窗口';
    const w = win || {};
    const scope = parseScope(w.scope);
    // 回填已保存的作用域选择数据
    scopeSelectedProjects.clear();
    scopeSelectedEnvs.clear();
    scopeSelectedTargets.clear();
    if (scope.type === 'project') {
      (scope.ids || []).forEach(id => scopeSelectedProjects.add(id));
    } else if (scope.type === 'env') {
      (scope.ids || []).forEach(id => scopeSelectedEnvs.add(id));
    } else if (scope.type === 'server' || scope.type === 'service') {
      (scope.env_ids || []).forEach(id => scopeSelectedEnvs.add(id));
      (scope.ids || []).forEach(id => scopeSelectedTargets.add(id));
    }
    // 时间回填格式
    const startVal = w.start_at ? new Date(w.start_at * 1000).toISOString().slice(0,16) : '';
    const endVal = w.end_at ? new Date(w.end_at * 1000).toISOString().slice(0,16) : '';

    openDrawer({ title, body: `
      <div class="form-grid">
        <div class="field full"><label>名称</label><input id="winName" value="${esc(w.name||'')}" placeholder="如：春节封网"></div>
        <div class="field"><label>开始时间</label><input id="winStart" type="datetime-local" value="${startVal}"></div>
        <div class="field"><label>结束时间</label><input id="winEnd" type="datetime-local" value="${endVal}"></div>
        <div class="field"><label>拦截级别</label>
          <select id="winLevel"><option value="block" ${w.block_level==='block'?'selected':''}>硬拦截（禁止发布）</option><option value="warn" ${(w.block_level||'warn')==='warn'?'selected':''}>软警告（确认后放行）</option></select></div>
        <div class="field"><label>作用域</label>
          <select id="winScopeType"></select></div>
        <div class="field full check"><label><input type="checkbox" id="winRepeat" ${w.repeat_yearly?'checked':''}> 每年重复（节假日适用）</label></div>
        <div class="field full" id="winScopePickWrap" style="display:none">
          <label id="winScopePickLabel">选择目标</label>
          <div id="winScopePickTags" class="scope-tag-box"></div>
          <div id="winScopeTargetWrap" style="margin-top:8px;display:none">
            <label id="winScopeTargetLabel">目标</label>
            <div id="winScopeTargetTags" class="scope-tag-box">
              <div class="muted" style="font-size:12px">请先选择环境</div>
            </div>
          </div>
        </div>
      </div>
      <div style="display:flex;gap:10px;margin-top:20px">
        <button class="btn flex-1" id="winCancel">取消</button>
        <button class="btn primary flex-1" id="winSave">保存</button>
      </div>`});

    // ---- 作用域选择逻辑 ----
    const selScope = document.getElementById('winScopeType');
    const scopePickWrap = document.getElementById('winScopePickWrap');
    const scopePickLabel = document.getElementById('winScopePickLabel');
    const scopePickTags = document.getElementById('winScopePickTags');
    const targetWrap = document.getElementById('winScopeTargetWrap');
    const targetTagsEl = document.getElementById('winScopeTargetTags');
    const targetLabel = document.getElementById('winScopeTargetLabel');
    let scopeEnvs = [];

    selScope.innerHTML = SCOPE_TYPES.map(([k,v]) =>
      `<option value="${k}" ${(scope.type||'all')===k?'selected':''}>${v}</option>`).join('');

    function renderScopePick() {
      const st = selScope.value;
      if (!scopeEnvs.length) {
        scopePickTags.innerHTML = '<div class="muted" style="font-size:12px">加载中...</div>';
        return;
      }
      if (st === 'project') {
        const seen = new Map();
        scopeEnvs.forEach(e => { if (!seen.has(e.project_id)) seen.set(e.project_id, e.project_name); });
        scopePickLabel.textContent = '项目（可多选）';
        scopePickTags.innerHTML = [...seen.entries()].map(([pid, pname]) =>
          `<span class="scope-tag ${scopeSelectedProjects.has(pid)?'active':''}" data-pid="${pid}">${esc(pname)}</span>`
        ).join('');
        scopePickTags.querySelectorAll('.scope-tag[data-pid]').forEach(tag => {
          tag.onclick = () => {
            const pid = Number(tag.dataset.pid);
            if (scopeSelectedProjects.has(pid)) scopeSelectedProjects.delete(pid);
            else scopeSelectedProjects.add(pid);
            tag.classList.toggle('active');
          };
        });
      } else {
        scopePickLabel.textContent = '环境（可多选）';
        scopePickTags.innerHTML = scopeEnvs.map(e => {
          const active = scopeSelectedEnvs.has(e.id) ? 'active' : '';
          return `<span class="scope-tag ${active}" data-eid="${e.id}">${esc(e.project_name)}/${esc(e.name)}</span>`;
        }).join('');
        scopePickTags.querySelectorAll('.scope-tag[data-eid]').forEach(tag => {
          tag.onclick = () => {
            const eid = Number(tag.dataset.eid);
            if (scopeSelectedEnvs.has(eid)) scopeSelectedEnvs.delete(eid);
            else scopeSelectedEnvs.add(eid);
            tag.classList.toggle('active');
            if (st === 'server' || st === 'service') {
              if (scopeSelectedEnvs.size > 0) loadTargets();
              else { targetWrap.style.display='none'; targetTagsEl.innerHTML=''; }
            }
          };
        });
      }
    }

    function updateScopeVis() {
      const st = selScope.value;
      const needCascade = (st === 'server' || st === 'service');
      const needPick = (st === 'project' || st === 'env' || needCascade);
      scopePickWrap.style.display = needPick ? '' : 'none';
      if (!needCascade) targetWrap.style.display = 'none';
      if (needPick) {
        renderScopePick();
        if (needCascade) {
          targetLabel.textContent = st === 'server' ? '服务器' : '服务';
          if (scopeSelectedEnvs.size > 0) loadTargets();
          else targetWrap.style.display = 'none';
        }
      }
    }

    async function loadScopeEnvs() {
      try {
        scopeEnvs = await api('/api/alerts/scope/environments');
        renderScopePick();
        const st = selScope.value;
        if ((st === 'server' || st === 'service') && scopeSelectedEnvs.size > 0) loadTargets();
      } catch(e) {
        scopePickTags.innerHTML = '<div class="muted" style="color:var(--danger);font-size:12px">环境加载失败</div>';
      }
    }

    async function loadTargets() {
      const st = selScope.value;
      const ep = st === 'server' ? 'servers' : 'services';
      const envIds = [...scopeSelectedEnvs].join(',');
      targetWrap.style.display = '';
      targetTagsEl.innerHTML = '<div class="muted" style="font-size:12px">加载中...</div>';
      try {
        const targets = await api(`/api/alerts/scope/${ep}?env_ids=${envIds}`);
        if (!targets.length) {
          targetTagsEl.innerHTML = '<div class="muted" style="font-size:12px">所选环境下无可用目标</div>';
          return;
        }
        targetTagsEl.innerHTML = targets.map(t => {
          const label = st === 'server' ? t.host : t.name;
          const checked = scopeSelectedTargets.has(t.id) ? 'checked' : '';
          return `<label class="scope-target-cb"><input type="checkbox" data-tid="${t.id}" ${checked}> ${esc(label)}</label>`;
        }).join('');
        targetTagsEl.querySelectorAll('input[data-tid]').forEach(cb => {
          cb.onchange = () => {
            const tid = Number(cb.dataset.tid);
            if (cb.checked) scopeSelectedTargets.add(tid);
            else scopeSelectedTargets.delete(tid);
          };
        });
      } catch(e) {
        targetTagsEl.innerHTML = '<div class="muted" style="color:var(--danger);font-size:12px">目标加载失败</div>';
      }
    }

    selScope.onchange = () => {
      scopeSelectedTargets.clear();
      updateScopeVis();
    };
    updateScopeVis();
    loadScopeEnvs();

    document.getElementById('winCancel').onclick = () => {
      const c = document.getElementById('dwClose');
      if (c) c.click();
    };
    document.getElementById('winSave').onclick = async () => {
      const startDt = document.getElementById('winStart').value;
      const endDt = document.getElementById('winEnd').value;
      if (!startDt || !endDt) { toastError('请选择时间范围'); return; }
      const scopeType = selScope.value;
      let scopeIds, scopeEnvIds;
      if (scopeType === 'project') {
        scopeIds = [...scopeSelectedProjects];
        scopeEnvIds = [];
      } else if (scopeType === 'env') {
        scopeIds = [...scopeSelectedEnvs];
        scopeEnvIds = [];
      } else if (scopeType === 'server' || scopeType === 'service') {
        scopeIds = [...scopeSelectedTargets];
        scopeEnvIds = [...scopeSelectedEnvs];
      } else {
        scopeIds = [];
        scopeEnvIds = [];
      }
      const scope = {type: scopeType, ids: scopeIds};
      if (scopeEnvIds.length) scope.env_ids = scopeEnvIds;
      if ((scopeType === 'project' || scopeType === 'env' ||
           scopeType === 'server' || scopeType === 'service') && scopeIds.length === 0) {
        const label = scopeType === 'project' ? '项目'
                    : scopeType === 'env' ? '环境'
                    : scopeType === 'server' ? '服务器' : '服务';
        toastError(`请至少选择一个${label}`); return;
      }
      try {
        const payload = {
          name: document.getElementById('winName').value,
          start_at: Math.floor(new Date(startDt).getTime() / 1000),
          end_at: Math.floor(new Date(endDt).getTime() / 1000),
          block_level: document.getElementById('winLevel').value,
          repeat_yearly: document.getElementById('winRepeat').checked,
          scope
        };
        if (editing) {
          await api(`/api/deploy/windows/${w.id}`, {method:'PUT', body:JSON.stringify(payload)});
          toastSuccess('窗口已更新');
        } else {
          payload.project_id = 0;
          await api('/api/deploy/windows', {method:'POST', body:JSON.stringify(payload)});
          toastSuccess('窗口已创建');
        }
        document.getElementById('dwClose').click();
        scopeNameCache = null;
        await loadWindows(); renderList();
      } catch(e) { toastError(e.message); }
    };
  }

  window.deploy_windows = {
    render: async function(root){
      destroyed = false;
      await loadWindows();
      if (destroyed) return [];
      root.innerHTML = `
        <div class="deploy-toolbar">
          <span style="flex:1"></span>
          ${isAdmin() ? '<button class="btn primary" id="newWinBtn">+ 新建窗口</button>' : ''}
        </div>
        <div id="winBox"></div>`;
      const nb = document.getElementById('newWinBtn');
      if (nb) nb.onclick = () => openForm(null);
      renderList();
      return [];
    },
    destroy: function(){ destroyed = true; }
  };
})();
