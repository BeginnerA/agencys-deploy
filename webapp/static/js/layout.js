// layout.js — 顶部栏 + 用户菜单 + 全局搜索 + 跑马灯
function renderTopbar(){
  const u = currentUser();
  const roleName = {admin:'管理员', operator:'操作员', viewer:'只读'}[u.role] || esc(u.role);
  const tb = document.getElementById('topbar');
  tb.innerHTML = `
    <button class="tb-btn" id="navToggle" title="折叠导航">☰</button>
    <div class="tb-search">
      <span class="ico">🔍</span>
      <input id="globalSearch" placeholder="搜索服务 / 任务 #id ...">
      <div class="drop-panel" id="searchDrop"></div>
    </div>
    <button class="tb-btn" id="refreshBtn" title="手动刷新监控">⟳</button>
    <div class="user-chip" id="userChip">
      <div class="user-avatar">${esc((u.username || '?').slice(0,1).toUpperCase())}</div>
      <div class="user-meta"><div class="u-name">${esc(u.username || '未知')}</div>
      <div class="u-role">${roleName}</div></div>
    </div>
    <div id="userMenu">
      <button class="btn ghost sm" id="menuPwd" style="width:100%;justify-content:flex-start">🔑 修改密码</button>
      <button class="btn ghost sm" id="menuLogout" style="width:100%;justify-content:flex-start">⏻ 退出登录</button>
    </div>`;
  const toggleNav = () => document.getElementById('sidebar').classList.toggle('collapsed');
  document.getElementById('navToggle').onclick = toggleNav;
  document.getElementById('refreshBtn').onclick = async () => {
    try{ await api('/api/monitor/refresh', {method:'POST'}); toastSuccess('监控探测已刷新'); }
    catch(e){ toastError(e.message); }
  };
  // 用户菜单
  const chip = document.getElementById('userChip');
  const menu = document.getElementById('userMenu');
  chip.onclick = (e) => {
    e.stopPropagation();
    const r = chip.getBoundingClientRect();
    menu.style.display = menu.style.display === 'block' ? 'none' : 'block';
    menu.style.top = (r.bottom + 6) + 'px';
    menu.style.right = (window.innerWidth - r.right) + 'px';
  };
  document.addEventListener('click', () => { menu.style.display = 'none'; });
  document.getElementById('menuLogout').onclick = logout;
  document.getElementById('menuPwd').onclick = () => openPwdModal();
  // 全局搜索
  const input = document.getElementById('globalSearch');
  const drop = document.getElementById('searchDrop');
  let searchTimer = null;
  input.addEventListener('input', () => {
    clearTimeout(searchTimer);
    const kw = input.value.trim();
    if (!kw){ drop.style.display = 'none'; return; }
    searchTimer = setTimeout(async () => {
      try{
        const tasks = await api('/api/tasks?limit=100').catch(() => []);
        const projects = await api('/api/projects').catch(() => []);
        // 占位符提示输入 #id，剥离 # 前缀后匹配任务 id/名称
        const k2 = kw.replace(/^#/, '');
        const tList = tasks.filter(t => t.name.includes(k2) || String(t.id) === k2).slice(0, 6);
        const sList = [];
        for (const p of projects){
          const svcs = await api(`/api/projects/${p.id}/services`).catch(() => []);
          svcs.filter(s => s.name.includes(k2)).slice(0, 4).forEach(s =>
            sList.push({label: s.name, sub: `${p.name} 服务`, goto: () => {
              localStorage.setItem('dev_filter_project', p.id); location.hash = '#/devices';
            }}));
        }
        const rows = [
          ...tList.map(t => ({label: `#${t.id} ${t.name}`, sub: t.env || '', goto: () => openTaskLogDrawer(t.id)})),
          ...sList,
        ];
        if (!rows.length){ drop.innerHTML = '<div class="drop-item">无匹配结果</div>'; }
        else {
          drop.innerHTML = rows.map(r => `<div class="drop-item">${esc(r.label)}<span class="k">${esc(r.sub)}</span></div>`).join('');
          [...drop.children].forEach((el, i) => el.onclick = () => { drop.style.display='none'; input.value=''; rows[i].goto(); });
        }
        const r = input.getBoundingClientRect();
        drop.style.top = (r.bottom + 6) + 'px';
        drop.style.left = r.left + 'px';
        drop.style.width = Math.max(280, r.width) + 'px';
        drop.style.display = 'block';
      }catch(e){ /* 搜索失败静默 */ }
    }, 300);
  });
  input.addEventListener('keydown', e => { if(e.key === 'Escape'){ drop.style.display = 'none'; input.value=''; } });
  document.addEventListener('click', (e) => { if(!input.contains(e.target) && !drop.contains(e.target)) drop.style.display='none'; });
}
// 修改密码弹窗（首登强制改密复用；改密成功前业务 API 均 403）
function openPwdModal(){
  const root = document.getElementById('modalRoot');
  root.classList.remove('hidden');
  root.innerHTML = `
    <div class="modal-mask" id="pwdMask">
      <div class="modal-card">
        <h3>修改密码</h3>
        <div class="field" style="margin-bottom:12px"><label>原密码</label>
          <input type="password" id="pwdOld" placeholder="原密码"></div>
        <div class="field" style="margin-bottom:20px"><label>新密码（至少 6 位）</label>
          <input type="password" id="pwdNew" placeholder="新密码"></div>
        <div style="display:flex;gap:10px;justify-content:flex-end">
          <button class="btn" id="pwdCancel">取消</button>
          <button class="btn primary" id="pwdOk">确认修改</button>
        </div>
      </div>
    </div>`;
  const close = () => { root.classList.add('hidden'); root.innerHTML = ''; };
  document.getElementById('pwdMask').onclick = (e) => { if(e.target.id === 'pwdMask') close(); };
  document.getElementById('pwdCancel').onclick = close;
  document.getElementById('pwdOk').onclick = async () => {
    try{
      await api('/api/auth/password', {method:'POST', body: JSON.stringify({
        old_password: document.getElementById('pwdOld').value,
        new_password: document.getElementById('pwdNew').value})});
      const u = currentUser(); u.must_change_password = 0;
      localStorage.setItem('user', JSON.stringify(u));
      close(); toastSuccess('密码已修改');
    }catch(e){ toastError(e.message); }
  };
}
// 跑马灯：渲染最近发布事件
function renderMarquee(latest){
  const bar = document.getElementById('marqueeBar');
  if (bar.dataset.closed === '1') return;
  bar.classList.remove('hidden');
  bar.innerHTML = latest
    ? `<span class="mq-label">最近发布</span><span class="mq-text">#${esc(latest.id)} ${esc(latest.name)}（${esc(latest.env||'prod')}）→ ${esc(latest.version||latest.previous_version||'-')}，${esc(latest.status)}，${fmtTime(latest.started_at)}</span>
       <button class="mq-close" id="mqClose">✕</button>`
    : `<span class="mq-label">最近发布</span><span class="mq-text">等待发布事件...</span>
       <button class="mq-close" id="mqClose">✕</button>`;
  document.getElementById('mqClose').onclick = () => { bar.classList.add('hidden'); bar.dataset.closed = '1'; };
}
