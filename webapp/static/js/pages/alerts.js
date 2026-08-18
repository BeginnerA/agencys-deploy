// pages/alerts.js — 告警中心（告警列表 Tab + 规则管理 Tab，消费后端 /api/alerts API）
(function(){
  let timer = null;
  let destroyed = false;
  let searchTimer = null;
  let currentTab = 'rules';  // 预留 Tab 切换（Task 5 完善），当前默认 rules 便于测试

  // 告警列表状态
  let alerts = [];
  let total = 0;
  let currentPage = 1;
  const pageSize = 20;
  let sevFilter = 'all';
  let statusFilter = 'all';
  let keyword = '';

  // 统计计数
  let firingCount = 0;
  let ackedCount = 0;
  let resolvedCount = 0;

  // ---- 规则管理 Tab 状态 ----
  let rules = [];
  let editingRuleId = null;
  let ruleTypeFilter = 'all';

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
  const MT_LABEL = {metric:'指标',status:'状态',log_keyword:'日志关键词'};
  const CMP_LABEL = {'<':'<','<=':'≤','>':'>','>=':'≥'};

  // ---- 数据加载 ----

  async function loadAlerts(){
    try {
      const q = new URLSearchParams({page: String(currentPage), page_size: String(pageSize)});
      if (sevFilter !== 'all') q.set('severity', sevFilter);
      if (statusFilter !== 'all') q.set('status', statusFilter);
      if (keyword) q.set('keyword', keyword);
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

  async function loadStats(){
    try {
      const [f, a, r] = await Promise.all([
        api('/api/alerts?status=firing&page_size=1').catch(() => ({total:0})),
        api('/api/alerts?status=acked&page_size=1').catch(() => ({total:0})),
        api('/api/alerts?status=resolved&page_size=1').catch(() => ({total:0})),
      ]);
      if (destroyed) return;
      firingCount = f.total || 0;
      ackedCount = a.total || 0;
      resolvedCount = r.total || 0;
      updateStats();
      updateAlertBadge(firingCount);
    } catch(e) {
      console.warn('loadStats failed', e);
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
      </div>
      <div id="tabContent"></div>`;
    root.querySelectorAll('.tab-btn[data-tab]').forEach(btn => {
      btn.onclick = () => switchTab(btn.dataset.tab);
    });
    if (currentTab === 'alerts') renderAlertsTab();
    else renderRulesTab();
  }

  function switchTab(tab){
    if (currentTab === tab) return;
    currentTab = tab;
    renderPage();
    if (tab === 'alerts') { loadStats(); loadAlerts(); }
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
      <div id="alertList">${skeletonHTML(4)}</div>
      <div id="alertPagination"></div>`;
    bindToolbar();
  }

  function bindToolbar(){
    const root = document.getElementById('pageRoot');
    // 级别筛选
    root.querySelectorAll('.chip[data-sev]').forEach(c => {
      c.onclick = () => {
        sevFilter = sevFilter === c.dataset.sev ? 'all' : c.dataset.sev;
        currentPage = 1;
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
          loadAlerts();
        }, 300);
      };
    }
  }

  function renderList(){
    const box = document.getElementById('alertList');
    if (!box) return;
    if (!alerts.length) {
      box.innerHTML = `<div class="panel">${emptyState('🔕', '暂无告警')}</div>`;
      renderPagination();
      return;
    }
    box.innerHTML = alerts.map(a => {
      const target = parseTarget(a.target_info);
      const sevCls = (a.severity || 'P2').toLowerCase();
      const stLabel = ST_LABEL[a.status] || esc(a.status);
      const stCls = a.status === 'firing' ? 'running'
                  : a.status === 'acked' ? 'warning'
                  : 'success';
      return `
        <div class="alert-row ${esc(a.status)}" data-id="${a.id}">
          <span class="badge ${sevCls}">${esc(a.severity || 'P2')}</span>
          <div class="ar-main">
            <div class="ar-title">${esc(a.subject)}</div>
            <div class="ar-detail">${esc(a.description || '')}${target ? ' · ' + esc(target) : ''}</div>
          </div>
          <span class="badge ${stCls}" style="margin-right:8px">${stLabel}</span>
          <span class="ar-time" title="首次出现">${fmtTime(a.first_seen_at)}</span>
          <span class="ar-time" title="最近出现" style="margin-left:4px">${fmtTime(a.last_seen_at)}</span>
          ${a.status === 'firing'
            ? '<button class="btn sm primary" data-act="ack" style="margin-left:8px">确认</button>'
            : ''}
          ${a.status !== 'resolved'
            ? '<button class="btn sm danger" data-act="resolve" style="margin-left:4px">解决</button>'
            : ''}
        </div>`;
    }).join('');
    // 绑定操作按钮（事件委托）
    box.onclick = (e) => {
      const btn = e.target.closest('[data-act]');
      if (!btn) return;
      const row = btn.closest('.alert-row');
      if (!row) return;
      const id = Number(row.dataset.id);
      if (btn.dataset.act === 'ack') ackAlert(id);
      else if (btn.dataset.act === 'resolve') resolveAlert(id);
    };
    renderPagination();
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
      loadStats();
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
      loadStats();
    } catch(e) {
      toastError(e.message || '操作失败');
    }
  }

  // ==================== 规则管理 Tab ====================

  async function loadRules(){
    try {
      rules = await api('/api/alerts/rules');
      if (destroyed) return;
      renderRuleList();
    } catch(e) {
      console.warn('loadRules failed', e);
      const box = document.getElementById('ruleListContainer');
      if (box) box.innerHTML = `<div class="panel">规则加载失败</div>`;
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

  function renderRuleList(){
    const box = document.getElementById('ruleListContainer');
    if (!box) return;
    const filtered = ruleTypeFilter === 'all' ? rules : rules.filter(r => r.metric_type === ruleTypeFilter);
    if (!filtered.length) {
      box.innerHTML = `<div class="panel">${emptyState('📏', '暂无规则')}</div>`;
      return;
    }
    box.innerHTML = `
      <div class="panel" style="padding:0"><div class="tbl-wrap"><table class="tbl rules-tbl">
        <thead><tr><th>名称</th><th>类型</th><th>指标</th><th>条件</th><th>级别</th><th>渠道</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>${filtered.map(r => {
          const scope = parseScope(r.scope);
          const chList = (r.channels || []).map(c => CHANNELS.find(([k]) => k === c)?.[1] || c);
          let cond = '-';
          if (r.metric_type === 'metric') cond = `${CMP_LABEL[r.compare] || r.compare} ${r.threshold}`;
          else if (r.metric_type === 'log_keyword') cond = esc(r.metric || '');
          return `<tr>
            <td><b>${esc(r.name)}</b>${r.remark ? `<div class="rule-remark">${esc(r.remark)}</div>` : ''}</td>
            <td><span class="rule-mt-badge mt-${esc(r.metric_type)}">${MT_LABEL[r.metric_type] || r.metric_type}</span></td>
            <td>${esc(r.metric || '-')}</td>
            <td class="num">${cond}${r.duration_seconds ? `<div class="rule-sub">持续 ${r.duration_seconds}s</div>` : ''}</td>
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
    const title = rule ? '编辑规则' : '新建规则';
    const r = rule || {};
    const scope = parseScope(r.scope);
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
          <select id="rfScopeType">${SCOPE_TYPES.map(([k,v]) =>
            `<option value="${k}" ${scope.type===k?'selected':''}>${v}</option>`).join('')}</select></div>
        <div class="field" id="rfScopeIdsWrap" style="${scope.type==='all'?'display:none':''}">
          <label>作用域 IDs（逗号分隔）</label>
          <input id="rfScopeIds" value="${esc((scope.ids||[]).join(','))}" placeholder="1,2,3"></div>
        <div class="field"><label>升级间隔（分钟）</label>
          <input id="rfEscInt" type="number" value="${r.escalation_interval ?? 30}" min="0"></div>
        <div class="field check"><input type="checkbox" id="rfEnabled" ${r.enabled !== 0 ? 'checked' : ''}>
          <label for="rfEnabled" style="margin:0">启用</label></div>
        <div class="field full"><label>通知渠道</label>
          <div class="rule-ch-box" id="rfChannels">${CHANNELS.map(([k,v]) =>
            `<label class="rule-ch-cb"><input type="checkbox" value="${k}" ${channels.includes(k)?'checked':''}> ${v}</label>`
          ).join('')}</div></div>
        <div class="field full" id="rfLogKwWrap" style="display:none"><label>日志关键词</label>
          <input id="rfLogKw" value="${esc(r.metric_type==='log_keyword' ? (r.metric||'') : '')}" placeholder="匹配日志中的关键词"></div>
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
    }
    selType.onchange = () => { updateMetricOpts(); updateFieldVis(); };
    updateMetricOpts();
    // 回填 metric（编辑时）
    if (r.metric && (r.metric_type !== 'log_keyword')) selMetric.value = r.metric;
    updateFieldVis();
    // 作用域联动
    const selScope = document.getElementById('rfScopeType');
    selScope.onchange = () => {
      document.getElementById('rfScopeIdsWrap').style.display = selScope.value === 'all' ? 'none' : '';
    };
    // 按钮
    document.getElementById('rfCancel').onclick = () => {
      const c = document.getElementById('dwClose');
      if (c) c.click();
    };
    document.getElementById('rfSave').onclick = () => saveRule();
  }

  async function saveRule(){
    const saveBtn = document.getElementById('rfSave');
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = '保存中…'; }
    try {
      const name = document.getElementById('rfName').value.trim();
      if (!name) { toastError('规则名称必填'); return; }
      const metricType = document.getElementById('rfMetricType').value;
      const metric = metricType === 'log_keyword'
        ? document.getElementById('rfLogKw').value.trim()
        : document.getElementById('rfMetric').value;
      const compare = document.getElementById('rfCompare').value;
      const threshold = parseFloat(document.getElementById('rfThreshold').value) || 0;
      const duration = parseInt(document.getElementById('rfDuration').value) || 300;
      const severity = document.getElementById('rfSeverity').value;
      const scopeType = document.getElementById('rfScopeType').value;
      const scopeIdsRaw = document.getElementById('rfScopeIds').value.trim();
      const scopeIds = scopeType === 'all' ? []
        : scopeIdsRaw.split(',').map(s => s.trim()).filter(Boolean).map(Number).filter(n => !isNaN(n));
      const scope = {type: scopeType, ids: scopeIds};
      const channels = [...document.querySelectorAll('#rfChannels input:checked')].map(c => c.value);
      if (!channels.length) { toastError('至少选择一个通知渠道'); return; }
      const escalationInterval = parseInt(document.getElementById('rfEscInt').value) || 30;
      const enabled = document.getElementById('rfEnabled').checked;
      const remark = document.getElementById('rfRemark').value.trim();
      const payload = {name, metric_type: metricType, metric, compare, threshold,
        duration_seconds: duration, scope, severity, channels, escalation_interval: escalationInterval,
        enabled, remark};
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
    if (!raw) return {type:'all', ids:[]};
    if (typeof raw === 'string') {
      try { raw = JSON.parse(raw); } catch(e) { return {type:'all', ids:[]}; }
    }
    return {type: raw.type || 'all', ids: raw.ids || []};
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
      renderPage();
      if (currentTab === 'alerts') {
        await Promise.all([loadStats(), loadAlerts()]);
        if (destroyed) return [];
        timer = setInterval(() => { loadAlerts(); loadStats(); }, 15000);
      }
      return [timer];
    },
    destroy: function(){
      destroyed = true;
      if (timer) clearInterval(timer);
      if (searchTimer) clearTimeout(searchTimer);
      timer = null;
      searchTimer = null;
    },
  };
})();
