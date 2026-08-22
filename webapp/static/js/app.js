// app.js — SPA 入口
(function init(){
  if (!token()){ location.href = '/'; return; }
  renderSidebar();
  renderTopbar();
  if (currentUser().must_change_password) openPwdModal();
  navigate(true);
  window.addEventListener('hashchange', () => navigate(false));
  // 底部跑马灯：3s 轮询最新任务，关闭后停止轮询
  poll(async () => {
    const bar = document.getElementById('marqueeBar');
    if (bar && bar.dataset.closed === '1') return false;
    const tasks = await api('/api/tasks?limit=1');
    renderMarquee(tasks[0]);
  }, 3000);
  // 全局告警角标：15s 轮询活跃告警数，无论当前页面均实时更新
  poll(async () => {
    const data = await api('/api/alerts/badge').catch(() => null);
    if (data) updateAlertBadge(data.firing || 0);
  }, 15000);
})();
