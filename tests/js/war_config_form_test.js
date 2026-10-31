// config-service-form.js WAR/Tomcat 显隐回归：字段集、构建锁定与管理按钮
// 运行：node tests/js/war_config_form_test.js
// 复用 config_service_form_visibility_test.js 的 vm harness，只改三处：
// api 对两次调用都返回 []；openWith('war_tomcat', {runtime_config: {...}})；断言。
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const noop = () => {};
const elements = {};
function el(id) {
  if (!elements[id]) {
    elements[id] = {
      id, innerHTML: '', value: '', checked: false, disabled: false,
      style: {}, textContent: '', onclick: null, onchange: null,
      classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
      addEventListener: noop, removeEventListener: noop, focus: noop,
    };
  }
  return elements[id];
}

// 只记录字段元信息：显隐/必填的断言不依赖真实 DOM，也不复制表单实现
const fields = [];
const H = {
  field: o => {
    fields.push({ label: o.label, required: !!o.required });
    return `<div class="field" data-label="${o.label}">${o.input || ''}</div>`;
  },
  section: o => ({ html: `<section id="sec-${o.id}">${o.body}</section>`, bind: noop }),
  fieldErr: noop,
  cmdList: () => ({ html: '', bind: noop }),
  rowList: () => ({ html: '', bind: noop }),
  tagInput: () => ({ bind: noop }),
};

const ctx = {
  console, setTimeout, clearTimeout,
  document: {
    getElementById: el,
    querySelectorAll: () => [],
    createElement: () => el('__created' + Object.keys(elements).length),
  },
  window: {},
  openDrawer: noop, skeletonHTML: () => '', errorState: () => '',
  esc: v => String(v == null ? '' : v),
  api: async () => [],
  toastError: noop, toastSuccess: noop,
};
ctx.window.cfgHelpers = H;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(
  __dirname, '../../webapp/static/js/pages/config/config-service-form.js'), 'utf8'), ctx);

const failures = [];
function check(name, cond) {
  if (!cond) failures.push(name);
}

const WAR_RC = {
  app_home: '/opt/apps/orders', tomcat_home: '/opt/tomcat',
  java_home: '/usr/lib/jvm/java-17', run_user: 'appuser',
  unit_name: 'orders-war', port: 18080, context_path: '/orders',
  jvm_opts: ['-Xms512m'], env_files: [],
  stop_timeout: 60, start_timeout: 120,
  readiness: { type: 'http', path: '/health', expected_status: 200,
               body_contains: '', timeout: 30, interval: 2 },
};
const BASE = {
  id: 1, name: 'orders', image_name: '', service_name: '', git_repo: '',
  build_type: 'maven', deploy_mode: 'container', build_order: [],
  nacos_groups: [], dubbo_services: [], log_error_keywords: ['ERROR'],
  runtime_config: { app_home: '/opt/hqb-auth', port: 8080 },
};
const requiredOf = label => {
  const f = fields.find(x => x.label === label);
  return f ? f.required : null;
};

async function openWith(mode, patch) {
  Object.keys(elements).forEach(k => delete elements[k]);
  fields.length = 0;
  ctx.window.cfgServiceForm.open(
    el('container'), { id: 7 },
    Object.assign({}, BASE, { deploy_mode: mode }, patch || {}), noop);
  await new Promise(r => setTimeout(r, 0));  // open() 内先 await api 才 render
  return el('dwBody').innerHTML;
}

(async () => {
  let html = await openWith('war_tomcat', { runtime_config: WAR_RC });
  check('WAR：显示 Tomcat/上下文/健康字段',
        html.includes('id="wTomcatHome"') && html.includes('id="wContext"')
        && html.includes('id="wHealthPath"'));
  check('WAR：不显示 Docker 与 jar 专属字段',
        !html.includes('id="sDockerCtx"') && !html.includes('id="jArtifactPattern"'));
  check('WAR：镜像短名不必填', requiredOf('镜像短名') === false);
  check('WAR：构建类型锁定 Maven',
        /<option value="maven" selected/.test(html) && html.includes('id="sBuildType"'));
  check('WAR：保留构建分区', html.includes('id="sec-build"'));
  check('WAR：提供预览/接入/更新/恢复按钮',
        ['wPreviewBtn', 'wOnboardBtn', 'wConfigBtn', 'wRecoveryBtn']
          .every(id => html.includes('id="' + id + '"')));
  // 保存后（BASE id:1）更新按钮必须可点：后端逐台拒绝未接入/维护主机，前端不再一刀切禁用
  check('WAR：保存后更新按钮无 disabled',
        !/<button[^>]*id="wConfigBtn"[^>]*disabled/.test(html));
  // 未保存（无 id）：更新按钮保持禁用
  html = await openWith('war_tomcat', { runtime_config: WAR_RC, id: undefined });
  check('WAR：未保存时更新按钮 disabled',
        /<button[^>]*id="wConfigBtn"[^>]*disabled/.test(html));

  // 从容器切到 WAR：字段集切换且构建类型回填 Maven
  html = await openWith('container', {build_type: 'frontend'});
  el('sDeployMode').onchange({target: {value: 'war_tomcat'}});
  html = el('dwBody').innerHTML;
  check('切换 WAR：重渲染 Tomcat 字段', html.includes('id="wTomcatHome"'));
  check('切换 WAR：构建类型回填 Maven', /<option value="maven" selected/.test(html));
  check('切换 WAR：Docker 字段消失', !html.includes('id="sDockerCtx"'));

  if (failures.length) {
    console.error('FAIL');
    failures.forEach(f => console.error(' - ' + f));
    process.exit(1);
  }
  console.log('PASS war_config_form_test');
})();
