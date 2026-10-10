// config-env-form.js — 环境新增/编辑：模板选择 → 分组表单 → 高级模式(YAML) → 绑定服务器 → 校验保存
(function(){
  'use strict';
  const H = window.cfgHelpers;

  const BLOCK_KEYS = ['registry','nacos','mysql_snailjob','ssh','build','deploy'];
  // 分区字段级定义：key, label, type(number/select/text/password), placeholder, hint, options
  const BLOCK_FIELDS = {
    registry: [
      { key:'mode', label:'分发模式', type:'select', options:[["direct","direct — 直传服务器，无需镜像仓库（本机构建后 scp + docker load）"],["push","push — 推送到镜像仓库，再分发到服务器"]], hint:'direct = 本机构建后 scp 到服务器 docker load，不需要镜像仓库；push = 构建后推送到远程镜像仓库' },
      { key:'namespace', label:'镜像命名空间', required:true, placeholder:'如 app-ns', hint:'direct 模式为镜像名前缀；push 模式为仓库项目空间（完整镜像 {host}/{namespace}/{image}:{version}）' },
      { key:'host', label:'仓库地址', placeholder:'如 harbor.example.com', hint:'仅 push 模式使用；direct 模式可留空' },
      { key:'username', label:'仓库账号', placeholder:'可留空用环境变量 REGISTRY_USERNAME' },
      { key:'password', label:'仓库密码', type:'password', placeholder:'可留空用环境变量 REGISTRY_PASSWORD' },
    ],
    nacos: [
      { key:'url', label:'Nacos 地址', placeholder:'如 http://10.0.0.1:8848', hint:'服务注册中心地址，发布前会检查实例' },
      { key:'namespace', label:'命名空间 ID', placeholder:'留空 = public' },
      { key:'group', label:'默认分组', placeholder:'如 DEFAULT_GROUP', hint:'服务未配置 nacos_groups 时使用该分组' },
      { key:'username', label:'账号', placeholder:'可留空用环境变量 NACOS_USERNAME' },
      { key:'password', label:'密码', type:'password', placeholder:'可留空用环境变量 NACOS_PASSWORD' },
    ],
    mysql_snailjob: [
      { key:'host', label:'数据库地址', placeholder:'SnailJob 元数据库 IP' },
      { key:'port', label:'端口', type:'number' },
      { key:'database', label:'数据库名' },
      { key:'username', label:'账号', placeholder:'可留空用环境变量 MYSQL_USERNAME' },
      { key:'password', label:'密码', type:'password', placeholder:'可留空用环境变量 MYSQL_PASSWORD' },
    ],
    ssh: [
      { key:'user', label:'登录用户', placeholder:'如 root' },
      { key:'port', label:'SSH 端口', type:'number' },
      { key:'key_file', label:'私钥路径', placeholder:'如 ~/.ssh/id_rsa_deploy' },
      { key:'password', label:'密码', type:'password', placeholder:'敏感密码建议环境变量 SSH_PASSWORD', hint:'密码优先、失败自动回退密钥；留空仅用密钥' },
    ],
    build: [
      { key:'maven_home', label:'Maven 目录', placeholder:'留空自动探测（MAVEN_HOME/PATH）' },
      { key:'java_home', label:'JDK 目录', placeholder:'留空自动探测（JAVA_HOME/PATH）' },
      { key:'maven_profile', label:'Maven Profile', placeholder:'留空按环境名自动推导（prod/test/dev）' },
    ],
    deploy: [
      { key:'project_root', label:'源码根目录', placeholder:'本地业务项目路径，如 D:/work/project' },
      { key:'health_timeout', label:'健康检查超时（秒）', type:'number' },
      { key:'health_interval', label:'检查间隔（秒）', type:'number' },
      { key:'canary_wait', label:'首台灰度等待（秒）', type:'number', hint:'0 = 不等待' },
      { key:'dist_dir', label:'直传临时目录（本地）', placeholder:'direct 模式镜像 tar 导出目录' },
      { key:'remote_tmp_dir', label:'直传临时目录（服务器）', placeholder:'direct 模式服务器存放 tar 的目录' },
      { key:'upload_streams', label:'并行上传通道数', type:'number', hint:'direct 模式镜像包分片并行上传通道数（1-8），留空默认 4；1 = 传统单流，高延迟链路提速明显' },
      { key:'image_compress_level', label:'镜像包压缩级别', type:'number', hint:'direct 模式 gzip 压缩级别（1-9），留空默认 6；调低打包更快、包体略大，带宽紧张可调高' },
      { key:'git_tag_suffix', label:'Git Tag 后缀', placeholder:'如 test', hint:'发布成功打 Git Tag 时附加在版本号后，最终 tag = v{version}-{后缀}；留空则打 v{version}。测试/生产环境配不同后缀可在 Git 上区分（如 test 配 test，prod 留空）' },
    ],
  };

  function open(container, project, env, opts){
    opts = opts || {};
    const st = {
      project, env, container,
      step: opts.step === 'bind' ? 2 : (env ? 1 : 0),   // 0=模板 1=填写 2=绑定（保存在第 2 步按钮区）
      mode: 'form',
      data: env ? deepClone6(env) : {},
      boundIds: [],
      origBound: [],
      servers: [],
      envs: [],
    };
    if (env) st.data = {
      name: env.name, is_default: !!env.is_default, monitor_enabled: !!env.monitor_enabled,
      docker_compose_path: env.docker_compose_path || '', maven_profile: env.maven_profile || '',
      registry: env.registry || {}, nacos: env.nacos || {}, mysql_snailjob: env.mysql_snailjob || {},
      ssh: env.ssh || {}, build: env.build || {}, deploy: env.deploy || {},
    };
    // 保存后统一 navRetry() 强制重渲染当前路由（location.hash 相同时不会触发 subChanged 刷新）
    const reload = () => navRetry();
    openDrawer({ title: env ? '编辑环境：' + env.name : '新建环境', wide: true, body: '<div class="cfg-home">' + skeletonHTML(4) + '</div>', onClose: null });
    const bodyEl = document.getElementById('dwBody');

    (async () => {
      try{
        st.servers = await api('/api/servers');
        st.envs = await api(`/api/projects/${project.id}/environments`);
        if (env){
          try{ st.origBound = await api(`/api/environments/${env.id}/servers`); }catch(e){ st.origBound = []; }
          st.boundIds = st.origBound.map(s => s.id);
        }
        render();
      }catch(e){ bodyEl.innerHTML = errorState('加载失败：' + esc(e.message), 'navRetry'); }
    })();

    function deepClone6(e){
      const c = {};
      BLOCK_KEYS.forEach(k => c[k] = JSON.parse(JSON.stringify(e[k] || {})));
      return c;
    }

    function render(){
      const steps = ['选择模板','填写配置','绑定服务器','校验保存'];
      const active = st.step;
      const body = `
        <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
          ${H.stepper(steps, active)}
          <span style="flex:1"></span>
          <label class="mode-switch"><input type="checkbox" id="envMode" ${st.mode === 'yaml' ? 'checked' : ''}> 高级模式：直接编辑 YAML</label>
        </div>
        <div id="envStepBody"></div>
        <div style="display:flex;gap:10px;margin-top:18px">
          ${st.step > 0 && !st.env ? '<button class="btn" id="envPrev">← 上一步</button>' : ''}
          ${st.step > 0 && st.step < 2 ? '<button class="btn primary flex-1" id="envNext">下一步：绑定服务器 →</button>' : ''}
          ${st.step === 2 ? '<button class="btn primary flex-1" id="envSave">✓ 校验并保存</button>' : ''}
        </div>`;
      document.getElementById('dwBody').innerHTML = body;
      document.getElementById('envMode').onchange = () => { toggleMode(); render(); };
      if (st.step === 0) renderTpl();
      else if (st.step === 1) renderForm();
      else renderBind();
      const prev = document.getElementById('envPrev');
      if (prev) prev.onclick = () => { st.step--; render(); };
      const next = document.getElementById('envNext');
      if (next) next.onclick = () => { if (collectForm()){ st.step = 2; render(); } };
      const save = document.getElementById('envSave');
      if (save) save.onclick = async () => { if (await collectBindAndSave()) reload(); };
    }

    // ---- 步骤 0：模板选择 ----
    function renderTpl(){
      const body = document.getElementById('envStepBody');
      body.innerHTML = `
        <div class="tpl-grid">${H.ENV_TEMPLATES.map(t => H.templateCard(t, false)).join('')}</div>
        <div class="tpl-note">模板可在保存后随时更改，不会丢失已填内容；保存前可随时返回调整。</div>
        <div id="copySel" style="margin-top:12px;display:none"></div>`;
      body.querySelectorAll('.tpl-card').forEach(card => {
        card.onclick = () => {
          const t = H.ENV_TEMPLATES.find(x => x.id === card.dataset.tpl);
          if (t.copy){
            if (st.envs.length === 0){
              const box = document.getElementById('copySel');
              box.style.display = 'block';
              box.innerHTML = '<div class="weak">暂无可复制的环境，请先创建至少一个环境</div>';
            }else if (st.envs.length === 1){
              loadCopy(st.envs[0].id);
            }else{
              const box = document.getElementById('copySel');
              box.style.display = 'block';
              box.innerHTML = `<div class="field full"><label>选择要复制的源环境</label>
                <select id="envCopySrc">${st.envs.map(e => `<option value="${e.id}">${esc(e.name)}</option>`).join('')}</select>
                <div class="f-hint">选中后自动带入该环境的全部 6 大块配置，改名字和差异项即可</div></div>`;
              const sel = document.getElementById('envCopySrc');
              sel && (sel.onchange = () => loadCopy(Number(sel.value)));
            }
          }else{
            st.data = JSON.parse(JSON.stringify(t.data));
            st.data.name = '';
            st.step = 1;
            render();
          }
        };
      });
      async function loadCopy(eid){
        try{
          const src = await api(`/api/projects/${project.id}/environments/${eid}`);
          st.data = deepClone6(src);
          st.data.name = '';
          st.data.maven_profile = src.maven_profile || '';
          st.data.docker_compose_path = src.docker_compose_path || '';
          st.step = 1;
          render();
        }catch(e){ toastError('复制失败：' + e.message); }
      }
    }

    // ---- 步骤 1：分组表单 / YAML ----
    function renderForm(){
      const body = document.getElementById('envStepBody');
      if (st.mode === 'yaml'){
        const yaml = H.objToYaml(pickBlocks());
        body.innerHTML = `<div class="yaml-wrap">
          <div class="yaml-tip">表单与 YAML 共享同一份数据，切换不会丢失任何已填内容。环境基础信息（名称/默认等）仍在表单中维护。</div>
          <textarea id="envYaml" spellcheck="false">${esc(yaml)}</textarea>
        </div>`;
        return;
      }
      // 基本信息区
      const basic = `<div class="form-grid" style="grid-template-areas:'name profile' 'def profile' 'mon profile';grid-template-columns:1fr 1fr">
        <div class="field" style="grid-area:name"><label>环境名称 *（字母数字 _ -，保存后不可改）</label><input id="envName" value="${esc(st.data.name)}" ${env ? 'disabled' : ''} placeholder="如 prod / test / dev"></div>
        <div class="field" style="grid-area:def;display:flex;align-items:center;gap:10px">
          <label style="margin:0;white-space:nowrap">默认环境</label>
          <input type="checkbox" id="envDef" ${st.data.is_default ? 'checked' : ''} style="display:none">
          <div id="swDefEnv" style="position:relative;display:inline-block;width:40px;height:22px;cursor:pointer;border-radius:11px">
            <span id="swDefEnvBg" style="position:absolute;inset:0;background:${st.data.is_default ? 'var(--c-primary,#4f7cff)' : 'var(--c-muted,#888)'};border-radius:11px;transition:.2s"></span>
            <span id="swDefEnvDot" style="position:absolute;left:${st.data.is_default ? '20px' : '2px'};top:2px;width:18px;height:18px;background:#fff;border-radius:50%;transition:.2s"></span>
          </div>
          <span id="swDefEnvTxt" class="weak" style="font-size:12px">${st.data.is_default ? '是 — 作为项目默认发布环境' : '否 — 非默认环境'}</span>
        </div>
        <div class="field" style="grid-area:mon;display:flex;align-items:center;gap:10px">
          <label style="margin:0;white-space:nowrap">启用监控</label>
          <input type="checkbox" id="envMon" ${st.data.monitor_enabled ? 'checked' : ''} style="display:none">
          <div id="swMonEnv" style="position:relative;display:inline-block;width:40px;height:22px;cursor:pointer;border-radius:11px">
            <span id="swMonEnvBg" style="position:absolute;inset:0;background:${st.data.monitor_enabled ? 'var(--c-primary,#4f7cff)' : 'var(--c-muted,#888)'};border-radius:11px;transition:.2s"></span>
            <span id="swMonEnvDot" style="position:absolute;left:${st.data.monitor_enabled ? '20px' : '2px'};top:2px;width:18px;height:18px;background:#fff;border-radius:50%;transition:.2s"></span>
          </div>
          <span id="swMonEnvTxt" class="weak" style="font-size:12px">${st.data.monitor_enabled ? '已启用 — 采集指标与告警' : '未启用 — 不采集监控数据'}</span>
        </div>
        <div class="field" style="grid-area:profile"><label>Maven Profile</label><select id="envProfile">
          <option value="">留空自动推导</option>
          ${['prod','test','dev','custom'].map(p => `<option value="${p}" ${st.data.maven_profile === p ? 'selected' : ''}>${p}</option>`).join('')}
        </select>
      </div>`;
      const sections = [
        H.section({ id:'registry', icon:'📦', title:'镜像分发（registry）', desc:'direct=直传无需仓库 / push=推送到仓库', open: !!Object.keys(st.data.registry || {}).length, body: blockForm('registry') }),
        H.section({ id:'nacos', icon:'🛰️', title:'服务注册中心（nacos）', desc:'微服务注册到 Nacos，发布前后检查实例', open: !!Object.keys(st.data.nacos || {}).length, body: blockForm('nacos') }),
        H.section({ id:'mysql_snailjob', icon:'🗄️', title:'SnailJob 任务调度数据库', desc:'定时任务元数据库；无定时任务可跳过', open: false, body: blockForm('mysql_snailjob') }),
        H.section({ id:'ssh', icon:'🔑', title:'服务器连接（ssh）', desc:'环境级 SSH 凭据，服务器未单独配置时继承', open: !!Object.keys(st.data.ssh || {}).length, body: blockForm('ssh') }),
        H.section({ id:'build', icon:'🔨', title:'本地构建工具链（build）', desc:'Maven/JDK 目录，留空自动探测', open: false, body: blockForm('build') }),
        H.section({ id:'deploy', icon:'🚀', title:'发布行为（deploy）', desc:'健康检查/直传目录/Compose 路径', open: !!Object.keys(st.data.deploy || {}).length, body: blockForm('deploy') + composeField() }),
      ];
      body.innerHTML = `
        <div class="cfg-section"><div class="cs-head"><span class="cs-ico">📝</span><span class="cs-title">基本信息</span><span class="cs-desc">环境名称与全局开关</span></div><div class="cs-body">${basic}</div></div>
        ${sections.map(s => s.html).join('')}
        <div class="f-hint">带 * 为必填；? 图标悬停查看字段用途；分区折叠不影响已填内容。</div>`;
      sections.forEach(s => s.bind());
      // 开关绑定：点击 div 切换 checkbox + 更新视觉
      function bindEnvToggle(divId, cbId, bgId, dotId, txtId, onText, offText) {
        const div = document.getElementById(divId);
        if (!div) return;
        div.onclick = () => {
          const cb = document.getElementById(cbId);
          cb.checked = !cb.checked;
          document.getElementById(bgId).style.background = cb.checked ? 'var(--c-primary,#4f7cff)' : 'var(--c-muted,#888)';
          document.getElementById(dotId).style.left = cb.checked ? '20px' : '2px';
          document.getElementById(txtId).textContent = cb.checked ? onText : offText;
        };
      }
      bindEnvToggle('swDefEnv', 'envDef', 'swDefEnvBg', 'swDefEnvDot', 'swDefEnvTxt',
        '是 — 作为项目默认发布环境', '否 — 非默认环境');
      bindEnvToggle('swMonEnv', 'envMon', 'swMonEnvBg', 'swMonEnvDot', 'swMonEnvTxt',
        '已启用 — 采集指标与告警', '未启用 — 不采集监控数据');
      refreshBadges();
    }
    function blockForm(block){
      const data = st.data[block] || {};
      return `<div class="form-grid">${(BLOCK_FIELDS[block] || []).map(f => {
        const val = data[f.key] !== undefined && data[f.key] !== null ? data[f.key] : '';
        let input;
        if (f.type === 'select'){
          input = `<select id="env${block}_${f.key}">${f.options.map(([v, l]) => `<option value="${v}" ${String(val) === v ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`;
        }else if (f.type === 'number'){
          input = `<input id="env${block}_${f.key}" type="number" value="${esc(val)}">`;
        }else{
          input = `<input id="env${block}_${f.key}" type="${f.type || 'text'}" value="${esc(val)}" placeholder="${esc(f.placeholder || '')}" ${f.key === 'password' && val === '***' ? 'data-masked="1"' : ''}>`;
        }
        return H.field({ label: f.label, required: f.required, tip: f.hint, input, hint: f.key === 'password' && val === '***' ? '原密码已遮蔽：留空不修改，改动其它字段保存后原密码将被清除（改用密钥或环境变量）' : f.hint });
      }).join('')}</div>`;
    }
    function composeField(){
      return `<div class="form-grid"><div class="field full"><label>服务器 docker-compose.yml 路径</label><input id="envDeployCompose" value="${esc(st.data.docker_compose_path || '')}" placeholder="如 /opt/app/docker-compose.yml"></div></div>`;
    }
    function refreshBadges(){
      BLOCK_KEYS.forEach(k => {
        const sec = document.getElementById('sec_' + k);
        if (!sec) return;
        const obj = st.data[k] || {};
        const has = Object.values(obj).some(v => v !== '' && v !== null && v !== undefined && v !== 0);
        sec.classList.toggle('has', has);
      });
    }
    function pickBlocks(){
      const o = {};
      BLOCK_KEYS.forEach(k => o[k] = st.data[k] || {});
      return o;
    }
    function collectBlocks(){
      if (st.mode === 'yaml'){
        try{ Object.assign(st.data, H.yamlToObj(val('envYaml'))); return true; }
        catch(e){ toastError('YAML 解析失败：' + e.message); return false; }
      }
      BLOCK_KEYS.forEach(k => {
        const out = {};
        (BLOCK_FIELDS[k] || []).forEach(f => {
          const el = document.getElementById(`env${k}_${f.key}`);
          if (!el) return;
          const v = el.value.trim();
          out[f.key] = f.type === 'number' ? (v === '' ? '' : Number(v)) : v;
        });
        st.data[k] = out;
      });
      st.data.docker_compose_path = val('envDeployCompose');
      return true;
    }
    // 基本信息与 collectForm 同源收集（toggleMode 共用）：分叉会导致切 YAML 再切回时名称/开关丢失
    function collectBasic(){
      st.data.name = val('envName');
      st.data.is_default = chk('envDef');
      st.data.monitor_enabled = chk('envMon');
      st.data.maven_profile = val('envProfile');
    }
    function toggleMode(){
      // 步骤 0/2 无表单 DOM：collectBlocks/collectBasic 会把已填数据清空，且 YAML 视图只在步骤 1 渲染，直接短路
      if (st.step !== 1){ toastError('请先在「填写配置」步骤切换高级模式'); return; }
      if (st.mode === 'form'){
        collectBlocks();   // 仅收集表单值，不做业务校验（模板预填可能未填全关联必填，切换视图不应被拦截）
        collectBasic();
        st.mode = 'yaml';
      }else{
        try{ Object.assign(st.data, H.yamlToObj(val('envYaml'))); st.mode = 'form'; }
        catch(e){ toastError('YAML 解析失败：' + e.message); }
      }
    }

    // ---- 表单收集 + 校验（返回是否通过） ----
    function collectForm(){
      if (st.mode === 'yaml'){
        try{ Object.assign(st.data, H.yamlToObj(val('envYaml'))); }
        catch(e){ toastError('YAML 解析失败：' + e.message); return false; }
      }else{
        collectBasic();
        collectBlocks();
      }
      if (!H.safeName(st.data.name)){
        H.fieldErr('envName', '环境名称仅允许字母数字 _ -');
        toastError('环境名称仅允许字母数字 _ -');
        return false;
      }
      const reg = st.data.registry || {};
      if (reg.mode && !reg.namespace){
        toastError('镜像分发：选择了分发模式则命名空间必填');
        return false;
      }
      const nacos = st.data.nacos || {};
      if (Object.keys(nacos).some(k => nacos[k]) && !nacos.url){
        toastError('Nacos：填写了账号/分组等配置则地址必填');
        return false;
      }
      return true;
    }

    // ---- 步骤 2：绑定服务器 ----
    function renderBind(){
      const body = document.getElementById('envStepBody');
      // 注意：此处不再重复 collectForm()——进入本步时表单 DOM 已销毁，重新收集会把数据读空并误判校验失败
      const bound = st.boundIds.map(id => st.servers.find(s => s.id === id)).filter(Boolean);
      const unbound = st.servers.filter(s => !st.boundIds.includes(s.id));
      body.innerHTML = `
        <div class="f-hint" style="margin-bottom:10px">勾选 = 绑定到本环境；顺序即发布顺序（首台灰度 → 其余按序跟随）。</div>
        <div class="cfg-section"><div class="cs-head"><span class="cs-ico">📌</span><span class="cs-title">已绑定（${bound.length} 台）</span></div><div class="cs-body" id="boundList">
          ${bound.length ? bound.map((s, i) => boundRow(s, i)).join('') : '<div class="weak" style="font-size:12px">尚未绑定任何服务器，从下方勾选添加</div>'}
        </div></div>
        <div class="cfg-section"><div class="cs-head"><span class="cs-ico">🖥️</span><span class="cs-title">平台服务器（${unbound.length} 台未绑定）</span></div><div class="cs-body">
          ${unbound.length ? `<div class="form-grid">${unbound.map(s => `
            <label class="field check" style="grid-column:1/-1;margin:0"><input type="checkbox" class="bindChk" value="${s.id}">
              ${esc(s.host)}
              ${s.conn ? (s.conn.ok ? '<span class="ok">SSH 正常</span>' : '<span class="err">SSH 异常</span>') : '<span class="weak">SSH 未知</span>'}
              ${s.enabled ? '' : '<span class="weak">（停用）</span>'}</label>`).join('')}</div>`
          : '<div class="weak" style="font-size:12px">平台暂无其它服务器；可到服务器页先添加</div>'}
        </div></div>
        <div class="f-hint">同一环境建议至少 1 台服务器（首台灰度观察无异常后再发布其余）。</div>`;
      body.querySelectorAll('.bindChk').forEach(c => c.onchange = () => {
        const id = Number(c.value);
        if (c.checked) st.boundIds.push(id);
        else st.boundIds = st.boundIds.filter(x => x !== id);
        renderBind();
      });
      body.querySelectorAll('[data-bup]').forEach(b => b.onclick = () => { move(Number(b.dataset.bup), -1); renderBind(); });
      body.querySelectorAll('[data-bdown]').forEach(b => b.onclick = () => { move(Number(b.dataset.bdown), 1); renderBind(); });
      body.querySelectorAll('[data-bdel]').forEach(b => b.onclick = () => { st.boundIds = st.boundIds.filter(x => x !== Number(b.dataset.bdel)); renderBind(); });
    }
    function boundRow(s, i){
      return `<div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px dashed var(--border)">
        <span class="stp-n" style="width:22px;height:22px;border-radius:50%;border:1px solid var(--primary);color:var(--primary);display:inline-flex;align-items:center;justify-content:center;font-size:11px;flex-shrink:0">${i + 1}</span>
        <span style="font-size:13px">${esc(s.host)}</span>
        <span class="weak" style="font-size:11px">${i === 0 ? '首台' : '跟随'}</span>
        <span style="flex:1"></span>
        <button class="btn sm ghost" data-bup="${s.id}" ${i === 0 ? 'disabled' : ''}>↑</button>
        <button class="btn sm ghost" data-bdown="${s.id}" ${i === boundListLen() - 1 ? 'disabled' : ''}>↓</button>
        <button class="btn sm danger" data-bdel="${s.id}">✕</button>
      </div>`;
      function boundListLen(){ return st.boundIds.length; }
    }
    function move(id, d){
      const i = st.boundIds.indexOf(id);
      const j = i + d;
      if (i < 0 || j < 0 || j >= st.boundIds.length) return;
      const t = st.boundIds[i]; st.boundIds[i] = st.boundIds[j]; st.boundIds[j] = t;
    }

    // ---- 步骤 2 保存：POST/PUT + 绑定 diff ----
    async function collectBindAndSave(){
      // 数据已在进入绑定步骤前收集校验；绑定页表单 DOM 已销毁，此处仅兑底校验，不能重新收集
      if (!H.safeName(st.data.name)){
        toastError('环境名称仅允许字母数字 _ -');
        return false;
      }
      const payload = {
        name: st.data.name, is_default: !!st.data.is_default, monitor_enabled: !!st.data.monitor_enabled,
        docker_compose_path: st.data.docker_compose_path || '', maven_profile: st.data.maven_profile || '',
      };
      BLOCK_KEYS.forEach(k => payload[k] = st.data[k] || {});
      // 密码既有缺陷规避：编辑态 ssh 密码留空且未改动其它 ssh 字段 → 不提交 ssh（后端保留原值）
      if (env && env.ssh && env.ssh.password === '***'){
        const orig = env.ssh || {};
        const cur = payload.ssh || {};
        const othersSame = ['user','port','key_file'].every(k => String(cur[k] || '') === String(orig[k] || ''));
        const pwdChanged = !!(cur.password && cur.password !== '***');
        if (!pwdChanged && othersSame) delete payload.ssh;
        else if (!pwdChanged && cur.password === '***') delete payload.ssh.password;  // 只改了 user/port 等：置空密码并明示
        if (!pwdChanged && othersSame === false){
          // 已删 password 键 → 整列替换会把密码清空，toast 明示
          setTimeout(() => toastError('SSH 分区已修改但未填新密码：原密码将被清除，将改用密钥或环境变量 SSH_PASSWORD'), 100);
        }
      }
      try{
        let eid = env ? env.id : null;
        if (env){
          await api(`/api/projects/${project.id}/environments/${env.id}`, { method: 'PUT', body: JSON.stringify(payload) });
        }else{
          const r = await api(`/api/projects/${project.id}/environments`, { method: 'POST', body: JSON.stringify(payload) });
          eid = r.id;
        }
        // 绑定 diff
        const origIds = st.origBound.map(s => s.id);
        const newIds = st.boundIds;
        const removed = origIds.filter(id => !newIds.includes(id));
        const added = newIds.filter(id => !origIds.includes(id));
        for (const sid of removed) await api(`/api/environments/${eid}/servers/${sid}`, { method: 'DELETE' });
        for (const sid of added) await api(`/api/environments/${eid}/servers`, { method: 'POST', body: JSON.stringify({ server_id: sid }) });
        const orderChanged = JSON.stringify(origIds) !== JSON.stringify(newIds);
        if (orderChanged && newIds.length) await api(`/api/environments/${eid}/servers/order`, { method: 'PUT', body: JSON.stringify({ server_ids: newIds }) });
        toastSuccess(env ? '环境已更新' : '环境已创建');
        const dr = document.getElementById('drawerRoot');
        dr.classList.add('hidden'); dr.innerHTML = '';
        return true;
      }catch(e){ toastError(e.message); return false; }
    }

    function val(id){ const el = document.getElementById(id); return el ? el.value : ''; }
    function chk(id){ const el = document.getElementById(id); return el ? el.checked : false; }
  }

  window.cfgEnvForm = { open };
})();
