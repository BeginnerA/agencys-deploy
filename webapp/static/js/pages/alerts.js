// pages/alerts.js — 告警中心（告警列表 Tab，消费后端 /api/alerts API）
(function(){
  let timer = null;
  let destroyed = false;
  let searchTimer = null;
  let currentTab = 'alerts';  // 预留 Tab 切换（Task 5 实现规则/静默/渠道等 Tab）

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

  // 常量定义
  const SEVERITIES = [['P0','P0'],['P1','P1'],['P2','P2'],['P3','P3']];
  const STATUSES = [['firing','活跃'],['acked','已确认'],['resolved','已解决']];
  const SEV_LABEL = {P0:'P0 紧急',P1:'P1 重要',P2:'P2 一般',P3:'P3 提示'};
  const ST_LABEL = {firing:'活跃',acked:'已确认',resolved:'已解决'};

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

  // ---- 工具函数 ----

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
      await Promise.all([loadStats(), loadAlerts()]);
      if (destroyed) return [];
      timer = setInterval(() => { loadAlerts(); loadStats(); }, 15000);
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
