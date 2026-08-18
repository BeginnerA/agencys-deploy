// config-pipeline-editor.js — 流水线画布编辑器：服务池拖拽 + 阶段流编排 + 模板 + 实时发布命令预览
(function(){
  'use strict';
  const H = window.cfgHelpers;

  function open(container, project, pl, onSaved){
    const st = {
      project, container, onSaved,
      name: pl ? pl.name : '',
      buildFirst: pl ? !!pl.build_first : true,
      stages: Array.isArray(pl && pl.stages) ? JSON.parse(JSON.stringify(pl.stages)) : [],
      services: [],
      query: '',
      verEdit: null,   // {i, j} 正在编辑版本的服务块坐标
    };
    openDrawer({ title: pl ? '编辑流水线：' + pl.name : '新建流水线', wide: true,
      body: '<div class="cfg-home">' + skeletonHTML(3) + '</div>', onClose: null });
    const bodyEl = document.getElementById('dwBody');
    (async () => {
      try{
        st.services = await api(`/api/projects/${project.id}/services`);
        render();
      }catch(e){ bodyEl.innerHTML = errorState('加载失败：' + esc(e.message), 'navRetry'); }
    })();

    const usedSet = () => {
      const s = new Set();
      st.stages.forEach(sg => (sg.services || []).forEach(x => s.add(x.service_id)));
      return s;
    };
    const svcName = id => { const x = st.services.find(s => s.id === id); return x ? x.name : ('#' + id); };

    function render(){
      const used = usedSet();
      const q = st.query.trim().toLowerCase();
      const pool = st.services.filter(s => !q || s.name.toLowerCase().includes(q));
      bodyEl.innerHTML = `
        <div style="display:flex;align-items:flex-end;gap:10px;flex-wrap:wrap;margin-bottom:14px">
          <div class="field" style="flex:1;min-width:200px;margin:0"><label>流水线名称 *</label><input id="plName" value="${esc(st.name)}" placeholder="如 full-release"></div>
          <label class="mode-switch" style="margin-bottom:12px"><input type="checkbox" id="plBuildFirst" ${st.buildFirst ? 'checked' : ''}> 构建优先（先统一构建全部镜像再发布）</label>
          <div style="margin-bottom:12px"><select class="filter-sel" id="plTpl">
            <option value="">＋ 套用模板…</option>
            ${H.PIPELINE_TEMPLATES.map(t => `<option value="${t.id}">${esc(t.name)}：${esc(t.desc)}</option>`).join('')}
          </select></div>
        </div>
        <div class="pl-canvas">
          <div class="pl-pool">
            <div class="pp-title">服务池 <span class="weak" style="font-weight:400">（拖到下方阶段）</span></div>
            <div class="pp-search"><input id="plSearch" placeholder="搜索服务…" value="${esc(st.query)}"></div>
            <div class="pp-list">
              ${pool.length ? pool.map(s => `
                <div class="pl-svc ${used.has(s.id) ? 'used' : ''}" draggable="${!used.has(s.id)}" data-svc="${s.id}">
                  <span>⚙️</span><span>${esc(s.name)}</span>${used.has(s.id) ? '<span class="usd">已用</span>' : ''}
                </div>`).join('') : (st.services.length ? '<div class="weak" style="font-size:11px;padding:6px">无匹配服务</div>' : '<div class="weak" style="font-size:11px;padding:6px">暂无服务，请先到服务页创建</div>')}
            </div>
          </div>
          <div class="pl-stages" id="plStages">
            ${st.stages.map((sg, i) => stageHTML(sg, i)).join('')}
            <div class="add-stage" id="plAddStage">＋ 添加阶段</div>
          </div>
        </div>
        <div class="pl-preview" id="plPreview"></div>
        <div class="pl-tip">阶段按从上到下顺序执行，某阶段失败则停止后续阶段；并行阶段内的服务会同时发布，请确保目标服务器不冲突。</div>
        <div style="display:flex;gap:10px;margin-top:14px"><button class="btn primary flex-1" id="plSave">✓ 保存流水线</button></div>`;
      bind();
      updatePreview();
    }

    function stageHTML(sg, i){
      const svcs = (sg.services || []).map((x, j) => {
        const ed = st.verEdit && st.verEdit.i === i && st.verEdit.j === j;
        return `
        <div class="svc-block" draggable="true" data-block="${i}:${j}">
          <span class="sb-name">${esc(svcName(x.service_id))}</span>
          <span class="sb-ver" data-ver="${i}:${j}" title="设置服务级版本（覆盖命令行 --version/--previous-version）">${x.version ? 'v' + esc(x.version) : '版本'}</span>
          <span class="sb-x" data-x="${i}:${j}">✕</span>
        </div>
        ${ed ? `
        <div class="svc-ver-ed" style="display:flex;gap:6px;align-items:center;padding:6px 8px;border:1px dashed var(--border-strong);border-radius:var(--r-ctl);flex-wrap:wrap">
          <input style="flex:1;min-width:130px" placeholder="新版本号（覆盖 --version）" value="${esc(x.version || '')}" id="svVer">
          <input style="flex:1;min-width:130px" placeholder="回滚版本（覆盖 --previous-version）" value="${esc(x.previous_version || '')}" id="svPrev">
          <button class="btn sm primary" id="svVerOk">✓</button>
          <button class="btn sm ghost" id="svVerCancel">取消</button>
        </div>` : ''}`;
      }).join('');
      return `
      <div class="stage-card" data-stage="${i}">
        <div class="sc-head">
          <span class="sc-drag" draggable="true" title="拖动调整阶段顺序">⠿</span>
          <input class="sc-name" value="${esc(sg.name || '')}" placeholder="阶段名，如 网关">
          <label class="sc-parallel"><input type="checkbox" class="sc-par" ${sg.parallel ? 'checked' : ''}> 并行</label>
          <button class="sc-del" title="删除阶段">🗑</button>
        </div>
        <div class="stage-warn">⚠ 并行阶段内的服务会同时发布，请确保目标服务器不冲突</div>
        <div class="stage-svc ${svcs ? '' : 'empty'}" data-drop="${i}">
          ${svcs || '<span>拖入服务</span>'}
        </div>
      </div>`;
    }

    function bind(){
      const plName = document.getElementById('plName');
      if (plName) plName.oninput = () => { st.name = plName.value; updatePreview(); };
      document.getElementById('plBuildFirst').onchange = e => { st.buildFirst = e.target.checked; updatePreview(); };
      // 搜索仅刷新服务池列表，避免丢失输入焦点
      document.getElementById('plSearch').oninput = e => {
        st.query = e.target.value;
        const q = st.query.trim().toLowerCase();
        const used = usedSet();
        const list = document.querySelector('.pp-list');
        if (!list) return;
        list.innerHTML = st.services.filter(s => !q || s.name.toLowerCase().includes(q)).map(s => `
          <div class="pl-svc ${used.has(s.id) ? 'used' : ''}" draggable="${!used.has(s.id)}" data-svc="${s.id}">
            <span>⚙️</span><span>${esc(s.name)}</span>${used.has(s.id) ? '<span class="usd">已用</span>' : ''}
          </div>`).join('') || (st.services.length ? '<div class="weak" style="font-size:11px;padding:6px">无匹配服务</div>' : '<div class="weak" style="font-size:11px;padding:6px">暂无服务，请先到服务页创建</div>');
        bindPool(list);
      };
      bindPool(document.querySelector('.pp-list'));

      document.getElementById('plTpl').onchange = e => {
        const t = H.PIPELINE_TEMPLATES.find(x => x.id === e.target.value);
        if (!t) return;
        e.target.value = '';
        st.stages = H.applyPipelineTemplate(t, st.services);
        st.verEdit = null;
        render();
        toastSuccess('已按服务名自动归位到模板阶段，可再手动调整');
      };
      document.getElementById('plAddStage').onclick = () => { st.stages.push({ name:'新阶段', parallel:false, services:[] }); render(); };
      document.getElementById('plSave').onclick = save;

      // ---- 阶段卡：名称/并行/删除/整卡排序 ----
      document.querySelectorAll('.stage-card').forEach(card => {
        const i = Number(card.dataset.stage);
        card.querySelector('.sc-name').oninput = e => { st.stages[i].name = e.target.value; };
        card.querySelector('.sc-par').onchange = e => {
          st.stages[i].parallel = e.target.checked;
          card.classList.toggle('parallel', e.target.checked);
        };
        card.querySelector('.sc-del').onclick = async () => {
          const cnt = (st.stages[i].services || []).length;
          if (cnt && !await confirmDialog(`阶段「${st.stages[i].name || (i + 1)}」内有 ${cnt} 个服务，删除阶段将同时移除它们，确认？`)) return;
          st.stages.splice(i, 1);
          st.verEdit = null;
          render();
        };
        card.addEventListener('dragover', e => {
          if (e.dataTransfer.types.includes('text/plain')){ e.preventDefault(); card.style.borderColor = 'var(--primary)'; }
        });
        card.addEventListener('dragleave', () => { card.style.borderColor = ''; });
        card.addEventListener('drop', e => {
          e.preventDefault(); card.style.borderColor = '';
          const t = e.dataTransfer.getData('text/plain') || '';
          if (!t.startsWith('stage:')) return;
          const from = Number(t.split(':')[1]);
          if (from === i) return;
          const [sg] = st.stages.splice(from, 1);
          st.stages.splice(i, 0, sg);
          st.verEdit = null;   // 阶段顺序变化，版本编辑器坐标失效
          render();
        });
      });
      document.querySelectorAll('.sc-drag').forEach(h => {
        h.addEventListener('dragstart', e => {
          e.dataTransfer.setData('text/plain', 'stage:' + h.closest('.stage-card').dataset.stage);
          e.dataTransfer.effectAllowed = 'move';
        });
      });

      // ---- 阶段容器：拖入（来自服务池或其它阶段） ----
      document.querySelectorAll('.stage-svc').forEach(dz => {
        dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('dragover'); });
        dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
        dz.addEventListener('drop', e => {
          e.preventDefault(); dz.classList.remove('dragover');
          const t = e.dataTransfer.getData('text/plain') || '';
          const i = Number(dz.dataset.drop);
          if (t.startsWith('svc:')){
            const sid = Number(t.split(':')[1]);
            if (usedSet().has(sid)) return;
            st.stages[i].services = st.stages[i].services || [];
            st.stages[i].services.push({ service_id: sid });
            st.verEdit = null;   // 结构变更后旧坐标失效
            render();
          }else if (t.startsWith('block:')){
            const [fi, fj] = t.split(':').slice(1).map(Number);
            if (fi === i) return;
            const [svc] = st.stages[fi].services.splice(fj, 1);
            st.stages[i].services = st.stages[i].services || [];
            st.stages[i].services.push(svc);
            st.verEdit = null;   // 结构变更后旧坐标失效
            render();
          }
        });
      });

      // ---- 服务块：拖拽源 + 作为放置目标（拖内排序/插入到指定位置） ----
      document.querySelectorAll('.svc-block').forEach(b => {
        b.addEventListener('dragstart', e => {
          e.dataTransfer.setData('text/plain', 'block:' + b.dataset.block);
          e.dataTransfer.effectAllowed = 'move';
        });
        b.addEventListener('dragover', e => { e.preventDefault(); });
        b.addEventListener('drop', e => {
          e.preventDefault();
          const t = e.dataTransfer.getData('text/plain') || '';
          const [ti, tj] = b.dataset.block.split(':').map(Number);
          if (t.startsWith('svc:')){
            const sid = Number(t.split(':')[1]);
            if (usedSet().has(sid)) return;
            st.stages[ti].services.splice(tj, 0, { service_id: sid });
            st.verEdit = null;   // 结构变更后旧坐标失效
            render();
          }else if (t.startsWith('block:')){
            const [fi, fj] = t.split(':').slice(1).map(Number);
            if (fi === ti && fj === tj) return;
            const [svc] = st.stages[fi].services.splice(fj, 1);
            // 同阶段内向前移动：删除后索引前移一位；跨阶段则索引不变
            const ins = (fi === ti && fj < tj) ? tj - 1 : tj;
            st.stages[ti].services.splice(Math.max(ins, 0), 0, svc);
            st.verEdit = null;   // 结构变更后旧坐标失效
            render();
          }
        });
      });

      // ---- 服务块：移出 / 版本设置 ----
      document.querySelectorAll('.sb-x').forEach(x => {
        x.onclick = () => {
          const [i, j] = x.dataset.x.split(':').map(Number);
          st.stages[i].services.splice(j, 1);
          st.verEdit = null;
          render();
        };
      });
      document.querySelectorAll('.sb-ver').forEach(v => {
        v.onclick = () => {
          const [i, j] = v.dataset.ver.split(':').map(Number);
          st.verEdit = (st.verEdit && st.verEdit.i === i && st.verEdit.j === j) ? null : { i, j };
          render();
        };
      });
      const verOk = document.getElementById('svVerOk');
      if (verOk) verOk.onclick = () => {
        const svc = st.stages[st.verEdit.i].services[st.verEdit.j];
        const ver = val('svVer').trim();
        const prev = val('svPrev').trim();
        if (ver) svc.version = ver; else delete svc.version;
        if (prev) svc.previous_version = prev; else delete svc.previous_version;
        st.verEdit = null;
        render();
      };
      const verCancel = document.getElementById('svVerCancel');
      if (verCancel) verCancel.onclick = () => { st.verEdit = null; render(); };
    }

    function bindPool(list){
      if (!list) return;
      list.querySelectorAll('.pl-svc').forEach(el => {
        el.addEventListener('dragstart', e => {
          e.dataTransfer.setData('text/plain', 'svc:' + el.dataset.svc);
          e.dataTransfer.effectAllowed = 'copy';
        });
      });
    }

    function updatePreview(){
      const el = document.getElementById('plPreview');
      if (!el) return;
      const hasVer = st.stages.some(sg => (sg.services || []).some(x => x.version));
      el.textContent = H.pipelineCmd(st.name || 'pipeline', st.project.id, hasVer);
      el.title = hasVer ? '存在服务级版本：命令行 --version 仅作为默认值，会被服务级版本覆盖' : '发布时通过 --version 指定版本号';
    }

    async function save(){
      if (!st.name.trim()){ toastError('流水线名称必填'); return; }
      // 从 DOM 收集阶段名（并行开关已在事件中同步到数据）
      document.querySelectorAll('.stage-card').forEach(card => {
        const i = Number(card.dataset.stage);
        st.stages[i].name = card.querySelector('.sc-name').value.trim();
      });
      const payload = { name: st.name.trim(), build_first: st.buildFirst, stages: st.stages };
      try{
        if (pl) await api(`/api/projects/${project.id}/pipelines/${pl.id}`, { method: 'PUT', body: JSON.stringify(payload) });
        else await api(`/api/projects/${project.id}/pipelines`, { method: 'POST', body: JSON.stringify(payload) });
        toastSuccess(pl ? '流水线已更新' : '流水线已创建');
        const dr = document.getElementById('drawerRoot');
        dr.classList.add('hidden'); dr.innerHTML = '';
        if (onSaved) onSaved(); else navRetry();
      }catch(e){ toastError(e.message); }
    }

    function val(id){ const el = document.getElementById(id); return el ? el.value : ''; }
  }

  window.cfgPipelineEditor = { open };
})();
