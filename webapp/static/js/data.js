// data.js — 告警聚合算法 + 健康指数计算（规格 §7/§6.2）
const ALERT_ACK_PREFIX = 'alert_acked_';

// 生成告警：返回 [{key, level, source, title, detail, ts, status}]
// level: critical(严重)/warning(警告)；status: unacked/acked
function computeAlerts(tasks, overview, monitorByProject){
  const alerts = [];
  const now = Date.now() / 1000;
  const seen = {};   // 去重 key
  const push = (key, level, source, title, detail, ts) => {
    if (seen[key]) return;
    seen[key] = 1;
    const acked = localStorage.getItem(ALERT_ACK_PREFIX + key) === '1';
    alerts.push({ key, level, source, title, detail, ts, status: acked ? 'acked' : 'unacked' });
  };
  // 规则 1：失败任务（type 含 rollback → 严重）
  tasks.forEach(t => {
    if (t.status !== 'failed') return;
    // 注：后端当前不持久化 rollback 字段（tasks.type 仅 project/pipeline），
    // 此处判断为未来兼容保留；现阶段回滚失败与发布失败均按 warning 展示
    const isRollback = t.type === 'rollback' || (t.name || '').includes('回滚');
    push('task_failed_' + t.id,
         isRollback ? 'critical' : 'warning',
         'task',
         `任务 #${t.id} ${t.name} 发布失败`,
         `环境 ${t.env || '-'} · 版本 ${t.version || t.previous_version || '-'}${isRollback ? ' · 回滚失败' : ''}`,
         t.finished_at || t.started_at || now);
  });
  // 规则 2：running 超 30 分钟
  tasks.forEach(t => {
    if (t.status !== 'running' || !t.started_at) return;
    const runningSec = now - t.started_at;
    if (runningSec > 30 * 60){
      push('task_timeout_' + t.id, 'warning', 'task',
           `任务 #${t.id} ${t.name} 运行超时`,
           `已运行 ${Math.round(runningSec / 60)} 分钟（>30 分钟）`, t.started_at);
    }
  });
  // 规则 3：服务异常（probe_failed / not_deployed 且服务 enabled）
  Object.entries(monitorByProject || {}).forEach(([projectName, mon]) => {
    const svcs = (mon && mon.services) || {};
    Object.entries(svcs).forEach(([svcName, st]) => {
      if (st.status !== 'probe_failed' && st.status !== 'not_deployed') return;
      push('svc_' + projectName + '_' + svcName, 'warning', 'service',
           `服务 ${svcName}（${projectName}）${st.status === 'probe_failed' ? '探测失败' : '未部署'}`,
           st.status === 'probe_failed' ? 'SSH 探测无响应，请检查服务与网络' : '服务尚未部署到目标环境', st.ts || now);
    });
  });
  // 按时间倒序
  alerts.sort((a, b) => (b.ts || 0) - (a.ts || 0));
  return alerts;
}
// 标记已处理
function ackAlert(key){ localStorage.setItem(ALERT_ACK_PREFIX + key, '1'); }
function unackAlert(key){ localStorage.removeItem(ALERT_ACK_PREFIX + key); }

// 健康指数（规格 §6.2）：{demo, values[6]} —— 稳定性/性能/资源/安全/可用性/响应
function computeHealth(overview, mon){
  const svcs = mon && mon.services ? Object.values(mon.services) : [];
  const online = svcs.filter(s => s.status === 'online').length;
  const demo = svcs.length === 0;

  // 1. 稳定性：在线服务比例
  const stability = svcs.length ? Math.min(1, (online / svcs.length) * 1.25) : 0.85;

  // 2. 性能：任务成功率 + 平均耗时
  const rate = overview.success_rate === null ? 0.92 : overview.success_rate / 100;
  const avgDur = overview.tasks_avg_duration || 0;
  let performance = rate * 80;
  if (avgDur > 0) {
    if (avgDur < 120) performance += 20;
    else if (avgDur < 300) performance += 15;
    else if (avgDur < 600) performance += 10;
    else performance += 5;
  } else {
    performance += 10;
  }
  performance = Math.min(100, Math.round(performance));

  // 3. 资源：容器 CPU/内存使用率（40-60% 为最佳）
  const resources = mon && mon.resources ? Object.values(mon.resources) : [];
  let resourceScore = 75;
  if (resources.length > 0) {
    const avgCpu = resources.reduce((s, r) => s + (r.cpu || 0), 0) / resources.length;
    const avgMem = resources.reduce((s, r) => s + (r.mem || 0), 0) / resources.length;
    const cpuScore = avgCpu < 40 ? 100 : avgCpu < 60 ? 90 : avgCpu < 80 ? 70 : 50;
    const memScore = avgMem < 40 ? 100 : avgMem < 60 ? 90 : avgMem < 80 ? 70 : 50;
    resourceScore = Math.round((cpuScore + memScore) / 2);
  }

  // 4. 安全：SSH 密钥配置率（后端无服务器数据时 score 为 null → 用中性默认 75，避免误导性满分）
  const security = (mon && mon.security && mon.security.score != null) ? mon.security.score : 75;

  // 5. 可用性：发布成功率
  const availability = Math.round(rate * 100);

  // 6. 响应：HTTP 健康检查成功率 + 响应时间
  const healthChecks = mon && mon.health_checks ? Object.values(mon.health_checks) : [];
  let responseScore = 85;
  if (healthChecks.length > 0) {
    const okCount = healthChecks.filter(h => h.ok).length;
    const avgMs = healthChecks.reduce((s, h) => s + (h.ms || 0), 0) / healthChecks.length;
    const okRate = okCount / healthChecks.length;
    let msScore = 100;
    if (avgMs < 100) msScore = 100;
    else if (avgMs < 300) msScore = 90;
    else if (avgMs < 500) msScore = 80;
    else if (avgMs < 1000) msScore = 70;
    else msScore = 60;
    responseScore = Math.round(okRate * 70 + (msScore / 100) * 30);
  }

  return { demo, values: [
    Math.round(stability * 100), performance, resourceScore, security, availability, responseScore] };
}
