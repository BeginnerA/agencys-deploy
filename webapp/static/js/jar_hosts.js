// jar_hosts.js — jar_service 每主机版本/进程视图（纯渲染，供设备页抽屉与 Node 用例）
(function(global){
  const STATUS_LABEL = { online: '在线', stopped: '已停止',
                         not_deployed: '未部署', probe_failed: '探测失败',
                         unknown: '未知' };
  const DOT_CLASS = { online: 'ok', stopped: 'bad', probe_failed: 'warn' };

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
        <td class="muted">${r.live ? '实时' : '版本快照'}</td>
      </tr>`;
    }).join('');
    const hint = rows.some(r => !r.live)
      ? '<div class="weak" style="margin-top:8px;font-size:12px">'
        + '「版本快照」来自最近一次发布/监控回写；开启实时监控后可看到进程状态。</div>'
      : '';
    return `<div class="tbl-wrap"><table class="tbl"><thead><tr>
      <th>主机</th><th>状态</th><th>当前版本</th><th>上一版本</th>
      <th>PID</th><th>运行时长</th><th>重启</th><th>CPU</th><th>内存</th><th>数据</th>
    </tr></thead><tbody>${body}</tbody></table></div>${hint}`;
  }

  global.jarHostTableHTML = jarHostTableHTML;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { jarHostTableHTML };
  }
})(typeof window !== 'undefined' ? window : globalThis);
