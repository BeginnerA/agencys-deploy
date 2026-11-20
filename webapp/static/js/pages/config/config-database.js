// config-database.js — 平台库快照与恢复（#/config/database，仅管理员）
(function(){
  'use strict';

  const KIND_CN = { manual:'手工', 'pre-migration':'迁移前自动', 'pre-restore':'恢复前自动' };

  // 纯展示逻辑，不碰 DOM：node tests/js/config_database_view_test.js 覆盖这一层
  const view = {
    kindLabel(kind){ return KIND_CN[kind] || String(kind || ''); },
    // 空值不能显示成「0 B」：那是真的 0 字节文件，和「没登记大小」是两回事
    fmtBytes(n){
      const v = Number(n);
      if (n === null || n === undefined || n === '' || !Number.isFinite(v) || v < 0) return '—';
      if (v < 1024) return v + ' B';
      const units = ['KB','MB','GB','TB'];
      let i = -1, x = v;
      while (x >= 1024 && i < units.length - 1){ x /= 1024; i++; }
      return x.toFixed(1) + ' ' + units[i];
    },
    canRestore(row){ return !!row && !row.file_missing; },
    // 两段式恢复的第一段只写标记，横幅必须说「还没换库」，否则管理员以为已经回滚完成
    pendingBannerText(p){
      if (!p) return '';
      return '已暂存恢复：' + p.name + '。线上库此刻尚未改动，重启平台后才会换库；'
           + '若这不是你要的结果，在重启前点「撤回待恢复」即可取消。';
    },
    restoreConfirmText(row){
      return '确认用快照「' + row.name + '」替换平台库？平台会先把当前库另存为一条'
           + '「恢复前自动」快照，本次操作还能再回退；换库在平台重启后生效（重启前可撤回），'
           + '重启后该时点之后的配置、任务与审计记录不再可见。';
    },
    // 边界由后端随策略一起下发，前端只回显，判据不在这里复制第二份
    retentionSummaryText(r){
      const b = (r && r.bounds) || {}, k = b.keep || [], a = b.audit_retention_days || [];
      return '快照保留最近 ' + r.keep + ' 份（可设 ' + k[0] + '~' + k[1] + '），审计日志保留 '
           + r.audit_retention_days + ' 天（可设 ' + a[0] + '~' + a[1] + '）；'
           + '轮转与清理由平台每小时的维护周期执行。';
    },
  };

  let mountedRoot = null;

  async function render(root){
    mountedRoot = root;
    root.innerHTML = '<div class="cfg-home">' + skeletonHTML(4) + '</div>';
    try{
      const data = await api('/api/db/backups');
      paint(root, data);
    }catch(e){
      root.innerHTML = errorState('快照台账加载失败：' + esc(e.message), 'navRetry');
    }
    return [];
  }

  function paint(root, data){
    const backups = data.backups || [], pending = data.pending, retention = data.retention || {};
    root.innerHTML = `<div class="cfg-home">
      <div style="display:flex;align-items:center;gap:12px">
        <div class="page-title">数据库快照与恢复</div>
        <button class="btn sm ghost" onclick="location.hash='#/config'">← 返回工作台</button>
      </div>
      <div class="page-sub">平台库（配置 / 任务 / 审计）的本地快照：不停机拍紧凑副本，回滚需重启生效</div>
      ${pending ? `<div class="db-banner">
        <span class="dbb-ico">⏳</span>
        <div class="dbb-text">${esc(view.pendingBannerText(pending))}</div>
        <button class="btn sm" id="dbCancelPending">撤回待恢复</button>
      </div>` : ''}
      <div class="db-tools">
        <button class="btn primary" id="dbSnapNow">📸 立即快照</button>
        <span class="db-hint">不影响正在进行的发布；每次新建后也会自动留一份「迁移前自动」快照</span>
        <button class="btn ghost" id="dbReload">刷新</button>
      </div>
      <div class="panel db-retention">
        <div class="section-title">保留策略</div>
        <div class="field"><label>快照保留份数</label>
          <input id="dbKeep" type="number" min="${retention.bounds ? retention.bounds.keep[0] : ''}"
                 max="${retention.bounds ? retention.bounds.keep[1] : ''}" value="${retention.keep}"></div>
        <div class="field"><label>审计日志保留天数</label>
          <input id="dbAuditDays" type="number"
                 min="${retention.bounds ? retention.bounds.audit_retention_days[0] : ''}"
                 max="${retention.bounds ? retention.bounds.audit_retention_days[1] : ''}"
                 value="${retention.audit_retention_days}"></div>
        <div class="field db-save-btn"><label>&nbsp;</label><button class="btn" id="dbSaveRet">保存策略</button></div>
        <div class="hint">${esc(view.retentionSummaryText(retention))}</div>
      </div>
      <div class="panel" style="padding:0">
        ${backups.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr>
          <th>时间</th><th>种类</th><th>标签</th><th>快照大小</th><th>校验</th><th>操作</th></tr></thead><tbody>
          ${backups.map(rowHTML).join('')}</tbody></table></div>`
        : `<div class="empty-state"><div class="e-ico">📦</div><div>还没有快照，先点「立即快照」</div></div>`}
      </div>
    </div>`;

    document.getElementById('dbSnapNow').onclick = takeSnapshot;
    document.getElementById('dbReload').onclick = () => repaint();
    document.getElementById('dbSaveRet').onclick = saveRetention;
    const cp = document.getElementById('dbCancelPending');
    if (cp) cp.onclick = cancelPending;
    root.querySelectorAll('[data-restore]').forEach(btn => {
      btn.onclick = () => stageRestore(btn.dataset.restore, backups.find(b => b.name === btn.dataset.restore));
    });
  }

  function rowHTML(b){
    const size = view.fmtBytes(b.size_bytes)
      + (b.src_bytes ? ` <span class="weak">← ${view.fmtBytes(b.src_bytes)}</span>` : '');
    const verify = b.file_missing
      ? '<span class="badge critical">文件已丢失</span>'
      : (b.sha256
        ? `<span class="weak" title="恢复前会重新计算比对">已记录</span>`
        : '<span class="badge warning">校验值待补</span>');
    return `<tr>
      <td>${b.created_at ? esc(fmtTime(b.created_at)) : '<span class="weak">未知</span>'}</td>
      <td>${esc(view.kindLabel(b.kind))}${b.id ? ` <span class="weak">#${b.id}</span>` : ''}</td>
      <td>${b.label ? esc(b.label) : '<span class="weak">-</span>'}</td>
      <td>${size}</td>
      <td>${verify}</td>
      <td>${view.canRestore(b)
        ? `<button class="btn sm danger" data-restore="${esc(b.name)}">恢复到此快照</button>`
        : '<span class="weak">不可恢复</span>'}</td>
    </tr>`;
  }

  async function repaint(){
    if (!mountedRoot) return;
    await render(mountedRoot);
  }

  async function takeSnapshot(){
    const label = await promptDialog('本次快照备注（可留空）', '', '例如：新增环境之前');
    if (label === null) return;   // 取消：什么都不做
    try{
      const rec = await api('/api/db/backups', { method:'POST', body: JSON.stringify({ label }) });
      toastSuccess('快照已生成：' + rec.name + '（' + view.fmtBytes(rec.size_bytes) + '）');
      await repaint();
    }catch(e){ toastError(e.message); }
  }

  async function stageRestore(name, row){
    if (!await confirmDialog(view.restoreConfirmText(row || { name }), '恢复前确认')) return;
    try{
      await api('/api/db/backups/' + encodeURIComponent(name) + '/restore', { method:'POST' });
      toastSuccess('已暂存恢复，重启平台后生效（重启前可在本页撤回）');
      await repaint();
    }catch(e){ toastError(e.message); }
  }

  async function cancelPending(){
    if (!await confirmDialog('撤回后待恢复标记与暂存副本一并删除，快照台账不受影响。确认撤回？', '撤回待恢复')) return;
    try{
      const res = await api('/api/db/restore-pending', { method:'DELETE' });
      toastSuccess(res.cancelled ? '已撤回，重启后不会换库' : '没有待恢复项（可能已随重启生效）');
      await repaint();
    }catch(e){ toastError(e.message); }
  }

  async function saveRetention(){
    const body = { keep: Number(val('dbKeep')), audit_retention_days: Number(val('dbAuditDays')) };
    try{
      await api('/api/db/retention', { method:'PUT', body: JSON.stringify(body) });
      toastSuccess('保留策略已保存');
      await repaint();
    }catch(e){ toastError(e.message); }
  }

  function val(id){ return document.getElementById(id).value; }

  window.cfgDbView = view;
  window.cfgDb = { render, destroy(){ mountedRoot = null; } };
})();
