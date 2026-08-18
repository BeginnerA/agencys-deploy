// app.js — SPA 入口
(function init(){
  if (!token()){ location.href = '/'; return; }
  renderSidebar();
  renderTopbar();
  if (currentUser().must_change_password) openPwdModal();
  navigate(true);
  window.addEventListener('hashchange', () => navigate(false));
  // 底部跑马灯：3s 轮询最新任务
  poll(async () => {
    const tasks = await api('/api/tasks?limit=1');
    renderMarquee(tasks[0]);
  }, 3000);
})();
