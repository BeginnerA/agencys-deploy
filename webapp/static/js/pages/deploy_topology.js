// pages/deploy_topology.js — 服务依赖拓扑 + 影响分析 + 发布顺序
(function(){
  let destroyed = false;
  let projects = [];
  let selectedProject = null;
  let topologyData = { nodes: [], edges: [] };
  let pos = {};          // 节点位置缓存
  let highlightedNode = null;

  const EDGE_COLORS = {
    dubbo:  'rgba(0,212,255,.55)',
    http:   'rgba(0,230,118,.55)',
    manual: 'rgba(254,176,25,.55)',
  };
  const EDGE_LABELS = { dubbo: 'Dubbo', http: 'HTTP', manual: '手动' };

  async function loadProjects(){
    projects = await api('/api/projects').catch(() => []);
    if (projects.length && !selectedProject) selectedProject = projects[0].id;
  }

  async function loadTopology(){
    if (!selectedProject) return;
    topologyData = await api(`/api/deploy/topology/${selectedProject}`).catch(() => ({ nodes:[], edges:[] }));
  }

  /* ---- 力导向布局 ---- */
  function computeLayout(W, H){
    const nodes = topologyData.nodes || [];
    const edges = topologyData.edges || [];
    pos = {};
    const cx = W/2, cy = H/2, radius = Math.min(W,H) * 0.32;
    nodes.forEach((n, i) => {
      const angle = (2 * Math.PI * i) / Math.max(nodes.length, 1) - Math.PI/2;
      pos[n.name] = { x: cx + Math.cos(angle) * radius, y: cy + Math.sin(angle) * radius };
    });
    // 弹簧力 + 斥力，50 次迭代
    for (let iter = 0; iter < 50; iter++){
      const alpha = 1 - iter / 50;
      // 斥力（所有节点对）
      for (let i = 0; i < nodes.length; i++){
        for (let j = i+1; j < nodes.length; j++){
          const a = pos[nodes[i].name], b = pos[nodes[j].name];
          if (!a || !b) continue;
          let dx = b.x - a.x, dy = b.y - a.y;
          let dist = Math.sqrt(dx*dx + dy*dy) || 1;
          let force = 800 * alpha / (dist * dist);
          a.x -= dx/dist * force; a.y -= dy/dist * force;
          b.x += dx/dist * force; b.y += dy/dist * force;
        }
      }
      // 弹簧力（边）
      for (const e of edges){
        const a = pos[e.source], b = pos[e.target];
        if (!a || !b) continue;
        let dx = b.x - a.x, dy = b.y - a.y;
        let dist = Math.sqrt(dx*dx + dy*dy) || 1;
        let force = (dist - 140) * 0.08 * alpha;
        a.x += dx/dist * force; a.y += dy/dist * force;
        b.x -= dx/dist * force; b.y -= dy/dist * force;
      }
      // 中心引力
      for (const n of nodes){
        const p = pos[n.name];
        p.x += (cx - p.x) * 0.01 * alpha;
        p.y += (cy - p.y) * 0.01 * alpha;
        p.x = Math.max(50, Math.min(W-50, p.x));
        p.y = Math.max(50, Math.min(H-50, p.y));
      }
    }
  }

  /* ---- 渲染 SVG ---- */
  function renderTopology(){
    const svg = document.getElementById('topoSvg');
    if (!svg) return;
    const W = svg.clientWidth || 700, H = svg.clientHeight || 460;
    const nodes = topologyData.nodes || [];
    const edges = topologyData.edges || [];
    if (!nodes.length){
      svg.innerHTML = '<text x="50%" y="50%" text-anchor="middle" fill="var(--text-2)" font-size="14">暂无依赖关系数据，点击「+ 添加依赖」开始配置</text>';
      return;
    }
    computeLayout(W, H);

    const nodeSet = new Set(nodes.map(n => n.name));
    // 高亮关联节点/边
    const hlNodes = new Set();
    const hlEdges = new Set();
    if (highlightedNode){
      hlNodes.add(highlightedNode);
      edges.forEach((e, i) => {
        if (e.source === highlightedNode || e.target === highlightedNode){
          hlEdges.add(i);
          hlNodes.add(e.source);
          hlNodes.add(e.target);
        }
      });
    }
    const hasHighlight = highlightedNode != null;

    let html = '<defs>';
    // 箭头 marker
    html += '<marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="rgba(0,212,255,.5)"/></marker>';
    html += '<marker id="arrow-dim" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="rgba(0,212,255,.15)"/></marker>';
    html += '</defs>';

    // 边
    edges.forEach((e, i) => {
      const a = pos[e.source], b = pos[e.target];
      if (!a || !b) return;
      const dimmed = hasHighlight && !hlEdges.has(i);
      const color = EDGE_COLORS[e.type] || EDGE_COLORS.manual;
      const sw = hasHighlight && hlEdges.has(i) ? 2.5 : 1.5;
      const marker = dimmed ? 'url(#arrow-dim)' : 'url(#arrow)';
      const opacity = dimmed ? 0.15 : 1;
      html += `<line class="topo-edge" data-idx="${i}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="${color}" stroke-width="${sw}" marker-end="${marker}" opacity="${opacity}"/>`;
      // 边类型标签
      if (!dimmed){
        const mx = (a.x+b.x)/2, my = (a.y+b.y)/2;
        html += `<text x="${mx}" y="${my-6}" font-size="9" fill="${color}" text-anchor="middle" opacity="0.8">${EDGE_LABELS[e.type]||''}</text>`;
      }
    });

    // 节点
    nodes.forEach(n => {
      const p = pos[n.name];
      if (!p) return;
      const dimmed = hasHighlight && !hlNodes.has(n.name);
      const isHl = highlightedNode === n.name;
      const r = isHl ? 24 : 20;
      const opacity = dimmed ? 0.2 : 1;
      const fill = isHl ? 'var(--warning)' : 'var(--primary)';
      html += `<g class="topo-node" data-name="${esc(n.name)}" opacity="${opacity}">`;
      html += `<circle cx="${p.x}" cy="${p.y}" r="${r}" fill="${fill}" stroke="${isHl ? 'var(--warning)' : 'rgba(0,212,255,.5)'}" stroke-width="${isHl?3:2}" filter="drop-shadow(0 0 4px rgba(0,212,255,.3))"/>`;
      // 节点内文字（缩写）
      const short = n.name.length > 8 ? n.name.slice(0,7)+'…' : n.name;
      html += `<text x="${p.x}" y="${p.y+4}" font-size="10" fill="#fff" text-anchor="middle" font-weight="600">${esc(short)}</text>`;
      // 节点下标签
      html += `<text x="${p.x}" y="${p.y+r+14}" font-size="11" fill="var(--text-1)" text-anchor="middle">${esc(n.name)}</text>`;
      html += '</g>';
    });

    svg.innerHTML = html;

    // 绑定交互
    svg.querySelectorAll('.topo-node').forEach(g => {
      g.addEventListener('click', () => onNodeClick(g.dataset.name));
      g.addEventListener('mouseenter', () => { highlightedNode = g.dataset.name; renderTopology(); });
      g.addEventListener('mouseleave', () => { if (highlightedNode && highlightedNode !== g.dataset.name) return; });
    });
    svg.addEventListener('mouseleave', () => { highlightedNode = null; renderTopology(); });
  }

  /* ---- 点击节点 → 影响分析 ---- */
  async function onNodeClick(name){
    highlightedNode = name;
    renderTopology();
    const panel = document.getElementById('impactPanel');
    if (!panel) return;
    panel.innerHTML = '<div style="color:var(--text-2);font-size:13px">加载中…</div>';
    try {
      const data = await api(`/api/deploy/impact/${selectedProject}?service=${encodeURIComponent(name)}`);
      renderImpactPanel(panel, data);
    } catch(e){
      panel.innerHTML = `<div style="color:var(--danger)">加载失败: ${esc(e.message)}</div>`;
    }
  }

  function renderImpactPanel(panel, data){
    const up = data.upstream || [];
    const down = data.downstream || [];
    const iup = data.indirect_upstream || [];
    const idown = data.indirect_downstream || [];
    let html = `<div class="impact-card">`;
    html += `<div class="impact-header">${esc(data.service)} — 影响分析</div>`;
    html += `<div class="impact-grid">`;
    // 上游
    html += `<div class="impact-col"><div class="impact-col-title"> 上游（依赖我的服务）</div>`;
    if (up.length) up.forEach(u => {
      html += `<div class="impact-item"><span class="dep-type-badge type-${u.type}">${EDGE_LABELS[u.type]||u.type}</span> ${esc(u.name)}</div>`;
    }); else html += '<div class="impact-item muted">无</div>';
    if (iup.length){
      html += '<div class="impact-sub-title">间接上游</div>';
      iup.forEach(n => html += `<div class="impact-item muted">… ${esc(n)}</div>`);
    }
    html += '</div>';
    // 下游
    html += `<div class="impact-col"><div class="impact-col-title">⬇ 下游（我依赖的服务）</div>`;
    if (down.length) down.forEach(d => {
      html += `<div class="impact-item"><span class="dep-type-badge type-${d.type}">${EDGE_LABELS[d.type]||d.type}</span> ${esc(d.name)}</div>`;
    }); else html += '<div class="impact-item muted">无</div>';
    if (idown.length){
      html += '<div class="impact-sub-title">间接下游</div>';
      idown.forEach(n => html += `<div class="impact-item muted">… ${esc(n)}</div>`);
    }
    html += '</div></div></div>';
    panel.innerHTML = html;
  }

  /* ---- 发布顺序 ---- */
  async function showPublishOrder(){
    const panel = document.getElementById('impactPanel');
    if (!panel) return;
    panel.innerHTML = '<div style="color:var(--text-2);font-size:13px">计算发布顺序…</div>';
    try {
      const data = await api(`/api/deploy/publish-order/${selectedProject}`);
      const order = data.order || [];
      let html = `<div class="impact-card">`;
      html += `<div class="impact-header">📋 建议发布顺序${data.has_cycle ? ' <span style="color:var(--warning);font-size:12px">（存在循环依赖，仅供参考）</span>' : ''}</div>`;
      html += '<div style="display:flex;flex-wrap:wrap;gap:8px;padding:12px 16px">';
      order.forEach((name, i) => {
        html += `<div class="order-step"><span class="order-num">${i+1}</span><span class="order-name">${esc(name)}</span></div>`;
      });
      html += '</div></div>';
      panel.innerHTML = html;
      highlightedNode = null;
      renderTopology();
    } catch(e){
      panel.innerHTML = `<div style="color:var(--danger)">加载失败: ${esc(e.message)}</div>`;
    }
  }

  /* ---- 添加依赖 ---- */
  function openAddDepForm(){
    openDrawer({ title: '添加服务依赖', body: `
      <div class="form-grid">
        <div class="field"><label>调用方</label><input id="depSrc" placeholder="gateway"></div>
        <div class="field"><label>被调用方</label><input id="depTgt" placeholder="auth"></div>
        <div class="field full"><label>类型</label>
          <select id="depType"><option value="dubbo">Dubbo</option><option value="http">HTTP</option><option value="manual">手动</option></select></div>
      </div>
      <div style="display:flex;gap:10px;margin-top:20px"><button class="btn primary flex-1" id="depSave">保存</button></div>`});
    document.getElementById('depSave').onclick = async () => {
      try {
        await api('/api/deploy/topology', {method:'POST', body:JSON.stringify({
          project_id: selectedProject,
          source_service: document.getElementById('depSrc').value,
          target_service: document.getElementById('depTgt').value,
          dep_type: document.getElementById('depType').value
        })});
        toastSuccess('依赖已添加');
        document.getElementById('dwClose').click();
        await loadTopology(); renderTopology();
      } catch(e) { toastError(e.message); }
    };
  }

  /* ---- 图例 ---- */
  function renderLegend(){
    const svg = document.getElementById('topoSvg');
    if (!svg) return;
    const W = svg.clientWidth || 700;
    let html = '<div style="display:flex;gap:16px;align-items:center;justify-content:center;padding:8px;font-size:12px;color:var(--text-2)">';
    html += '<span style="display:flex;align-items:center;gap:4px"><span style="width:20px;height:3px;background:rgba(0,212,255,.55);display:inline-block;border-radius:2px"></span> Dubbo</span>';
    html += '<span style="display:flex;align-items:center;gap:4px"><span style="width:20px;height:3px;background:rgba(0,230,118,.55);display:inline-block;border-radius:2px"></span> HTTP</span>';
    html += '<span style="display:flex;align-items:center;gap:4px"><span style="width:20px;height:3px;background:rgba(254,176,25,.55);display:inline-block;border-radius:2px"></span> 手动</span>';
    html += '<span style="margin-left:8px;color:var(--text-2);font-size:11px"> 点击节点查看影响分析 · 悬停高亮关联</span>';
    html += '</div>';
    // 插入到 SVG 后面
    const existing = document.getElementById('topoLegend');
    if (existing) existing.remove();
    const div = document.createElement('div');
    div.id = 'topoLegend';
    div.innerHTML = html;
    svg.parentNode.insertBefore(div, svg.nextSibling);
  }

  window.deploy_topology = {
    render: async function(root){
      destroyed = false;
      highlightedNode = null;
      await loadProjects();
      await loadTopology();
      if (destroyed) return [];
      root.innerHTML = `
        <div class="deploy-toolbar">
          <select class="filter-sel" id="topoProjSel">${projects.map(p =>
            `<option value="${p.id}" ${p.id === selectedProject ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select>
          <span style="flex:1"></span>
          <button class="btn ghost sm" id="orderBtn"> 发布顺序</button>
          ${canDeploy() ? '<button class="btn primary" id="addDepBtn">+ 添加依赖</button>' : ''}
        </div>
        <svg id="topoSvg" class="topo-svg"></svg>
        <div id="impactPanel" style="margin-top:12px"></div>`;
      document.getElementById('topoProjSel').onchange = async (e) => {
        selectedProject = Number(e.target.value);
        highlightedNode = null;
        await loadTopology();
        renderTopology();
        renderLegend();
        const panel = document.getElementById('impactPanel');
        if (panel) panel.innerHTML = '';
      };
      const addBtn = document.getElementById('addDepBtn');
      if (addBtn) addBtn.onclick = openAddDepForm;
      const orderBtn = document.getElementById('orderBtn');
      if (orderBtn) orderBtn.onclick = showPublishOrder;
      renderTopology();
      renderLegend();
      // 窗口 resize 重绘
      const ro = new ResizeObserver(() => { renderTopology(); renderLegend(); });
      ro.observe(root);
      return [() => ro.disconnect()];
    },
    destroy: function(){ destroyed = true; }
  };
})();
