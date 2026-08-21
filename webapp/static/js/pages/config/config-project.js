// config-project.js — 项目仪表盘 + 子页框架 + 各列表 + 项目 CRUD + YAML 页
(function(){
  'use strict';
  const { touchRecent, getRecent } = window.cfgHelpers;
  const SUB_TABS = [
    ['overview','概览'],['services','服务'],['environments','环境'],['pipelines','流水线'],['servers','服务器'],['yaml','YAML 配置'],
  ];

  async function render(root, pid, sub){
    try{
      pid = Number(pid);
      if (!Number.isInteger(pid) || pid <= 0) throw new Error('项目 ID 无效');
      const projects = await api('/api/projects');
      const project = projects.find(p => p.id === pid);
      if (!project) throw new Error('项目不存在或已被删除');
      touchRecent(pid, project.name);
      if (!SUB_TABS.some(([k]) => k === sub)) sub = 'overview';

      // 服务器与绑定信息（供上下文条徽章与服务器子页）
      const [servers, envs] = await Promise.all([
        api('/api/servers'),
        api(`/api/projects/${pid}/environments`),
      ]);
      const bindings = await Promise.all(envs.map(async e => {
        try{ return { env: e, list: await api(`/api/environments/${e.id}/servers`) }; }
        catch(err){ return { env: e, list: [], failed: true }; }   // 请求失败不参与未绑定统计，避免删除后竞态误报
      }));
      const okCnt = servers.filter(s => s.conn && s.conn.ok).length;
      const errCnt = servers.filter(s => s.conn && !s.conn.ok).length;
      const unbindEnvs = bindings.filter(b => !b.failed && b.list.length === 0);

      const issues = [];
      if (servers.length && unbindEnvs.length) issues.push(`${unbindEnvs.length} 个环境未绑定服务器`);
      if (errCnt) issues.push(`${errCnt} 台服务器 SSH 异常`);

      root.innerHTML = `
        <div class="proj-bar">
          <div class="pb-ico">🗂️</div>
          <div>
            <div class="pb-name">${esc(project.name)} <span class="pb-code">${esc(project.code)}</span></div>
            <div class="pb-desc">${esc(project.description || '暂无描述')} · Git 组织：${esc(project.git_org || '-')}</div>
          </div>
          <div class="pb-switch">
            <select id="projSwitch" class="filter-sel" title="切换项目">
              ${projects.map(p => `<option value="${p.id}" ${p.id === pid ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}
            </select>
          </div>
          <div class="pb-badges">
            <span class="badge-health ${errCnt ? 'bad' : ''}">🖥️ SSH ${okCnt}/${servers.length} 正常</span>
            ${issues.map(t => `<span class="badge-issue">⚠ ${esc(t)}</span>`).join('')}
            <button class="btn sm ghost" data-editp>编辑项目</button>
            <button class="btn sm danger" data-delp>删除项目</button>
            <button class="btn sm ghost" data-back>← 返回工作台</button>
          </div>
        </div>
        <div class="proj-tabs">${SUB_TABS.map(([k, v]) =>
          `<span class="pt ${k === sub ? 'on' : ''}" data-sub="${k}">${v}</span>`).join('')}
        </div>
        <div id="subBody">${skeletonHTML(4)}</div>`;

      root.querySelectorAll('[data-sub]').forEach(t => t.onclick = () => { location.hash = `#/config/p/${pid}/${t.dataset.sub}`; });
      root.querySelector('[data-editp]').onclick = () => openProjectForm(root, project);
      root.querySelector('[data-delp]').onclick = () => delProject(project);
      root.querySelector('[data-back]').onclick = () => { location.hash = '#/config'; };
      // 项目切换：保持当前子页跳转到目标项目（enhanceSelect 自动换肤）
      const ps = document.getElementById('projSwitch');
      if (ps) ps.onchange = e => { location.hash = `#/config/p/${e.target.value}/${sub}`; };

      const sb = document.getElementById('subBody');
      const route = { services: renderServices, environments: renderEnvs, pipelines: renderPipelines, servers: renderServers, yaml: renderYaml, overview: renderOverview };
      await route[sub](sb, project);
      // 快速开始/服务器保存后自动打开新建表单
      const auto = sessionStorage.getItem('cfgAutoOpen');
      if (auto){
        sessionStorage.removeItem('cfgAutoOpen');
        if (auto === 'service' && sub === 'services') cfgServiceForm.open(sb, project, null, () => renderServices(sb, project));
        else if (auto === 'env' && sub === 'environments') cfgEnvForm.open(sb, project, null);
        else if (auto === 'pipeline' && sub === 'pipelines') cfgPipelineEditor.open(sb, project, null);
        else if (auto === 'server' && sub === 'servers') cfgServerForm.open(sb, null, () => renderServers(sb));
      }
    }catch(e){
      root.innerHTML = errorState('项目加载失败：' + esc(e.message), 'navRetry');
    }
  }

  // ---- 概览：最近发布记录 ----
  async function renderOverview(sb, project){
    try{
      const tasks = await api(`/api/tasks?project_id=${project.id}&limit=8`);
      sb.innerHTML = `
        <div class="overview-grid">
          <div class="ov-card" data-go="services"><div class="oc-num">${project.services || 0}</div><div class="oc-label">服务</div><div class="oc-desc">代码仓库与构建方式，供流水线编排</div><div class="oc-go">管理 →</div></div>
          <div class="ov-card" data-go="environments"><div class="oc-num">${project.environments || 0}</div><div class="oc-label">环境</div><div class="oc-desc">模板化配置 + 绑定服务器</div><div class="oc-go">管理 →</div></div>
          <div class="ov-card" data-go="pipelines"><div class="oc-num">${project.pipelines || 0}</div><div class="oc-label">流水线</div><div class="oc-desc">拖拽画布编排发布顺序</div><div class="oc-go">管理 →</div></div>
          <div class="ov-card" data-go="servers"><div class="oc-num">${(await api('/api/servers')).length}</div><div class="oc-label">服务器</div><div class="oc-desc">平台级资源，环境中选择绑定</div><div class="oc-go">管理 →</div></div>
        </div>
        <div class="panel" style="margin-top:var(--sp-4)">
          <div class="panel-title">最近发布记录</div>
          ${tasks.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>类型</th><th>名称</th><th>环境</th><th>版本</th><th>状态</th><th>时间</th></tr></thead><tbody>
          ${tasks.map(t => `<tr>
            <td>${t.type === 'pipeline' ? '流水线' : '服务'}</td>
            <td>${esc(t.name)}</td><td>${esc(t.env || '-')}</td><td class="num">${esc(t.version || '-')}</td>
            <td>${statusBadge(t.status)}</td><td class="num">${fmtTime(t.started_at)}</td>
          </tr>`).join('')}</tbody></table></div>`
          : `<div class="empty-state"><div class="e-ico">📋</div><div>暂无发布记录，去发布中心发起发布</div></div>`}
        </div>`;
      sb.querySelectorAll('[data-go]').forEach(c => c.onclick = () => { location.hash = `#/config/p/${project.id}/${c.dataset.go}`; });
    }catch(e){
      sb.innerHTML = errorState('概览加载失败：' + esc(e.message), 'navRetry');
    }
  }

  // ---- 服务列表 ----
  async function renderServices(sb, project){
    try{
      const svcs = await api(`/api/projects/${project.id}/services`);
      const enabledCnt = svcs.filter(s => s.enabled).length;
      sb.innerHTML = `<div class="deploy-toolbar">
          <span class="weak" style="font-size:12px">服务（${enabledCnt}/${svcs.length} 启用）</span><span style="flex:1"></span>
          <button class="btn sm primary" id="addSvc">+ 新建服务</button>
        </div>
        <div class="panel" style="padding:0">${svcs.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr>
          <th>名称</th><th>镜像</th><th>服务名</th><th>端口</th><th>启用</th><th>操作</th></tr></thead><tbody>
          ${svcs.map(s => `<tr style="${s.enabled ? '' : 'opacity:0.5'}">
            <td>${esc(s.name)}</td><td class="num">${esc(s.image_name)}</td><td class="num">${esc(s.service_name)}</td>
            <td class="num">${s.health_port ?? '-'}</td>
            <td><div data-toggle-div="${s.id}" style="position:relative;display:inline-block;width:36px;height:20px;cursor:pointer;border-radius:10px" title="${s.enabled ? '点击停用' : '点击启用'}">
              <input type="checkbox" ${s.enabled ? 'checked' : ''} data-toggle="${s.id}" style="display:none">
              <span style="position:absolute;inset:0;background:${s.enabled ? 'var(--c-success,#22c55e)' : 'var(--c-muted,#888)'};border-radius:10px;transition:.2s"></span>
              <span style="position:absolute;left:${s.enabled ? '18px' : '2px'};top:2px;width:16px;height:16px;background:#fff;border-radius:50%;transition:.2s"></span>
            </div></td>
            <td><button class="btn sm ghost" data-edit="${s.id}">编辑</button>
                <button class="btn sm danger" data-del="${s.id}">删除</button></td>
          </tr>`).join('')}</tbody></table></div>`
        : `<div class="empty-state"><div class="e-ico">🧩</div><div>暂无服务，点右上角新建</div></div>`}</div>`;
      // 快速切换启用/停用
      sb.querySelectorAll('[data-toggle-div]').forEach(async div => {
        div.onclick = async () => {
          const sid = Number(div.dataset.toggleDiv);
          const s = svcs.find(x => x.id === sid);
          if (!s) return;
          const cb = div.querySelector('input[type=checkbox]');
          cb.checked = !cb.checked;
          // 更新视觉
          const spans = div.querySelectorAll('span');
          spans[0].style.background = cb.checked ? 'var(--c-success,#22c55e)' : 'var(--c-muted,#888)';
          spans[1].style.left = cb.checked ? '18px' : '2px';
          try{
            await api(`/api/projects/${project.id}/services/${sid}`, { method: 'PUT', body: JSON.stringify({ enabled: cb.checked }) });
            toastSuccess(cb.checked ? `已启用「${s.name}」` : `已停用「${s.name}」`);
            await renderServices(sb, project);
          }catch(e){ toastError(e.message); cb.checked = !cb.checked; await renderServices(sb, project); }
        };
      });
      document.getElementById('addSvc').onclick = () => cfgServiceForm.open(sb, project, null, () => renderServices(sb, project));
      sb.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => cfgServiceForm.open(sb, project, svcs.find(s => s.id === Number(b.dataset.edit)), () => renderServices(sb, project)));
      sb.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
        const s = svcs.find(x => x.id === Number(b.dataset.del));
        if (!await confirmDialog(`确认删除服务「${s.name}」？`)) return;
        try{ await api(`/api/projects/${project.id}/services/${s.id}`, { method: 'DELETE' }); toastSuccess('服务已删除'); await renderServices(sb, project); }
        catch(e){ toastError(e.message); }  // 被流水线引用时后端 400 文案直接展示
      });
    }catch(e){ sb.innerHTML = errorState('服务列表加载失败：' + esc(e.message), 'navRetry'); }
  }

  // ---- 环境列表（含绑定服务器入口，填补现有盲区） ----
  async function renderEnvs(sb, project){
    try{
      const envs = await api(`/api/projects/${project.id}/environments`);
      const binds = await Promise.all(envs.map(async e => {
        try{ return { env: e, list: await api(`/api/environments/${e.id}/servers`) }; }
        catch(err){ return { env: e, list: [], failed: true }; }   // 请求失败不参与未绑定统计，避免删除后竞态误报
      }));
      sb.innerHTML = `<div class="deploy-toolbar">
          <span class="weak" style="font-size:12px">环境（${envs.length}）</span><span style="flex:1"></span>
          <button class="btn sm primary" id="addEnv">+ 新建环境</button>
        </div>
        <div class="panel" style="padding:0">${envs.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr>
          <th>名称</th><th>默认</th><th>绑定服务器</th><th>Compose 路径</th><th>Maven Profile</th><th>操作</th></tr></thead><tbody>
          ${binds.map(({env: e, list, failed}) => `<tr>
            <td>${esc(e.name)}</td>
            <td>${e.is_default ? '<span class="badge success">默认</span>' : '-'}</td>
            <td class="num">${list.length ? `${list.length} 台 <span class="weak">(${esc(list.map((s, i) => i === 0 ? '首台' : '跟随').join('/'))})</span>` : (failed ? '<span class="weak">加载失败</span>' : '<span class="err">未绑定</span>')}</td>
            <td class="num">${esc(e.docker_compose_path || '-')}</td>
            <td class="num">${esc(e.maven_profile || '-')}</td>
            <td><button class="btn sm ghost" data-bind="${e.id}">绑定服务器</button>
                <button class="btn sm ghost" data-edit="${e.id}">编辑</button>
                <button class="btn sm danger" data-del="${e.id}">删除</button></td>
          </tr>`).join('')}</tbody></table></div>`
        : `<div class="empty-state"><div class="e-ico">🌐</div><div>暂无环境，点右上角新建（有模板一键填充）</div></div>`}</div>`;
      document.getElementById('addEnv').onclick = () => cfgEnvForm.open(sb, project, null);
      sb.querySelectorAll('[data-bind]').forEach(b => b.onclick = () => cfgEnvForm.open(sb, project, envs.find(x => x.id === Number(b.dataset.bind)), { step: 'bind' }));
      sb.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => cfgEnvForm.open(sb, project, envs.find(x => x.id === Number(b.dataset.edit))));
      sb.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
        const e = envs.find(x => x.id === Number(b.dataset.del));
        if (!await confirmDialog(`确认删除环境「${e.name}」？`)) return;
        try{ await api(`/api/projects/${project.id}/environments/${e.id}`, { method: 'DELETE' }); toastSuccess('环境已删除'); await renderEnvs(sb, project); }
        catch(err){ toastError(err.message); }
      });
    }catch(e){ sb.innerHTML = errorState('环境列表加载失败：' + esc(e.message), 'navRetry'); }
  }

  // ---- 流水线列表 ----
  async function renderPipelines(sb, project){
    try{
      const pls = await api(`/api/projects/${project.id}/pipelines`);
      sb.innerHTML = `<div class="deploy-toolbar">
          <span class="weak" style="font-size:12px">流水线（${pls.length}）</span><span style="flex:1"></span>
          <button class="btn sm primary" id="addPl">+ 新建流水线</button>
        </div>
        <div class="panel" style="padding:0">${pls.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr>
          <th>名称</th><th>先构建</th><th>阶段数</th><th>操作</th></tr></thead><tbody>
          ${pls.map(pl => `<tr>
            <td>${esc(pl.name)}</td>
            <td>${pl.build_first ? '<span class="badge warning">构建优先</span>' : '否'}</td>
            <td class="num">${(pl.stages || []).length}</td>
            <td><button class="btn sm ghost" data-edit="${pl.id}">编辑</button>
                <button class="btn sm danger" data-del="${pl.id}">删除</button></td>
          </tr>`).join('')}</tbody></table></div>`
        : `<div class="empty-state"><div class="e-ico">🔁</div><div>暂无流水线，点右上角用画布编排</div></div>`}</div>`;
      document.getElementById('addPl').onclick = () => cfgPipelineEditor.open(sb, project, null);
      sb.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => cfgPipelineEditor.open(sb, project, pls.find(x => x.id === Number(b.dataset.edit))));
      sb.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
        const pl = pls.find(x => x.id === Number(b.dataset.del));
        if (!await confirmDialog(`确认删除流水线「${pl.name}」？`)) return;
        try{ await api(`/api/projects/${project.id}/pipelines/${pl.id}`, { method: 'DELETE' }); toastSuccess('流水线已删除'); await renderPipelines(sb, project); }
        catch(err){ toastError(err.message); }
      });
    }catch(e){ sb.innerHTML = errorState('流水线列表加载失败：' + esc(e.message), 'navRetry'); }
  }

  // ---- 服务器列表（平台级资源；说明条 + 新建/编辑/删除） ----
  async function renderServers(sb){
    try{
      const svs = await api('/api/servers');
      const connBadge = s => {
        const c = s.conn;
        if (!c) return '<span class="weak">未知</span>';
        const tip = c.ok ? fmtTime(c.ts) : (c.error || '连接失败');
        return c.ok
          ? `<span class="ok" title="最后探测：${esc(tip)}">正常</span>`
          : `<span class="err" title="${esc(tip)}">异常</span>`;
      };
      sb.innerHTML = `<div class="deploy-toolbar">
          <span class="weak" style="font-size:12px">服务器（${svs.length}）· 平台级资源，在环境表单中选择绑定</span><span style="flex:1"></span>
          <button class="btn sm primary" id="addSrv">+ 新建服务器</button>
        </div>
        <div class="panel" style="padding:0">${svs.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr>
          <th>主机</th><th>端口</th><th>用户</th><th>Nacos IP</th><th>连接状态</th><th>备注</th><th>启用</th><th>操作</th></tr></thead><tbody>
          ${svs.map(s => `<tr>
            <td class="num">${esc(s.host)}</td>
            <td class="num">${s.ssh_port ?? '-'}</td>
            <td class="num">${esc(s.ssh_user || '-')}</td>
            <td class="num">${esc(s.nacos_ip || '-')}</td>
            <td>${connBadge(s)}</td>
            <td class="muted">${esc(s.remark || '-')}</td>
            <td>${s.enabled ? '<span class="ok">启用</span>' : '<span class="weak">停用</span>'}</td>
            <td><button class="btn sm ghost" data-edit="${s.id}">编辑</button>
                <button class="btn sm danger" data-del="${s.id}">删除</button></td>
          </tr>`).join('')}</tbody></table></div>`
        : `<div class="empty-state"><div class="e-ico">🖥️</div><div>暂无服务器，点右上角新建（保存时自动测试连接）</div></div>`}</div>`;
      const refreshSrv = () => renderServers(sb);
      document.getElementById('addSrv').onclick = () => cfgServerForm.open(sb, null, refreshSrv);
      sb.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => cfgServerForm.open(sb, svs.find(x => x.id === Number(b.dataset.edit)), refreshSrv));
      sb.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
        const s = svs.find(x => x.id === Number(b.dataset.del));
        if (!await confirmDialog(`确认删除服务器「${s.host}」？`)) return;
        try{ await api('/api/servers/' + s.id, { method: 'DELETE' }); toastSuccess('服务器已删除'); await renderServers(sb); }
        catch(err){ toastError(err.message); }
      });
    }catch(e){ sb.innerHTML = errorState('服务器列表加载失败：' + esc(e.message), 'navRetry'); }
  }

  // ---- YAML 导入导出（沿用旧逻辑） ----
  function renderYaml(sb){
    sb.innerHTML = `<div class="panel">
        <div class="panel-title">YAML 导入 / 导出</div>
        <p class="muted" style="margin-bottom:16px;font-size:13px">
          导出：将 DB 中全部配置写入 <code>data/export/config</code>（按项目/环境拆分 YAML）。<br>
          导入：从 <code>config/</code> 目录（deploy.yaml/pipelines.yaml/projects.yaml/servers.yaml）全量重建 DB。
        </p>
        <div style="display:flex;gap:10px">
          <button class="btn primary" id="ioExport">⬇ 导出配置</button>
          <button class="btn danger" id="ioImport">⬆ 导入配置（覆盖 DB）</button>
        </div>
        <div class="io-results" id="ioResults"></div>
      </div>`;
    document.getElementById('ioExport').onclick = async () => {
      try{
        const r = await api('/api/config/export');
        document.getElementById('ioResults').innerHTML =
          `<div class="io-item"><span class="io-ok">✓</span><span>已导出 ${r.files.length} 个文件 → ${esc(r.dir)}</span></div>`;
        toastSuccess('配置已导出');
      }catch(err){ toastError(err.message); }
    };
    document.getElementById('ioImport').onclick = async () => {
      if (!await confirmDialog('导入将覆盖现有 DB 配置，确认继续？')) return;
      try{
        const r = await api('/api/config/import', { method: 'POST' });
        const items = Object.entries(r.results || {});
        document.getElementById('ioResults').innerHTML = items.length ? items.map(([k, v]) =>
          `<div class="io-item"><span class="${v.ok ? 'io-ok' : 'io-fail'}">${v.ok ? '✓' : '✗'}</span>
           <span>${esc(k)}：${esc(v.message || '')}</span></div>`).join('') : '<div class="io-item">无变更</div>';
        items.some(([, v]) => !v.ok) ? toastError('导入完成，存在失败项（见明细）') : toastSuccess('配置已导入');
      }catch(err){ toastError(err.message); }
    };
  }

  // ---- 项目 CRUD ----
  function openProjectForm(container, p, onSaved){
    openDrawer({ title: p ? '编辑项目' : '新建项目', body: `
      <div class="form-grid">
        <div class="field"><label>名称 *</label><input id="pName" value="${esc(p ? p.name : '')}"></div>
        <div class="field"><label>编码 *</label><input id="pCode" value="${esc(p ? p.code : '')}" ${p ? 'disabled' : ''} placeholder="字母数字 _ -，唯一，创建后不可改"></div>
        <div class="field full"><label>描述</label><input id="pDesc" value="${esc(p ? p.description : '')}"></div>
        <div class="field full"><label>Git 组织</label><input id="pGit" value="${esc(p ? p.git_org : '')}" placeholder="代码仓库所属组织/命名空间，发布打 tag 时使用"></div>
      </div><div style="display:flex;gap:10px;margin-top:20px"><button class="btn primary flex-1" id="cfSubmit">${p ? '保存' : '创建'}</button></div>` });
    document.getElementById('cfSubmit').onclick = async () => {
      try{
        const payload = { name: val('pName').trim(), description: val('pDesc').trim(), git_org: val('pGit').trim() };
        if (!p) payload.code = val('pCode').trim();
        if (!payload.name || (!p && !payload.code)) throw new Error('名称与编码必填');
        await api(p ? `/api/projects/${p.id}` : '/api/projects', { method: p ? 'PUT' : 'POST', body: JSON.stringify(payload) });
        toastSuccess(p ? '项目已更新' : '项目已创建');
        const dr = document.getElementById('drawerRoot');
        dr.classList.add('hidden'); dr.innerHTML = '';
        if (onSaved) onSaved();
        else location.hash = '#/config';
      }catch(e){ toastError(e.message); }
    };
  }
  async function delProject(project){
    if (!await confirmDialog(`确认删除项目「${project.name}」？将级联删除其服务/环境/流水线。`)) return;
    try{
      await api('/api/projects/' + project.id, { method: 'DELETE' });
      toastSuccess('项目已删除');
      location.hash = '#/config';
    }catch(e){ toastError(e.message); }
  }
  function val(id){ return document.getElementById(id).value; }

  window.cfgProject = {
    render: async function(root, pid, sub){ await render(root, pid, sub); return []; },
    openProjectForm,
    delProject,
  };
})();
