// config-service-form.js — 服务新增/编辑：从现有服务复制 + 分组表单 + 字段白话说明
(function(){
  'use strict';
  const H = window.cfgHelpers;

  function open(container, project, svc, onSaved){
    const st = {
      project, container, onSaved,
      svcs: [],
      data: svc ? JSON.parse(JSON.stringify(svc)) : {
        name:'', image_name:'', service_name:'', git_repo:'',
        build_order:[], docker_context:'', dockerfile:'',
        nacos_groups:[], dubbo_services:[], snailjob_group:'',
        health_port:'', health_url:'',
        log_error_keywords:['ERROR','Exception','Failed to start'], clear_log_dir:'',
      },
    };
    openDrawer({ title: svc ? '编辑服务：' + svc.name : '新建服务', wide: true,
      body: '<div class="cfg-home">' + skeletonHTML(3) + '</div>', onClose: null });
    const bodyEl = document.getElementById('dwBody');
    (async () => {
      try{
        st.svcs = (await api(`/api/projects/${project.id}/services`)).filter(x => !svc || x.id !== svc.id);
        render();
      }catch(e){ bodyEl.innerHTML = errorState('加载失败：' + esc(e.message), 'navRetry'); }
    })();

    function render(){
      const d = st.data;
      // 复制/编辑源可能缺数组字段（历史数据 NULL），统一归一化避免 length 崩溃
      ['build_order','nacos_groups','dubbo_services','log_error_keywords'].forEach(k => {
        if (!Array.isArray(d[k])) d[k] = [];
      });
      // ① 基本信息
      const basic = `
        <div class="form-grid">
          ${H.field({ label:'服务名', required:true, tip:'发布命令 --project 参数；须与 docker-compose 服务名、镜像名对应；项目内唯一', input:`<input id="sName" value="${esc(d.name)}" placeholder="如 service-biz">`, hint:'被流水线 stages 引用后改名不受影响（引用按 id），但请同步更新发布习惯' })}
          ${H.field({ label:'镜像短名', required:true, tip:'完整镜像名 = {registry}/{namespace}/{image_name}:{version}', input:`<input id="sImage" value="${esc(d.image_name)}" placeholder="如 biz">`, hint:'不要带命名空间前缀，命名空间来自环境配置' })}
          ${H.field({ label:'Git 仓库路径', required:true, tip:'相对 deploy.project_root；发布成功后在仓库打 v{version} tag 并推送远端', input:`<input id="sGit" value="${esc(d.git_repo)}" placeholder="如 service-biz">`, hint:'多服务共用仓库时填仓库目录名' })}
          ${H.field({ label:'docker-compose 服务名', required:true, tip:'docker-compose.yml 里的服务名', input:`<input id="sCompose" value="${esc(d.service_name)}" placeholder="留空自动填服务名">` })}
        </div>`;
      // ② 构建配置
      const build = `
        <div class="form-grid">
          <div class="field full"><label>构建顺序（build_order）</label>
            ${H.rowList({ id:'sBuildOrder', rows: d.build_order }).html}
            <div class="f-hint">按顺序对每个模块执行 mvn clean {goal} -P{profile}；common-build / api 等被依赖模块一般 install，最终服务模块 package；extra_profile 用于同一仓库按 profile 产出不同 jar 的项目。</div>
          </div>
          ${H.field({ label:'Docker 上下文目录', tip:'docker build 的上下文', input:`<input id="sDockerCtx" value="${esc(d.docker_context)}" placeholder="如 .">` })}
          ${H.field({ label:'Dockerfile 路径', tip:'相对 docker_context', input:`<input id="sDockerfile" value="${esc(d.dockerfile)}" placeholder="如 Dockerfile">` })}
        </div>`;
      // ③ 注册中心与任务调度
      const reg = `
        <div class="form-grid">
          <div class="field full"><label>Nacos 分组（nacos_groups）</label>
            ${H.tagInput({ id:'sNacosGroups', values: d.nacos_groups, placeholder:'输入分组名后回车，如 DEFAULT_GROUP' }).html}
            <div class="f-hint">服务在多个分组注册时须全部列出（上下线会遍历所有分组）；不填则使用环境默认分组。</div>
          </div>
          <div class="field full"><label>Dubbo 额外服务（dubbo_services）</label>
            ${H.tagInput({ id:'sDubbo', values: d.dubbo_services, placeholder:'输入服务名后回车' }).html}
            <div class="f-hint">可选；Nacos 里除应用服务名外还可能出现的服务名。</div>
          </div>
          <div class="field full"><label>SnailJob 分组（snailjob_group）</label><input id="sSnailjob" value="${esc(d.snailjob_group)}" placeholder="如 DEFAULT_GROUP">
            <div class="f-hint">普通微服务留空即可；定时任务服务填 SnailJob 的 group_name。</div>
          </div>
        </div>`;
      // ④ 健康检查与日志
      const health = `
        <div class="form-grid">
          ${H.field({ label:'健康检查端口', tip:'用于判断服务是否已启动', input:`<input id="sHealthPort" type="number" value="${esc(d.health_port || '')}" placeholder="如 8080">` })}
          ${H.field({ label:'健康检查 URL', tip:'{ip} 会被替换为当前服务器 IP', input:`<input id="sHealthUrl" value="${esc(d.health_url)}" placeholder="如 http://{ip}:8080/actuator/health">`, hint:'留空则退化为容器状态 + 日志检查' })}
          <div class="field full"><label>启动失败关键字（log_error_keywords）</label>
            ${H.tagInput({ id:'sLogKw', values: d.log_error_keywords, placeholder:'输入关键字后回车' }).html}
            <div class="f-hint">启动日志中出现任一关键字即判定启动失败；默认已预填常用关键字。</div>
          </div>
          <div class="field full"><label>启动前清空日志目录（clear_log_dir）</label><input id="sClearLog" value="${esc(d.clear_log_dir)}" placeholder="如 /opt/app/logs">
            <div class="f-hint">宿主机应用日志目录（如 1Panel 挂载的 logs）；配置后在启动新容器前清空该目录下所有文件（保留目录结构）；留空则不清空。</div>
          </div>
        </div>`;

      const groups = [
        H.section({ id:'basic', icon:'📝', title:'基本信息', desc:'服务名 / 镜像 / 仓库 / compose 关联', open:true, body: basic }),
        H.section({ id:'build', icon:'🔨', title:'构建配置', desc:'构建顺序与 Docker 上下文', open:!!d.build_order.length, body: build }),
        H.section({ id:'reg', icon:'🛰️', title:'注册中心与任务调度', desc:'Nacos 分组 / Dubbo / SnailJob', open:!!(d.nacos_groups.length || d.dubbo_services.length || d.snailjob_group), body: reg }),
        H.section({ id:'health', icon:'💚', title:'健康检查与日志', desc:'探活端口 / URL / 启动失败判定', open:true, body: health }),
      ];

      bodyEl.innerHTML = `
        ${st.svcs.length ? `
        <div class="cfg-section"><div class="cs-head"><span class="cs-ico">📋</span><span class="cs-title">从现有服务复制</span><span class="cs-desc">把现有服务的全部配置带入下方表单</span></div>
        <div class="cs-body"><div class="field full"><label>选择源服务</label>
          <select id="svcCopy"><option value="">— 选择要复制的服务 —</option>${st.svcs.map(x => `<option value="${x.id}">${esc(x.name)}</option>`).join('')}</select>
          <div class="f-hint">选中后自动带入下方全部字段（含构建顺序 / 分组 / 错误关键字列表），改名字和差异项即可；服务名唯一性在保存时校验。</div>
        </div></div></div>` : ''}
        ${groups.map(g => g.html).join('')}
        <div style="display:flex;gap:10px;margin-top:18px"><button class="btn primary flex-1" id="svcSave">✓ 保存服务</button></div>`;

      groups.forEach(g => g.bind());
      H.rowList({ id:'sBuildOrder', rows: d.build_order }).bind();
      H.tagInput({ id:'sNacosGroups', values: d.nacos_groups }).bind();
      H.tagInput({ id:'sDubbo', values: d.dubbo_services }).bind();
      H.tagInput({ id:'sLogKw', values: d.log_error_keywords }).bind();

      const copySel = document.getElementById('svcCopy');
      if (copySel) copySel.onchange = e => {
        const src = st.svcs.find(x => x.id === Number(e.target.value));
        if (!src) return;
        const c = JSON.parse(JSON.stringify(src));
        delete c.id; delete c.project_id; delete c.created_at; delete c.enabled;
        st.data = c;
        render();
        toastSuccess(`已带入「${src.name}」的全部配置，改名字和差异项即可`);
      };
      document.getElementById('svcSave').onclick = async () => {
        if (!collect()) return;
        const d = st.data;
        const payload = {
          name: d.name, image_name: d.image_name, service_name: d.service_name || d.name,
          git_repo: d.git_repo, docker_context: d.docker_context, dockerfile: d.dockerfile,
          build_order: d.build_order, nacos_groups: d.nacos_groups, dubbo_services: d.dubbo_services,
          snailjob_group: d.snailjob_group, health_url: d.health_url,
          health_port: d.health_port, log_error_keywords: d.log_error_keywords, clear_log_dir: d.clear_log_dir,
        };
        try{
          if (svc) await api(`/api/projects/${project.id}/services/${svc.id}`, { method: 'PUT', body: JSON.stringify(payload) });
          else await api(`/api/projects/${project.id}/services`, { method: 'POST', body: JSON.stringify(payload) });
          toastSuccess(svc ? '服务已更新' : '服务已创建');
          const dr = document.getElementById('drawerRoot');
          dr.classList.add('hidden'); dr.innerHTML = '';
          if (onSaved) onSaved(); else navRetry();
        }catch(e){ toastError(e.message); }  // 服务名重复等后端 400 文案直接展示
      };
    }

    function collect(){
      const d = st.data;
      d.name = val('sName').trim();
      d.image_name = val('sImage').trim();
      d.git_repo = val('sGit').trim();
      d.service_name = val('sCompose').trim();
      d.docker_context = val('sDockerCtx').trim();
      d.dockerfile = val('sDockerfile').trim();
      d.snailjob_group = val('sSnailjob').trim();
      d.health_url = val('sHealthUrl').trim();
      d.clear_log_dir = val('sClearLog').trim();
      const hp = val('sHealthPort').trim();
      d.health_port = hp === '' ? 0 : Number(hp);
      d.build_order = H.rowList({ id:'sBuildOrder', rows:[] }).read();
      d.nacos_groups = H.tagInput({ id:'sNacosGroups', values:[] }).read();
      d.dubbo_services = H.tagInput({ id:'sDubbo', values:[] }).read();
      d.log_error_keywords = H.tagInput({ id:'sLogKw', values:[] }).read();
      // 就地校验
      let ok = true;
      const checks = [
        ['sName', 'name', '服务名必填'],
        ['sImage', 'image_name', '镜像短名必填'],
        ['sGit', 'git_repo', 'Git 仓库路径必填'],
      ];
      checks.forEach(([id, key, msg]) => {
        if (!d[key]){ H.fieldErr(id, msg); ok = false; }
        else H.fieldErr(id, '');
      });
      if (!Number.isInteger(d.health_port) || d.health_port < 0){
        H.fieldErr('sHealthPort', '健康检查端口必须为非负整数'); ok = false;
      }else H.fieldErr('sHealthPort', '');
      return ok;
    }

    function val(id){ const el = document.getElementById(id); return el ? el.value : ''; }
  }

  window.cfgServiceForm = { open };
})();
