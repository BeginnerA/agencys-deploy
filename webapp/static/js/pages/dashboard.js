// pages/dashboard.js — 监控大屏（KPI/雷达/机房/趋势/服务器/最近发布）
(function(){
  let charts = {};      // ECharts 实例
  let dataTimer = null; // 大屏数据轮询
  let overview = null;  // dashboard/overview 缓存
  let trendMode = 7;
  let destroyed = false; // 路由切换后丢弃 in-flight 响应，防覆盖新页面/泄漏轮询

  // ---- KPI 条（规格 §6.1）----
  function kpiRow(o){
    const items = [
      ['🗂️', o.projects, '项目'], ['🧩', o.services, '服务'], ['🌐', o.environments, '环境'],
      ['🖥️', o.servers, '服务器'], ['📋', o.tasks_total, '任务总数'],
      ['📈', o.success_rate === null ? '—' : o.success_rate + '%', '发布成功率'],
    ];
    return `<div class="kpi-row">${items.map(([ico, v, label]) => `
      <div class="kpi-card"><span class="k-ico">${ico}</span>
        <div class="k-num num flash">${v}</div><div class="k-label">${label}</div></div>`).join('')}</div>`;
  }

  // ---- 健康指数（规格 §6.2：真实加权 + 示例兜底）—— 算法统一在 data.js computeHealth ----

  function gaugeOption(name, value, unit, max, color){
    return { series: [{ type:'gauge', min:0, max, radius:'92%', center:['50%','58%'],
      startAngle:210, endAngle:-30, progress:{show:true, width:10, itemStyle:{color}},
      axisLine:{lineStyle:{width:10, color:[[1,'rgba(0,212,255,.08)']]}},
      pointer:{show:false}, axisTick:{show:false}, splitLine:{show:false}, axisLabel:{show:false},
      detail:{valueAnimation:true, formatter:'{value}'+unit, fontSize:20,
              fontFamily:'DIN Alternate,monospace', color, offsetCenter:[0,'38%']},
      title:{offsetCenter:[0,'78%'], fontSize:12, color:'#8FA8C8'},
      data:[{value:+value.toFixed(1), name}] }] };
  }

  // ---- 发布趋势（规格 §6.4：柱线双轴）----
  function trendOption(trend){
    const days = Object.keys(trend).sort();
    const success = days.map(d => trend[d].success);
    const failed = days.map(d => trend[d].failed);
    const rate = days.map(d => {
      const t = trend[d].total;
      return t ? +(trend[d].success / t * 100).toFixed(1) : null;
    });
    return {
      tooltip: { trigger:'axis', backgroundColor:'#112241', borderColor:'rgba(0,212,255,.3)',
                 textStyle:{color:'#E6F1FF', fontSize:12} },
      legend: { data:['成功','失败','成功率%'], textStyle:{color:'#8FA8C8'}, top:0, right:0 },
      grid: { left:8, right:36, top:34, bottom:4, containLabel:true },
      xAxis: { type:'category', data:days, axisLine:{lineStyle:{color:'rgba(0,212,255,.2)'}},
               axisLabel:{color:'#8FA8C8'} },
      yAxis: [
        { type:'value', minInterval:1, splitLine:{lineStyle:{color:'rgba(0,212,255,.07)'}},
          axisLabel:{color:'#8FA8C8'} },
        { type:'value', max:100, splitLine:{show:false}, axisLabel:{color:'#8FA8C8', formatter:'{value}%'} },
      ],
      series: [
        { name:'成功', type:'bar', stack:'t', barWidth:14, itemStyle:{color:'#00E396', borderRadius:[3,3,0,0]},
          data:success },
        { name:'失败', type:'bar', stack:'t', barWidth:14, itemStyle:{color:'#FF4560', borderRadius:[0,0,3,3]},
          data:failed },
        { name:'成功率%', type:'line', yAxisIndex:1, smooth:true, symbol:'circle', symbolSize:5,
          lineStyle:{color:'#00D4FF', width:2}, itemStyle:{color:'#00D4FF'}, data:rate },
      ] };
  }

  // ---- 最近发布（规格 §6.6）----
  function sideList(title, items, emptyTxt){
    const rows = items.map(t => `
      <div class="list-item" data-task="${t.id}">
        <div class="li-main ellipsis">#${t.id} ${esc(t.name)}${t.env ? ' <span class="weak">['+esc(t.env)+']</span>' : ''}</div>
        ${statusBadge(t.status)}
      </div>`).join('');
    return `<div class="panel"><div class="panel-title">${title}</div>
      <div class="rlist">${rows || `<div class="empty-state" style="padding:20px 0"><div class="e-ico">📭</div><div>${emptyTxt}</div></div>`}</div></div>`;
  }

  // ---- 环境切换后重新加载监控数据 ----
  async function loadMonitor(){
    if (destroyed) return;
    try {
      const mon = await api(`/api/monitor/overview?project_id=${window._dashProjectId}&env=${window._dashCurEnv}`).catch(() => null);
      if (!destroyed) renderAll(mon);
    } catch(e) { /* ignore */ }
  }

  // ---- 模块导出（router 约定：render 返回定时器数组）----
  window.dashboard = {
    render: async function(root){
      destroyed = false;
      root.innerHTML = skeletonHTML(6);
      const load = async () => {
        try{
          overview = await api('/api/dashboard/overview');
          try{
            const projects = await api('/api/projects');
            if (projects.length){
              window._dashProjectId = projects[0].id;
              const envs = await api(`/api/projects/${projects[0].id}/environments`);
              window._dashEnvs = envs;
              if (!window._dashCurEnv || !envs.find(e => e.name === window._dashCurEnv)){
                const def = envs.find(e => e.is_default) || envs[0];
                window._dashCurEnv = def ? def.name : '';
              }
              await loadMonitor();
            } else if (!destroyed) {
              renderAll(null);
            }
          }catch(e){ /* ignore */ }
        }catch(e){ if (!destroyed) root.innerHTML = errorState('监控数据加载失败：' + e.message, 'navigate'); }
      };
      await load();
      if (destroyed) return [];
      dataTimer = setInterval(load, 5000);
      return [dataTimer];
    },
    destroy: function(){
      destroyed = true;
      Object.values(charts).forEach(c => { try{ c.dispose(); }catch(e){} });
      charts = {};
      if (dataTimer) clearInterval(dataTimer);
      dataTimer = null;
    },
  };

  function renderAll(mon){
    if (destroyed) return;
    // 先释放旧 echarts 实例，防止 5s 轮询全量重渲染时实例泄漏
    Object.values(charts).forEach(c => { try{ c.dispose(); }catch(e){} });
    charts = {};
    const root = document.getElementById('pageRoot');
    const radar = computeHealth(overview, mon);
    const hm = mon && mon.host_metrics || {};
    const hmEntries = Object.entries(hm).filter(([host, v]) => host && typeof v === 'object' && v.cpu != null);
    const hasHM = hmEntries.length > 0;
    const hmAvg = hasHM ? {
      cpu: +(hmEntries.reduce((s, [, v]) => s + (v.cpu || 0), 0) / hmEntries.length).toFixed(1),
      mem: +(hmEntries.reduce((s, [, v]) => s + (v.mem || 0), 0) / hmEntries.length).toFixed(1),
      disk: +(hmEntries.reduce((s, [, v]) => s + (v.disk || 0), 0) / hmEntries.length).toFixed(1),
      load: +(hmEntries.reduce((s, [, v]) => s + (v.load || 0), 0) / hmEntries.length).toFixed(1),
    } : null;
    const hmWarn = hasHM ? hmEntries.filter(([, v]) => (v.cpu||0)>80||(v.mem||0)>80||(v.disk||0)>80||(v.load||0)>80).length : 0;
    // 合并服务器状态 + 资源明细为统一列表
    const allServers = (mon && mon.servers) || [];
    const hmMap = Object.fromEntries(hmEntries);
    const mergedServers = [
      ...allServers.map(s => ({host: s.host, ssh_ok: s.ssh_ok, containers: s.containers, ts: s.ts, error: s.error, hm: hmMap[s.host] || null})),
      ...hmEntries.filter(([h]) => !allServers.some(s => s.host === h)).map(([host, m]) => ({host, ssh_ok: false, containers: 0, ts: 0, error: '', hm: m}))
    ];
    const curEnv = (mon && mon.env) || window._dashCurEnv || '';
    const envOpts = (window._dashEnvs || []).map(e =>
      `<option value="${esc(e.name)}" ${e.name===curEnv?'selected':''}>${esc(e.name)}</option>`).join('');
    root.innerHTML = `
      ${kpiRow(overview)}
      <div class="dash-grid">
        <div class="panel chart-box"><div class="panel-title">雷达健康指数${radar.demo ? '<span class="demo-tag">演示数据</span>' : ''}</div>
          <div id="chartRadar" style="height:260px"></div></div>
        <div class="panel chart-box"><div class="panel-title">发布趋势
            <span class="pt-right trend-tabs">
              <button class="tab-btn ${trendMode===7?'active':''}" data-days="7">近 7 日</button>
              <button class="tab-btn ${trendMode===30?'active':''}" data-days="30">近 30 日</button>
            </span></div>
          <div id="chartTrend" style="height:260px"></div></div>
        <div style="display:flex;flex-direction:column;gap:var(--sp-4)">
          ${sideList('最近发布', overview.recent || [], '暂无发布记录')}
        </div>
      </div>
      <div class="panel">
        <div class="panel-title">服务器状态
          <span class="muted" style="font-weight:400;font-size:12px">（${mergedServers.length} 台）</span>
          ${hmWarn > 0 ? `<span class="hm-warn-tag">${hmWarn} 台超阈值</span>` : ''}
          ${envOpts ? `<span class="pt-right"><select class="env-sel" id="envSwitch">${envOpts}</select></span>` : ''}
        </div>
        ${hasHM ? `
        <div class="hm-summary-row">
          ${[['gSumCpu','CPU 均值',hmAvg.cpu,'#FF7A45'],['gSumMem','内存均值',hmAvg.mem,'#00D4FF'],
             ['gSumDisk','磁盘均值',hmAvg.disk,'#FEB019'],['gSumLoad','负载均值',hmAvg.load,'#00E396']]
            .map(([id,name,val,color]) => `<div class="hm-gauge-box"><div id="${id}" style="height:140px"></div></div>`).join('')}
        </div>` : ''}
        <div class="tbl-wrap"><table class="tbl hm-tbl"><thead><tr>
          <th style="width:200px">主机</th><th>SSH</th><th>容器</th><th>CPU</th><th>内存</th><th>磁盘</th><th>负载</th><th>最后探测</th></tr></thead><tbody>
          ${mergedServers.length ? mergedServers.map(sv => {
            const m = sv.hm;
            return `<tr>
              <td><span class="dot ${sv.ssh_ok?'ok':'bad'}"></span><span class="num">${esc(sv.host)}</span></td>
              <td>${sv.ssh_ok ? '<span class="ok">连通</span>' : '<span class="err">失败</span>'}</td>
              <td class="num">${sv.containers ?? '-'}</td>
              ${m ? ['cpu','mem','disk','load'].map(k => {
                const val = m[k] || 0;
                const cls = val > 80 ? 'hm-high' : val > 60 ? 'hm-mid' : 'hm-low';
                return `<td><div class="hm-bar-wrap"><div class="hm-bar ${cls}" style="width:${Math.min(100,val)}%"></div>
                  <span class="hm-bar-val">${val.toFixed(1)}%</span></div></td>`;
              }).join('') : '<td colspan="4" class="muted" style="text-align:center">—</td>'}
              <td class="muted">${sv.error ? esc(sv.error) : (sv.ts ? fmtTime(sv.ts) : '—')}</td></tr>`;
          }).join('') : `<tr><td colspan="8" class="muted" style="text-align:center;padding:20px">暂无服务器数据</td></tr>`}
        </tbody></table></div>
      </div>
    `;
    // 环境切换
    const envSel = document.getElementById('envSwitch');
    if (envSel) envSel.onchange = () => { window._dashCurEnv = envSel.value; loadMonitor(); };
    // 趋势 tab 切换
    root.querySelectorAll('.trend-tabs .tab-btn').forEach(b => b.onclick = () => {
      trendMode = Number(b.dataset.days);
      document.querySelectorAll('.trend-tabs .tab-btn').forEach(x => x.classList.toggle('active', x === b));
      renderTrend();
    });
    // 任务点击 → 日志抽屉
    root.querySelectorAll('.list-item[data-task]').forEach(item => {
      item.onclick = () => { if (window.openTaskLogDrawer) openTaskLogDrawer(Number(item.dataset.task)); };
    });
    // 雷达
    charts.radar = echarts.init(document.getElementById('chartRadar'));
    charts.radar.setOption({
      tooltip:{}, legend:{data:['健康指数'], textStyle:{color:'#8FA8C8'}, bottom:0},
      radar: {
        indicator: [['稳定性',100],['性能',100],['资源',100],['安全',100],['可用性',100],['响应',100]]
          .map(([n,m])=>({name:n,max:m})),
        radius:'62%', splitArea:{areaStyle:{color:['rgba(0,212,255,.02)','rgba(0,212,255,.05)']}},
        axisLine:{lineStyle:{color:'rgba(0,212,255,.25)'}},
        splitLine:{lineStyle:{color:'rgba(0,212,255,.15)'}},
        axisName:{color:'#8FA8C8', fontSize:12},
      },
      series:[{type:'radar', symbolSize:4,
        lineStyle:{color:'#00D4FF', width:2}, itemStyle:{color:'#00D4FF'},
        areaStyle:{color:'rgba(0,212,255,.22)'},
        data:[{value: radar.values, name:'健康指数'}]}],
    });
    // 趋势
    charts.trend = echarts.init(document.getElementById('chartTrend'));
    renderTrend();
    // 汇总仪表盘
    if (hasHM && hmAvg) {
      const sumDefs = [['gSumCpu','CPU 均值',hmAvg.cpu,100,'#FF7A45'],['gSumMem','内存均值',hmAvg.mem,100,'#00D4FF'],
                       ['gSumDisk','磁盘均值',hmAvg.disk,100,'#FEB019'],['gSumLoad','负载均值',hmAvg.load,100,'#00E396']];
      sumDefs.forEach(([id, name, val, max, color]) => {
        const el = document.getElementById(id);
        if (el) {
          charts[id] = echarts.init(el);
          charts[id].setOption(gaugeOption(name, val, '%', max, color));
        }
      });
    }
  }
  function renderTrend(){
    const trend = trendMode === 7 ? (overview.trend7 || {}) : (overview.trend30 || {});
    charts.trend.setOption(trendOption(trend));
  }
})();
