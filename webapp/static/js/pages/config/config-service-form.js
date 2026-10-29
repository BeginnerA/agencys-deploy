// config-service-form.js — 服务新增/编辑：从现有服务复制 + 分组表单 + 字段白话说明
(function(){
  'use strict';
  const H = window.cfgHelpers;

  // 进程族：deploy_mode → 固定/缺省 build_type；none 作为外部投入产物的兜底
  const PROCESS_BUILD = { jar_service: 'maven', go_binary: 'go', rust_binary: 'rust',
                          node_process: 'node', python_process: 'python' };
  const PROCESS_RC_DEFAULT = {
    jar_service: { java_bin: '/usr/bin/java', artifact_pattern: 'target/*.jar',
                   readiness: { type: 'http', path: '/actuator/health',
                                timeout: 90, interval: 5 } },
    go_binary: { readiness: { type: 'process', path: '', timeout: 90, interval: 5 } },
    rust_binary: { readiness: { type: 'process', path: '', timeout: 90, interval: 5 } },
    node_process: { node_bin: '/usr/bin/node', start_file: '',
                    readiness: { type: 'process', path: '', timeout: 90, interval: 5 } },
    python_process: { requirements: 'requirements.txt', start_file: '',
                      readiness: { type: 'process', path: '', timeout: 90, interval: 5 } },
  };

  function open(container, project, svc, onSaved){
    const st = {
      project, container, onSaved,
      svcs: [],
      data: svc ? Object.assign(JSON.parse(JSON.stringify(svc)), { enabled: !!svc.enabled }) : {
        name:'', image_name:'', service_name:'', git_repo:'',
        git_branch:'', git_pull_before_build:false, git_tag_enabled:true,
        build_type:'maven', deploy_mode:'container',
        runtime_config:{app_home:'', java_bin:'/usr/bin/java', jvm_opts:'',
                        app_args:'', run_user:'', work_dir:'', unit_name:'',
                        port:'', stop_timeout:60, start_timeout:120,
                        artifact_pattern:'target/*.jar', env_files:[],
                        readiness:{type:'http', path:'/actuator/health',
                                   timeout:90, interval:5}},
        site_root:'', backup_dir:'', static_dist_dir:'', zip_prefix:'', entry_file:'', file_mode:'',
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
      const isStatic = (d.deploy_mode || 'container') === 'static_archive';
      const isProc = !!PROCESS_BUILD[d.deploy_mode];
      const isJar = d.deploy_mode === 'jar_service';
      const isGo = d.deploy_mode === 'go_binary';
      const isRust = d.deploy_mode === 'rust_binary';
      const isNode = d.deploy_mode === 'node_process';
      const isPython = d.deploy_mode === 'python_process';
      if (!d.runtime_config || typeof d.runtime_config !== 'object') d.runtime_config = {};
      const rc = d.runtime_config;
      const basic = `
        <div class="form-grid">
          <div class="field full">
            <label>发布方式</label>
            <select id="sDeployMode">
              <option value="container" ${!isStatic && !isProc ? 'selected' : ''}>🐳 容器（Docker 镜像 + compose）</option>
              <option value="static_archive" ${isStatic ? 'selected' : ''}>🗂️ 静态站点（zip → 1Panel/nginx 目录）</option>
              <option value="jar_service" ${isJar ? 'selected' : ''}>☕ 纯 Java 进程（jar → systemd）</option>
              <option value="go_binary" ${isGo ? 'selected' : ''}>🐹 Go 单二进制（systemd）</option>
              <option value="rust_binary" ${isRust ? 'selected' : ''}>🦀 Rust 单二进制（systemd）</option>
              <option value="node_process" ${d.deploy_mode === 'node_process' ? 'selected' : ''}>🟩 Node 进程（systemd）</option>
              <option value="python_process" ${d.deploy_mode === 'python_process' ? 'selected' : ''}>🐍 Python 进程（wheelhouse + venv）</option>
            </select>
            <div class="f-hint">静态站点：前端构建产物 zip 上传 1Panel 站点目录解压切换，无容器无 Nacos；版本号用日期号 YYYYMMDD 而非 semver。纯 Java 进程：Maven 构建出可执行 jar，按版本放入 app_home/releases 并切换 CURRENT 链接，由 systemd 托管；无容器、无镜像。Go/Rust 单二进制：平台机交叉编译（Go）或本地构建（Rust）出 Linux 可执行文件，按版本放入 app_home/releases 并切换 CURRENT 链接；目标机零运行时依赖。单实例重启期间服务短暂不可用，不承诺零停机。Node 进程：平台机 npm 构建（ci/build/prune）打 tar.gz（含 node_modules），目标机仅需 node；Python 进程：平台机收集 manylinux wheel，目标机 app_home/venv 离线安装，需预装 python3-venv。</div>
          </div>
          <div class="field full" style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
            <label style="margin:0;white-space:nowrap">启用状态</label>
            <input type="checkbox" id="sEnabled" ${d.enabled ? 'checked' : ''} style="display:none">
            <div id="swEnabled" style="position:relative;display:inline-block;width:40px;height:22px;cursor:pointer;border-radius:11px">
              <span id="swEnabledBg" style="position:absolute;inset:0;background:${d.enabled ? 'var(--c-primary,#4f7cff)' : 'var(--c-muted,#888)'};border-radius:11px;transition:.2s"></span>
              <span id="swEnabledDot" style="position:absolute;left:${d.enabled ? '20px' : '2px'};top:2px;width:18px;height:18px;background:#fff;border-radius:50%;transition:.2s"></span>
            </div>
            <span id="swEnabledTxt" class="weak" style="font-size:12px">${d.enabled ? '启用中 — 参与监控、告警与发布' : '已停用 — 不出现在监控/告警/总览'}</span>
          </div>
          ${H.field({ label:'服务名', required:true, tip:'发布命令 --project 参数；容器模式须与 docker-compose 服务名、镜像名对应；项目内唯一', input:`<input id="sName" value="${esc(d.name)}" placeholder="如 service-biz">`, hint:'被流水线 stages 引用后改名不受影响（引用按 id），但请同步更新发布习惯' })}
          ${H.field({ label:'镜像短名', required:!isStatic && !isProc, tip:'完整镜像名 = {registry}/{namespace}/{image_name}:{version}；静态站点不适用，留空自动回退服务名', input:`<input id="sImage" value="${esc(d.image_name)}" placeholder="${isStatic ? '留空即可（静态站点）' : '如 biz'}">`, hint:'不要带命名空间前缀，命名空间来自环境配置' })}
          ${H.field({ label:'Git 仓库路径', required:!isStatic && !isProc, tip:'相对 deploy.project_root 的目录名，或 http(s) 远程 URL（首次构建自动 clone 到 project_root 下同名目录）；发布成功后在仓库打 v{version}(-{后缀}) tag 并推送远端（可下方关闭）', input:`<input id="sGit" value="${esc(d.git_repo)}" placeholder="如 service-biz 或 https://git.example.com/service-biz.git">`, hint:'多服务共用仓库时填仓库目录名；远程 URL 形态建议勾选下方「构建前拉取代码」；手工传 zip 的静态发布可留空' })}
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
          ${H.field({ label:'docker-compose 服务名', required:!isStatic && !isProc, tip:'docker-compose.yml 里的服务名；静态站点不适用，留空自动回退服务名', input:`<input id="sCompose" value="${esc(d.service_name)}" placeholder="留空自动填服务名">` })}
        </div>`;
      // ② 构建配置（静态=前端；jar=Maven；go/rust=go/rust；node/python=node/python（均可切 none 外部投入））
      const isFrontend = isStatic || (!isProc && d.build_type === 'frontend');
      const btLocked = isStatic || isJar;  // 构建类型跟随发布方式锁定
      const btOptions = (mode) => {
        if (mode === 'jar_service') return `<option value="maven" selected>☕ Java (Maven)</option>`;
        if (mode === 'go_binary') return `<option value="go" ${d.build_type === 'go' ? 'selected' : ''}>🐹 Go 交叉编译</option><option value="none" ${d.build_type === 'none' ? 'selected' : ''}>📦 外部投入产物（none）</option>`;
        if (mode === 'rust_binary') return `<option value="rust" ${d.build_type === 'rust' ? 'selected' : ''}>🦀 Cargo 构建</option><option value="none" ${d.build_type === 'none' ? 'selected' : ''}>📦 外部投入产物（none）</option>`;
        if (mode === 'node_process') return `<option value="node" ${d.build_type === 'node' ? 'selected' : ''}>🟩 Node (npm)</option><option value="none" ${d.build_type === 'none' ? 'selected' : ''}>📦 外部投入产物（none）</option>`;
        if (mode === 'python_process') return `<option value="python" ${d.build_type === 'python' ? 'selected' : ''}>🐍 Python (wheelhouse)</option><option value="none" ${d.build_type === 'none' ? 'selected' : ''}>📦 外部投入产物（none）</option>`;
        return `<option value="maven" ${!isFrontend && !isStatic ? 'selected' : ''}>☕ Java (Maven)</option>
                <option value="frontend" ${isFrontend || isStatic ? 'selected' : ''}>🌐 Web 前端 (npm/pnpm/yarn)</option>`;
      };
      const build = `
        <div class="form-grid">
          <div class="field full">
            <label>构建类型</label>
            <select id="sBuildType" ${btLocked ? 'disabled' : ''}>${btOptions(d.deploy_mode)}</select>
            <div class="f-hint">${isStatic ? '静态站点固定为前端构建（构建产出目录打包 zip，不产镜像）'
              : isJar ? 'Java 项目使用 Maven 构建'
              : isGo ? 'Go：平台机交叉编译 Linux 二进制（CGO_ENABLED=0，GOOS=linux；架构与 ldflags 见构建配置文件）；none = 自行构建后把产物放入平台 dist 目录'
              : isRust ? 'Rust：cargo 构建（Windows 平台机须配置 rust_target，建议 WSL/Linux 构建或选 none）'
              : isNode ? 'Node：平台机 npm ci（有 build 脚本再 npm run build）→ npm prune --omit=dev，连同 node_modules 打 tar.gz；none = 自行构建后把产物放入平台 dist 目录'
              : isPython ? 'Python：平台机 pip download 收集 manylinux wheel 打成 wheelhouse 包（源码 + requirements + wheels/）；none = 自行构建后把产物放入平台 dist 目录'
              : 'Java 项目使用 Maven 构建；Web 前端项目使用 npm/pnpm/yarn 等命令构建'}</div>
          </div>
          ${isFrontend ? `
          <div class="field full"><label>构建步骤（commands）</label>
            ${H.cmdList({ id:'sBuildOrder', rows: d.build_order }).html}
            <div class="f-hint">每个步骤填写工作目录和命令列表（每行一条命令，框高随内容自动伸展）；支持 \${VERSION} 变量替换；自动探测 npm/pnpm/yarn 可执行路径；步骤级环境变量（env，如 NODE_OPTIONS）保存时原样保留。</div>
          </div>` : (isProc && !isJar ? `
          <div class="field full"><span class="weak">Go / Rust / Node / Python 构建无需 build_order：Go、Rust 的产物定位用构建配置（build.go_main / build.rust_bin），Node、Python 直接以项目根为工作目录，详见使用文档。</span></div>` : `
          <div class="field full"><label>构建顺序（build_order）</label>
            ${H.rowList({ id:'sBuildOrder', rows: d.build_order }).html}
            <div class="f-hint">按顺序对每个模块执行 mvn clean {goal} -P{profile}；common-build / api 等被依赖模块一般 install，最终服务模块 package；extra_profile 用于同一仓库按 profile 产出不同 jar 的项目。</div>
          </div>`)}
          ${isStatic || isProc ? '' : `
          ${H.field({ label:'Docker 上下文目录', tip:'docker build 的上下文', input:`<input id="sDockerCtx" value="${esc(d.docker_context)}" placeholder="如 .">` })}
          ${H.field({ label:'Dockerfile 路径', tip:'相对 docker_context', input:`<input id="sDockerfile" value="${esc(d.dockerfile)}" placeholder="如 Dockerfile">` })}`}
        </div>`;
      // ②b 静态站点专属配置（deploy_mode=static_archive）
      const staticCfg = isStatic ? `
        <div class="form-grid">
          ${H.field({ label:'站点根目录（site_root）', required:true, tip:'1Panel 站点根，如 /data/1panel/www/sites/example.com/index；切换是同父目录两次 rename 原子换入，路径本身不变，nginx 无需改动', input:`<input id="sSiteRoot" value="${esc(d.site_root || '')}" placeholder="/data/1panel/www/sites/example.com/index">`, hint:'必须为绝对路径；站点须已在 1Panel 创建（平台不会代建站点）' })}
          ${H.field({ label:'备份目录（backup_dir）', tip:'历代 zip 存放目录，默认站点根父目录下的 backup/（即 1Panel 站点的 backup/ 约定）；回滚源', input:`<input id="sBackupDir" value="${esc(d.backup_dir || '')}" placeholder="留空默认：站点父目录/backup">` })}
          ${H.field({ label:'构建产出目录（static_dist_dir）', tip:'相对 deploy.project_root 的构建产出目录，打包为 zip', input:`<input id="sStaticDist" value="${esc(d.static_dist_dir || '')}" placeholder="如 web/dist">`, hint:'手工上传 zip 不自动构建时可留空（把 {前缀}-{版本}.zip 放入平台 dist 目录，发布时勾选跳过构建）' })}
          ${H.field({ label:'zip 名前缀（zip_prefix）', tip:'产物为 {前缀}-{版本}.zip，默认取服务名；与现有手动发布习惯对齐可填「达人端」这类名称', input:`<input id="sZipPrefix" value="${esc(d.zip_prefix || '')}" placeholder="留空默认取服务名">` })}
          ${H.field({ label:'入口文件（entry_file）', tip:'解压后据此定位产物根目录（兼容 zip 多包一层目录），默认 index.html', input:`<input id="sEntryFile" value="${esc(d.entry_file || '')}" placeholder="留空默认 index.html">` })}
          ${H.field({ label:'文件权限（file_mode）', tip:'换入后文件权限归一，默认 0644（目录固定 0755，属主 root:root）', input:`<input id="sFileMode" value="${esc(d.file_mode || '')}" placeholder="留空默认 0644">` })}
        </div>` : '';
      // ②c 进程族专属配置（jar/go/rust/node/python 共享组 + 模式专属组）
      const procCfg = isProc ? `
        <div class="form-grid">
          ${H.field({ label:'应用根目录（app_home）', required:true, input:`<input id="jAppHome" value="${esc(rc.app_home || '')}" placeholder="${isJar ? '/opt/apps/hqb-auth' : '/opt/apps/' + esc(d.name || 'app')}">`, hint:`版本目录 releases/ 与 CURRENT 链接的根；回滚只换链接，不重传${isJar ? ' jar' : '产物'}` })}
          ${isJar ? H.field({ label:'Java 路径（java_bin）', input:`<input id="jJavaBin" value="${esc(rc.java_bin || '/usr/bin/java')}">`, hint:'目标机已安装的 JDK/JRE 绝对路径，平台不负责安装' })
                 + H.field({ label:'JVM 参数（jvm_opts）', input:`<input id="jJvmOpts" value="${esc(rc.jvm_opts || '')}" placeholder="-Xms512m -Xmx1g">` }) : ''}
          ${d.deploy_mode === 'node_process' ? H.field({ label:'Node 路径（node_bin）', input:`<input id="gNodeBin" value="${esc(rc.node_bin || '/usr/bin/node')}">`, hint:'目标机 node 绝对路径，平台不负责安装' }) : ''}
          ${(d.deploy_mode === 'node_process' || d.deploy_mode === 'python_process') ? H.field({ label:'入口文件（start_file）', required:true, input:`<input id="gStartFile" value="${esc(rc.start_file || '')}" placeholder="${d.deploy_mode === 'node_process' ? 'dist/server.js' : 'main.py'}">`, hint:'CURRENT 下的相对路径；ExecStart 直接执行该入口' }) : ''}
          ${d.deploy_mode === 'python_process' ? H.field({ label:'依赖清单（requirements）', input:`<input id="pRequirements" value="${esc(rc.requirements || 'requirements.txt')}">`, hint:'相对项目根；平台机收 wheel，目标机 venv 离线安装' })
                 + H.field({ label:'目标机 Python 版本（python_version）', input:`<input id="pPythonVersion" value="${esc(rc.python_version || '')}" placeholder="3.11">`, hint:'wheel 解析用；留空取平台机版本并需自行核对目标机一致' }) : ''}
          ${H.field({ label:'启动参数（app_args）', input:`<input id="jAppArgs" value="${esc(rc.app_args || '')}" placeholder="--spring.profiles.active=prod">` })}
          ${H.field({ label:'运行用户（run_user）', input:`<input id="jRunUser" value="${esc(rc.run_user || '')}" placeholder="appuser">`, hint:'留空则由 systemd 以 root 运行；平台不改目标机用户与权限' })}
          ${H.field({ label:'工作目录（work_dir）', input:`<input id="jWorkDir" value="${esc(rc.work_dir || '')}" placeholder="留空默认 {app_home}/CURRENT">` })}
          ${H.field({ label:'systemd 单元名（unit_name）', input:`<input id="jUnit" value="${esc(rc.unit_name || '')}" placeholder="留空默认同服务名">` })}
          ${H.field({ label:'端口（port）', input:`<input id="jPort" type="number" value="${esc(rc.port || '')}" placeholder="8080">`, hint:'HTTP/TCP 就绪方式必填；仅进程存活方式可留空' })}
          ${H.field({ label:'就绪方式（readiness.type）', input:`<select id="jReadinessType">
              <option value="http" ${(isJar ? (!rc.readiness || rc.readiness.type === 'http') : rc.readiness?.type === 'http') ? 'selected' : ''}>HTTP 健康端点</option>
              <option value="tcp" ${rc.readiness?.type === 'tcp' ? 'selected' : ''}>TCP 端口</option>
              <option value="process" ${(isJar ? (rc.readiness && rc.readiness.type === 'process') : (!rc.readiness?.type || rc.readiness?.type === 'process')) ? 'selected' : ''}>仅进程存活</option>
            </select>`, hint:isJar ? 'Java 缺省 HTTP + /actuator/health' : 'Go/Rust/Node/Python 缺省仅进程存活；有健康端点请显式选 HTTP/TCP' })}
          ${H.field({ label:'健康路径（readiness.path）', input:`<input id="jReadinessPath" value="${esc(rc.readiness?.path || (isJar ? '/actuator/health' : ''))}">`, hint:isJar ? '仅 HTTP 方式使用；要求响应体含 UP 语义（Actuator 约定）' : '仅 HTTP 方式使用' })}
          ${H.field({ label:'就绪超时/间隔（秒）', input:`<input id="jReadinessTimeout" type="number" value="${esc((rc.readiness && rc.readiness.timeout) || 90)}" style="width:45%"> <input id="jReadinessInterval" type="number" value="${esc((rc.readiness && rc.readiness.interval) || 5)}" style="width:45%">`, hint:'前为总超时，后为轮询间隔' })}
          ${H.field({ label:'停止/启动超时（秒）', input:`<input id="jStopTimeout" type="number" value="${esc(rc.stop_timeout || 60)}" style="width:45%"> <input id="jStartTimeout" type="number" value="${esc(rc.start_timeout || 120)}" style="width:45%">`, hint:'写入 systemd TimeoutStopSec / TimeoutStartSec' })}
          ${isJar ? H.field({ label:'可执行 jar 匹配（artifact_pattern）', input:`<input id="jArtifactPattern" value="${esc(rc.artifact_pattern || 'target/*.jar')}">`, hint:'命中多个时取体积最大者（fat jar 必然更大），并排除 sources/javadoc/original' }) : ''}
          ${H.field({ label:'环境变量文件（env_files）', input:`<textarea id="jEnvFiles" rows="2" placeholder="${isJar ? '/opt/apps/hqb-auth/.env' : '/opt/apps/' + esc(d.name || 'app') + '/.env'}">${esc((rc.env_files || []).join('\n'))}</textarea>`, hint:'每行一个绝对路径，写入 systemd EnvironmentFile；文件缺失会导致单元启动失败' })}
          <div class="hint">单实例发布重启期间服务不可用，平台不承诺零停机。首次发布前请用下方「预览 systemd 单元」核对 ExecStart。</div>
          <button class="btn" id="jUnitPreview" type="button" ${d.id ? '' : 'disabled title="保存后可预览"'}>预览 systemd 单元</button>
          <pre id="jUnitContent" class="weak" style="display:none;white-space:pre-wrap;font-size:12px;margin:0"></pre>
        </div>` : '';
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
        H.section({ id:'basic', icon:'📝', title:'基本信息', desc:'发布方式 / 服务名 / 镜像 / 仓库关联', open:true, body: basic }),
        H.section({ id:'build', icon:'🔨', title:'构建配置', desc:isStatic ? '前端构建步骤与静态打包产出' : (isProc ? '进程族构建与产物' : '构建顺序与 Docker 上下文'), open:!!(d.build_order.length || isStatic || isProc), body: build }),
        isStatic
          ? H.section({ id:'static', icon:'🗂️', title:'静态站点', desc:'1Panel 站点目录与 zip 产物约定', open:true, body: staticCfg })
          : (isProc
              ? H.section({ id:'jar', icon:'⚙️', title:'进程配置', desc:'app_home / systemd 单元 / 就绪方式', open:true, body: procCfg })
              : null),
        isStatic ? null : H.section({ id:'reg', icon:'🛰️', title:'注册中心与任务调度', desc:'Nacos 分组 / Dubbo / SnailJob', open:!!(d.nacos_groups.length || d.dubbo_services.length || d.snailjob_group), body: reg }),
        H.section({ id:'health', icon:'💚', title:'健康检查与日志', desc:isStatic ? '站点探活 URL（发布后 HTTP 验证）' : '探活端口 / URL / 启动失败判定', open:true, body: health }),
      ].filter(Boolean);

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
      // 发布方式切换：静态↔容器↔进程族重新渲染表单（字段集差异大，整体重建最可靠）
      const dmSel = document.getElementById('sDeployMode');
      if (dmSel) dmSel.onchange = e => {
        d.deploy_mode = e.target.value;
        const prev = d.runtime_config || {};
        const base = PROCESS_RC_DEFAULT[d.deploy_mode] || {};
        d.runtime_config = { ...base, ...prev,
          readiness: { ...(base.readiness || {}), ...(prev.readiness || {}) } };
        if (d.deploy_mode === 'static_archive') d.build_type = 'frontend';
        else if (PROCESS_BUILD[d.deploy_mode]) d.build_type = PROCESS_BUILD[d.deploy_mode];
        render();
      };
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
          deploy_mode: d.deploy_mode || 'container',
          runtime_config: PROCESS_BUILD[d.deploy_mode] ? d.runtime_config : {},
          site_root: d.site_root || '', backup_dir: d.backup_dir || '',
          static_dist_dir: d.static_dist_dir || '', zip_prefix: d.zip_prefix || '',
          entry_file: d.entry_file || '', file_mode: d.file_mode || '',
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
      // systemd 单元预览：复用当前服务 id，直接取后端生成的单元文本（不做前端拼装）
      const previewBtn = document.getElementById('jUnitPreview');
      if (previewBtn) previewBtn.onclick = async () => {
        if (!svc) return toastError('请先保存服务，再预览单元');
        try {
          const res = await api(`/api/projects/${project.id}/services/${svc.id}/unit`);
          const pre = document.getElementById('jUnitContent');
          pre.style.display = 'block';
          pre.textContent = `# ${res.unit_name}.service\n\n${res.content}`;
        } catch (e) { toastError(e.message); }
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
      d.deploy_mode = val('sDeployMode') || 'container';
      const isStatic = d.deploy_mode === 'static_archive';
      const isProc = !!PROCESS_BUILD[d.deploy_mode];
      const isJar = d.deploy_mode === 'jar_service';
      d.site_root = val('sSiteRoot').trim();
      d.backup_dir = val('sBackupDir').trim();
      d.static_dist_dir = val('sStaticDist').trim();
      d.zip_prefix = val('sZipPrefix').trim();
      d.entry_file = val('sEntryFile').trim();
      d.file_mode = val('sFileMode').trim();
      d.build_type = isJar ? 'maven'
        : (isProc ? (val('sBuildType') || PROCESS_BUILD[d.deploy_mode])
                  : (isStatic ? 'frontend' : (val('sBuildType') || 'maven')));
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
        : (isProc && !isJar ? [] : H.rowList({ id:'sBuildOrder', rows:[] }).read()));
      d.nacos_groups = H.tagInput({ id:'sNacosGroups', values:[] }).read();
      d.dubbo_services = H.tagInput({ id:'sDubbo', values:[] }).read();
      d.log_error_keywords = H.tagInput({ id:'sLogKw', values:[] }).read();
      // 就地校验
      let ok = true;
      const checks = isStatic
        ? [['sName', 'name', '服务名必填']]
        : isProc
          ? [['sName', 'name', '服务名必填']]
          : [['sName', 'name', '服务名必填'],
             ['sImage', 'image_name', '镜像短名必填'],
             ['sGit', 'git_repo', 'Git 仓库路径必填']];
      checks.forEach(([id, key, msg]) => {
        if (!d[key]){ H.fieldErr(id, msg); ok = false; }
        else H.fieldErr(id, '');
      });
      if (isProc) {
        const num = (id, dflt) => {
          const n = Number(val(id));
          return Number.isFinite(n) && n > 0 ? n : dflt;
        };
        const prev = d.runtime_config || {};
        d.runtime_config = {
          ...prev,
          app_home: val('jAppHome').trim(),
          app_args: val('jAppArgs').trim(),
          run_user: val('jRunUser').trim(),
          work_dir: val('jWorkDir').trim(),
          unit_name: val('jUnit').trim(),
          port: Number(val('jPort') || 0),
          stop_timeout: num('jStopTimeout', 60),
          start_timeout: num('jStartTimeout', 120),
          env_files: val('jEnvFiles').split('\n').map(x => x.trim()).filter(Boolean),
          readiness: {
            ...(prev.readiness || {}),
            type: val('jReadinessType') || (isJar ? 'http' : 'process'),
            path: val('jReadinessPath').trim() || (isJar ? '/actuator/health' : ''),
            timeout: num('jReadinessTimeout', 90),
            interval: num('jReadinessInterval', 5),
          },
        };
        if (isJar) {
          d.runtime_config.java_bin = val('jJavaBin').trim() || '/usr/bin/java';
          d.runtime_config.jvm_opts = val('jJvmOpts').trim();
          d.runtime_config.artifact_pattern = val('jArtifactPattern').trim() || 'target/*.jar';
        }
        if (d.deploy_mode === 'node_process') {
          d.runtime_config.node_bin = val('gNodeBin').trim() || '/usr/bin/node';
          d.runtime_config.start_file = val('gStartFile').trim();
        }
        if (d.deploy_mode === 'python_process') {
          d.runtime_config.requirements = val('pRequirements').trim() || 'requirements.txt';
          d.runtime_config.start_file = val('gStartFile').trim();
          d.runtime_config.python_version = val('pPythonVersion').trim();
        }
        if (!d.runtime_config.app_home.startsWith('/')) {
          H.fieldErr('jAppHome', isJar ? '应用根目录必须为绝对路径（如 /opt/apps/hqb-auth）' : '应用根目录必须为绝对路径（如 /opt/apps/app）'); ok = false;
        } else H.fieldErr('jAppHome', '');
        const rt = d.runtime_config.readiness.type;
        const p = d.runtime_config.port;
        if ((rt === 'http' || rt === 'tcp') && !(Number.isInteger(p) && p > 0 && p < 65536)) {
          H.fieldErr('jPort', 'HTTP/TCP 就绪方式必须填写 1-65535 的端口'); ok = false;
        } else H.fieldErr('jPort', '');
        if ((d.deploy_mode === 'node_process' || d.deploy_mode === 'python_process')
            && (!d.runtime_config.start_file || d.runtime_config.start_file.startsWith('/'))) {
          H.fieldErr('gStartFile', '入口文件必填，且为 CURRENT 下的相对路径'); ok = false;
        } else if (document.getElementById('gStartFile')) H.fieldErr('gStartFile', '');
      }
      if (isStatic && !d.site_root.startsWith('/')){
        H.fieldErr('sSiteRoot', '静态发布必须填写站点根绝对路径（如 /data/1panel/www/sites/example.com/index）'); ok = false;
      } else if (isStatic) H.fieldErr('sSiteRoot', '');
      if (!Number.isInteger(d.health_port) || d.health_port < 0){
        H.fieldErr('sHealthPort', '健康检查端口必须为非负整数'); ok = false;
      }else H.fieldErr('sHealthPort', '');
      return ok;
    }

    function val(id){ const el = document.getElementById(id); return el ? el.value : ''; }
  }

  window.cfgServiceForm = { open };
})();
