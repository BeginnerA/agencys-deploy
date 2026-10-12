// config-service-form.js — 服务新增/编辑：从现有服务复制 + 分组表单 + 字段白话说明
(function(){
  'use strict';
  const H = window.cfgHelpers;

  function open(container, project, svc, onSaved){
    const st = {
      project, container, onSaved,
      svcs: [],
      data: svc ? Object.assign(JSON.parse(JSON.stringify(svc)), { enabled: !!svc.enabled }) : {
        name:'', image_name:'', service_name:'', git_repo:'',
        git_branch:'', git_pull_before_build:false, git_tag_enabled:true,
        build_type:'maven',
        build_order:[], docker_context:'', dockerfile:'',
        nacos_groups:[], dubbo_services:[], snailjob_group:'',
        health_port:'', health_url:'',
        log_error_keywords:['ERROR','Exception','Failed to start'], clear_log_dir:'',
        enabled: true,
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
          <div class="field full" style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
            <label style="margin:0;white-space:nowrap">启用状态</label>
            <input type="checkbox" id="sEnabled" ${d.enabled ? 'checked' : ''} style="display:none">
            <div id="swEnabled" style="position:relative;display:inline-block;width:40px;height:22px;cursor:pointer;border-radius:11px">
              <span id="swEnabledBg" style="position:absolute;inset:0;background:${d.enabled ? 'var(--c-primary,#4f7cff)' : 'var(--c-muted,#888)'};border-radius:11px;transition:.2s"></span>
              <span id="swEnabledDot" style="position:absolute;left:${d.enabled ? '20px' : '2px'};top:2px;width:18px;height:18px;background:#fff;border-radius:50%;transition:.2s"></span>
            </div>
            <span id="swEnabledTxt" class="weak" style="font-size:12px">${d.enabled ? '启用中 — 参与监控、告警与发布' : '已停用 — 不出现在监控/告警/总览'}</span>
          </div>
          ${H.field({ label:'服务名', required:true, tip:'发布命令 --project 参数；须与 docker-compose 服务名、镜像名对应；项目内唯一', input:`<input id="sName" value="${esc(d.name)}" placeholder="如 service-biz">`, hint:'被流水线 stages 引用后改名不受影响（引用按 id），但请同步更新发布习惯' })}
          ${H.field({ label:'镜像短名', required:true, tip:'完整镜像名 = {registry}/{namespace}/{image_name}:{version}', input:`<input id="sImage" value="${esc(d.image_name)}" placeholder="如 biz">`, hint:'不要带命名空间前缀，命名空间来自环境配置' })}
          ${H.field({ label:'Git 仓库路径', required:true, tip:'相对 deploy.project_root 的目录名，或 http(s) 远程 URL（首次构建自动 clone 到 project_root 下同名目录）；发布成功后在仓库打 v{version}(-{后缀}) tag 并推送远端（可下方关闭）', input:`<input id="sGit" value="${esc(d.git_repo)}" placeholder="如 service-biz 或 https://git.example.com/service-biz.git">`, hint:'多服务共用仓库时填仓库目录名；远程 URL 形态建议勾选下方「构建前拉取代码」' })}
          ${H.field({ label:'构建分支', tip:'构建前 checkout 的分支或 tag；留空则不切换，使用当前分支', input:`<input id="sGitBranch" value="${esc(d.git_branch || '')}" placeholder="如 master / v1.0.0">` })}
          <div class="field full" style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
            <label style="margin:0;white-space:nowrap">构建前拉取代码</label>
            <input type="checkbox" id="sGitPull" ${d.git_pull_before_build ? 'checked' : ''} style="display:none">
            <div id="swGitPull" style="position:relative;display:inline-block;width:40px;height:22px;cursor:pointer;border-radius:11px">
              <span id="swGitPullBg" style="position:absolute;inset:0;background:${d.git_pull_before_build ? 'var(--c-primary,#4f7cff)' : 'var(--c-muted,#888)'};border-radius:11px;transition:.2s"></span>
              <span id="swGitPullDot" style="position:absolute;left:${d.git_pull_before_build ? '20px' : '2px'};top:2px;width:18px;height:18px;background:#fff;border-radius:50%;transition:.2s"></span>
            </div>
            <span id="swGitPullTxt" class="weak" style="font-size:12px">${d.git_pull_before_build ? '开启 — 构建前执行 git pull 拉取最新代码' : '关闭 — 使用本地现有代码构建'}</span>
          </div>
          <div class="field full" style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
            <label style="margin:0;white-space:nowrap">发布后打 Git Tag</label>
            <input type="checkbox" id="sGitTag" ${d.git_tag_enabled ? 'checked' : ''} style="display:none">
            <div id="swGitTag" style="position:relative;display:inline-block;width:40px;height:22px;cursor:pointer;border-radius:11px">
              <span id="swGitTagBg" style="position:absolute;inset:0;background:${d.git_tag_enabled ? 'var(--c-primary,#4f7cff)' : 'var(--c-muted,#888)'};border-radius:11px;transition:.2s"></span>
              <span id="swGitTagDot" style="position:absolute;left:${d.git_tag_enabled ? '20px' : '2px'};top:2px;width:18px;height:18px;background:#fff;border-radius:50%;transition:.2s"></span>
            </div>
            <span id="swGitTagTxt" class="weak" style="font-size:12px">${d.git_tag_enabled ? '开启 — 发布成功后打 v{version}(-{环境后缀}) tag 并推送远端' : '关闭 — 发布成功不打 Git Tag'}</span>
          </div>
          ${H.field({ label:'docker-compose 服务名', required:true, tip:'docker-compose.yml 里的服务名', input:`<input id="sCompose" value="${esc(d.service_name)}" placeholder="留空自动填服务名">` })}
        </div>`;
      // ② 构建配置
      const isFrontend = d.build_type === 'frontend';
      const build = `
        <div class="form-grid">
          <div class="field full">
            <label>构建类型</label>
            <select id="sBuildType">
              <option value="maven" ${!isFrontend ? 'selected' : ''}>☕ Java (Maven)</option>
              <option value="frontend" ${isFrontend ? 'selected' : ''}>🌐 Web 前端 (npm/pnpm/yarn)</option>
            </select>
            <div class="f-hint">Java 项目使用 Maven 构建；Web 前端项目使用 npm/pnpm/yarn 等命令构建</div>
          </div>
          ${isFrontend ? `
          <div class="field full"><label>构建步骤（commands）</label>
            ${H.cmdList({ id:'sBuildOrder', rows: d.build_order }).html}
            <div class="f-hint">每个步骤填写工作目录和命令列表（每行一条命令）；支持 \${VERSION} 变量替换；自动探测 npm/pnpm/yarn 可执行路径。</div>
          </div>` : `
          <div class="field full"><label>构建顺序（build_order）</label>
            ${H.rowList({ id:'sBuildOrder', rows: d.build_order }).html}
            <div class="f-hint">按顺序对每个模块执行 mvn clean {goal} -P{profile}；common-build / api 等被依赖模块一般 install，最终服务模块 package；extra_profile 用于同一仓库按 profile 产出不同 jar 的项目。</div>
          </div>`}
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
          ${H.field({ label:'探活端口', tip:'服务业务端口；无专用健康接口时监控会自动用宽松 HTTP/TCP 探活', input:`<input id="sHealthPort" type="number" value="${esc(d.health_port || '')}" placeholder="如 8080">` })}
          ${H.field({ label:'健康检查 URL', tip:'（可选）专用探活接口，{ip} 会被替换为当前服务器 IP', input:`<input id="sHealthUrl" value="${esc(d.health_url)}" placeholder="如 /actuator/health（仅在需严格 200 时填）">`, hint:'留空则自动退化：宽松 HTTP（任意响应码即存活）→ TCP 端口连通兜底' })}
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
      if (isFrontend) {
        H.cmdList({ id:'sBuildOrder', rows: d.build_order }).bind();
      } else {
        H.rowList({ id:'sBuildOrder', rows: d.build_order }).bind();
      }
      // 构建类型切换：重新渲染表单
      const btSel = document.getElementById('sBuildType');
      if (btSel) btSel.onchange = e => {
        d.build_type = e.target.value;
        // 切换构建类型时清空构建步骤（避免 Maven 步骤与前端步骤混用）
        d.build_order = [];
        render();
      };
      H.tagInput({ id:'sNacosGroups', values: d.nacos_groups }).bind();
      H.tagInput({ id:'sDubbo', values: d.dubbo_services }).bind();
      H.tagInput({ id:'sLogKw', values: d.log_error_keywords }).bind();

      // 开关绑定：点击 div 切换 checkbox + 更新视觉
      function bindToggle(divId, cbId, bgId, dotId, txtId, onText, offText, w, h) {
        const div = document.getElementById(divId);
        if (!div) return;
        div.onclick = () => {
          const cb = document.getElementById(cbId);
          cb.checked = !cb.checked;
          document.getElementById(bgId).style.background = cb.checked ? 'var(--c-primary,#4f7cff)' : 'var(--c-muted,#888)';
          document.getElementById(dotId).style.left = cb.checked ? (w - 20) + 'px' : '2px';
          document.getElementById(txtId).textContent = cb.checked ? onText : offText;
        };
      }
      bindToggle('swEnabled', 'sEnabled', 'swEnabledBg', 'swEnabledDot', 'swEnabledTxt',
        '启用中 — 参与监控、告警与发布', '已停用 — 不出现在监控/告警/总览', 40, 22);
      bindToggle('swGitPull', 'sGitPull', 'swGitPullBg', 'swGitPullDot', 'swGitPullTxt',
        '开启 — 构建前执行 git pull 拉取最新代码', '关闭 — 使用本地现有代码构建', 40, 22);
      bindToggle('swGitTag', 'sGitTag', 'swGitTagBg', 'swGitTagDot', 'swGitTagTxt',
        '开启 — 发布成功后打 v{version}(-{环境后缀}) tag 并推送远端', '关闭 — 发布成功不打 Git Tag', 40, 22);

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
          git_repo: d.git_repo, git_branch: d.git_branch, git_pull_before_build: d.git_pull_before_build,
          git_tag_enabled: d.git_tag_enabled,
          build_type: d.build_type || 'maven',
          docker_context: d.docker_context, dockerfile: d.dockerfile,
          build_order: d.build_order, nacos_groups: d.nacos_groups, dubbo_services: d.dubbo_services,
          snailjob_group: d.snailjob_group, health_url: d.health_url,
          health_port: d.health_port, log_error_keywords: d.log_error_keywords, clear_log_dir: d.clear_log_dir,
          enabled: document.getElementById('sEnabled').checked,
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
      d.git_branch = val('sGitBranch').trim();
      d.git_pull_before_build = document.getElementById('sGitPull').checked;
      d.git_tag_enabled = document.getElementById('sGitTag').checked;
      d.build_type = val('sBuildType') || 'maven';
      d.service_name = val('sCompose').trim();
      d.docker_context = val('sDockerCtx').trim();
      d.dockerfile = val('sDockerfile').trim();
      d.snailjob_group = val('sSnailjob').trim();
      d.health_url = val('sHealthUrl').trim();
      d.clear_log_dir = val('sClearLog').trim();
      const hp = val('sHealthPort').trim();
      d.health_port = hp === '' ? 0 : Number(hp);
      d.enabled = document.getElementById('sEnabled').checked;
      d.build_order = (d.build_type === 'frontend'
        ? H.cmdList({ id:'sBuildOrder', rows:[] }).read()
        : H.rowList({ id:'sBuildOrder', rows:[] }).read());
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
