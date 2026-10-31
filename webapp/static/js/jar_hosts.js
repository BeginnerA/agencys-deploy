// jar_hosts.js — jar_service 每主机版本/进程视图（纯渲染，供设备页抽屉与 Node 用例）
(function(global){
  const STATUS_LABEL = { online: '在线', stopped: '已停止',
                         not_deployed: '未部署', probe_failed: '探测失败',
                         unknown: '未知',
                         unhealthy: '不健康', unmanaged: '未托管' };
  const DOT_CLASS = { online: 'ok', stopped: 'bad', probe_failed: 'warn',
                      unhealthy: 'bad', unmanaged: 'warn' };
  // WAR 实例托管列（字段存在才启用；不改变 jar 行既有输出）
  const CONFIG_LABEL = { managed: '受管', maintenance: '维护中',
                         owner_mismatch: '归属不一致', unmanaged: '未接入' };
  const OP_LABEL = { idle: '空闲', in_progress: '操作中' };
  const PHASE_LABEL = { preflight: '预检', traffic: '摘流/恢复流量',
                        stopping: '停止', switching: '切换', starting: '启动',
                        health: '健康检查', recovery: '自动恢复',
                        rollback_preflight: '回滚预检', rollback_stop: '回滚停止',
                        restart_preflight: '重启预检', restart_stop: '重启停止' };
  const APP_LABEL = { ok: '健康', failed: '不健康', unknown: '未知' };
  const RECOVERY_LABEL = { not_needed: '无需', succeeded: '成功',
                           blocked: '受阻', failed: '失败',
                           unavailable: '不可用', unknown: '未知' };

  function esc(v){
    return String(v == null ? '' : v).replace(/[&<>"']/g, c =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  function fmtUptime(h){
    if (h == null || h < 0) return '—';
    if (h < 1) return Math.max(1, Math.round(h * 60)) + '分钟';
    if (h < 48) return h.toFixed(1) + '小时';
    return Math.floor(h / 24) + '天' + Math.floor(h % 24) + '时';
  }
  function fmtNum(v, unit){
    return (v == null || v === '') ? '—' : esc(v) + (unit || '');
  }

  function jarHostTableHTML(rows){
    rows = Array.isArray(rows) ? rows : [];
    if (!rows.length) {
      return '<div class="empty-state"><div class="e-ico">🖥️</div>'
           + '<div>暂无主机版本数据</div></div>';
    }
    const warMode = rows.some(r => r && Object.prototype.hasOwnProperty.call(r, 'config_status'));
    const body = rows.map(r => {
      const status = r.status || 'unknown';
      return `<tr>
        <td><span class="dot ${DOT_CLASS[status] || 'gray'}"></span><span class="num">${esc(r.host)}</span></td>
        <td>${esc(STATUS_LABEL[status] || status)}</td>
        <td class="num">${esc(r.version || '—')}</td>
        <td class="num">${esc(r.previous_version || '—')}</td>
        <td class="num">${r.pid ? esc(r.pid) : '—'}</td>
        <td>${fmtUptime(r.uptime_hours)}</td>
        <td class="num">${r.restarts == null ? '—' : esc(r.restarts)}</td>
        <td class="num">${fmtNum(r.cpu, '%')}</td>
        <td class="num">${fmtNum(r.rss_mb, ' MB')}</td>
        ${warMode ? `<td>${esc(STATUS_LABEL[r.process_status] || r.process_status || '—')}</td>
          <td>${esc(APP_LABEL[r.app_health] || r.app_health || '—')}</td>
          <td>${esc(RECOVERY_LABEL[r.recovery_status] || r.recovery_status || '—')}</td>
          <td>${esc(CONFIG_LABEL[r.config_status] || r.config_status || '—')}</td>
          <td>${r.version_drift ? '是' : '否'}</td>
          <td>${r.maintenance ? '是' : '否'}</td>
          <td>${esc(r.operation_state === 'in_progress'
                    ? (PHASE_LABEL[r.operation_phase] || '操作中')
                    : (OP_LABEL[r.operation_state] || r.operation_state || '—'))}</td>` : ''}
        <td class="muted">${r.live ? '实时' : '版本快照'}</td>
      </tr>`;
    }).join('');
    const hint = rows.some(r => !r.live)
      ? '<div class="weak" style="margin-top:8px;font-size:12px">'
        + '「版本快照」来自最近一次发布/监控回写；开启实时监控后可看到进程状态。</div>'
      : '';
    return `<div class="tbl-wrap"><table class="tbl"><thead><tr>
      <th>主机</th><th>状态</th><th>当前版本</th><th>上一版本</th>
      <th>PID</th><th>运行时长</th><th>重启</th><th>CPU</th><th>内存</th>
      ${warMode ? '<th>进程</th><th>应用</th><th>恢复</th><th>配置</th><th>漂移</th><th>维护</th><th>操作</th>' : ''}
      <th>数据</th>
    </tr></thead><tbody>${body}</tbody></table></div>${hint}`;
  }

  global.jarHostTableHTML = jarHostTableHTML;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { jarHostTableHTML };
  }
})(typeof window !== 'undefined' ? window : globalThis);
