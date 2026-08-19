// pages/logs.js — 日志中心（任务日志/审计日志/服务日志三 Tab + 项目/环境筛选 + 日志追尾）
(function(){
  let timer = null;       // 追尾定时器
  let tab = 'task';       // task / audit / service（audit 仅 admin）
  let projFilter = 'all'; // all / 项目 id
  let envFilter = 'all';  // all / 环境名（仅选中项目时生效）
  let projects = [];
  let envs = [];
  let tasks = [];
  let currentId = null;
  let offset = 0;         // 当前日志拉取偏移
  let destroyed = false;  // 路由切换后丢弃 in-flight 回调，防操作已销毁 DOM
  let auditPage = 1;
  let auditTotal = 0;
  const auditPageSize = 50;

  // 服务日志分页状态
  let svcLogPage = 1;
  let svcLogTotal = 0;
  const svcLogPageSize = 50;
  let svcProjFilter = 'all'; // 服务日志项目筛选

  const ACTION_CN = {
    login:'登录', login_failed:'登录失败', change_password:'修改密码',
    create_user:'创建用户', update_user:'更新用户', delete_user:'删除用户',
    create_project:'创建项目', update_project:'更新项目', delete_project:'删除项目',
    create_service:'创建服务', update_service:'更新服务', delete_service:'删除服务',
    create_environment:'创建环境', update_environment:'更新环境', delete_environment:'删除环境',
    bind_server:'绑定服务器', unbind_server:'解绑服务器', reorder_servers:'调整服务器顺序',
    create_server:'创建服务器', update_server:'更新服务器', delete_server:'删除服务器',
    test_server:'测试服务器连接',
    create_pipeline:'创建流水线', update_pipeline:'更新流水线', delete_pipeline:'删除流水线',
    import_config:'导入配置', export_config:'导出配置',
    deploy:'发布', rollback:'回滚', kill_task:'终止任务', delete_task:'删除任务',
  };
  const TARGET_CN = {user:'用户', project:'项目', service:'服务', environment:'环境',
                     server:'服务器', pipeline:'流水线', config:'配置', task:'任务'};

  function logBox(){ return document.getElementById('logBox'); }
  function stopTail(){ if (timer){ clearInterval(timer); timer = null; } }
  async function loadProjects(){
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
  async function loadTaskList(){
    const q = new URLSearchParams({limit:'200'});
    if (projFilter !== 'all') q.set('project_id', projFilter);
    tasks = await api('/api/tasks?' + q).catch(() => []);
    const sel = document.getElementById('logTaskSel');
    if (!sel) return;
    const visible = envFilter === 'all' ? tasks : tasks.filter(t => t.env === envFilter);
    sel.innerHTML = '<option value="">— 选择任务 —</option>' + visible.map(t =>
      `<option value="${t.id}" ${t.id === currentId ? 'selected' : ''}>#${t.id} ${esc(t.name)} [${esc(t.env || '-')}] ${t.action === 'rollback' ? '↩回滚' : '发布'} ${esc(t.status)}</option>`
    ).join('');
  }
  // 全量拉取：offset 循环直至任务终态/无新日志或 1000 轮（防死循环）
  async function fetchFull(){
    stopTail();
    const id = currentId; // 快照：切换任务后丢弃在途回调，防日志串台
    if (!id){ if (logBox()) logBox().textContent = '请选择任务'; return; }
    if (logBox()) logBox().textContent = '加载中...';
    let text = '';
    let status = '';
    let off = 0;
    let truncated = false;
    try{
      for (let i = 0; i < 1000; i++){
        if (destroyed || !logBox() || currentId !== id) return;
        const d = await api(`/api/tasks/${id}/log?offset=${off}`);
        if (destroyed || !logBox() || currentId !== id) return;
        if (off === 0 && d.truncated) truncated = true;
        if (d.log) text += d.log;
        off = d.offset;
        status = d.status;
        // 终态且无新日志 → 日志全部读完，停止循环
        if (['success','failed','killed'].includes(d.status) && !d.log) break;
        // 运行中暂无新日志 → 停止全量循环（运行中随后进入追尾）
        if (!d.log && ['running','pending'].includes(d.status)) break;
      }
    }catch(e){
      if (destroyed || !logBox()) return;
      logBox().textContent = '日志加载失败：' + (e.message || e);
      return;
    }
    if (destroyed || !logBox() || currentId !== id) return;
    offset = off; // 校验通过后才提交偏移，供追尾继续
    logBox().textContent = (truncated ? '── 日志较长，仅显示最近输出 ──\n' : '') + (text || '（无日志输出）');
    logBox().scrollTop = logBox().scrollHeight;
    document.getElementById('logTaskStatus').innerHTML = statusBadge(status);
    if (['running','pending'].includes(status)) startTail();
  }
  // 追尾：3s 增量拉取，终态自动停止
  function startTail(){
    stopTail();
    timer = setInterval(async () => {
      try{
        if (destroyed || !logBox()){ stopTail(); return; }
        const id = currentId; // 快照：切任务后丢弃在途回调
        const d = await api(`/api/tasks/${id}/log?offset=${offset}`);
        if (destroyed || !logBox() || currentId !== id) return;
        if (d.log){ logBox().textContent += d.log; logBox().scrollTop = logBox().scrollHeight; }
        offset = d.offset;
        if (['success','failed','killed'].includes(d.status)){
          stopTail();
          document.getElementById('logTaskStatus').innerHTML = statusBadge(d.status);
          loadTaskList();
        }
      }catch(e){ stopTail(); }
    }, 3000);
  }

  // ---- 审计日志（admin）----
  function fmtDetail(detail){
    try{ const s = JSON.stringify(JSON.parse(detail || '{}'));
      return s.length > 60 ? s.slice(0, 60) + '…' : s; }
    catch(e){ return detail || '-'; }
  }
  async function renderAudit(){
    const tbody = document.getElementById('auditBody');
    if (!tbody) return;
    let data = { rows: [], total: 0 };
    try{
      data = await api(`/api/audit?page=${auditPage}&page_size=${auditPageSize}`);
    }catch(e){ /* 权限/失败静默，空表 */ }
    if (destroyed || !document.getElementById('auditBody')) return;
    const { rows, total } = data;
    auditTotal = total;
    const totalPages = Math.max(1, Math.ceil(total / auditPageSize));
    tbody.innerHTML = rows.length ? rows.map(r => `<tr>
      <td class="num muted">${fmtTime(r.created_at)}</td>
      <td>${esc(r.username || '-')}</td>
      <td>${ACTION_CN[r.action] || esc(r.action)}</td>
      <td class="muted">${TARGET_CN[r.target_type] || esc(r.target_type || '-')}${r.target_id ? ' #' + esc(r.target_id) : ''}</td>
      <td class="audit-detail" title="${esc(fmtDetail(r.detail))}">${esc(fmtDetail(r.detail))}</td>
    </tr>`).join('')
      : '<tr><td colspan="5" class="empty-row">暂无审计记录</td></tr>';
    // 分页控件（靠右）
    const pg = document.getElementById('auditPager');
    if (pg){
      if (total === 0){ pg.innerHTML = ''; return; }
      const items = [];
      items.push(`<span class="muted" style="font-size:12px">共 ${total} 条</span>`);
      items.push(`<button class="btn sm" ${auditPage <= 1 ? 'disabled' : ''} data-pg="${auditPage - 1}">‹ 上一页</button>`);
      items.push(`<span class="muted" style="font-size:12px">${auditPage} / ${totalPages}</span>`);
      items.push(`<button class="btn sm" ${auditPage >= totalPages ? 'disabled' : ''} data-pg="${auditPage + 1}">下一页 ›</button>`);
      pg.innerHTML = items.join('');
      pg.querySelectorAll('button[data-pg]').forEach(b => b.onclick = () => {
        auditPage = Number(b.dataset.pg);
        renderAudit();
      });
    }
  }

  // ---- Tab 渲染 ----
  async function renderTab(){
    if (destroyed) return;
    const body = document.getElementById('logBody');
    if (!body) return;
    if (tab === 'audit'){
      stopTail();
      body.innerHTML = `
        <div class="panel">
          <div class="panel-title">审计日志</div>
          <div class="tbl-wrap"><table class="tbl"><thead><tr>
            <th>时间</th><th>用户</th><th>动作</th><th>对象</th><th>详情</th></tr></thead>
            <tbody id="auditBody"><tr><td colspan="5" class="empty-row">加载中...</td></tr></tbody>
          </table></div>
          <div id="auditPager" style="display:flex;gap:8px;align-items:center;justify-content:flex-end;padding:8px 0"></div>
        </div>`;
      await renderAudit();
      return;
    }
    if (tab === 'service') {
      stopTail();
      body.innerHTML = `
        <div class="panel">
          <div class="panel-title">服务日志
            <span style="flex:1"></span>
            ${isAdmin() ? '<button class="btn sm ghost" id="svcLogSettingsBtn">⚙ 保留设置</button>' : ''}
          </div>
          <div class="flex gap-3" style="margin-bottom:12px;flex-wrap:wrap;align-items:center">
            <span class="weak" style="font-size:12px">项目</span>
            <select class="filter-sel" id="svcLogProjSel"></select>
            <span class="weak" style="font-size:12px">环境</span>
            <select class="filter-sel" id="svcLogEnvSel">
              <option value="">全部环境</option>
            </select>
            <span class="weak" style="font-size:12px">服务</span>
            <select class="filter-sel" id="svcLogSvcSel">
              <option value="">全部服务</option>
            </select>
            <span class="weak" style="font-size:12px">关键词</span>
            <input type="text" class="filter-input" id="svcLogKeyword" placeholder="ERROR / Exception / ..." style="width:200px">
            <button class="btn sm" id="svcLogSearch">检索</button>
          </div>
          <div id="svcLogResults">
            <div class="muted" style="text-align:center;padding:40px">请输入检索条件</div>
          </div>
        </div>
        <div id="svcLogContextDrawer" class="drawer-mask" style="display:none">
          <div class="drawer" style="width:80vw;max-width:1000px">
            <div class="drawer-head">
              <b>日志上下文</b>
              <div class="dh-right">
                <button class="btn sm ghost" id="svcLogCtxClose">关闭</button>
              </div>
            </div>
            <div class="drawer-body">
              <pre id="svcLogCtxContent" class="log-console" style="height:calc(100vh - 140px);overflow:auto"></pre>
            </div>
          </div>
        </div>`;
      loadServiceLogOptions();
      document.getElementById('svcLogSearch').onclick = () => { svcLogPage = 1; searchServiceLogs(); };
      document.getElementById('svcLogCtxClose').onclick = () => {
        document.getElementById('svcLogContextDrawer').style.display = 'none';
      };
      document.getElementById('svcLogProjSel').onchange = () => {
        svcProjFilter = document.getElementById('svcLogProjSel').value;
        loadServiceLogOptions();
      };
      if (isAdmin()) {
        document.getElementById('svcLogSettingsBtn').onclick = () => openLogSettingsDrawer();
      }
      return;
    }
    body.innerHTML = `
      <div class="panel">
        <div class="panel-title">任务日志</div>
        <div class="flex gap-3" style="margin-bottom:12px;flex-wrap:wrap;align-items:center">
          <span class="weak" style="font-size:12px">项目</span>
          <select class="filter-sel" id="logProjSel"></select>
          <span class="weak" style="font-size:12px">环境</span>
          <select class="filter-sel" id="logEnvSel"></select>
          <span style="width:1px;height:20px;background:var(--border);margin:0 4px"></span>
          <select class="filter-sel" id="logTaskSel"></select>
          <span id="logTaskStatus"></span>
          <span style="flex:1"></span>
          <button class="btn sm" id="logRefresh">⟳ 全量刷新</button>
        </div>
        <div class="log-console logs-console" id="logBox">请选择任务</div>
      </div>`;
    const projSel = document.getElementById('logProjSel');
    if (projSel){
      projSel.innerHTML = '<option value="all">全部项目</option>' + projects.map(p =>
        `<option value="${p.id}" ${p.id === Number(projFilter) ? 'selected' : ''}>${esc(p.name)}</option>`).join('');
      projSel.onchange = async () => {
        projFilter = projSel.value; envFilter = 'all';
        resetTask();
        await loadEnvs();
        await loadTaskList();
        renderTab(); // 重建环境选项
      };
    }
    const envSel = document.getElementById('logEnvSel');
    if (envSel){
      if (projFilter === 'all'){
        envSel.disabled = true;
        envSel.innerHTML = '<option value="all">全部环境</option>';
      }else{
        envSel.disabled = false;
        envSel.innerHTML = '<option value="all">全部环境</option>' + envs.map(e =>
          `<option value="${esc(e.name)}" ${e.name === envFilter ? 'selected' : ''}>${esc(e.name)}</option>`).join('');
        envSel.onchange = () => {
          envFilter = envSel.value;
          resetTask();
          loadTaskList();
        };
      }
    }
    const sel = document.getElementById('logTaskSel');
    if (sel) sel.onchange = () => {
      currentId = sel.value ? Number(sel.value) : null;
      document.getElementById('logTaskStatus').innerHTML = '';
      fetchFull();
    };
    const rb = document.getElementById('logRefresh');
    if (rb) rb.onclick = fetchFull;
    await loadTaskList();
  }
  // 筛选变化时重置已选任务（旧任务的日志可能不在新筛选范围内）
  function resetTask(){
    currentId = null;
    offset = 0;
    stopTail();
    const st = document.getElementById('logTaskStatus');
    if (st) st.innerHTML = '';
    if (logBox()) logBox().textContent = '请选择任务';
  }

  // ---- 服务日志 Tab ----
  async function loadServiceLogOptions() {
    const q = svcProjFilter !== 'all' ? `?project_id=${svcProjFilter}` : '';
    const services = await api('/api/logs/services' + q).catch(() => []);
    const projSel = document.getElementById('svcLogProjSel');
    const envSel = document.getElementById('svcLogEnvSel');
    const svcSel = document.getElementById('svcLogSvcSel');
    if (!projSel || !envSel || !svcSel) return;
    // 项目下拉（仅初始化一次，避免丢失用户选择）
    if (projSel.options.length <= 1) {
      projSel.innerHTML = '<option value="all">全部项目</option>' +
        projects.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('');
      projSel.value = svcProjFilter;
    }
    const envSet = new Set(services.map(s => s.env));
    envSel.innerHTML = '<option value="">全部环境</option>' +
      [...envSet].sort().map(e => `<option value="${esc(e)}">${esc(e)}</option>`).join('');
    const svcSet = new Set(services.map(s => s.service));
    svcSel.innerHTML = '<option value="">全部服务</option>' +
      [...svcSet].sort().map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
    // 环境变化时过滤服务列表
    envSel.onchange = () => {
      const env = envSel.value;
      const filtered = env ? services.filter(s => s.env === env) : services;
      const filteredSvcSet = new Set(filtered.map(s => s.service));
      svcSel.innerHTML = '<option value="">全部服务</option>' +
        [...filteredSvcSet].sort().map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
    };
  }

  async function searchServiceLogs() {
    const env = document.getElementById('svcLogEnvSel')?.value || '';
    const service = document.getElementById('svcLogSvcSel')?.value || '';
    const keyword = document.getElementById('svcLogKeyword')?.value || '';
    if (!keyword && !env && !service) {
      toastError('请至少选择一个筛选条件');
      return;
    }
    const results = document.getElementById('svcLogResults');
    if (results) results.innerHTML = '<div class="muted" style="text-align:center;padding:20px">检索中...</div>';
    try {
      const params = new URLSearchParams();
      if (env) params.set('env', env);
      if (service) params.set('service', service);
      if (keyword) params.set('keyword', keyword);
      params.set('page', String(svcLogPage));
      params.set('page_size', String(svcLogPageSize));
      const data = await api('/api/logs?' + params);
      svcLogTotal = data.total || 0;
      renderServiceLogResults(data);
    } catch (e) {
      if (results) results.innerHTML = `<div class="panel">检索失败：${esc(e.message)}</div>`;
    }
  }

  function renderServiceLogResults(data) {
    const results = document.getElementById('svcLogResults');
    if (!results) return;
    if (!data.rows || data.rows.length === 0) {
      results.innerHTML = '<div class="muted" style="text-align:center;padding:40px">无匹配结果</div>';
      return;
    }
    const totalPages = Math.max(1, Math.ceil(svcLogTotal / svcLogPageSize));
    results.innerHTML = `
      <div class="muted" style="font-size:12px;margin-bottom:8px">共 ${svcLogTotal} 条，第 ${svcLogPage}/${totalPages} 页</div>
      <div class="tbl-wrap">
        <table class="tbl">
          <thead><tr><th style="width:140px">时间</th><th style="width:80px">环境</th><th style="width:120px">服务</th><th>内容</th><th style="width:60px">操作</th></tr></thead>
          <tbody>
            ${data.rows.map(r => `
              <tr>
                <td class="num muted">${fmtTime(r.ts)}</td>
                <td>${esc(r.env)}</td>
                <td>${esc(r.service)}</td>
                <td class="svc-log-content" title="${esc(r.file_path)}">${esc((r.content || '').slice(0, 120))}</td>
                <td><button class="btn sm" data-ctx="${r.id}">上下文</button></td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
      <div id="svcLogPager" style="display:flex;gap:8px;align-items:center;justify-content:flex-end;padding:8px 0"></div>`;
    // 分页控件
    const pg = document.getElementById('svcLogPager');
    if (pg && svcLogTotal > svcLogPageSize) {
      pg.innerHTML = `
        <span class="muted" style="font-size:12px">共 ${svcLogTotal} 条</span>
        <button class="btn sm" ${svcLogPage <= 1 ? 'disabled' : ''} id="svcLogPgPrev">‹ 上一页</button>
        <span class="muted" style="font-size:12px">${svcLogPage} / ${totalPages}</span>
        <button class="btn sm" ${svcLogPage >= totalPages ? 'disabled' : ''} id="svcLogPgNext">下一页 ›</button>`;
      const prev = document.getElementById('svcLogPgPrev');
      const next = document.getElementById('svcLogPgNext');
      if (prev) prev.onclick = () => { if (svcLogPage > 1) { svcLogPage--; searchServiceLogs(); } };
      if (next) next.onclick = () => { if (svcLogPage < totalPages) { svcLogPage++; searchServiceLogs(); } };
    }
    // 事件委托绑定上下文按钮
    results.onclick = async (e) => {
      const btn = e.target.closest('[data-ctx]');
      if (!btn) return;
      const entryId = btn.dataset.ctx;
      const drawer = document.getElementById('svcLogContextDrawer');
      const content = document.getElementById('svcLogCtxContent');
      if (!drawer || !content) return;
      drawer.style.display = 'flex';
      content.textContent = '加载中...';
      try {
        const ctxData = await api(`/api/logs/context?id=${entryId}&lines=50`);
        if (ctxData.error) {
          content.textContent = ctxData.error;
          return;
        }
        content.textContent = ctxData.lines.join('');
      } catch (e) {
        content.textContent = '加载失败：' + e.message;
      }
    };
  }

  // ---- 日志保留设置抽屉 ----
  async function openLogSettingsDrawer() {
    let settings = { log_retention_days: 7, log_archive_days: 30 };
    try { settings = await api('/api/logs/settings'); } catch(e) { /* ignore */ }
    const bodyHTML = `
      <div class="form-grid">
        <div class="field">
          <label>日志保留天数</label>
          <input type="number" id="logSetRetention" value="${settings.log_retention_days || 7}" min="1" max="365">
          <div class="field-hint">超过该天数的日志文件将被自动归档压缩</div>
        </div>
        <div class="field">
          <label>归档保留天数</label>
          <input type="number" id="logSetArchive" value="${settings.log_archive_days || 30}" min="1" max="365">
          <div class="field-hint">超过该天数的归档文件将被自动永久删除</div>
        </div>
        <div class="full" style="display:flex;gap:10px;justify-content:flex-end;margin-top:8px">
          <button class="btn" id="logSetCancel">取消</button>
          <button class="btn primary" id="logSetSave">保存</button>
        </div>
      </div>`;
    openDrawer({ title: '日志保留设置', body: bodyHTML });
    document.getElementById('logSetCancel').onclick = () => {
      const c = document.getElementById('dwClose'); if (c) c.click();
    };
    document.getElementById('logSetSave').onclick = async () => {
      const retention = parseInt(document.getElementById('logSetRetention').value) || 7;
      const archive = parseInt(document.getElementById('logSetArchive').value) || 30;
      if (retention < 1 || retention > 365 || archive < 1 || archive > 365) {
        toastError('天数需在 1~365 之间'); return;
      }
      try {
        await api('/api/logs/settings', { method: 'PUT', body: JSON.stringify({ log_retention_days: retention, log_archive_days: archive }) });
        toastSuccess('设置已保存');
        const c = document.getElementById('dwClose'); if (c) c.click();
      } catch(e) { toastError(e.message || '保存失败'); }
    };
  }

  // ---- 模块导出 ----
  window.logs = {
    render: async function(root){
      destroyed = false;
      await loadProjects();
      await loadEnvs();
      root.innerHTML = `
        <div class="deploy-toolbar">
          ${[['task','任务日志'],['audit','审计日志'],['service','服务日志']]
            .filter(([k]) => k === 'task' || k === 'audit' && isAdmin() || k === 'service')
            .map(([k, v]) => `<span class="chip ${k === tab ? 'active' : ''}" data-tab="${k}">${v}</span>`).join('')}
        </div>
        <div id="logBody"></div>`;
      root.querySelectorAll('.chip[data-tab]').forEach(c => c.onclick = () => {
        tab = c.dataset.tab;
        document.querySelectorAll('.chip[data-tab]').forEach(x => x.classList.toggle('active', x === c));
        renderTab();
      });
      await renderTab();
      return [];
    },
    destroy: function(){ destroyed = true; stopTail(); },
  };
})();
