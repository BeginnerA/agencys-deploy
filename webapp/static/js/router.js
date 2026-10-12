// router.js — SPA hash 路由
const ROUTES = [
  { path:'dashboard', group:'运维监控', icon:'📊', name:'监控中心', page:'dashboard', crumbs:['运维中心','监控中心'] },
  { path:'devices',   group:'运维监控', icon:'🖥️', name:'项目总览', page:'devices',   crumbs:['运维中心','项目总览'] },
  { path:'agent',     group:'智能运维', icon:'🤖', name:'智能助手', page:'agent',     crumbs:['智能运维','智能助手'] },
  { path:'deploy',    group:'运维操作', icon:'🚀', name:'发布中心', page:'deploy',    crumbs:['运维操作','发布中心'] },
  { path:'deploy/schedules', group:'运维操作', icon:'📅', name:'定时计划', page:'deploy_schedules', crumbs:['运维操作','发布中心','定时计划'], sub:true, parent:'deploy' },
  { path:'deploy/windows',   group:'运维操作', icon:'🔒', name:'发布窗口', page:'deploy_windows',   crumbs:['运维操作','发布中心','发布窗口'], sub:true, parent:'deploy' },
  { path:'deploy/canary',    group:'运维操作', icon:'🐤', name:'灰度策略', page:'deploy_canary',    crumbs:['运维操作','发布中心','灰度策略'], sub:true, parent:'deploy' },
  { path:'deploy/topology',  group:'运维操作', icon:'🕸️', name:'依赖拓扑', page:'deploy_topology',  crumbs:['运维操作','发布中心','依赖拓扑'], sub:true, parent:'deploy' },
  { path:'deploy/compare',   group:'运维操作', icon:'📊', name:'发布对比', page:'deploy_compare',   crumbs:['运维操作','发布中心','发布对比'], sub:true, parent:'deploy' },
  { path:'alerts',    group:'运维操作', icon:'🔔', name:'告警中心', page:'alerts',    crumbs:['运维操作','告警中心'] },
  { path:'logs',      group:'运维操作', icon:'📜', name:'日志中心', page:'logs',      crumbs:['运维操作','日志中心'] },
  { path:'config',    group:'系统管理', icon:'⚙️', name:'配置管理', page:'config',    crumbs:['系统管理','配置管理'], adminOnly:true, sub:true },
];
// 惰性取页面模块：router.js 先于 pages/*.js 加载，顶层不能引用未定义变量
function getPageModule(name){ return window[name]; }
let currentRoute = null;
let lastHash = null;
let activeTimers = [];

function getRoute(){
  const hash = location.hash.replace(/^#\/?/, '').split('?')[0] || 'dashboard';
  // 先尝试完整匹配（如 deploy/schedules）
  const fullMatch = ROUTES.find(r => r.path === hash);
  if (fullMatch) return fullMatch;
  // 回退到基础路径匹配
  const base = hash.split('/')[0];
  return ROUTES.find(r => r.path === base) || ROUTES[0];
}
function visibleRoutes(){
  return ROUTES.filter(r => !r.adminOnly || isAdmin());
}
// 路由切换：销毁旧页 → 渲染新页
async function navigate(force){
  const route = getRoute();
  if (route.adminOnly && !isAdmin()){ location.hash = '#/dashboard'; return; }
  const hash = location.hash || '#/';
  const normHash = hash.replace(/^#\/?/, '').split('?')[0] || '';  // 与 getRoute 同规范化，避免尾斜杠/query 差异误触发刷新
  const sameBase = currentRoute && currentRoute.path === route.path;
  const subChanged = route.sub && lastHash !== null && normHash !== lastHash;
  lastHash = normHash;
  if (!force && sameBase && !subChanged) return;
  activeTimers.forEach(t => clearInterval(t));
  activeTimers = [];
  if (currentRoute && getPageModule(currentRoute.page) && getPageModule(currentRoute.page).destroy){
    try{ getPageModule(currentRoute.page).destroy(); }catch(e){ console.warn('destroy err', e); }
  }
  currentRoute = route;
  renderNavActive();
  renderBreadcrumb();
  const container = document.getElementById('content');
  container.innerHTML = '<div class="page-inner" id="pageRoot"></div>';
  const root = document.getElementById('pageRoot');
  root.innerHTML = skeletonHTML(5);
  try{
    const mod = getPageModule(route.page);
    if (!mod){
      // 页面模块未加载（多为旧页面实例持有旧脚本列表），给出明确提示而非裸报错
      root.innerHTML = errorState('页面模块未加载（' + route.page + '），请刷新页面', 'navRetry');
      return;
    }
    const timers = await mod.render(root);
    (timers || []).forEach(t => activeTimers.push(t));
  }catch(e){
    console.error('page render failed', e);
    root.innerHTML = errorState('页面加载失败：' + e.message, 'navRetry');
  }
}
// 错误态重试：强制重新渲染当前路由（force 绕过同路由短路）
function navRetry(){ navigate(true); }
// 导航渲染 + 激活态（app.js 在 DOM 就绪后调用；首次调用时 currentRoute 尚为 null）
function renderSidebar(){
  const sb = document.getElementById('sidebar');
  const activePath = currentRoute ? currentRoute.path : null;
  const groups = {};
  visibleRoutes().forEach(r => { (groups[r.group] = groups[r.group] || []).push(r); });
  const nav = Object.entries(groups).map(([g, items]) => `
    <div class="nav-group">
      <div class="nav-group-title">${g}</div>
      ${items.map(r => `
        <div class="nav-item ${r.path === activePath ? 'active' : ''}" data-path="${r.path}">
          <span class="nav-ico">${r.icon}</span><span class="nav-txt">${r.name}</span>
          ${r.path === 'alerts' ? '<span class="nav-badge hidden" id="alertBadge">0</span>' : ''}
          ${r.path === 'agent' ? '<span class="nav-badge hidden" id="adviceBadge">0</span>' : ''}
        </div>`).join('')}
    </div>`).join('');
  sb.innerHTML = `
    <div class="side-logo">
      <div class="logo-ico">⚡</div>
      <div><div class="logo-name">${window.PLATFORM_NAME}</div>
      <div class="logo-sub">DEPLOY CONSOLE</div></div>
    </div>
    <nav class="nav-scroll" aria-label="主导航">${nav}</nav>
    <div class="side-foot">© 2026 运维平台</div>`;
  sb.querySelectorAll('.nav-item').forEach(item => {
    item.onclick = () => { location.hash = '#/' + item.dataset.path; };
  });
}
function renderNavActive(){
  document.querySelectorAll('#sidebar .nav-item').forEach(i =>
    i.classList.toggle('active', i.dataset.path === currentRoute.path));
}
function renderBreadcrumb(){
  const el = document.getElementById('breadcrumb');
  // 子页场景（config 下有 p/:id 等子路由）允许点击上一级返回，避免进入后无法回到工作台
  const base = (location.hash || '').replace(/^#\/?/, '').split('?')[0].split('/')[0];
  const canGoBack = (base === 'config' || base === 'deploy') && (location.hash || '').includes('/');
  el.innerHTML = currentRoute.crumbs.map((c, i) => {
    const isLast = i === currentRoute.crumbs.length - 1;
    if (isLast) return `<span class="cur">${c}</span>`;
    return canGoBack
      ? `<span data-crumb-go="${base}">${c}</span><span class="sep">/</span>`
      : `<span>${c}</span><span class="sep">/</span>`;
  }).join('');
  el.querySelectorAll('[data-crumb-go]').forEach(s => s.onclick = () => { location.hash = '#/' + s.dataset.crumbGo; });
}
// 供 alerts 页更新角标
window.updateAlertBadge = function(count){
  const badge = document.getElementById('alertBadge');
  if (!badge) return;
  if (count > 0){ badge.classList.remove('hidden'); badge.textContent = count > 99 ? '99+' : count; }
  else badge.classList.add('hidden');
};
// 供 agent 页/全局轮询更新待处理建议角标
window.updateAdviceBadge = function(count){
  const badge = document.getElementById('adviceBadge');
  if (!badge) return;
  if (count > 0){ badge.classList.remove('hidden'); badge.textContent = count > 99 ? '99+' : count; }
  else badge.classList.add('hidden');
};
