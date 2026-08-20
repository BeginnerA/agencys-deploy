// pages/deploy_windows.js — 发布窗口/封网期管理
(function(){
  let destroyed = false;
  let windows = [];

  async function loadWindows(){
    windows = await api('/api/deploy/windows').catch(() => []);
  }

  function renderCalendar(){
    const now = new Date();
    const year = now.getFullYear(), month = now.getMonth();
    const firstDay = new Date(year, month, 1).getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const today = now.getDate();
    let cells = '';
    ['日','一','二','三','四','五','六'].forEach(d =>
      cells += `<div style="text-align:center;font-size:11px;color:var(--text-secondary);padding:4px">${d}</div>`);
    for (let i = 0; i < firstDay; i++)
      cells += '<div class="cal-cell" style="visibility:hidden"></div>';
    for (let d = 1; d <= daysInMonth; d++) {
      const dayStart = new Date(year, month, d).getTime() / 1000;
      const dayEnd = dayStart + 86400;
      let cls = 'cal-cell' + (d === today ? ' today' : '');
      let title = '';
      for (const w of windows) {
        let ws = w.start_at, we = w.end_at;
        if (w.repeat_yearly) {
          const wy = new Date(ws * 1000).getFullYear();
          const offset = (year - wy) * 365.25 * 86400;
          ws += offset; we += offset;
        }
        if (ws < dayEnd && we > dayStart) {
          cls += w.block_level === 'block' ? ' cal-block' : ' cal-warn';
          title = w.name;
          break;
        }
      }
      cells += `<div class="${cls}" title="${esc(title)}"><span class="cal-day">${d}</span></div>`;
    }
    return cells;
  }

  function renderList(){
    const root = document.getElementById('pageRoot');
    const box = root.querySelector('#winBox');
    if (!box) return;
    box.innerHTML = `
      <div class="panel" style="padding:16px">
        <div style="font-weight:600;margin-bottom:8px">${new Date().toLocaleDateString('zh-CN', {year:'numeric',month:'long'})}</div>
        <div class="cal-grid">${renderCalendar()}</div>
      </div>
      <div class="panel" style="padding:0;margin-top:12px"><div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>#</th><th>名称</th><th>开始</th><th>结束</th><th>级别</th><th>每年重复</th><th>操作</th></tr></thead>
        <tbody>${windows.map(w => `<tr>
          <td class="num">${w.id}</td>
          <td>${esc(w.name)}</td>
          <td class="num muted">${fmtTime(w.start_at)}</td>
          <td class="num muted">${fmtTime(w.end_at)}</td>
          <td>${w.block_level === 'block' ? '<span class="tag tag-warn">硬拦截</span>' : '<span class="tag">软警告</span>'}</td>
          <td>${w.repeat_yearly ? '✓' : '-'}</td>
          <td>
            <button class="btn danger ghost sm" data-del="${w.id}">删除</button>
          </td>
        </tr>`).join('')}</tbody></table></div></div>`;
    box.querySelectorAll('[data-del]').forEach(btn => btn.onclick = async () => {
      if (!await confirmDialog('确认删除此窗口？')) return;
      await api(`/api/deploy/windows/${btn.dataset.del}`, {method:'DELETE'});
      await loadWindows(); renderList();
      toastSuccess('已删除');
    });
  }

  function openCreateForm(){
    openDrawer({ title: '新建发布窗口', body: `
      <div style="display:flex;flex-direction:column;gap:12px">
        <div><label class="dg-label">名称</label><input id="winName" class="inp" placeholder="如：春节封网"></div>
        <div><label class="dg-label">开始时间</label><input id="winStart" type="datetime-local" class="inp"></div>
        <div><label class="dg-label">结束时间</label><input id="winEnd" type="datetime-local" class="inp"></div>
        <div><label class="dg-label">拦截级别</label>
          <select id="winLevel" class="inp"><option value="block">硬拦截（禁止发布）</option><option value="warn">软警告（确认后放行）</option></select></div>
        <div><label><input type="checkbox" id="winRepeat"> 每年重复（节假日适用）</label></div>
        <button class="btn primary" id="winSave">保存</button>
      </div>`});
    document.getElementById('winSave').onclick = async () => {
      const startDt = document.getElementById('winStart').value;
      const endDt = document.getElementById('winEnd').value;
      if (!startDt || !endDt) { toastError('请选择时间范围'); return; }
      try {
        await api('/api/deploy/windows', {method:'POST', body:JSON.stringify({
          name: document.getElementById('winName').value,
          start_at: Math.floor(new Date(startDt).getTime() / 1000),
          end_at: Math.floor(new Date(endDt).getTime() / 1000),
          block_level: document.getElementById('winLevel').value,
          repeat_yearly: document.getElementById('winRepeat').checked,
          project_id: 0
        })});
        toastSuccess('窗口已创建');
        document.getElementById('dwClose').click();
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
      if (nb) nb.onclick = openCreateForm;
      renderList();
      return [];
    },
    destroy: function(){ destroyed = true; }
  };
})();
