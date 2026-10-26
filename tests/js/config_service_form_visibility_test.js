// config-service-form.js 显隐回归：三种 deploy_mode 的字段集互斥，切换时回填 Maven
// 运行：node tests/js/config_service_form_visibility_test.js
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

const BASE = {
  id: 1, name: 'hqb-auth', image_name: '', service_name: '', git_repo: '',
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
  let html = await openWith('container');
  check('容器：显示 Docker 字段',
        html.includes('id="sDockerCtx"') && html.includes('id="sDockerfile"'));
  check('容器：不渲染 Java 字段', !html.includes('id="jAppHome"'));
  check('容器：镜像短名必填', requiredOf('镜像短名') === true);

  html = await openWith('static_archive');
  check('静态：显示站点根', html.includes('id="sSiteRoot"'));
  check('静态：隐藏 Docker 与 Java 字段',
        !html.includes('id="sDockerCtx"') && !html.includes('id="jAppHome"'));

  html = await openWith('jar_service');
  check('jar：显示 app_home / 端口 / 单元名',
        html.includes('id="jAppHome"') && html.includes('id="jPort"')
        && html.includes('id="jUnit"'));
  check('jar：隐藏 Docker 字段', !html.includes('id="sDockerCtx"'));
  check('jar：镜像短名不必填', requiredOf('镜像短名') === false);
  check('jar：保留构建分区', html.includes('id="sec-build"'));

  // 模式切换必须同时回填构建类型：从「前端服务复制来的配置」切 jar，不能沿用 frontend
  html = await openWith('container', { build_type: 'frontend' });
  el('sDeployMode').onchange({ target: { value: 'jar_service' } });
  html = el('dwBody').innerHTML;
  check('切换 jar：重渲染出 Java 字段', html.includes('id="jAppHome"'));
  check('切换 jar：构建类型回填 Maven',
        /<option value="maven" selected/.test(html));
  check('切换 jar：Docker 字段消失', !html.includes('id="sDockerCtx"'));

  if (failures.length) {
    console.error('FAIL');
    failures.forEach(f => console.error(' - ' + f));
    process.exit(1);
  }
  console.log('PASS config_service_form_visibility_test');
})();
