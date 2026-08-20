// pages/deploy_topology.js — 服务依赖拓扑 + 影响分析
(function(){
  let destroyed = false;
  let projects = [];
  let selectedProject = null;
  let topologyData = { nodes: [], edges: [] };

  async function loadProjects(){
    projects = await api('/api/projects').catch(() => []);
    if (projects.length && !selectedProject) selectedProject = projects[0].id;
  }

  async function loadTopology(){
    if (!selectedProject) return;
    topologyData = await api(`/api/deploy/topology/${selectedProject}`).catch(() => ({ nodes:[], edges:[] }));
  }

  function renderTopology(){
    const svg = document.getElementById('topoSvg');
    if (!svg) return;
    const W = svg.clientWidth || 600, H = svg.clientHeight || 400;
    const nodes = topologyData.nodes || [];
    const edges = topologyData.edges || [];
    if (!nodes.length) {
      svg.innerHTML = '<text x="50%" y="50%" text-anchor="middle" fill="var(--text-secondary)">暂无依赖关系数据</text>';
      return;
    }
    const pos = {};
    nodes.forEach((n, i) => {
      const angle = (2 * Math.PI * i) / nodes.length;
      pos[n.name] = { x: W/2 + Math.cos(angle) * 150, y: H/2 + Math.sin(angle) * 150 };
    });
    for (let iter = 0; iter < 10; iter++) {
      for (const e of edges) {
        const a = pos[e.source], b = pos[e.target];
        if (!a || !b) continue;
        const dx = b.x - a.x, dy = b.y - a.y;
        const dist = Math.sqrt(dx*dx + dy*dy) || 1;
        const force = (dist - 120) * 0.05;
        a.x += dx/dist * force; a.y += dy/dist * force;
        b.x -= dx/dist * force; b.y -= dy/dist * force;
      }
      for (const n of nodes) {
        const p = pos[n.name];
        p.x = Math.max(40, Math.min(W-40, p.x));
        p.y = Math.max(40, Math.min(H-40, p.y));
      }
    }
    let html = '<defs><marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="var(--border)"/></marker></defs>';
    for (const e of edges) {
      const a = pos[e.source], b = pos[e.target];
      if (!a || !b) continue;
      html += `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="var(--border)" stroke-width="1.5" marker-end="url(#arrow)"/>`;
    }
    for (const n of nodes) {
      const p = pos[n.name];
      html += `<g class="topo-node" data-name="${esc(n.name)}"><circle cx="${p.x}" cy="${p.y}" r="20"/><text x="${p.x}" y="${p.y+30}" font-size="11">${esc(n.name)}</text></g>`;
    }
    svg.innerHTML = html;
  }

  function openAddDepForm(){
    openDrawer({ title: '添加服务依赖', body: `
      <div style="display:flex;flex-direction:column;gap:12px">
        <div><label class="dg-label">调用方</label><input id="depSrc" class="inp" placeholder="gateway"></div>
        <div><label class="dg-label">被调用方</label><input id="depTgt" class="inp" placeholder="auth"></div>
        <div><label class="dg-label">类型</label>
          <select id="depType" class="inp"><option value="dubbo">Dubbo</option><option value="http">HTTP</option><option value="manual">手动</option></select></div>
        <button class="btn primary" id="depSave">保存</button>
      </div>`});
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

  window.deploy_topology = {
    render: async function(root){
      destroyed = false;
      await loadProjects();
      await loadTopology();
      if (destroyed) return [];
      root.innerHTML = `
        <div class="deploy-toolbar">
          <select class="filter-sel" id="topoProjSel">${projects.map(p =>
            `<option value="${p.id}" ${p.id === selectedProject ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select>
          <span style="flex:1"></span>
          ${canDeploy() ? '<button class="btn primary" id="addDepBtn">+ 添加依赖</button>' : ''}
        </div>
        <svg id="topoSvg" class="topo-svg"></svg>
        <div id="impactPanel" style="margin-top:12px"></div>`;
      document.getElementById('topoProjSel').onchange = async (e) => {
        selectedProject = Number(e.target.value);
        await loadTopology();
        renderTopology();
      };
      const addBtn = document.getElementById('addDepBtn');
      if (addBtn) addBtn.onclick = openAddDepForm;
      renderTopology();
      return [];
    },
    destroy: function(){ destroyed = true; }
  };
})();
