// pages/deploy_canary.js — 灰度策略配置（模板驱动表单编辑器）
(function(){
  let destroyed = false;
  let configs = [];
  let projects = [];

  // 预设模板
  const CANARY_TEMPLATES = {
    standard: {
      name: '标准三批',
      steps: [
        { servers: 1, observe_minutes: 5, auto_advance: true },
        { servers: 2, observe_minutes: 10, auto_advance: true },
        { servers: 'all', observe_minutes: 0, auto_advance: false }
      ]
    },
    quick: {
      name: '快速两批',
      steps: [
        { servers: 1, observe_minutes: 3, auto_advance: true },
        { servers: 'all', observe_minutes: 0, auto_advance: false }
      ]
    },
    conservative: {
      name: '保守五批',
      steps: [
        { servers: 1, observe_minutes: 5, auto_advance: false },
        { servers: 2, observe_minutes: 10, auto_advance: true },
        { servers: 3, observe_minutes: 15, auto_advance: true },
        { servers: 5, observe_minutes: 10, auto_advance: true },
        { servers: 'all', observe_minutes: 0, auto_advance: false }
      ]
    },
    full: {
      name: '全量一次',
      steps: [
        { servers: 'all', observe_minutes: 0, auto_advance: false }
      ]
    }
  };

  async function loadData(){
    projects = await api('/api/projects').catch(() => []);
    configs = await api('/api/deploy/canary-configs').catch(() => []);
  }

  function renderStepsPreview(steps){
    if (!Array.isArray(steps) || !steps.length) return '';
    const maxSrv = Math.max(...steps.map(s => s.servers === 'all' ? 10 : s.servers));
    return `<div class="canary-preview">
      ${steps.map((s, i) => {
        const srv = s.servers === 'all' ? '全量' : s.servers + '台';
        const h = s.servers === 'all' ? 100 : Math.max(20, (s.servers / maxSrv) * 100);
        return `<div class="preview-bar" style="height:${h}%" title="${srv} · ${s.observe_minutes}min · ${s.auto_advance ? '自动' : '人工'}">
          <span class="preview-label">${srv}</span>
          <span class="preview-time">${s.observe_minutes}min</span>
        </div>`;
      }).join('')}
    </div>`;
  }

  function renderStepsDisplay(steps){
    if (!Array.isArray(steps)) return '-';
    return steps.map((s, i) => `
      ${i > 0 ? '<span class="canary-arrow">→</span>' : ''}
      <div class="canary-step">
        <div class="step-num">${s.servers === 'all' ? '全量' : s.servers + '台'}</div>
        <div class="step-label">观察 ${s.observe_minutes}min</div>
        <div class="step-label">${s.auto_advance ? '自动推进' : '人工确认'}</div>
      </div>`).join('');
  }

  function renderList(){
    const root = document.getElementById('pageRoot');
    const box = root.querySelector('#canaryBox');
    if (!box) return;
    const projMap = {};
    projects.forEach(p => projMap[p.id] = p.name);
    box.innerHTML = configs.length ? configs.map(c => `
      <div class="panel" style="padding:16px;margin-bottom:12px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
          <div><strong>${esc(c.name)}</strong> <span class="muted">· ${esc(projMap[c.project_id] || '全局')}</span>
          ${c.is_default ? ' <span class="tag tag-ok">默认</span>' : ''}</div>
          <div style="display:flex;gap:8px">
            ${canDeploy() ? `<button class="btn ghost sm" data-edit="${c.id}">编辑</button>
            <button class="btn danger ghost sm" data-del="${c.id}">删除</button>` : ''}
          </div>
        </div>
        <div class="canary-steps">${renderStepsDisplay(c.steps)}</div>
      </div>`).join('')
      : `<div class="panel">${emptyState('🐤', '暂无灰度策略')}</div>`;
    box.querySelectorAll('[data-del]').forEach(btn => btn.onclick = async () => {
      if (!await confirmDialog('确认删除此策略？')) return;
      await api(`/api/deploy/canary-configs/${btn.dataset.del}`, {method:'DELETE'});
      await loadData(); renderList();
    });
    box.querySelectorAll('[data-edit]').forEach(btn => btn.onclick = () => {
      const c = configs.find(x => x.id === Number(btn.dataset.edit));
      if (c) openForm(c);
    });
  }

  function openForm(existing){
    const isEdit = !!existing;
    const title = isEdit ? '编辑灰度策略' : '新建灰度策略';
    let batchRows = existing ? (existing.steps || []).map(s => ({...s})) : [];

    const body = `
      <div class="form-grid">
        <div class="field"><label>策略名称</label><input id="canaryName" value="${esc(existing ? existing.name : '')}" placeholder="如：标准三批"></div>
        <div class="field"><label>所属项目</label>
          <select id="canaryProj">${projects.map(p =>
            `<option value="${p.id}" ${existing && p.id === existing.project_id ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select></div>
        ${!isEdit ? `<div class="field full">
          <label>快速模板</label>
          <div class="template-pills" id="templatePills">
            ${Object.entries(CANARY_TEMPLATES).map(([k, v]) =>
              `<button class="pill" data-tpl="${k}">${v.name}</button>`).join('')}
            <button class="pill" data-tpl="custom">+ 自定义</button>
          </div>
        </div>` : ''}
        <div class="field full">
          <label>批次配置</label>
          <div id="batchEditor"></div>
        </div>
        <div class="field full">
          <label>预览</label>
          <div id="batchPreview" style="min-height:60px"></div>
        </div>
        <div class="field full check"><label><input type="checkbox" id="canaryDefault" ${existing && existing.is_default ? 'checked' : ''}> 设为默认策略</label></div>
      </div>
      <div style="display:flex;gap:10px;margin-top:20px">
        <button class="btn primary flex-1" id="canarySave">保存</button>
      </div>`;

    openDrawer({ title, wide: true, body });

    function renderBatchEditor(){
      const editor = document.getElementById('batchEditor');
      if (!editor) return;
      if (!batchRows.length) {
        editor.innerHTML = `<p class="muted" style="padding:8px 0">暂无批次，点击下方按钮添加</p>`;
      } else {
        editor.innerHTML = `<table class="tbl" style="margin-bottom:8px">
          <thead><tr><th style="width:40px">#</th><th>服务器数量</th><th>观察时间(分钟)</th><th>推进方式</th><th style="width:40px"></th></tr></thead>
          <tbody>${batchRows.map((r, i) => `<tr>
            <td class="num">${i + 1}</td>
            <td>${r.servers === 'all'
              ? '<input type="number" class="batch-srv" data-i="' + i + '" value="" placeholder="全部" min="1" style="width:80px"> <select class="batch-all-sel" data-i="' + i + '"><option value="all" selected>全部</option><option value="num">指定数量</option></select>'
              : '<input type="number" class="batch-srv" data-i="' + i + '" value="' + r.servers + '" min="1" style="width:80px"> <select class="batch-all-sel" data-i="' + i + '"><option value="num" selected>指定数量</option><option value="all">全部</option></select>'}</td>
            <td><input type="number" class="batch-obs" data-i="${i}" value="${r.observe_minutes}" min="0" style="width:80px"></td>
            <td><select class="batch-adv" data-i="${i}">
              <option value="true" ${r.auto_advance ? 'selected' : ''}>自动推进</option>
              <option value="false" ${!r.auto_advance ? 'selected' : ''}>人工确认</option>
            </select></td>
            <td><button class="btn danger ghost sm" data-rm="${i}">✕</button></td>
          </tr>`).join('')}</tbody>
        </table>
        <button class="btn ghost sm" id="addBatchBtn">+ 添加批次</button>`;
      }
      // 绑定事件
      editor.querySelectorAll('.batch-srv').forEach(inp => {
        inp.oninput = () => { batchRows[Number(inp.dataset.i)].servers = Number(inp.value) || 1; updatePreview(); };
      });
      editor.querySelectorAll('.batch-all-sel').forEach(sel => {
        sel.onchange = () => {
          const idx = Number(sel.dataset.i);
          if (sel.value === 'all') {
            batchRows[idx].servers = 'all';
          } else {
            batchRows[idx].servers = 1;
          }
          renderBatchEditor();
          updatePreview();
        };
      });
      editor.querySelectorAll('.batch-obs').forEach(inp => {
        inp.oninput = () => { batchRows[Number(inp.dataset.i)].observe_minutes = Number(inp.value) || 0; updatePreview(); };
      });
      editor.querySelectorAll('.batch-adv').forEach(sel => {
        sel.onchange = () => { batchRows[Number(sel.dataset.i)].auto_advance = sel.value === 'true'; };
      });
      editor.querySelectorAll('[data-rm]').forEach(btn => {
        btn.onclick = () => { batchRows.splice(Number(btn.dataset.rm), 1); renderBatchEditor(); updatePreview(); };
      });
      const addBtn = document.getElementById('addBatchBtn');
      if (addBtn) addBtn.onclick = () => {
        batchRows.push({ servers: 1, observe_minutes: 5, auto_advance: true });
        renderBatchEditor();
        updatePreview();
      };
      updatePreview();
    }

    function updatePreview(){
      const prev = document.getElementById('batchPreview');
      if (prev) prev.innerHTML = batchRows.length ? renderStepsPreview(batchRows) : '<p class="muted">暂无批次</p>';
    }

    // 模板按钮
    if (!isEdit) {
      setTimeout(() => {
        document.querySelectorAll('#templatePills [data-tpl]').forEach(btn => {
          btn.onclick = () => {
            document.querySelectorAll('#templatePills .pill').forEach(p => p.classList.remove('active'));
            btn.classList.add('active');
            const tpl = CANARY_TEMPLATES[btn.dataset.tpl];
            if (btn.dataset.tpl === 'custom') {
              batchRows = [{ servers: 1, observe_minutes: 5, auto_advance: true }];
            } else if (tpl) {
              batchRows = tpl.steps.map(s => ({...s}));
            }
            renderBatchEditor();
          };
        });
        renderBatchEditor();
      }, 0);
    } else {
      setTimeout(() => { renderBatchEditor(); }, 0);
    }

    // 保存
    setTimeout(() => {
      document.getElementById('canarySave').onclick = async () => {
        const name = document.getElementById('canaryName').value.trim();
        if (!name) { toastError('请填写策略名称'); return; }
        if (!batchRows.length) { toastError('至少需要一个批次'); return; }
        // 规范化 servers
        const steps = batchRows.map((r, i) => ({
          batch: i + 1,
          servers: r.servers === 'all' ? 'all' : Math.max(1, Number(r.servers) || 1),
          observe_minutes: Math.max(0, Number(r.observe_minutes) || 0),
          auto_advance: !!r.auto_advance
        }));
        const payload = {
          project_id: Number(document.getElementById('canaryProj').value),
          name,
          steps,
          is_default: document.getElementById('canaryDefault').checked
        };
        try {
          if (isEdit) {
            await api(`/api/deploy/canary-configs/${existing.id}`, {method:'PUT', body:JSON.stringify(payload)});
            toastSuccess('策略已更新');
          } else {
            await api('/api/deploy/canary-configs', {method:'POST', body:JSON.stringify(payload)});
            toastSuccess('策略已创建');
          }
          document.getElementById('dwClose').click();
          await loadData(); renderList();
        } catch(e) { toastError(e.message); }
      };
    }, 0);
  }

  window.deploy_canary = {
    render: async function(root){
      destroyed = false;
      await loadData();
      if (destroyed) return [];
      root.innerHTML = `
        <div class="deploy-toolbar">
          <span style="flex:1"></span>
          ${canDeploy() ? '<button class="btn primary" id="newCanaryBtn">+ 新建策略</button>' : ''}
        </div>
        <div id="canaryBox"></div>`;
      const nb = document.getElementById('newCanaryBtn');
      if (nb) nb.onclick = () => openForm(null);
      renderList();
      return [];
    },
    destroy: function(){ destroyed = true; }
  };
})();
