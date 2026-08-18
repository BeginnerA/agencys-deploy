// config-form-helpers.js — 配置管理共享工具：字段/分区/步骤条/标签输入器/行编辑器/YAML 转换/模板常量/最近项目
(function(){
  'use strict';

  // ---- 字段包装：label + 必填星标 + ？提示 + 控件 + 底部解释 + 错误槽 ----
  function field({label, required, tip, input, hint, full}){
    return `<div class="field ${full ? 'full' : ''}">
      <div class="f-label">${label}${required ? '<span class="req">*</span>' : ''}${tip ? `<span class="q" title="${esc(tip)}">?</span>` : ''}</div>
      ${input}
      <div class="f-hint">${esc(hint || '')}</div>
      <div class="f-err" style="display:none"></div>
    </div>`;
  }
  // 就地校验：标红输入框 + 显示错误；msg 为空则清除
  function fieldErr(inputId, msg){
    const el = document.getElementById(inputId);
    if (!el) return;
    const errEl = el.closest('.field') ? el.closest('.field').querySelector('.f-err') : null;
    const face = el.closest('.sel-wrap') ? el.closest('.sel-wrap').querySelector('.sel-face') : null;
    if (msg){
      el.classList.add('err');
      if (face) face.classList.add('err');
      if (errEl){ errEl.textContent = msg; errEl.style.display = 'block'; }
    }else{
      el.classList.remove('err');
      if (face) face.classList.remove('err');
      if (errEl){ errEl.textContent = ''; errEl.style.display = 'none'; }
    }
  }

  // ---- 可折叠分组区：返回 {html, bind, markFilled} ----
  function section({id, icon, title, desc, open=true, body}){
    const wrapId = 'sec_' + id;
    return {
      html: `<div class="cfg-section ${open ? '' : 'closed'}" id="${wrapId}">
        <div class="cs-head">
          <span class="cs-ico">${icon}</span><span class="cs-title">${esc(title)}</span>
          <span class="cs-desc">${esc(desc || '')}</span>
          <span class="cs-badge">已填写</span><span class="cs-arrow">▼</span>
        </div>
        <div class="cs-body">${body}</div>
      </div>`,
      bind(){
        const sec = document.getElementById(wrapId);
        if (!sec) return;
        sec.querySelector('.cs-head').onclick = () => sec.classList.toggle('closed');
      },
      markFilled(has){
        const sec = document.getElementById(wrapId);
        if (sec) sec.classList.toggle('has', !!has);
      },
    };
  }

  // ---- 步骤条 ----
  function stepper(steps, active){
    return `<div class="steps">${steps.map((s, i) => `
      <span class="stp ${i < active ? 'done' : (i === active ? 'on' : '')}">
        <span class="stp-n">${i < active ? '✓' : i + 1}</span>${esc(s)}
      </span>${i < steps.length - 1 ? '<span class="stp-sep"></span>' : ''}`).join('')}
    </div>`;
  }

  // ---- 模板卡（环境模板选择） ----
  function templateCard(t, sel){
    return `<div class="tpl-card ${sel ? 'sel' : ''}" data-tpl="${esc(t.id)}">
      <div class="tc-ico">${t.icon}</div>
      <div class="tc-title">${esc(t.title)}</div>
      ${t.fill ? `<div class="tc-fill">✓ 自动填充：${esc(t.fill)}</div>` : ''}
      ${t.need ? `<div class="tc-need">✎ 只需改：${esc(t.need)}</div>` : ''}
    </div>`;
  }

  // ---- 标签输入器（回车添加 / ✕ 删除） ----
  function tagInput({id, values, placeholder}){
    return {
      html: `<div class="tag-input" id="${id}">
        ${(values || []).map(v => `<span class="tag" data-tag="${esc(v)}">${esc(v)}<span class="t-x">✕</span></span>`).join('')}
        <input placeholder="${esc(placeholder || '输入后回车添加')}">
      </div>`,
      bind(){
        const box = document.getElementById(id);
        if (!box) return;
        const inp = box.querySelector('input');
        const add = () => {
          const v = inp.value.trim();
          if (!v) return;
          const exists = [...box.querySelectorAll('.tag')].some(t => t.dataset.tag === v);
          if (!exists){
            const sp = document.createElement('span');
            sp.className = 'tag'; sp.dataset.tag = v;
            sp.innerHTML = `${esc(v)}<span class="t-x">✕</span>`;
            box.insertBefore(sp, inp);
          }
          inp.value = '';
        };
        inp.addEventListener('keydown', e => { if (e.key === 'Enter'){ e.preventDefault(); add(); } });
        inp.addEventListener('blur', add);
        box.addEventListener('click', e => {
          if (e.target.classList.contains('t-x')) e.target.parentElement.remove();
        });
      },
      read(){
        const box = document.getElementById(id);
        if (!box) return [];
        return [...box.querySelectorAll('.tag')].map(t => t.dataset.tag);
      },
    };
  }

  // ---- 行编辑器（build_order：模块名/相对路径/goal/额外 profile） ----
  function rowList({id, rows}){
    const buildRow = (r, i) => `
      <div class="row-item" data-i="${i}">
        <input class="ri-name" placeholder="模块名（仅展示）" value="${esc(r.name || '')}">
        <input class="ri-path" placeholder="相对路径，如 common-build" value="${esc(r.path || '')}">
        <select class="ri-goal">
          ${['package','install'].map(g => `<option value="${g}" ${(r.goal || 'package') === g ? 'selected' : ''}>${g}</option>`).join('')}
        </select>
        <input class="ri-extra" placeholder="额外 profile（可选）" value="${esc(r.extra_profile || '')}">
        <span class="ri-btns">
          <button data-act="up" title="上移">↑</button>
          <button data-act="down" title="下移">↓</button>
          <button class="del" data-act="del" title="删除">✕</button>
        </span>
      </div>`;
    const getBox = () => document.getElementById(id);
    return {
      html: `<div class="row-list" id="${id}">
        ${(rows || []).map((r, i) => buildRow(r, i)).join('')}
        <div class="row-add"><button class="btn sm" data-act="add">＋ 添加构建步骤</button></div>
      </div>`,
      bind(){
        const box = getBox();
        if (!box) return;
        const rawRead = () => [...box.querySelectorAll('.row-item')].map(el => ({
          name: el.querySelector('.ri-name').value.trim(),
          path: el.querySelector('.ri-path').value.trim(),
          goal: el.querySelector('.ri-goal').value,
          extra_profile: el.querySelector('.ri-extra').value.trim(),
        }));
        const read = () => rawRead().filter(r => r.path || r.name);
        const bindActions = () => {
          box.querySelectorAll('.row-item').forEach((row, i) => {
            row.querySelectorAll('button[data-act]').forEach(btn => {
              btn.onclick = () => {
                const act = btn.dataset.act;
                const arr = rawRead();   // 含空行，索引与 DOM 行一致；过滤留给对外 read()
                if (act === 'up' && i > 0){ const t = arr[i-1]; arr[i-1] = arr[i]; arr[i] = t; }
                else if (act === 'down' && i < arr.length - 1){ const t = arr[i+1]; arr[i+1] = arr[i]; arr[i] = t; }
                else if (act === 'del') arr.splice(i, 1);
                box.querySelectorAll('.row-item').forEach(el => el.remove());
                arr.forEach((r, j) => box.insertAdjacentHTML('beforeend', buildRow(r, j)));
                bindActions();
              };
            });
          });
        };
        box.querySelector('.row-add button').onclick = () => {
          box.insertAdjacentHTML('beforeend', buildRow({name:'', path:'', goal:'package', extra_profile:''}, read().length));
          bindActions();
        };
        bindActions();
      },
      read(){
        const box = getBox();
        if (!box) return [];
        return [...box.querySelectorAll('.row-item')].map(el => ({
          name: el.querySelector('.ri-name').value.trim(),
          path: el.querySelector('.ri-path').value.trim(),
          goal: el.querySelector('.ri-goal').value,
          extra_profile: el.querySelector('.ri-extra').value.trim(),
        })).filter(r => r.path || r.name);
      },
    };
  }

  // ---- 受限 YAML 子集转换（环境 6 大块均为嵌套 map + 标量，无数组） ----
  function yamlToObj(text){
    const lines = String(text || '').split(/\r?\n/);
    const root = {};
    const stack = [{ indent: -1, obj: root }];
    let saw = false;
    const parseScalar = raw => {
      const v = String(raw).trim();
      if (v === '' || v === 'null' || v === '~') return null;
      if (v === 'true') return true;
      if (v === 'false') return false;
      if (/^-?\d+$/.test(v)) return parseInt(v, 10);
      if (/^-?\d*\.\d+$/.test(v)) return parseFloat(v);
      if (v.length >= 2 && ((v[0] === '"' && v[v.length-1] === '"') || (v[0] === "'" && v[v.length-1] === "'"))) return v.slice(1, -1);
      return v;
    };
    for (const line of lines){
      const noComment = line.replace(/^\s*#.*$/, '');
      if (!noComment.trim()) continue;
      if (/^\s*-/.test(noComment)) throw new Error('环境配置暂不支持数组（第 ' + (lines.indexOf(line) + 1) + ' 行）');
      const indent = noComment.match(/^\s*/)[0].length;
      const content = noComment.trim();
      const m = content.match(/^([A-Za-z_][A-Za-z0-9_-]*):(.*)$/);
      if (!m) throw new Error('YAML 语法错误：' + content.slice(0, 40));
      const key = m[1];
      let val = (m[2] || '').replace(/[ \t]+#.*$/, '').trim();
      while (stack.length > 1 && indent <= stack[stack.length - 1].indent) stack.pop();
      const parent = stack[stack.length - 1].obj;
      if (val === ''){
        if (parent[key] !== undefined) throw new Error('重复键：' + key);
        const child = {};
        parent[key] = child;
        stack.push({ indent, obj: child });
      }else{
        if (parent[key] !== undefined) throw new Error('重复键：' + key);
        parent[key] = parseScalar(val);
      }
      saw = true;
    }
    return saw ? root : {};
  }
  function objToYaml(obj, indent){
    const pad = '  '.repeat(indent || 0);
    const out = [];
    for (const [k, v] of Object.entries(obj || {})){
      if (v === null || v === undefined){ out.push(`${pad}${k}: null`); continue; }
      if (typeof v === 'object'){
        if (Array.isArray(v)) throw new Error('环境配置不支持数组字段：' + k);
        out.push(`${pad}${k}:`, objToYaml(v, (indent || 0) + 1));
      }else{
        out.push(`${pad}${k}: ${typeof v === 'string' ? (v === '' ? '\"\"' : v) : String(v)}`);
      }
    }
    return out.join('\n');
  }

  // ---- 校验工具 ----
  const IP_RE = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;
  const HOST_RE = /^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$/;
  function ipValid(host){
    const h = String(host || '').trim();
    // 允许 IPv4 地址
    const m = IP_RE.exec(h);
    if (m) return m.slice(1).every(seg => Number(seg) >= 0 && Number(seg) <= 255);
    // 允许域名/主机名（如 host.docker.internal、localhost）
    return HOST_RE.test(h);
  }
  const SAFE_NAME_RE = /^[A-Za-z0-9_-]+$/;
  function safeName(name){ return SAFE_NAME_RE.test(String(name || '')); }

  // ---- 最近使用项目（localStorage） ----
  const RECENT_KEY = 'cfgRecentProjects';
  function touchRecent(pid, name){
    try{
      const list = getRecent().filter(x => x.pid !== pid);
      list.unshift({ pid, name, ts: Date.now() });
      localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, 5)));
    }catch(e){ /* localStorage 不可用时静默 */ }
  }
  function getRecent(){
    try{ return JSON.parse(localStorage.getItem(RECENT_KEY) || '[]'); }
    catch(e){ return []; }
  }

  // ---- 流水线发布命令预览 ----
  // 有服务级版本时命令行 version 无效（服务级覆盖），提示条说明
  function pipelineCmd(name, pid, hasServiceVersion){
    let cmd = `python deploy_pipeline.py --pipeline ${name} --project-id ${pid}`;
    if (!hasServiceVersion) cmd += ' --version <发布时填写>';
    return cmd;
  }

  // ---- 模板常量：环境 5 模板 ----
  const ENV_TEMPLATES = [
    { id:'java', icon:'☕', title:'标准 Java 微服务环境',
      fill:'镜像分发(direct) / Nacos / SnailJob / SSH / 构建 / 发布行为默认值',
      need:'Nacos 地址、数据库地址、源码根目录',
      data:{ registry:{mode:'direct', namespace:'', host:'', username:'', password:''},
             nacos:{url:'', namespace:'', group:'DEFAULT_GROUP', username:'', password:''},
             mysql_snailjob:{host:'', port:3306, database:'snail_job', username:'', password:''},
             ssh:{user:'root', port:22, password:'', key_file:''},
             build:{maven_home:'', java_home:'', maven_profile:''},
             deploy:{project_root:'', docker_compose_path:'', health_timeout:120, health_interval:5, canary_wait:60, dist_dir:'', remote_tmp_dir:''} } },
    { id:'k8s', icon:'🐳', title:'K8s 容器环境',
      fill:'镜像分发(push) / 发布行为默认值',
      need:'镜像仓库地址、命名空间',
      data:{ registry:{mode:'push', namespace:'', host:'', username:'', password:''},
             nacos:{url:'', namespace:'', group:'DEFAULT_GROUP', username:'', password:''},
             mysql_snailjob:{host:'', port:3306, database:'snail_job', username:'', password:''},
             ssh:{user:'root', port:22, password:'', key_file:''},
             build:{maven_home:'', java_home:'', maven_profile:''},
             deploy:{project_root:'', docker_compose_path:'', health_timeout:120, health_interval:5, canary_wait:60, dist_dir:'', remote_tmp_dir:''} } },
    { id:'light', icon:'💡', title:'轻量单机环境',
      fill:'仅 SSH 与发布行为分区',
      need:'SSH 凭据与源码路径，约 1 分钟',
      data:{ registry:{}, nacos:{url:'', namespace:'', group:'DEFAULT_GROUP', username:'', password:''},
             mysql_snailjob:{}, ssh:{user:'root', port:22, password:'', key_file:''},
             build:{maven_home:'', java_home:'', maven_profile:''},
             deploy:{project_root:'', docker_compose_path:'', health_timeout:120, health_interval:5, canary_wait:60, dist_dir:'', remote_tmp_dir:''} } },
    { id:'copy', icon:'📋', title:'从现有环境复制', fill:'加载选中环境的全部配置', need:'选择源环境并改名', copy:true },
    { id:'blank', icon:'📄', title:'空白环境', fill:'不预填任何值', need:'逐项填写', data:{registry:{},nacos:{},mysql_snailjob:{},ssh:{},build:{},deploy:{}} },
  ];

  // ---- 模板常量：流水线 4 模板（阶段骨架；服务名匹配自动归位见 applyPipelineTemplate） ----
  const PIPELINE_TEMPLATES = [
    { id:'full', name:'全量发布', desc:'网关 → 基础设施 → 业务 → 定时任务，四阶段骨架',
      stages:[{name:'网关', parallel:false, services:[]},{name:'基础设施', parallel:false, services:[]},{name:'业务服务', parallel:true, services:[]},{name:'定时任务', parallel:true, services:[]}] },
    { id:'biz', name:'仅业务服务', desc:'单阶段，并行发布全部业务服务',
      stages:[{name:'业务服务', parallel:true, services:[]}] },
    { id:'tasks', name:'仅定时任务', desc:'单阶段，串行发布定时任务',
      stages:[{name:'定时任务', parallel:false, services:[]}] },
    { id:'blank', name:'空白流水线', desc:'从空画布开始', stages:[] },
  ];
  // 按服务名关键字把项目服务自动归位到模板阶段（gateway→网关、job/task→定时任务、admin/common/base→基础设施、其余→业务）
  function applyPipelineTemplate(tpl, services){
    const stages = JSON.parse(JSON.stringify(tpl.stages));
    const findStage = name => stages.find(s => s.name === name);
    for (const svc of services){
      const n = svc.name.toLowerCase();
      let target = null;
      if (/gateway/.test(n)) target = findStage('网关');
      else if (/(job|task|snail|worker)/.test(n)) target = findStage('定时任务');
      else if (/(admin|common|base|infra|mqtt|system)/.test(n)) target = findStage('基础设施');
      else target = findStage('业务服务');
      if (target) target.services.push({ service_id: svc.id });
    }
    return stages;
  }

  // ---- 模块导出 ----
  window.cfgHelpers = {
    field, fieldErr, section, stepper, templateCard,
    tagInput, rowList,
    yamlToObj, objToYaml,
    ipValid, safeName,
    touchRecent, getRecent,
    pipelineCmd,
    ENV_TEMPLATES, PIPELINE_TEMPLATES, applyPipelineTemplate,
  };
})();
