// app.js — SPA 入口
(function init(){
  if (!token()){ location.href = '/'; return; }
  renderSidebar();
  renderTopbar();
  if (currentUser().must_change_password) openPwdModal();
  navigate(true);
  window.addEventListener('hashchange', () => navigate(false));
  // ---- 全局实时绑定（事件驱动优先，通道离线自动退回下面给的兜底轮询周期）----
  // 底部跑马灯：任务状态变更即刷新（原 3s 盲轮询），离线退回 10s
  liveBind('tasks', async () => {
    const bar = document.getElementById('marqueeBar');
    if (bar && bar.dataset.closed === '1') return false;
    const tasks = await api('/api/tasks?limit=1');
    renderMarquee(tasks[0]);
  }, 10000);
  // 全局告警角标：新告警/确认/解决事件即刷新（任何人的操作都会推），离线退回 15s
  liveBind('alerts', async () => {
    const data = await api('/api/alerts/badge').catch(() => null);
    if (data) updateAlertBadge(data.firing || 0);
  }, 15000);
  // 全局待处理建议角标：后台 advisor 产出新建议即刷新，离线退回 30s
  liveBind('agent', async () => {
    const data = await api('/api/agent/advice/badge').catch(() => null);
    if (data) updateAdviceBadge(data.pending || 0);
  }, 30000);
})();
