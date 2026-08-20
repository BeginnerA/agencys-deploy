// pages/alerts.js — 告警中心（告警列表 Tab + 规则管理 Tab + 静默窗口 Tab + 历史统计 Tab，消费后端 /api/alerts API）
(function(){
  let timer = null;
  let destroyed = false;
  let searchTimer = null;
  let currentTab = 'alerts';  // Tab 切换：alerts | rules | silences | stats

  // 告警列表状态
  let alerts = [];
  let total = 0;
  let currentPage = 1;
  const pageSize = 20;
  let sevFilter = 'all';
  let statusFilter = 'all';
  let keyword = '';
  let projFilter = 'all';
  let projects = [];
  let envFilter = 'all';
  let svcFilter = 'all';
  let filterOptions = {};  // {project_code: {envs: [], services: []}}

  // 批量操作状态
  let selectedIds = new Set();

  function canManageAlerts(){ return ['admin','operator'].includes(currentUser().role); }

  // 统计计数
  let firingCount = 0;
  let ackedCount = 0;
  let resolvedCount = 0;

  // ---- 规则管理 Tab 状态 ----
  let rules = [];
  let editingRuleId = null;
  let ruleTypeFilter = 'all';
  let ruleSaving = false;
  let scopeSelectedProjects = new Set();  // 项目作用域已选项目 ID
  let scopeSelectedEnvs = new Set();      // 环境/服务器/服务作用域已选环境 ID
  let scopeSelectedTargets = new Set();   // 服务器/服务作用域已选目标 ID
  let scopeNameCache = null;  // {projects: {id: name}, envs: {id: name}, servers: {id: host}, services: {id: name}}

  // ---- 静默窗口 Tab 状态 ----
  let silences = [];
  let editingSilenceId = null;
  let silenceSaving = false;

  // ---- 历史统计 Tab 状态 ----
  let statsChart = null;
  let statsDays = 7;
  let statsResizeHandler = null;

  // 常量定义
  const SEVERITIES = [['P0','P0'],['P1','P1'],['P2','P2'],['P3','P3']];
  const STATUSES = [['firing','活跃'],['acked','已确认'],['resolved','已解决']];
  const SEV_LABEL = {P0:'P0 紧急',P1:'P1 重要',P2:'P2 一般',P3:'P3 提示'};
  const ST_LABEL = {firing:'活跃',acked:'已确认',resolved:'已解决'};

  // 规则常量
  const METRIC_TYPES = [['metric','指标'],['status','状态'],['log_keyword','日志关键词']];
  const METRIC_OPTIONS = {
    metric: [['cpu','CPU'],['mem','内存'],['disk','磁盘'],['load','负载']],
    status: [['service_down','服务离线'],['task_failed','任务失败']],
    log_keyword: []
  };
  const COMPARE_OPS = [['<','<'],['<=','<='],['>','>'],['>=','>=']];
  const CHANNELS = [['email','邮件'],['wecom','企微'],['webhook','Webhook'],['sms_aliyun','阿里云短信'],['feishu','飞书'],['dingtalk','钉钉']];
  const SCOPE_TYPES = [['all','全部'],['project','项目'],['env','环境'],['server','服务器'],['service','服务']];
  // 按规则类型筛选的可选作用域
  const SCOPE_OPTIONS_BY_TYPE = {
    metric: [['all','全部'],['project','项目'],['env','环境'],['server','服务器']],
    status: [['all','全部'],['project','项目'],['env','环境'],['service','服务']],
    log_keyword: [['all','全部'],['project','项目'],['env','环境'],['service','服务']]
  };
  const SILENCE_TYPES = [['one_time','一次性'],['recurring','周期']];
  const WEEKDAY_LABELS = {1:'周一',2:'周二',3:'周三',4:'周四',5:'周五',6:'周六',7:'周日'};
  const MT_LABEL = {metric:'指标',status:'状态',log_keyword:'日志关键词'};
  const CMP_LABEL = {'<':'<','<=':'≤','>':'>','>=':'≥'};

  // ---- 数据加载 ----

  async function loadAlerts(){
    try {
      const q = new URLSearchParams({page: String(currentPage), page_size: String(pageSize)});
      if (sevFilter !== 'all') q.set('severity', sevFilter);
      if (statusFilter !== 'all') q.set('status', statusFilter);
      if (keyword) q.set('keyword', keyword);
      if (projFilter !== 'all') q.set('project_id', projFilter);
      if (envFilter !== 'all') q.set('env', envFilter);
      if (svcFilter !== 'all') q.set('service', svcFilter);
      const data = await api('/api/alerts?' + q);
      if (destroyed) return;
      alerts = data.rows || [];
      total = data.total || 0;
      renderList();
    } catch(e) {
      // 列表加载失败时保留上次数据，不阻塞轮询
      console.warn('loadAlerts failed', e);
    }
  }

  function _filterQs(){
    const parts = [];
    if (projFilter !== 'all') parts.push('project_id=' + projFilter);
    if (envFilter !== 'all') parts.push('env=' + encodeURIComponent(envFilter));
    if (svcFilter !== 'all') parts.push('service=' + encodeURIComponent(svcFilter));
    return parts.length ? '&' + parts.join('&') : '';
  }

  async function loadAlertCounts(){
    try {
      const qs = _filterQs();
      const [f, a, r] = await Promise.all([
        api('/api/alerts?status=firing&page_size=1' + qs).catch(() => ({total:0})),
        api('/api/alerts?status=acked&page_size=1' + qs).catch(() => ({total:0})),
        api('/api/alerts?status=resolved&page_size=1' + qs).catch(() => ({total:0})),
      ]);
      if (destroyed) return;
      firingCount = f.total || 0;
      ackedCount = a.total || 0;
      resolvedCount = r.total || 0;
      updateStats();
      updateAlertBadge(firingCount);
    } catch(e) {
      console.warn('loadAlertCounts failed', e);
    }
  }

  function updateStats(){
    const fn = document.getElementById('statFiring');
    const an = document.getElementById('statAcked');
    const rn = document.getElementById('statResolved');
    if (fn) fn.textContent = firingCount;
    if (an) an.textContent = ackedCount;
    if (rn) rn.textContent = resolvedCount;
  }

  // ---- 渲染 ----

  function renderPage(){
    const root = document.getElementById('pageRoot');
    root.innerHTML = `
      <div class="alert-tabs-bar">
        <button class="tab-btn ${currentTab==='alerts'?'active':''}" data-tab="alerts">🔔 告警列表</button>
        <button class="tab-btn ${currentTab==='rules'?'active':''}" data-tab="rules">📏 规则管理</button>
        <button class="tab-btn ${currentTab==='silences'?'active':''}" data-tab="silences">🔕 静默窗口</button>
        <button class="tab-btn ${currentTab==='stats'?'active':''}" data-tab="stats">📊 历史统计</button>
      </div>
      <div id="tabContent"></div>`;
    root.querySelectorAll('.tab-btn[data-tab]').forEach(btn => {
      btn.onclick = () => switchTab(btn.dataset.tab);
    });
    if (currentTab === 'alerts') renderAlertsTab();
    else if (currentTab === 'rules') renderRulesTab();
    else if (currentTab === 'silences') renderSilencesTab();
    else if (currentTab === 'stats') renderStatsTab();
  }

  function switchTab(tab){
    if (currentTab === tab) return;
    currentTab = tab;
    renderPage();
    if (tab === 'alerts') { loadAlertCounts(); loadAlerts(); }
    else if (tab === 'rules') { loadRules(); }
    else if (tab === 'silences') { loadSilences(); }
    else if (tab === 'stats') { loadStats(); }
  }

  // ==================== 告警列表 Tab ====================

  function renderAlertsTab(){
    const tc = document.getElementById('tabContent');
    if (!tc) return;
    tc.innerHTML = `
      <div class="alert-stats">
        <div class="alert-stat firing"><div class="as-num" id="statFiring">0</div><div class="as-label">活跃</div></div>
        <div class="alert-stat acked"><div class="as-num" id="statAcked">0</div><div class="as-label">已确认</div></div>
        <div class="alert-stat resolved"><div class="as-num" id="statResolved">0</div><div class="as-label">已解决</div></div>
      </div>
      <div class="alert-toolbar deploy-toolbar">
        <span class="weak" style="font-size:12px">项目</span>
        <select class="filter-sel" id="alertProjSel"><option value="all">全部项目</option></select>
        <span class="weak" style="font-size:12px;margin-left:6px">环境</span>
        <select class="filter-sel" id="alertEnvSel"><option value="all">全部环境</option></select>
        <span class="weak" style="font-size:12px;margin-left:6px">服务</span>
        <select class="filter-sel" id="alertSvcSel"><option value="all">全部服务</option></select>
        <span style="width:1px;height:20px;background:var(--border);margin:0 4px"></span>
        <span class="weak" style="font-size:12px">级别</span>
        ${SEVERITIES.map(([k, v]) =>
          `<span class="chip ${k === sevFilter ? 'active' : ''}" data-sev="${k}">${v}</span>`).join('')}
        <span style="width:1px;height:20px;background:var(--border);margin:0 4px"></span>
        <span class="weak" style="font-size:12px">状态</span>
        ${STATUSES.map(([k, v]) =>
          `<span class="chip ${k === statusFilter ? 'active' : ''}" data-st="${k}">${v}</span>`).join('')}
        <span style="flex:1"></span>
        <input class="alert-search" type="text" placeholder="搜索告警..." value="${esc(keyword)}" id="alertKeyword">
      </div>
      <div id="alertBatchBar" class="alert-batch-bar" style="display:none">
        <label class="alert-batch-check"><input type="checkbox" id="alertSelectAll"> 全选</label>
        <span id="alertBatchCount" class="muted" style="font-size:12px"></span>
        <span style="flex:1"></span>
        <button class="btn sm" id="alertBatchAck">批量确认</button>
        <button class="btn sm danger" id="alertBatchResolve">批量解决</button>
        <button class="btn sm" id="alertBatchReopen" style="margin-left:4px">批量重新打开</button>
        <button class="btn sm danger" id="alertBatchDelete" style="margin-left:4px">批量删除</button>
      </div>
      <div id="alertList">${skeletonHTML(4)}</div>
      <div id="alertPagination"></div>`;
    bindToolbar();
    bindBatchOps();
    // 加载项目 + 筛选选项
    loadProjectsForFilter();
    loadFilterOptions();
  }

  async function loadFilterOptions(){
    try {
      filterOptions = await api('/api/alerts/filter-options').catch(() => ({}));
      populateEnvOptions();
      populateSvcOptions();
    } catch(e) { /* 不阻塞 */ }
  }

  function _projCode(){
    if (projFilter === 'all') return null;
    const p = projects.find(p => String(p.id) === String(projFilter));
    return p ? p.code : null;
  }

  function populateEnvOptions(){
    const sel = document.getElementById('alertEnvSel');
    if (!sel) return;
    const pc = _projCode();
    let envs = new Set();
    if (pc && filterOptions[pc]) {
      filterOptions[pc].envs.forEach(e => envs.add(e));
    } else if (!pc) {
      Object.values(filterOptions).forEach(d => d.envs.forEach(e => envs.add(e)));
    }
    sel.innerHTML = '<option value="all">全部环境</option>' +
      [...envs].sort().map(e => `<option value="${esc(e)}">${esc(e)}</option>`).join('');
    sel.value = envs.has(envFilter) ? envFilter : 'all';
  }

  function populateSvcOptions(){
    const sel = document.getElementById('alertSvcSel');
    if (!sel) return;
    const pc = _projCode();
    let svcs = new Set();
    if (pc && filterOptions[pc]) {
      filterOptions[pc].services.forEach(s => svcs.add(s));
    } else if (!pc) {
      Object.values(filterOptions).forEach(d => d.services.forEach(s => svcs.add(s)));
    }
    // 如果已选环境，进一步限定服务列表
    if (envFilter !== 'all') {
      const filtered = new Set();
      alerts.forEach(a => {
        const ti = a.target_info || {};
        if (ti.env === envFilter && ti.service) filtered.add(ti.service);
      });
      svcs = new Set([...svcs].filter(s => filtered.has(s)));
    }
    sel.innerHTML = '<option value="all">全部服务</option>' +
      [...svcs].sort().map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
    sel.value = svcs.has(svcFilter) ? svcFilter : 'all';
  }

  async function loadProjectsForFilter(){
    try {
      projects = await api('/api/projects').catch(() => []);
      const sel = document.getElementById('alertProjSel');
      if (!sel) return;
      sel.innerHTML = '<option value="all">全部项目</option>' +
        projects.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('');
      sel.value = projFilter;
      sel.onchange = () => {
        projFilter = sel.value;
        envFilter = 'all';
        svcFilter = 'all';
        currentPage = 1;
        selectedIds.clear();
        populateEnvOptions();
        populateSvcOptions();
        loadAlerts();
        loadAlertCounts();
      };
    } catch(e) { /* 项目加载失败不阻塞 */ }
  }

  function bindToolbar(){
    const root = document.getElementById('pageRoot');
    // 环境筛选
    const envSel = document.getElementById('alertEnvSel');
    if (envSel) envSel.onchange = () => {
      envFilter = envSel.value;
      svcFilter = 'all';
      currentPage = 1;
      selectedIds.clear();
      populateSvcOptions();
      loadAlerts();
      loadAlertCounts();
    };
    // 服务筛选
    const svcSel = document.getElementById('alertSvcSel');
    if (svcSel) svcSel.onchange = () => {
      svcFilter = svcSel.value;
      currentPage = 1;
      selectedIds.clear();
      loadAlerts();
      loadAlertCounts();
    };
    // 级别筛选
    root.querySelectorAll('.chip[data-sev]').forEach(c => {
      c.onclick = () => {
        sevFilter = sevFilter === c.dataset.sev ? 'all' : c.dataset.sev;
        currentPage = 1;
        selectedIds.clear();
        root.querySelectorAll('.chip[data-sev]').forEach(x =>
          x.classList.toggle('active', x.dataset.sev === sevFilter));
        loadAlerts();
      };
    });
    // 状态筛选
    root.querySelectorAll('.chip[data-st]').forEach(c => {
      c.onclick = () => {
        statusFilter = statusFilter === c.dataset.st ? 'all' : c.dataset.st;
        currentPage = 1;
        selectedIds.clear();
        root.querySelectorAll('.chip[data-st]').forEach(x =>
          x.classList.toggle('active', x.dataset.st === statusFilter));
        loadAlerts();
      };
    });
    // 关键词搜索（300ms 防抖）
    const inp = document.getElementById('alertKeyword');
    if (inp) {
      inp.oninput = () => {
        if (searchTimer) clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
          keyword = inp.value.trim();
          currentPage = 1;
          selectedIds.clear();
          loadAlerts();
        }, 300);
      };
    }
  }

  function bindBatchOps(){
    const selAll = document.getElementById('alertSelectAll');
    if (selAll) selAll.onchange = () => {
      if (selAll.checked) {
        alerts.forEach(a => selectedIds.add(a.id));
      } else {
        selectedIds.clear();
      }
      renderList();
      updateBatchBar();
    };
    const batchAck = document.getElementById('alertBatchAck');
    if (batchAck) batchAck.onclick = () => batchAction('ack');
    const batchResolve = document.getElementById('alertBatchResolve');
    if (batchResolve) batchResolve.onclick = () => batchAction('resolve');
    const batchDelete = document.getElementById('alertBatchDelete');
    if (batchDelete) batchDelete.onclick = () => batchAction('delete');
    const batchReopen = document.getElementById('alertBatchReopen');
    if (batchReopen) batchReopen.onclick = () => batchAction('reopen');
  }

  function updateBatchBar(){
    const bar = document.getElementById('alertBatchBar');
    const cnt = document.getElementById('alertBatchCount');
    if (!bar) return;
    const canManage = canManageAlerts();
    const hasActions = canManage && alerts.some(a => true);
    bar.style.display = (canManage && hasActions) ? 'flex' : 'none';
    if (cnt) cnt.textContent = selectedIds.size > 0 ? `已选 ${selectedIds.size} 条` : '';
    const selAll = document.getElementById('alertSelectAll');
    if (selAll) {
      const actionable = alerts.filter(a => true);
      selAll.checked = actionable.length > 0 && actionable.every(a => selectedIds.has(a.id));
    }
  }

  async function batchAction(action){
    if (selectedIds.size === 0) { toastError('请先选择告警'); return; }
    const label = action === 'ack' ? '确认' : action === 'resolve' ? '解决' : action === 'reopen' ? '重新打开' : '删除';
    if (!await confirmDialog(`确认批量${label} ${selectedIds.size} 条告警？`)) return;
    try {
      const resp = await api('/api/alerts/batch', {
        method: 'POST',
        body: JSON.stringify({ action, ids: [...selectedIds] })
      });
      toastSuccess(`已${label} ${resp.success || 0} 条告警`);
      selectedIds.clear();
      loadAlerts();
      loadAlertCounts();
    } catch(e) {
      toastError(e.message || '操作失败');
    }
  }

  function renderList(){
    const box = document.getElementById('alertList');
    if (!box) return;
    if (!alerts.length) {
      box.innerHTML = `<div class="panel">${emptyState('🔕', '暂无告警')}</div>`;
      renderPagination();
      updateBatchBar();
      return;
    }
    const canManage = canManageAlerts();
    box.innerHTML = alerts.map(a => {
      const target = parseTarget(a.target_info);
      const sevCls = (a.severity || 'P2').toLowerCase();
      const stLabel = ST_LABEL[a.status] || esc(a.status);
      const stCls = a.status === 'firing' ? 'running'
                  : a.status === 'acked' ? 'warning'
                  : 'success';
      const notifyCount = a.notify_count || 0;
      const canSelect = canManage;
      const checked = selectedIds.has(a.id) ? 'checked' : '';
      const canDelete = a.status === 'acked' || a.status === 'resolved';
      return `
        <div class="alert-row ${esc(a.status)}" data-id="${a.id}">
          ${canSelect ? `<label class="alert-row-check"><input type="checkbox" data-aid="${a.id}" ${checked}></label>` : '<span style="width:20px"></span>'}
          <span class="badge ${sevCls}">${esc(a.severity || 'P2')}</span>
          ${notifyCount > 0 ? `<span class="notify-count" title="通知次数">×${notifyCount}</span>` : ''}
          <div class="ar-main" data-detail="${a.id}" style="cursor:pointer">
            <div class="ar-title">${esc(a.subject)}</div>
            <div class="ar-detail">${esc(a.description || '')}${target ? ' · ' + esc(target) : ''}</div>
          </div>
          <span class="badge ${stCls}" style="margin-right:8px">${stLabel}</span>
          <span class="ar-time" title="首次出现">${fmtTime(a.first_seen_at)}</span>
          <span class="ar-time" title="最近出现" style="margin-left:4px">${fmtTime(a.last_seen_at)}</span>
          <button class="btn sm ghost" data-act="detail" style="margin-left:8px">详情</button>
          ${canManage && a.status === 'firing'
            ? '<button class="btn sm primary" data-act="ack" style="margin-left:4px">确认</button>'
            : ''}
          ${canManage && a.status !== 'resolved'
            ? '<button class="btn sm danger" data-act="resolve" style="margin-left:4px">解决</button>'
            : ''}
          ${canManage && a.status === 'resolved'
            ? '<button class="btn sm" data-act="reopen" style="margin-left:4px">重新打开</button>'
            : ''}
          ${canManage && canDelete
            ? '<button class="btn sm danger" data-act="delete" style="margin-left:4px">删除</button>'
            : ''}
          ${canManage && a.status === 'firing'
            ? '<button class="btn sm ghost" data-act="silence" style="margin-left:4px">静默 24h</button>'
            : ''}
        </div>`;
    }).join('');
    // 绑定操作按钮（事件委托）
    box.onclick = (e) => {
      // checkbox 变化
      const chk = e.target.closest('[data-aid]');
      if (chk) {
        const aid = Number(chk.dataset.aid);
        if (chk.checked) selectedIds.add(aid); else selectedIds.delete(aid);
        updateBatchBar();
        return;
      }
      // 详情点击
      const detailArea = e.target.closest('[data-detail]');
      if (detailArea) {
        openAlertDetail(Number(detailArea.dataset.detail));
        return;
      }
      const btn = e.target.closest('[data-act]');
      if (!btn) return;
      const row = btn.closest('.alert-row');
      if (!row) return;
      const id = Number(row.dataset.id);
      if (btn.dataset.act === 'ack') ackAlert(id);
      else if (btn.dataset.act === 'resolve') resolveAlert(id);
      else if (btn.dataset.act === 'reopen') reopenAlert(id);
      else if (btn.dataset.act === 'delete') deleteAlert(id);
      else if (btn.dataset.act === 'silence') silenceAlert(id);
      else if (btn.dataset.act === 'detail') openAlertDetail(id);
    };
    renderPagination();
    updateBatchBar();
  }

  function renderPagination(){
    const el = document.getElementById('alertPagination');
    if (!el) return;
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    if (total <= pageSize) { el.innerHTML = ''; return; }
    el.innerHTML = `
      <div class="alert-pagination">
        <button class="btn sm" id="pgPrev" ${currentPage <= 1 ? 'disabled' : ''}>上一页</button>
        <span class="pg-info">${currentPage} / ${totalPages}</span>
        <button class="btn sm" id="pgNext" ${currentPage >= totalPages ? 'disabled' : ''}>下一页</button>
      </div>`;
    const prev = document.getElementById('pgPrev');
    const next = document.getElementById('pgNext');
    if (prev) prev.onclick = () => { if (currentPage > 1) { currentPage--; loadAlerts(); } };
    if (next) next.onclick = () => { if (currentPage < totalPages) { currentPage++; loadAlerts(); } };
  }

  // ---- 操作 ----

  async function ackAlert(id){
    if (!await confirmDialog('确认将此告警标记为已确认？')) return;
    try {
      await api(`/api/alerts/${id}/ack`, {method:'POST'});
      toastSuccess('已确认');
      loadAlerts();
      loadAlertCounts();
    } catch(e) {
      toastError(e.message || '操作失败');
    }
  }

  async function resolveAlert(id){
    if (!await confirmDialog('确认将此告警标记为已解决？')) return;
    try {
      await api(`/api/alerts/${id}/resolve`, {method:'POST'});
      toastSuccess('已解决');
      loadAlerts();
      loadAlertCounts();
    } catch(e) {
      toastError(e.message || '操作失败');
    }
  }

  async function reopenAlert(id){
    if (!await confirmDialog('确认将此告警重新打开为活跃状态？')) return;
    try {
      await api(`/api/alerts/${id}/reopen`, {method:'POST'});
      toastSuccess('已重新打开');
      loadAlerts();
      loadAlertCounts();
    } catch(e) {
      toastError(e.message || '操作失败');
    }
  }

  async function deleteAlert(id){
    if (!await confirmDialog('确认删除此告警？删除后不可恢复。')) return;
    try {
      await api(`/api/alerts/${id}/delete`, {method:'DELETE'});
      toastSuccess('已删除');
      loadAlerts();
      loadAlertCounts();
    } catch(e) {
      toastError(e.message || '删除失败');
    }
  }

  async function silenceAlert(id){
    if (!await confirmDialog('确认将此告警静默 24 小时？')) return;
    try {
      const now = Math.floor(Date.now() / 1000);
      const endAt = now + 24 * 60 * 60;  // 24 小时后
      const alert = alerts.find(a => a.id === id);
      const scope = alert ? {rules: [alert.rule_id], envs: [], servers: []} : {rules: [], envs: [], servers: []};
      await api('/api/alerts/silences', {
        method: 'POST',
        body: JSON.stringify({
          name: `告警 #${id} 静默 24h`,
          type: 'one_time',
          start_at: now,
          end_at: endAt,
          scope: scope,
          enabled: true,
          remark: '快捷静默操作'
        })
      });
      toastSuccess('已静默 24 小时');
      loadAlerts();
    } catch(e) {
      toastError(e.message || '操作失败');
    }
  }

  // ==================== 历史统计 Tab ====================

  async function loadStats(){
    const container = document.getElementById('statsChartWrap');
    try {
      const data = await api('/api/alerts/stats?days=' + statsDays);
      if (destroyed) return;
      renderStatsChart(data);
    } catch(e) {
      console.warn('loadStats failed', e);
      if (container) {
        container.innerHTML = `<div class="panel"><div class="error-state"><div class="e-ico">⚠️</div>
          <div class="muted">统计数据加载失败</div>
          <button class="btn sm e-btn">重试</button></div></div>`;
        const btn = container.querySelector('.e-btn');
        if (btn) btn.addEventListener('click', loadStats);
      }
    }
  }

  function renderStatsTab(){
    const tc = document.getElementById('tabContent');
    if (!tc) return;
    tc.innerHTML = `
      <div class="alert-toolbar">
        <span class="weak" style="font-size:12px">时间范围</span>
        <select class="filter-sel" id="statsDaysSel">
          <option value="7" ${statsDays===7?'selected':''}>近 7 天</option>
          <option value="14" ${statsDays===14?'selected':''}>近 14 天</option>
          <option value="30" ${statsDays===30?'selected':''}>近 30 天</option>
        </select>
      </div>
      <div id="statsChartWrap">${skeletonHTML(3)}</div>`;
    const sel = document.getElementById('statsDaysSel');
    if (sel) {
      sel.onchange = () => {
        statsDays = Number(sel.value);
        loadStats();
      };
    }
  }

  function renderStatsChart(data){
    const wrap = document.getElementById('statsChartWrap');
    if (!wrap) return;
    // 检查是否有数据
    const hasData = data.some(d => (d.P0||0)+(d.P1||0)+(d.P2||0)+(d.P3||0) > 0);
    if (!hasData) {
      wrap.innerHTML = `<div class="panel">${emptyState('📊', '所选时间范围内暂无告警数据')}</div>`;
      return;
    }
    // 释放旧实例
    if (statsChart) { try{ statsChart.dispose(); }catch(e){} statsChart = null; }
    // 创建图表容器
    wrap.innerHTML = '<div id="statsChart" style="width:100%;height:400px"></div>';
    const dom = document.getElementById('statsChart');
    if (!dom || !window.echarts) {
      if (dom) dom.innerHTML = '<div class="panel"><div class="muted" style="text-align:center;padding:40px">图表库未加载</div></div>';
      return;
    }
    statsChart = echarts.init(dom);
    const dates = data.map(d => d.date);
    const sevColors = {P0:'#FF4560', P1:'#FF7A45', P2:'#FEB019', P3:'#00D4FF'};
    const series = ['P0','P1','P2','P3'].map(sev => ({
      name: sev,
      type: 'bar',
      stack: 'total',
      barMaxWidth: 32,
      itemStyle: {color: sevColors[sev], borderRadius: sev === 'P3' ? [3,3,0,0] : 0},
      emphasis: {focus: 'series'},
      data: data.map(d => d[sev] || 0)
    }));
    statsChart.setOption({
      tooltip: {
        trigger: 'axis',
        axisPointer: {type: 'shadow'},
        backgroundColor: 'rgba(13,27,51,.92)',
        borderColor: 'rgba(0,212,255,.25)',
        textStyle: {color: '#C8D6E5', fontSize: 12}
      },
      legend: {
        data: ['P0','P1','P2','P3'],
        textStyle: {color: '#8FA8C8'},
        top: 0,
        itemGap: 20
      },
      grid: {left: '3%', right: '4%', bottom: '3%', top: 40, containLabel: true},
      xAxis: {
        type: 'category',
        data: dates,
        axisLine: {lineStyle: {color: 'rgba(0,212,255,.2)'}},
        axisLabel: {color: '#8FA8C8', fontSize: 11},
        axisTick: {show: false}
      },
      yAxis: {
        type: 'value',
        minInterval: 1,
        axisLine: {show: false},
        axisLabel: {color: '#8FA8C8', fontSize: 11},
        splitLine: {lineStyle: {color: 'rgba(0,212,255,.08)'}
        }
      },
      series: series
    });
    // resize 监听
    if (statsResizeHandler) window.removeEventListener('resize', statsResizeHandler);
    statsResizeHandler = () => { if (statsChart) statsChart.resize(); };
    window.addEventListener('resize', statsResizeHandler);
  }

  // ==================== 规则管理 Tab ====================

  async function loadRules(){
    try {
      rules = await api('/api/alerts/rules');
      if (destroyed) return;
      scopeNameCache = null;  // 清除缓存，renderRuleList 会重新加载
      renderRuleList();
    } catch(e) {
      console.warn('loadRules failed', e);
      const box = document.getElementById('ruleListContainer');
      if (box) {
        box.innerHTML = `<div class="panel"><div class="error-state"><div class="e-ico">⚠️</div>
          <div class="muted">规则加载失败</div>
          <button class="btn sm e-btn">重试</button></div></div>`;
        box.querySelector('.e-btn')?.addEventListener('click', loadRules);
      }
    }
  }

  function renderRulesTab(){
    const tc = document.getElementById('tabContent');
    if (!tc) return;
    tc.innerHTML = `
      <div class="alert-toolbar">
        <span class="weak" style="font-size:12px">类型</span>
        ${METRIC_TYPES.map(([k,v]) =>
          `<span class="chip ${k === ruleTypeFilter ? 'active' : ''}" data-rt="${k}">${v}</span>`).join('')}
        <span style="flex:1"></span>
        <button class="btn primary sm" id="btnNewRule">＋ 新建规则</button>
      </div>
      <div id="ruleListContainer">${skeletonHTML(3)}</div>`;
    // 类型筛选
    tc.querySelectorAll('.chip[data-rt]').forEach(c => {
      c.onclick = () => {
        ruleTypeFilter = ruleTypeFilter === c.dataset.rt ? 'all' : c.dataset.rt;
        tc.querySelectorAll('.chip[data-rt]').forEach(x =>
          x.classList.toggle('active', x.dataset.rt === ruleTypeFilter));
        renderRuleList();
      };
    });
    document.getElementById('btnNewRule').onclick = () => openRuleDrawer(null);
    loadRules();
  }

  async function renderRuleList(){
    const box = document.getElementById('ruleListContainer');
    if (!box) return;
    const names = await ensureScopeNames();
    if (destroyed) return;
    const filtered = ruleTypeFilter === 'all' ? rules : rules.filter(r => r.metric_type === ruleTypeFilter);
    if (!filtered.length) {
      box.innerHTML = `<div class="panel">${emptyState('📏', '暂无规则')}</div>`;
      return;
    }
    box.innerHTML = `
      <div class="panel" style="padding:0"><div class="tbl-wrap"><table class="tbl rules-tbl">
        <thead><tr><th>名称</th><th>类型</th><th>指标</th><th>条件</th><th>作用域</th><th>级别</th><th>渠道</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>${filtered.map(r => {
          const scopeStr = scopeSummary(r.scope, names);
          const chList = (r.channels || []).map(c => CHANNELS.find(([k]) => k === c)?.[1] || c);
          let cond = '-';
          if (r.metric_type === 'metric') cond = `${CMP_LABEL[r.compare] || r.compare} ${r.threshold}`;
          else if (r.metric_type === 'log_keyword') {
            const kws = (r.metric || '').split(',').map(k => k.trim()).filter(Boolean);
            cond = kws.length ? kws.map(k => `<span class="rule-kw-tag">${esc(k)}</span>`).join('') : '-';
            const exKws = (r.exclude_keyword || '').split(',').map(k => k.trim()).filter(Boolean);
            if (exKws.length) cond += `<div class="rule-sub">排除: ${exKws.map(k => `<span class="rule-kw-tag rule-kw-exclude">${esc(k)}</span>`).join('')}</div>`;
          }
          return `<tr>
            <td><b>${esc(r.name)}</b>${r.remark ? `<div class="rule-remark">${esc(r.remark)}</div>` : ''}</td>
            <td><span class="rule-mt-badge mt-${esc(r.metric_type)}">${MT_LABEL[r.metric_type] || r.metric_type}</span></td>
            <td>${esc(r.metric || '-')}</td>
            <td class="num">${cond}${r.duration_seconds ? `<div class="rule-sub">持续 ${r.duration_seconds}s</div>` : ''}</td>
            <td><span class="rule-scope-summary" title="${esc(scopeStr)}">${esc(scopeStr)}</span></td>
            <td><span class="badge ${esc(r.severity||'P2').toLowerCase()}">${esc(r.severity||'P2')}</span></td>
            <td><div class="rule-ch-list">${chList.map(c => `<span class="rule-ch-tag">${esc(c)}</span>`).join('')}</div></td>
            <td><span class="badge ${r.enabled ? 'success' : 'pending'}">${r.enabled ? '启用' : '停用'}</span></td>
            <td class="rule-actions">
              <button class="btn sm" data-ra="edit" data-rid="${r.id}">编辑</button>
              <button class="btn sm" data-ra="toggle" data-rid="${r.id}">${r.enabled ? '停用' : '启用'}</button>
              <button class="btn sm danger" data-ra="del" data-rid="${r.id}">删除</button>
            </td>
          </tr>`;
        }).join('')}</tbody>
      </table></div></div>`;
    // 事件委托
    box.onclick = async (e) => {
      const btn = e.target.closest('[data-ra]');
      if (!btn) return;
      const id = Number(btn.dataset.rid);
      if (btn.dataset.ra === 'edit') { const r = rules.find(x => x.id === id); if (r) openRuleDrawer(r); }
      else if (btn.dataset.ra === 'toggle') await toggleRule(id);
      else if (btn.dataset.ra === 'del') await deleteRule(id);
    };
  }

  function openRuleDrawer(rule){
    editingRuleId = rule ? rule.id : null;
    scopeSelectedProjects.clear();
    scopeSelectedEnvs.clear();
    scopeSelectedTargets.clear();
    const title = rule ? '编辑规则' : '新建规则';
    const r = rule || {};
    const scope = parseScope(r.scope);
    // 回填已保存的作用域选择数据
    if (scope.type === 'project') {
      (scope.ids || []).forEach(id => scopeSelectedProjects.add(id));
    } else if (scope.type === 'env') {
      (scope.ids || []).forEach(id => scopeSelectedEnvs.add(id));
    } else if (scope.type === 'server' || scope.type === 'service') {
      (scope.env_ids || []).forEach(id => scopeSelectedEnvs.add(id));
      (scope.ids || []).forEach(id => scopeSelectedTargets.add(id));
    }
    const channels = r.channels || [];
    const bodyHTML = `
      <div class="form-grid" id="ruleForm">
        <div class="field"><label>名称 *</label><input id="rfName" value="${esc(r.name||'')}" placeholder="规则名称"></div>
        <div class="field"><label>类型</label>
          <select id="rfMetricType">${METRIC_TYPES.map(([k,v]) =>
            `<option value="${k}" ${(r.metric_type||'metric')===k?'selected':''}>${v}</option>`).join('')}</select></div>
        <div class="field" id="rfMetricWrap"><label>指标</label>
          <select id="rfMetric"></select></div>
        <div class="field" id="rfCompareWrap"><label>比较符</label>
          <select id="rfCompare">${COMPARE_OPS.map(([k,v]) =>
            `<option value="${k}" ${(r.compare||'>')===k?'selected':''}>${v}</option>`).join('')}</select></div>
        <div class="field" id="rfThresholdWrap"><label>阈值</label>
          <input id="rfThreshold" type="number" value="${r.threshold ?? 0}" step="0.1"></div>
        <div class="field"><label>持续时间（秒）</label>
          <input id="rfDuration" type="number" value="${r.duration_seconds ?? 300}" min="0"></div>
        <div class="field"><label>级别</label>
          <select id="rfSeverity">${SEVERITIES.map(([k,v]) =>
            `<option value="${k}" ${(r.severity||'P2')===k?'selected':''}>${v}</option>`).join('')}</select></div>
        <div class="field"><label>作用域</label>
          <select id="rfScopeType"></select></div>
        <div class="field full" id="rfScopePickWrap" style="display:none">
          <label id="rfScopePickLabel">选择目标</label>
          <div id="rfScopePickTags" class="scope-tag-box"></div>
          <div id="rfScopeTargetWrap" style="margin-top:8px;display:none">
            <label id="rfScopeTargetLabel">目标</label>
            <div id="rfScopeTargetTags" class="scope-tag-box">
              <div class="muted" style="font-size:12px">请先选择环境</div>
            </div>
          </div>
        </div>
        <div class="field"><label>升级间隔（分钟）</label>
          <input id="rfEscInt" type="number" value="${r.escalation_interval ?? 30}" min="0"></div>
        <div class="field check"><input type="checkbox" id="rfEnabled" ${r.enabled !== 0 ? 'checked' : ''}>
          <label for="rfEnabled" style="margin:0">启用</label></div>
        <div class="field full"><label>通知渠道</label>
          <div class="rule-ch-box" id="rfChannels">${CHANNELS.map(([k,v]) =>
            `<label class="rule-ch-cb"><input type="checkbox" value="${k}" ${channels.includes(k)?'checked':''}> ${v}</label>`
          ).join('')}</div></div>
        <div class="field full" id="rfLogKwWrap" style="display:none"><label>日志关键词</label>
          <input id="rfLogKw" value="${esc(r.metric_type==='log_keyword' ? (r.metric||'') : '')}" placeholder="多个关键词用逗号分隔，如 ERROR,FATAL,Exception">
          <div class="field-hint">采集器每轮扫描日志，命中任一关键词即触发告警；同一规则+服务由指纹去重不会重复告警</div></div>
        <div class="field full" id="rfExKwWrap" style="display:none"><label>排除关键词</label>
          <input id="rfExKw" value="${esc(r.metric_type==='log_keyword' ? (r.exclude_keyword||'') : '')}" placeholder="多个关键词用逗号分隔，如 DEBUG,heartbeat">
          <div class="field-hint">命中包含关键词后，若同一行也命中任一排除关键词，则不触发告警</div></div>
        <div class="field full"><label>备注</label>
          <textarea id="rfRemark" rows="2">${esc(r.remark||'')}</textarea></div>
        <div class="full" style="display:flex;gap:10px;justify-content:flex-end;margin-top:8px">
          <button class="btn" id="rfCancel">取消</button>
          <button class="btn primary" id="rfSave">保存</button>
        </div>
      </div>`;
    const drawerBody = openDrawer({title, body: bodyHTML, wide: true});
    // 指标选项联动
    const selType = document.getElementById('rfMetricType');
    const selMetric = document.getElementById('rfMetric');
    function updateMetricOpts(){
      const t = selType.value;
      const opts = METRIC_OPTIONS[t] || [];
      selMetric.innerHTML = opts.map(([k,v]) =>
        `<option value="${k}" ${r.metric===k?'selected':''}>${v}</option>`).join('');
      if (!selMetric.value && opts.length) selMetric.value = opts[0][0];
    }
    function updateFieldVis(){
      const t = selType.value;
      const isMetric = t === 'metric';
      const isLog = t === 'log_keyword';
      document.getElementById('rfCompareWrap').style.display = isMetric ? '' : 'none';
      document.getElementById('rfThresholdWrap').style.display = isMetric ? '' : 'none';
      document.getElementById('rfMetricWrap').style.display = isLog ? 'none' : '';
      document.getElementById('rfLogKwWrap').style.display = isLog ? '' : 'none';
      document.getElementById('rfExKwWrap').style.display = isLog ? '' : 'none';
    }
    // ---- 作用域选择（标签点选 + 服务器/服务级联） ----
    const selScope = document.getElementById('rfScopeType');
    const scopePickWrap = document.getElementById('rfScopePickWrap');
    const scopePickLabel = document.getElementById('rfScopePickLabel');
    const scopePickTags = document.getElementById('rfScopePickTags');
    const targetWrap = document.getElementById('rfScopeTargetWrap');
    const targetTagsEl = document.getElementById('rfScopeTargetTags');
    const targetLabel = document.getElementById('rfScopeTargetLabel');
    let scopeEnvs = [];

    // 根据规则类型筛选可选 scope type
    function updateScopeTypeOpts() {
      const mt = selType.value;
      const opts = SCOPE_OPTIONS_BY_TYPE[mt] || SCOPE_OPTIONS_BY_TYPE.metric;
      const curVal = selScope.value || scope.type || 'all';
      selScope.innerHTML = opts.map(([k,v]) =>
        `<option value="${k}" ${curVal===k?'selected':''}>${v}</option>`).join('');
      if (![...opts.map(o=>o[0])].includes(selScope.value)) selScope.value = 'all';
    }

    // 第一级标签：项目（project 类型）或 环境（env/server/service 类型）
    function renderScopePick() {
      const st = selScope.value;
      if (!scopeEnvs.length) {
        scopePickTags.innerHTML = '<div class="muted" style="font-size:12px">加载中...</div>';
        return;
      }
      if (st === 'project') {
        // 从环境列表按 project_id 分组去重出项目列表
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
            // 服务器/服务：选中环境后加载目标列表
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

    // 绑定事件：规则类型变更 → 更新指标选项 + scope 选项 + 可见性
    selType.onchange = () => { updateMetricOpts(); updateScopeTypeOpts(); updateFieldVis(); updateScopeVis(); };
    // 绑定事件：作用域类型变更 → 清除已选 + 更新选择区可见性
    selScope.onchange = () => {
      scopeSelectedProjects.clear();
      scopeSelectedTargets.clear();  // 切换类型时清除已选（不同类型 ID 不通用）
      updateScopeVis();
    };
    // 初始化
    updateMetricOpts();
    // 回填 metric（编辑时）
    if (r.metric && (r.metric_type !== 'log_keyword')) selMetric.value = r.metric;
    updateScopeTypeOpts();
    updateFieldVis();
    updateScopeVis();
    loadScopeEnvs();  // 统一预加载环境列表，供项目/环境/服务器/服务共用
    // 按钮
    document.getElementById('rfCancel').onclick = () => {
      const c = document.getElementById('dwClose');
      if (c) c.click();
    };
    document.getElementById('rfSave').onclick = () => saveRule();
  }

  async function saveRule(){
    if (ruleSaving) return;
    ruleSaving = true;
    const saveBtn = document.getElementById('rfSave');
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = '保存中…'; }
    try {
      const name = document.getElementById('rfName').value.trim();
      if (!name) { toastError('规则名称必填'); return; }
      const metricType = document.getElementById('rfMetricType').value;
      const metric = metricType === 'log_keyword'
        ? document.getElementById('rfLogKw').value.trim()
        : document.getElementById('rfMetric').value;
      const excludeKeyword = metricType === 'log_keyword'
        ? document.getElementById('rfExKw').value.trim()
        : '';
      const compare = document.getElementById('rfCompare').value;
      const threshold = parseFloat(document.getElementById('rfThreshold').value) || 0;
      const duration = parseInt(document.getElementById('rfDuration').value) || 300;
      const severity = document.getElementById('rfSeverity').value;
      const scopeType = document.getElementById('rfScopeType').value;
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
      const channels = [...document.querySelectorAll('#rfChannels input:checked')].map(c => c.value);
      const escalationInterval = parseInt(document.getElementById('rfEscInt').value) || 30;
      const enabled = document.getElementById('rfEnabled').checked;
      const remark = document.getElementById('rfRemark').value.trim();
      const payload = {name, metric_type: metricType, metric, compare, threshold,
        duration_seconds: duration, scope, severity, channels, escalation_interval: escalationInterval,
        exclude_keyword: excludeKeyword, enabled, remark};
      if (editingRuleId) {
        await api(`/api/alerts/rules/${editingRuleId}`, {method:'PUT', body: JSON.stringify(payload)});
        toastSuccess('规则已更新');
      } else {
        await api('/api/alerts/rules', {method:'POST', body: JSON.stringify(payload)});
        toastSuccess('规则已创建');
      }
      const c = document.getElementById('dwClose');
      if (c) c.click();
      loadRules();
    } catch(e) {
      toastError(e.message || '保存失败');
    } finally {
      ruleSaving = false;
      if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = '保存'; }
    }
  }

  async function toggleRule(id){
    try {
      const resp = await api(`/api/alerts/rules/${id}/toggle`, {method:'POST'});
      toastSuccess(resp.enabled ? '规则已启用' : '规则已停用');
      loadRules();
    } catch(e) {
      toastError(e.message || '操作失败');
    }
  }

  async function deleteRule(id){
    if (!await confirmDialog('确认删除此规则？删除后不可恢复。')) return;
    try {
      await api(`/api/alerts/rules/${id}`, {method:'DELETE'});
      toastSuccess('规则已删除');
      loadRules();
    } catch(e) {
      toastError(e.message || '删除失败');
    }
  }

  function parseScope(raw){
    if (!raw) return {type:'all', ids:[], env_ids:[]};
    if (typeof raw === 'string') {
      try { raw = JSON.parse(raw); } catch(e) { return {type:'all', ids:[], env_ids:[]}; }
    }
    return {
      type: raw.type || 'all',
      ids: raw.ids || [],
      env_ids: raw.env_ids || []
    };
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

  // ==================== 静默窗口 Tab ====================

  async function loadSilences(){
    try {
      silences = await api('/api/alerts/silences');
      if (destroyed) return;
      renderSilenceList();
    } catch(e) {
      console.warn('loadSilences failed', e);
      const box = document.getElementById('silenceListContainer');
      if (box) {
        box.innerHTML = `<div class="panel"><div class="error-state"><div class="e-ico">⚠️</div>
          <div class="muted">静默窗口加载失败</div>
          <button class="btn sm e-btn">重试</button></div></div>`;
        box.querySelector('.e-btn')?.addEventListener('click', loadSilences);
      }
    }
  }

  function renderSilencesTab(){
    const tc = document.getElementById('tabContent');
    if (!tc) return;
    tc.innerHTML = `
      <div class="alert-toolbar">
        <span style="flex:1"></span>
        <button class="btn primary sm" id="btnNewSilence">＋ 新建窗口</button>
      </div>
      <div id="silenceListContainer">${skeletonHTML(3)}</div>`;
    document.getElementById('btnNewSilence').onclick = () => openSilenceDrawer(null);
  }

  function renderSilenceList(){
    const box = document.getElementById('silenceListContainer');
    if (!box) return;
    if (!silences.length) {
      box.innerHTML = `<div class="panel">${emptyState('🔕', '暂无静默窗口')}</div>`;
      return;
    }
    box.innerHTML = `
      <div class="panel" style="padding:0"><div class="tbl-wrap"><table class="tbl silence-tbl">
        <thead><tr><th>名称</th><th>类型</th><th>时间</th><th>周期</th><th>作用域</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>${silences.map(s => {
          const typeLabel = s.type === 'recurring' ? '周期' : '一次性';
          const typeCls = s.type === 'recurring' ? 'recurring' : 'one-time';
          let timeStr = '';
          if (s.type === 'one_time') {
            timeStr = `${fmtTime(Number(s.start_at))} ~ ${fmtTime(Number(s.end_at))}`;
          } else {
            timeStr = `${esc(s.start_at)} ~ ${esc(s.end_at)}`;
          }
          const wdStr = (s.weekdays && s.weekdays.length)
            ? s.weekdays.map(d => WEEKDAY_LABELS[d] || d).join('、')
            : '-';
          const scope = parseSilenceScope(s.scope);
          const scopeParts = [];
          if (scope.rules && scope.rules.length) scopeParts.push(`规则: ${scope.rules.length}`);
          if (scope.envs && scope.envs.length) scopeParts.push(`环境: ${scope.envs.length}`);
          if (scope.servers && scope.servers.length) scopeParts.push(`服务器: ${scope.servers.length}`);
          const scopeStr = scopeParts.length ? scopeParts.join(', ') : '全部';
          return `<tr>
            <td><b>${esc(s.name)}</b>${s.remark ? `<div class="silence-remark">${esc(s.remark)}</div>` : ''}</td>
            <td><span class="silence-type-badge ${typeCls}">${typeLabel}</span></td>
            <td class="silence-time">${timeStr}</td>
            <td>${wdStr}</td>
            <td>${esc(scopeStr)}</td>
            <td><span class="badge ${s.enabled ? 'success' : 'pending'}">${s.enabled ? '启用' : '停用'}</span></td>
            <td class="silence-actions">
              <button class="btn sm" data-sa="edit" data-sid="${s.id}">编辑</button>
              <button class="btn sm danger" data-sa="del" data-sid="${s.id}">删除</button>
            </td>
          </tr>`;
        }).join('')}</tbody>
      </table></div></div>`;
    // 事件委托
    box.onclick = async (e) => {
      const btn = e.target.closest('[data-sa]');
      if (!btn) return;
      const id = Number(btn.dataset.sid);
      if (btn.dataset.sa === 'edit') {
        const s = silences.find(x => x.id === id);
        if (s) openSilenceDrawer(s);
      } else if (btn.dataset.sa === 'del') {
        await deleteSilence(id);
      }
    };
  }

  function parseSilenceScope(raw){
    if (!raw) return {rules:[], envs:[], servers:[]};
    if (typeof raw === 'string') {
      try { raw = JSON.parse(raw); } catch(e) { return {rules:[], envs:[], servers:[]}; }
    }
    return {
      rules: raw.rules || [],
      envs: raw.envs || [],
      servers: raw.servers || []
    };
  }

  function openSilenceDrawer(silence){
    editingSilenceId = silence ? silence.id : null;
    const title = silence ? '编辑静默窗口' : '新建静默窗口';
    const s = silence || {};
    const scope = parseSilenceScope(s.scope);
    const weekdays = s.weekdays || [];
    const wtype = s.type || 'one_time';

    const bodyHTML = `
      <div class="form-grid" id="silenceForm">
        <div class="field"><label>名称 *</label><input id="sfName" value="${esc(s.name||'')}" placeholder="窗口名称"></div>
        <div class="field"><label>类型</label>
          <select id="sfType">${SILENCE_TYPES.map(([k,v]) =>
            `<option value="${k}" ${wtype===k?'selected':''}>${v}</option>`).join('')}</select></div>
        <div class="field" id="sfStartWrap"><label id="sfStartLabel">开始时间</label>
          <input id="sfStart" value="${esc(s.start_at||'')}" placeholder=""></div>
        <div class="field" id="sfEndWrap"><label id="sfEndLabel">结束时间</label>
          <input id="sfEnd" value="${esc(s.end_at||'')}" placeholder=""></div>
        <div class="field full" id="sfWeekdaysWrap" style="display:none">
          <label>周期（多选）</label>
          <div class="silence-wd-box">${[1,2,3,4,5,6,7].map(d =>
            `<label class="silence-wd-cb"><input type="checkbox" value="${d}" ${weekdays.includes(d)?'checked':''}> ${WEEKDAY_LABELS[d]}</label>`
          ).join('')}</div></div>
        <div class="field"><label>作用域 - 规则 IDs（逗号分隔）</label>
          <input id="sfScopeRules" value="${esc((scope.rules||[]).join(','))}" placeholder="1,2,3"></div>
        <div class="field"><label>作用域 - 环境 IDs（逗号分隔）</label>
          <input id="sfScopeEnvs" value="${esc((scope.envs||[]).join(','))}" placeholder="1,2,3"></div>
        <div class="field"><label>作用域 - 服务器 IDs（逗号分隔）</label>
          <input id="sfScopeServers" value="${esc((scope.servers||[]).join(','))}" placeholder="1,2,3"></div>
        <div class="field check"><input type="checkbox" id="sfEnabled" ${s.enabled !== 0 ? 'checked' : ''}>
          <label for="sfEnabled" style="margin:0">启用</label></div>
        <div class="field full"><label>备注</label>
          <textarea id="sfRemark" rows="2">${esc(s.remark||'')}</textarea></div>
        <div class="full" style="display:flex;gap:10px;justify-content:flex-end;margin-top:8px">
          <button class="btn" id="sfCancel">取消</button>
          <button class="btn primary" id="sfSave">保存</button>
        </div>
      </div>`;
    openDrawer({title, body: bodyHTML, wide: true});

    // 类型联动
    const selType = document.getElementById('sfType');
    function updateFieldVis(){
      const t = selType.value;
      const isRecurring = t === 'recurring';
      const startInput = document.getElementById('sfStart');
      const endInput = document.getElementById('sfEnd');
      const startLabel = document.getElementById('sfStartLabel');
      const endLabel = document.getElementById('sfEndLabel');
      const wdWrap = document.getElementById('sfWeekdaysWrap');
      if (isRecurring) {
        startInput.type = 'text';
        endInput.type = 'text';
        startInput.placeholder = 'HH:MM（如 08:00）';
        endInput.placeholder = 'HH:MM（如 22:00）';
        startLabel.textContent = '开始时间（HH:MM）';
        endLabel.textContent = '结束时间（HH:MM）';
        wdWrap.style.display = '';
      } else {
        startInput.type = 'number';
        endInput.type = 'number';
        startInput.placeholder = 'Epoch 秒（如 1700000000）';
        endInput.placeholder = 'Epoch 秒（如 1700100000）';
        startLabel.textContent = '开始时间（Epoch 秒）';
        endLabel.textContent = '结束时间（Epoch 秒）';
        wdWrap.style.display = 'none';
      }
    }
    selType.onchange = updateFieldVis;
    updateFieldVis();

    // 按钮
    document.getElementById('sfCancel').onclick = () => {
      const c = document.getElementById('dwClose');
      if (c) c.click();
    };
    document.getElementById('sfSave').onclick = () => saveSilence();
  }

  async function saveSilence(){
    if (silenceSaving) return;
    const saveBtn = document.getElementById('sfSave');
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = '保存中…'; }
    silenceSaving = true;
    try {
      const name = document.getElementById('sfName').value.trim();
      if (!name) { toastError('窗口名称必填'); return; }
      const wtype = document.getElementById('sfType').value;
      const startAt = document.getElementById('sfStart').value.trim();
      const endAt = document.getElementById('sfEnd').value.trim();
      if (!startAt || !endAt) { toastError('起止时间必填'); return; }

      // 校验
      if (wtype === 'one_time') {
        if (isNaN(parseFloat(startAt)) || isNaN(parseFloat(endAt))) {
          toastError('一次性窗口需输入 epoch 秒'); return;
        }
      } else {
        const hhmm = /^([01]?\d|2[0-3]):[0-5]\d$/;
        if (!hhmm.test(startAt) || !hhmm.test(endAt)) {
          toastError('周期窗口需 HH:MM 格式'); return;
        }
      }

      // weekdays
      const weekdays = wtype === 'recurring'
        ? [...document.querySelectorAll('#sfWeekdaysWrap input:checked')].map(c => Number(c.value))
        : [];

      // scope
      const parseIds = (id) => {
        const raw = document.getElementById(id).value.trim();
        return raw ? raw.split(',').map(s => s.trim()).filter(Boolean).map(Number).filter(n => !isNaN(n)) : [];
      };
      const scope = {
        rules: parseIds('sfScopeRules'),
        envs: parseIds('sfScopeEnvs'),
        servers: parseIds('sfScopeServers')
      };

      const enabled = document.getElementById('sfEnabled').checked;
      const remark = document.getElementById('sfRemark').value.trim();

      const payload = {
        name, type: wtype, start_at: startAt, end_at: endAt,
        weekdays, scope, enabled, remark
      };

      if (editingSilenceId) {
        await api(`/api/alerts/silences/${editingSilenceId}`, {method:'PUT', body: JSON.stringify(payload)});
        toastSuccess('窗口已更新');
      } else {
        await api('/api/alerts/silences', {method:'POST', body: JSON.stringify(payload)});
        toastSuccess('窗口已创建');
      }
      const c = document.getElementById('dwClose');
      if (c) c.click();
      loadSilences();
    } catch(e) {
      toastError(e.message || '保存失败');
    } finally {
      silenceSaving = false;
      const saveBtn = document.getElementById('sfSave');
      if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = '保存'; }
    }
  }

  async function deleteSilence(id){
    if (!await confirmDialog('确认删除此静默窗口？删除后不可恢复。')) return;
    try {
      await api(`/api/alerts/silences/${id}`, {method:'DELETE'});
      toastSuccess('窗口已删除');
      loadSilences();
    } catch(e) {
      toastError(e.message || '删除失败');
    }
  }

  // ==================== 告警详情抽屉 ====================

  async function openAlertDetail(alertId) {
    const alert = alerts.find(a => a.id === alertId);
    if (!alert) return;
    const target = parseTarget(alert.target_info);
    const sevCls = (alert.severity || 'P2').toLowerCase();
    const stLabel = ST_LABEL[alert.status] || alert.status;

    const bodyHTML = `
      <div class="alert-detail">
        <div class="alert-detail-head">
          <span class="badge ${sevCls}" style="font-size:14px;padding:4px 12px">${esc(alert.severity || 'P2')}</span>
          <b style="font-size:16px;margin-left:8px">${esc(alert.subject)}</b>
          <span class="badge ${alert.status === 'firing' ? 'running' : alert.status === 'acked' ? 'warning' : 'success'}" style="margin-left:12px">${stLabel}</span>
        </div>
        <div class="alert-detail-meta">
          <div><span class="muted">描述：</span>${esc(alert.description || '-')}</div>
          <div><span class="muted">目标：</span>${esc(target || '-')}</div>
          <div><span class="muted">首次出现：</span>${fmtTime(alert.first_seen_at)}</div>
          <div><span class="muted">最近出现：</span>${fmtTime(alert.last_seen_at)}</div>
          <div><span class="muted">通知次数：</span>${alert.notify_count || 0}</div>
          <div><span class="muted">升级级别：</span>L${alert.escalation_level || 0}</div>
          ${alert.ack_by ? `<div><span class="muted">确认人：</span>${esc(alert.ack_by)} ${fmtTime(alert.ack_at)}</div>` : ''}
          ${alert.resolved_by ? `<div><span class="muted">解决人：</span>${esc(alert.resolved_by)} ${fmtTime(alert.resolved_at)}</div>` : ''}
        </div>
        <div class="alert-detail-section" id="alertLogCtxSection">
          <div class="alert-detail-section-title">📜 日志上下文</div>
          <div id="alertLogCtxList" class="alert-log-ctx">
            <div class="muted" style="padding:12px">加载中...</div>
          </div>
        </div>
        <div class="alert-detail-section">
          <div class="alert-detail-section-title">📨 通知投递历史</div>
          <div id="alertEventsList" class="alert-events-list">
            <div class="muted" style="padding:12px">加载中...</div>
          </div>
        </div>
      </div>`;

    openDrawer({ title: `告警详情 #${alertId}`, body: bodyHTML, wide: true });

    // 加载日志上下文（与投递历史并行）
    loadAlertLogCtx(alertId);
    // 加载投递历史
    try {
      const events = await api(`/api/alerts/${alertId}/events`);
      const el = document.getElementById('alertEventsList');
      if (!el) return;
      if (!events.length) {
        el.innerHTML = '<div class="muted" style="padding:12px;text-align:center">暂无投递记录</div>';
        return;
      }
      const CH_LABEL = Object.fromEntries(CHANNELS);
      el.innerHTML = events.map(ev => {
        const okCls = ev.ok ? 'evt-ok' : 'evt-fail';
        const okText = ev.ok ? '✓ 成功' : '✗ 失败';
        return `<div class="alert-event-row ${okCls}">
          <span class="evt-time">${fmtTime(ev.created_at)}</span>
          <span class="evt-channel">${CH_LABEL[ev.channel] || esc(ev.channel)}</span>
          <span class="evt-status ${okCls}">${okText}</span>
          <span class="evt-detail muted">${esc(ev.detail || '')}</span>
        </div>`;
      }).join('');
    } catch(e) {
      console.warn('openAlertDetail events load failed', e);
      const el = document.getElementById('alertEventsList');
      if (el) {
        const msg = e.message || '未知错误';
        el.innerHTML = `<div style="padding:12px;text-align:center">
          <div class="muted" style="color:var(--danger)">加载失败：${esc(msg)}</div>
          <button class="btn sm ghost" id="evtRetryBtn" style="margin-top:8px">重试</button>
        </div>`;
        const retryBtn = document.getElementById('evtRetryBtn');
        if (retryBtn) retryBtn.onclick = () => openAlertDetail(alertId);
      }
    }
  }

  // ---- 告警详情：日志上下文（命中行前后 N 行 + 高亮） ----

  async function loadAlertLogCtx(alertId) {
    const section = document.getElementById('alertLogCtxSection');
    const el = document.getElementById('alertLogCtxList');
    if (!section || !el) return;
    try {
      const data = await api(`/api/alerts/${alertId}/logs?lines=100`);
      if (!data.available) {
        // 非日志告警：不渲染该区块
        if (data.reason === 'not_log_alert') { section.remove(); return; }
        const msg = data.reason === 'no_position'
          ? '该告警未记录日志位置，无法展示上下文'
          : data.reason === 'expired'
            ? '日志已超出保留期，无法展示上下文'
            : '无法展示上下文';
        el.innerHTML = `<div class="muted" style="padding:12px;text-align:center">${msg}</div>`;
        return;
      }
      renderLogCtx(el, data);
    } catch(e) {
      console.warn('loadAlertLogCtx failed', e);
      el.innerHTML = `<div style="padding:12px;text-align:center">
        <div class="muted" style="color:var(--danger)">加载失败：${esc(e.message || '未知错误')}</div>
        <button class="btn sm ghost" id="logCtxRetryBtn" style="margin-top:8px">重试</button>
      </div>`;
      const retryBtn = document.getElementById('logCtxRetryBtn');
      if (retryBtn) retryBtn.onclick = () => loadAlertLogCtx(alertId);
    }
  }

  function renderLogCtx(el, data) {
    const rows = [...data.before, data.hit, ...data.after].filter(Boolean);
    const hitId = data.hit && !data.hit.fallback ? data.hit.id : null;  // 兜底行不标命中
    el.innerHTML = `
      <div class="alert-log-ctx-info">
        命中行前 ${data.before.length} 行${hitId ? ' · 命中行' : ''} · 后 ${data.after.length} 行
      </div>
      <div class="alert-log-ctx-lines">
        ${rows.map(r => {
          const isHit = hitId !== null && r.id === hitId;
          return `<div class="alert-log-ctx-line${isHit ? ' hit' : ''}">
            <span class="ctx-ts">${fmtHHMMSS(r.ts)}</span>
            <span class="ctx-content">${highlightKw(esc(r.content || ''), data.keyword)}</span>
          </div>`;
        }).join('')}
      </div>`;
  }

  // 秒级时间戳 → "HH:MM:SS"
  function fmtHHMMSS(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    const p = n => String(n).padStart(2, '0');
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  // 内容已 esc 转义，关键词同样先转义再匹配高亮（安全 + 特殊字符关键词可用）
  function highlightKw(text, kw) {
    if (!kw) return text;
    const escKw = esc(kw).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    try {
      return text.replace(new RegExp(`(${escKw})`, 'g'), '<mark>$1</mark>');
    } catch(e) {
      return text;
    }
  }

  // ---- 工具函数（告警列表用） ----

  function parseTarget(raw){
    if (!raw) return '';
    try {
      const obj = typeof raw === 'string' ? JSON.parse(raw) : raw;
      const parts = [];
      if (obj.project) parts.push(obj.project);
      if (obj.env) parts.push(obj.env);
      if (obj.service) parts.push(obj.service);
      if (obj.server) parts.push(obj.server);
      return parts.join(' / ');
    } catch(e) {
      return '';
    }
  }

  // ---- 模块导出 ----
  window.alerts = {
    render: async function(root){
      destroyed = false;
      await loadProjectsForFilter();
      renderPage();
      if (currentTab === 'alerts') {
        await Promise.all([loadAlertCounts(), loadAlerts()]);
        if (destroyed) return [];
        timer = setInterval(() => { loadAlerts(); loadAlertCounts(); }, 15000);
      } else if (currentTab === 'rules') {
        loadRules();
      } else if (currentTab === 'silences') {
        loadSilences();
      } else if (currentTab === 'stats') {
        loadStats();
      }
      return [timer];
    },
    destroy: function(){
      destroyed = true;
      if (timer) clearInterval(timer);
      if (searchTimer) clearTimeout(searchTimer);
      timer = null;
      searchTimer = null;
      // 清理历史统计图表
      if (statsResizeHandler) { window.removeEventListener('resize', statsResizeHandler); statsResizeHandler = null; }
      if (statsChart) { try{ statsChart.dispose(); }catch(e){} statsChart = null; }
    },
  };
})();
