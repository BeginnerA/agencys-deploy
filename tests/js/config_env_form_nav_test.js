// config-env-form.js 步骤回退：绑定服务器步骤必须能返回填写配置（编辑/新建均如此）；
// 编辑模式的填写配置步骤不提供"上一步"（回模板选择会用模板数据覆盖已填配置）
// 运行：node tests/js/config_env_form_nav_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const noop = () => {};
const elements = {};
// renderTpl/renderBind 在元素对象上调 querySelectorAll：缓存同一批伪元素，
// 使 onclick 赋值与测试中的触发调用指向同一对象
const pseudo = [{ dataset: { tpl: 'tpl-java' }, value: '', onclick: null }];
function el(id) {
  if (!elements[id]) {
    elements[id] = {
      id, innerHTML: '', value: '', checked: false, disabled: false,
      style: {}, textContent: '', onclick: null, onchange: null,
      classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
      addEventListener: noop, removeEventListener: noop, focus: noop,
      querySelectorAll: () => pseudo,
    };
  }
  return elements[id];
}

const H = {
  stepper: (steps, active) => steps.map((s, i) => `<span class="stp ${i === active ? 'on' : ''}">${i + 1}.${s}</span>`).join(''),
  templateCard: t => `<div class="tpl-card" data-tpl="${t.id}">${t.id}</div>`,
  ENV_TEMPLATES: [{ id: 'tpl-java', data: { registry: {}, nacos: {}, mysql_snailjob: {}, ssh: {}, build: {}, deploy: {} } }],
  field: o => `<div class="field">${o.input || ''}</div>`,
  fieldErr: noop,
  section: o => ({ html: `<div class="cfg-section" id="sec-${o.id}">${o.body}</div>`, bind: noop }),
  objToYaml: () => '',
  yamlToObj: () => ({}),
  safeName: v => /^[A-Za-z0-9_-]+$/.test(v || ''),
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
  __dirname, '../../webapp/static/js/pages/config/config-env-form.js'), 'utf8'), ctx);

const failures = [];
function check(name, cond) {
  if (!cond) failures.push(name);
}

const PROJ = { id: 1 };
const ENV = {
  id: 5, name: 'prod', is_default: 1, monitor_enabled: 0,
  docker_compose_path: '', maven_profile: '',
  registry: {}, nacos: {}, mysql_snailjob: {},
  ssh: { user: 'root', port: 22 }, build: {}, deploy: {},
};

async function reset() {
  Object.keys(elements).forEach(k => delete elements[k]);
  await new Promise(r => setTimeout(r, 0));
}

(async () => {
  // ---- 新建：step0 模板 → step1 填写（prev+next）→ next → step2 绑定（prev+save） ----
  await reset();
  ctx.window.cfgEnvForm.open(el('container'), PROJ, null, {});
  await new Promise(r => setTimeout(r, 0));
  let html = el('dwBody').innerHTML;
  check('新建 step0：渲染模板卡片', el('envStepBody').innerHTML.includes('data-tpl="tpl-java"'));
  el('dwBody').querySelectorAll('.tpl-card')[0].onclick();   // 选模板 → step1
  html = el('dwBody').innerHTML;
  check('新建 step1：渲染上一步（回模板）', html.includes('id="envPrev"'));
  check('新建 step1：渲染下一步（去绑定）', html.includes('id="envNext"'));
  el('envName').value = 'dev';
  el('envNext').onclick();                                    // collectForm → step2
  html = el('dwBody').innerHTML;
  check('新建 step2：渲染上一步（回填写）', html.includes('id="envPrev"'));
  check('新建 step2：渲染校验保存', html.includes('id="envSave"'));

  // ---- 编辑：绑定服务器入口直达 step2，必须能返回 step1 填写配置 ----
  await reset();
  ctx.window.cfgEnvForm.open(el('container'), PROJ, ENV, { step: 'bind' });
  await new Promise(r => setTimeout(r, 0));
  html = el('dwBody').innerHTML;
  check('编辑 step2：渲染上一步（回填写）', html.includes('id="envPrev"'));
  check('编辑 step2：渲染校验保存', html.includes('id="envSave"'));
  el('envPrev').onclick();                                    // 回 step1
  html = el('dwBody').innerHTML;
  check('编辑 step1：渲染填写配置表单', el('envStepBody').innerHTML.includes('id="envName"'));
  check('编辑 step1：不渲染上一步（回模板会覆盖已填数据）', !html.includes('id="envPrev"'));
  check('编辑 step1：渲染下一步（改完可回绑定）', html.includes('id="envNext"'));
  check('编辑 step1：配置数据回填（ssh.user=root）', /id="envssh_user"[^>]*value="root"/.test(el('envStepBody').innerHTML));

  // ---- 编辑默认入口（step1 起步）：无上一步，有下一步 ----
  await reset();
  ctx.window.cfgEnvForm.open(el('container'), PROJ, ENV, {});
  await new Promise(r => setTimeout(r, 0));
  html = el('dwBody').innerHTML;
  check('编辑 step1 起步：不渲染上一步', !html.includes('id="envPrev"'));
  check('编辑 step1 起步：渲染下一步（去绑定）', html.includes('id="envNext"'));

  if (failures.length) {
    console.error('FAIL');
    failures.forEach(f => console.error(' - ' + f));
    process.exit(1);
  }
  console.log('PASS config_env_form_nav_test');
})();
