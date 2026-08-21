# 配置管理重设计（傻瓜式运维创作）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将配置管理重构为"傻瓜式运维创作"：配置工作台首页 + 项目仪表盘 + 模板化的分组表单（环境/服务/服务器）+ 拖拽画布流水线编辑器 + 环境↔服务器绑定入口，后端 API 零改动。

**Architecture:** 纯前端重构。保留 `window.config = {render, destroy}` 入口（router.js 兼容），将旧 `pages/config.js` 移动为 `pages/config/config.js` 入口，其余逻辑拆分到 `pages/config/` 目录 7 个子模块（helpers/home/project/env-form/pipeline-editor/service-form/server-form），子模块挂全局命名空间 `window.cfg*`。路由改造支持 `#/config/p/<pid>/<sub>` 子路径（router.js 前缀匹配 + hash 变化强制刷新）。零依赖：拖拽用 HTML5 Drag & Drop，YAML 转换用手写受限子集解析器（环境 6 大块均为嵌套 map + 标量，无数组）。

**Tech Stack:** 原生 HTML/CSS/JS（无 npm、无构建链、无测试框架）。验证方式 = 启动 Flask + 浏览器冒烟（沿袭 2026-08-15 计划约定）。

---

## 全局约定

- **后端零改动**：不得修改 `webapp/routes/*`、`webapp/*.py`、`deploy*.py`、`scripts/*`。全部消费现有 API（已核实：projects/services/environments/pipelines CRUD、`/environments/<eid>/servers` bind/unbind/reorder、`/servers/test`、`/users`、`/tasks?project_id=&limit=`、`/config/import|export`）。
- **验证方式**：每个任务完成后的验证均以「启动 Flask + 浏览器访问」为基准。启动命令（后台运行）：
  ```powershell
  cd d:\work\IdeaProjects\Reid\agencys\agencys-deploy
  python webapp/app.py --port 5678
  ```
  浏览器访问 `http://127.0.0.1:5678/screen#/config` 检查页面与 console 无报错。若端口被占用（Windows Flask 双实例缺陷），先结束旧进程再启动。
- **commit 步骤**：项目当前不在 git 仓库中（已核实无 `.git`）。若执行时已初始化仓库，按步骤执行 `git add`/`git commit`；否则跳过该步。
- **安全与卫生（沿用既有约定）**：所有插值用户/后端数据处必须 `esc()`；抽屉关闭后 `root.innerHTML = ''` 防密码残留 DOM；密码输入编辑态显示"已遮蔽，留空不修改"。
- **命名空间**：`window.cfgHelpers / cfgHome / cfgProject / cfgEnvForm / cfgPipelineEditor / cfgServiceForm / cfgServerForm`；入口 `config.js` 导出 `window.config`。
- **表单保存回调**：各表单模块的 `open()` 接受可选 `onSaved` 回调，保存成功后由调用方刷新列表。
- **删除旧文件**：旧 config.js 逻辑在 Task 10 一次性替换，此前页面行为与旧版完全一致，无中间态风险。

## 文件结构（目标态）

```
webapp/static/js/pages/config.js                      # 移动后位置：config/config.js（入口，Task 1 移动，Task 10 替换内容）
webapp/static/js/pages/config/config-form-helpers.js  # 共享：字段/分区/步骤条/标签输入器/行编辑器/YAML 转换/模板常量/最近项目
webapp/static/js/pages/config/config-home.js          # 配置工作台首页（统计/快速开始/最近项目/空态）+ 用户管理视图
webapp/static/js/pages/config/config-project.js       # 项目仪表盘 + 子页框架 + 各列表 + 绑定管理 + YAML 页
webapp/static/js/pages/config/config-env-form.js      # 环境表单（模板/分组/高级模式/绑定服务器）
webapp/static/js/pages/config/config-pipeline-editor.js  # 流水线画布（拖拽）
webapp/static/js/pages/config/config-service-form.js  # 服务表单（复制/分组）
webapp/static/js/pages/config/config-server-form.js   # 服务器表单（凭据继承/角色卡/校验/测试）
webapp/static/css/pages/config.css                    # 扩充：统计卡/模板卡/分区/画布/校验态等
webapp/static/js/router.js                            # 修改：子路由前缀匹配 + hash 变化强制刷新
webapp/templates/screen.html                          # 修改：替换 config 相关 script 引用
```

---

## Task 1: 路由与页面壳改造（router.js + screen.html + 移动旧 config.js）

**Files:**
- Modify: `webapp/static/js/router.js`
- Modify: `webapp/templates/screen.html`
- Move: `webapp/static/js/pages/config.js` → `webapp/static/js/pages/config/config.js`

- [ ] **Step 1: 修改 `webapp/static/js/router.js`**（两处改动）

改动 A — `getRoute()` 前缀匹配（原为精确匹配 `#/config`，子路径 `#/config/p/1/services` 会 fallback 到 dashboard）：

```js
function getRoute(){
  const hash = location.hash.replace(/^#\/?/, '').split('?')[0] || 'dashboard';
  const base = hash.split('/')[0];   // 支持子路径：#/config/p/1/services → base='config'
  return ROUTES.find(r => r.path === base) || ROUTES[0];
}
```

改动 B — `ROUTES` 中 config 路由加 `sub: true`（声明其支持子路由，hash 变化时强制刷新）：

```js
  { path:'config',    group:'系统管理', icon:'⚙️', name:'配置管理', page:'config',    crumbs:['系统管理','配置管理'], adminOnly:true, sub:true },
```

改动 C — `navigate()` 短路逻辑：同一 base 下 hash 变化（config 子路由切换）时也要重新渲染。将：

```js
let currentRoute = null;
let activeTimers = [];

function getRoute(){ ... }
```

改为（在 `let currentRoute = null;` 后追加 `lastHash`）：

```js
let currentRoute = null;
let lastHash = null;
let activeTimers = [];
```

将：

```js
async function navigate(force){
  const route = getRoute();
  if (route.adminOnly && !isAdmin()){ location.hash = '#/dashboard'; return; }
  if (!force && currentRoute && currentRoute.path === route.path) return;
```

改为：

```js
async function navigate(force){
  const route = getRoute();
  if (route.adminOnly && !isAdmin()){ location.hash = '#/dashboard'; return; }
  const hash = location.hash || '#/';
  const sameBase = currentRoute && currentRoute.path === route.path;
  const subChanged = route.sub && lastHash !== null && hash !== lastHash;
  lastHash = hash;
  if (!force && sameBase && !subChanged) return;
```

- [ ] **Step 2: 修改 `webapp/templates/screen.html`**（替换 config 相关 script 引用）

将：

```html
<script src="/static/js/pages/config.js"></script>
```

替换为：

```html
<script src="/static/js/pages/config/config-form-helpers.js"></script>
<script src="/static/js/pages/config/config-home.js"></script>
<script src="/static/js/pages/config/config-project.js"></script>
<script src="/static/js/pages/config/config-env-form.js"></script>
<script src="/static/js/pages/config/config-pipeline-editor.js"></script>
<script src="/static/js/pages/config/config-service-form.js"></script>
<script src="/static/js/pages/config/config-server-form.js"></script>
<script src="/static/js/pages/config/config.js"></script>
```

- [ ] **Step 3: 移动旧 config.js 到新目录**

```powershell
New-Item -ItemType Directory -Path "d:\work\IdeaProjects\Reid\agencys\agencys-deploy\webapp\static\js\pages\config" -Force | Out-Null
Move-Item "d:\work\IdeaProjects\Reid\agencys\agencys-deploy\webapp\static\js\pages\config.js" "d:\work\IdeaProjects\Reid\agencys\agencys-deploy\webapp\static\js\pages\config\config.js"
```

（此时 screen.html 引用的新路径文件尚不存在 → 浏览器会 404 加载失败。**先创建 7 个空占位文件**保证页面可运行：）

```powershell
$dir = "d:\work\IdeaProjects\Reid\agencys\agencys-deploy\webapp\static\js\pages\config"
foreach ($f in @('config-form-helpers.js','config-home.js','config-project.js','config-env-form.js','config-pipeline-editor.js','config-service-form.js','config-server-form.js')) {
  Set-Content -Path "$dir\$f" -Value "// placeholder（Task 3-9 填充）"
}
```

- [ ] **Step 4: 验证**

启动 Flask（后台）后浏览器访问：
- `http://127.0.0.1:5678/screen#/config` → 旧版配置页正常渲染（topTab=projects）
- `http://127.0.0.1:5678/screen#/config/p/1/services` → 仍渲染旧版配置页（子路由此时无内部处理属预期中间态），**关键验证点：不会 fallback 到 dashboard**
- `http://127.0.0.1:5678/screen#/config/users` → 同上不 fallback
- console 无 404/报错（除占位文件本身无逻辑）

- [ ] **Step 5: Commit**

```bash
git add webapp/static/js/router.js webapp/templates/screen.html webapp/static/js/pages/
git commit -m "feat(config): 路由支持配置管理子路径并拆分页面模块目录"
```

---

## Task 2: config.css 扩充（统计卡/模板卡/分区/画布/校验态）

**Files:**
- Modify: `webapp/static/css/pages/config.css`（整体追加以下内容，原 13 行保留）

- [ ] **Step 1: 在 `webapp/static/css/pages/config.css` 末尾追加全部样式**

```css
/* ============ 页面标题与分区标题 ============ */
.page-title{font-size:20px;font-weight:700;color:var(--text-1);letter-spacing:.3px}
.page-sub{font-size:12px;color:var(--text-2);margin:2px 0 var(--sp-3)}
.section-title{font-size:15px;font-weight:600;color:var(--text-1);margin:var(--sp-5) 0 var(--sp-3);display:flex;align-items:center;gap:8px}
.section-title .tag{font-size:11px;color:var(--text-3);background:var(--bg-hover);border:1px solid var(--border);border-radius:var(--r-full);padding:2px 10px;font-weight:400}

/* ============ 配置工作台首页 ============ */
.cfg-home{display:flex;flex-direction:column;gap:var(--sp-5)}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:var(--sp-4)}
.stat-card{background:var(--bg-panel);border:1px solid var(--border);border-radius:var(--r-panel);padding:var(--sp-4);display:flex;flex-direction:column;gap:6px;cursor:pointer;transition:var(--t-smooth)}
.stat-card:hover{border-color:var(--border-strong);transform:translateY(-2px)}
.stat-card .st-ico{font-size:20px}
.stat-card .st-num{font-size:26px;font-weight:700;color:var(--text-1);font-family:var(--font-num);line-height:1}
.stat-card .st-num .sub{font-size:12px;color:var(--text-2);font-weight:400;margin-left:6px}
.stat-card .st-label{font-size:12px;color:var(--text-2)}
.stat-card .st-tip{font-size:11px;color:var(--text-3)}
.quick-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:var(--sp-4)}
.quick-card{background:var(--bg-panel);border:1px solid var(--border);border-radius:var(--r-panel);padding:var(--sp-4);cursor:pointer;transition:var(--t-smooth);display:flex;flex-direction:column;gap:8px}
.quick-card:hover{border-color:var(--border-strong);transform:translateY(-2px)}
.quick-card .qc-head{display:flex;align-items:center;gap:10px}
.quick-card .qc-ico{width:38px;height:38px;border-radius:10px;background:rgba(0,212,255,.1);display:flex;align-items:center;justify-content:center;font-size:19px;flex-shrink:0}
.quick-card .qc-title{font-size:14px;font-weight:600;color:var(--text-1)}
.quick-card .qc-desc{font-size:12px;color:var(--text-2);line-height:1.6}
.quick-card .qc-time{font-size:11px;color:var(--text-3)}
.recent-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:var(--sp-4)}
.recent-item{background:var(--bg-panel);border:1px solid var(--border);border-radius:var(--r-panel);padding:var(--sp-3) var(--sp-4);display:flex;align-items:center;gap:12px;cursor:pointer;transition:var(--t-smooth)}
.recent-item:hover{border-color:var(--border-strong)}
.recent-item .ri-ico{font-size:18px}
.recent-item .ri-name{font-size:13px;font-weight:600;color:var(--text-1)}
.recent-item .ri-meta{font-size:11px;color:var(--text-3)}
.onboard-card{background:linear-gradient(135deg,rgba(0,212,255,.08),rgba(0,227,150,.06));border:1px dashed var(--border-strong);border-radius:var(--r-panel);padding:var(--sp-5);display:flex;flex-direction:column;gap:12px;align-items:flex-start}
.onboard-steps{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:13px;color:var(--text-2)}
.onboard-steps .os-arrow{color:var(--text-3)}

/* ============ 项目仪表盘 ============ */
.proj-bar{background:var(--bg-panel);border:1px solid var(--border);border-radius:var(--r-panel);padding:var(--sp-4) var(--sp-5);display:flex;align-items:center;gap:var(--sp-4);flex-wrap:wrap}
.proj-bar .pb-ico{width:44px;height:44px;border-radius:12px;background:rgba(0,212,255,.12);display:flex;align-items:center;justify-content:center;font-size:22px;flex-shrink:0}
.proj-bar .pb-name{font-size:16px;font-weight:700;color:var(--text-1)}
.proj-bar .pb-desc{font-size:12px;color:var(--text-2)}
.proj-bar .pb-code{font-size:11px;color:var(--text-3);font-family:var(--font-num)}
.proj-bar .pb-badges{display:flex;gap:8px;flex-wrap:wrap;margin-left:auto}
.badge-issue{background:rgba(255,69,96,.12);color:var(--danger);border:1px solid rgba(255,69,96,.35);border-radius:var(--r-full);padding:3px 10px;font-size:11px}
.badge-health{background:rgba(0,227,150,.1);color:var(--success);border:1px solid rgba(0,227,150,.3);border-radius:var(--r-full);padding:3px 10px;font-size:11px}
.badge-health.bad{background:rgba(255,176,25,.1);color:var(--warning);border-color:rgba(255,176,25,.35)}
.overview-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:var(--sp-4)}
.ov-card{background:var(--bg-panel);border:1px solid var(--border);border-radius:var(--r-panel);padding:var(--sp-4);display:flex;flex-direction:column;gap:8px;cursor:pointer;transition:var(--t-smooth)}
.ov-card:hover{border-color:var(--border-strong);transform:translateY(-2px)}
.ov-card .oc-num{font-size:28px;font-weight:700;font-family:var(--font-num);color:var(--primary)}
.ov-card .oc-label{font-size:13px;font-weight:600;color:var(--text-1)}
.ov-card .oc-desc{font-size:11px;color:var(--text-3);line-height:1.5;flex:1}
.ov-card .oc-go{font-size:12px;color:var(--primary)}
.proj-tabs{display:flex;gap:4px;margin:var(--sp-4) 0;flex-wrap:wrap}
.proj-tabs .pt{font-size:13px;color:var(--text-2);padding:6px 14px;border-radius:var(--r-full);cursor:pointer;border:1px solid transparent;transition:var(--t-fast)}
.proj-tabs .pt:hover{color:var(--text-1);background:var(--bg-hover)}
.proj-tabs .pt.on{background:rgba(0,212,255,.12);color:var(--primary);border-color:rgba(0,212,255,.3)}

/* ============ 模板选择卡 ============ */
.tpl-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:var(--sp-3)}
.tpl-card{background:var(--bg-panel);border:1px solid var(--border);border-radius:var(--r-panel);padding:var(--sp-4);cursor:pointer;transition:var(--t-smooth);display:flex;flex-direction:column;gap:8px}
.tpl-card:hover{border-color:var(--border-strong);transform:translateY(-2px)}
.tpl-card.sel{border-color:var(--primary);box-shadow:var(--shadow-glow);background:rgba(0,212,255,.05)}
.tpl-card .tc-ico{font-size:24px}
.tpl-card .tc-title{font-size:13px;font-weight:600;color:var(--text-1)}
.tpl-card .tc-fill{font-size:11px;color:var(--success);line-height:1.5}
.tpl-card .tc-need{font-size:11px;color:var(--warning);line-height:1.5}
.tpl-note{margin-top:var(--sp-3);font-size:11px;color:var(--text-3)}

/* ============ 步骤条 ============ */
.steps{display:flex;align-items:center;gap:6px;margin-bottom:var(--sp-4);flex-wrap:wrap}
.steps .stp{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-3)}
.steps .stp .stp-n{width:20px;height:20px;border-radius:50%;border:1px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:11px;flex-shrink:0}
.steps .stp.on{color:var(--text-1)}
.steps .stp.on .stp-n{background:var(--primary);border-color:var(--primary);color:#06121F;font-weight:700}
.steps .stp.done .stp-n{background:rgba(0,227,150,.15);border-color:var(--success);color:var(--success)}
.steps .stp-sep{width:18px;height:1px;background:var(--border)}

/* ============ 分组折叠区 ============ */
.cfg-section{border:1px solid var(--border);border-radius:var(--r-panel);margin-bottom:var(--sp-3);overflow:hidden}
.cfg-section .cs-head{display:flex;align-items:center;gap:10px;padding:12px 16px;cursor:pointer;user-select:none;background:var(--bg-panel)}
.cfg-section .cs-head:hover{background:var(--bg-panel-2)}
.cfg-section .cs-ico{font-size:16px}
.cfg-section .cs-title{font-size:13px;font-weight:600;color:var(--text-1)}
.cfg-section .cs-desc{font-size:11px;color:var(--text-3);flex:1}
.cfg-section .cs-badge{font-size:10px;color:var(--success);border:1px solid rgba(0,227,150,.35);border-radius:var(--r-full);padding:1px 8px;display:none}
.cfg-section.has .cs-badge{display:inline-block}
.cfg-section .cs-arrow{font-size:11px;color:var(--text-3);transition:var(--t-fast)}
.cfg-section.closed .cs-arrow{transform:rotate(-90deg)}
.cfg-section .cs-body{padding:14px 16px;display:flex;flex-direction:column;gap:var(--sp-3);border-top:1px solid var(--border);background:rgba(10,20,47,.4)}
.cfg-section.closed .cs-body{display:none}

/* ============ 字段说明与校验 ============ */
.f-hint{font-size:11px;color:var(--text-3);margin-top:3px;line-height:1.6}
.f-err{font-size:11px;color:var(--danger);margin-top:3px;line-height:1.5}
.field .err{border-color:var(--danger)!important;box-shadow:0 0 0 1px var(--danger)}
.f-label{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-2);margin-bottom:5px}
.f-label .req{color:var(--danger)}
.f-label .q{margin-left:2px;color:var(--text-3);font-size:11px}

/* ============ 标签输入器 ============ */
.tag-input{display:flex;flex-wrap:wrap;gap:6px;border:1px solid var(--border);border-radius:var(--r-ctl);padding:6px 8px;background:var(--bg-panel);cursor:text}
.tag-input:focus-within{border-color:var(--primary)}
.tag-input .tag{background:rgba(0,212,255,.1);color:var(--primary);border-radius:var(--r-full);padding:2px 8px;font-size:12px;display:flex;align-items:center;gap:5px}
.tag-input .tag .t-x{cursor:pointer;color:var(--text-3);font-size:11px}
.tag-input .tag .t-x:hover{color:var(--danger)}
.tag-input input{flex:1;min-width:90px;border:none;background:transparent;color:var(--text-1);font-size:12px;outline:none}

/* ============ 行编辑器（build_order） ============ */
.row-list{display:flex;flex-direction:column;gap:8px}
.row-item{display:flex;gap:8px;align-items:center;background:var(--bg-panel);border:1px solid var(--border);border-radius:var(--r-ctl);padding:8px}
.row-item .ri-name{width:150px;flex-shrink:0}
.row-item .ri-path{flex:1}
.row-item .ri-goal{width:110px;flex-shrink:0}
.row-item .ri-extra{width:130px;flex-shrink:0}
.row-item .ri-btns{display:flex;gap:4px;flex-shrink:0}
.row-item .ri-btns button{width:26px;height:26px;border:1px solid var(--border);background:transparent;color:var(--text-2);border-radius:var(--r-ctl);cursor:pointer;font-size:12px}
.row-item .ri-btns button:hover{color:var(--primary);border-color:var(--border-strong)}
.row-item .ri-btns button.del:hover{color:var(--danger);border-color:var(--danger)}
.row-add{display:flex;gap:8px;align-items:center}
.row-add button{font-size:12px}

/* ============ YAML 高级模式 ============ */
.yaml-wrap{display:flex;flex-direction:column;gap:8px}
.yaml-wrap textarea{width:100%;min-height:420px;font-family:var(--font-num);font-size:12px;line-height:1.7;background:#08101F;border:1px solid var(--border);border-radius:var(--r-ctl);color:#C9E4FF;padding:12px;resize:vertical;outline:none}
.yaml-wrap textarea:focus{border-color:var(--primary)}
.yaml-tip{font-size:11px;color:var(--success)}
.mode-switch{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--text-2)}
.mode-switch input{margin:0}

/* ============ 流水线画布 ============ */
.pl-canvas{display:flex;gap:var(--sp-4);min-height:420px}
.pl-pool{width:240px;flex-shrink:0;background:var(--bg-panel);border:1px solid var(--border);border-radius:var(--r-panel);padding:var(--sp-3);display:flex;flex-direction:column;gap:8px;max-height:560px}
.pl-pool .pp-title{font-size:12px;color:var(--text-2);font-weight:600}
.pl-pool .pp-search input{width:100%}
.pl-pool .pp-list{display:flex;flex-direction:column;gap:6px;overflow-y:auto;flex:1}
.pl-svc{background:var(--bg-panel-2);border:1px solid var(--border);border-radius:var(--r-ctl);padding:8px 10px;font-size:12px;color:var(--text-1);cursor:grab;display:flex;align-items:center;gap:8px;transition:var(--t-fast)}
.pl-svc:hover{border-color:var(--border-strong)}
.pl-svc.used{opacity:.45;cursor:not-allowed}
.pl-svc.used .usd{font-size:10px;color:var(--text-3);margin-left:auto}
.pl-svc:active{cursor:grabbing}
.pl-stages{flex:1;display:flex;flex-direction:column;gap:var(--sp-3);min-width:0}
.stage-card{background:var(--bg-panel);border:1px solid var(--border);border-radius:var(--r-panel);padding:var(--sp-3)}
.stage-card .sc-head{display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap}
.stage-card .sc-drag{cursor:grab;color:var(--text-3);font-size:14px;user-select:none}
.stage-card .sc-name{width:180px}
.stage-card .sc-parallel{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--text-2);margin-left:auto}
.stage-card .sc-del{width:26px;height:26px;border:1px solid var(--border);background:transparent;color:var(--text-2);border-radius:var(--r-ctl);cursor:pointer}
.stage-card .sc-del:hover{color:var(--danger);border-color:var(--danger)}
.stage-warn{font-size:11px;color:var(--warning);margin-bottom:8px;display:none}
.stage-card.parallel .stage-warn{display:block}
.stage-svc{display:flex;flex-direction:column;gap:6px;min-height:40px;border:1px dashed transparent;border-radius:var(--r-ctl);padding:4px;transition:var(--t-fast)}
.stage-svc.dragover{border-color:var(--primary);background:rgba(0,212,255,.06)}
.stage-svc.empty{border-color:var(--border);display:flex;align-items:center;justify-content:center;color:var(--text-3);font-size:11px}
.svc-block{background:var(--bg-panel-2);border:1px solid var(--border);border-radius:var(--r-ctl);padding:7px 10px;display:flex;align-items:center;gap:8px;font-size:12px;color:var(--text-1);cursor:grab}
.svc-block .sb-name{font-weight:600}
.svc-block .sb-ver{font-size:10px;color:var(--primary);border:1px solid rgba(0,212,255,.35);border-radius:var(--r-full);padding:1px 8px;cursor:pointer}
.svc-block .sb-x{margin-left:auto;cursor:pointer;color:var(--text-3);font-size:12px}
.svc-block .sb-x:hover{color:var(--danger)}
.add-stage{display:flex;align-items:center;justify-content:center;gap:8px;border:1px dashed var(--border-strong);border-radius:var(--r-panel);padding:12px;color:var(--primary);font-size:13px;cursor:pointer;background:transparent;transition:var(--t-fast)}
.add-stage:hover{background:rgba(0,212,255,.05)}
.pl-preview{margin-top:var(--sp-4);background:#08101F;border:1px solid var(--border);border-radius:var(--r-ctl);padding:10px 14px;font-family:var(--font-num);font-size:12px;color:var(--success);word-break:break-all}
.pl-tip{font-size:11px;color:var(--text-3);margin-top:6px}

/* ============ 角色卡片 ============ */
.role-cards{display:grid;grid-template-columns:1fr 1fr;gap:var(--sp-3)}
.role-card{border:1px solid var(--border);border-radius:var(--r-panel);padding:var(--sp-3);cursor:pointer;transition:var(--t-smooth);background:var(--bg-panel)}
.role-card:hover{border-color:var(--border-strong)}
.role-card.sel{border-color:var(--primary);box-shadow:var(--shadow-glow);background:rgba(0,212,255,.05)}
.role-card .rc-title{font-size:13px;font-weight:600;color:var(--text-1);margin-bottom:4px}
.role-card .rc-title .rc-key{font-size:10px;color:var(--primary);font-family:var(--font-num);margin-left:6px}
.role-card .rc-desc{font-size:11px;color:var(--text-2);line-height:1.6}

/* ============ 连接测试面板 ============ */
.test-panel{border:1px solid var(--border);border-radius:var(--r-panel);padding:var(--sp-3);display:flex;flex-direction:column;gap:8px;background:var(--bg-panel)}
.test-panel .tp-row{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--text-2)}
.test-panel .tp-ok{color:var(--success)}
.test-panel .tp-fail{color:var(--danger)}
.test-panel .tp-wait{color:var(--text-3)}
.test-panel .tp-advice{font-size:11px;color:var(--warning);line-height:1.6;border-top:1px dashed var(--border);padding-top:8px}

/* ============ 保存成功引导 ============ */
.guide-steps{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:var(--sp-3) 0;font-size:12px;color:var(--text-2)}
.guide-steps .gs-item{padding:4px 12px;border:1px solid var(--border);border-radius:var(--r-full)}
.guide-steps .gs-item.done{color:var(--success);border-color:rgba(0,227,150,.35)}
.guide-steps .gs-arrow{color:var(--text-3)}
```

- [ ] **Step 2: 验证**

浏览器访问 `http://127.0.0.1:5678/screen#/config` → 旧版配置页样式不受影响（新类未使用，纯追加）。console 无报错。

- [ ] **Step 3: Commit**

```bash
git add webapp/static/css/pages/config.css
git commit -m "style(config): 配置管理新组件样式（统计卡/模板卡/分区/画布/校验态）"
```

---

## Task 3: config-form-helpers.js（共享工具 + 模板常量）

**Files:**
- Write: `webapp/static/js/pages/config/config-form-helpers.js`（完整覆盖占位文件）

- [ ] **Step 1: 写入完整文件**

```js
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
    if (msg){
      el.classList.add('err');
      if (errEl){ errEl.textContent = msg; errEl.style.display = 'block'; }
    }else{
      el.classList.remove('err');
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

  // ---- 标签输入器（回车添加 / ✕ 删除）----
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
  function ipValid(host){
    const m = IP_RE.exec(String(host || '').trim());
    if (!m) return false;
    return m.slice(1).every(seg => Number(seg) >= 0 && Number(seg) <= 255);
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
```

- [ ] **Step 2: 验证**

浏览器访问 `http://127.0.0.1:5678/screen#/config` 后，console 执行：

```js
const h = window.cfgHelpers;
// YAML 双向转换
const y = 'registry:\n  mode: direct\n  namespace: demo\nnacos:\n  url: http://x:8848\n  group: DEFAULT_GROUP\n';
const obj = h.yamlToObj(y);
console.log(obj.registry.mode === 'direct' && obj.nacos.group === 'DEFAULT_GROUP');
console.log(h.yamlToObj(h.objToYaml(obj)).nacos.url === 'http://x:8848');
// 校验
console.log(h.ipValid('10.0.0.1') === true, h.ipValid('256.1.1.1') === false, h.safeName('prod_1') === true);
// 模板
console.log(h.ENV_TEMPLATES.length === 5, h.PIPELINE_TEMPLATES.length === 4);
// 流水线模板归位
const svcs = [{id:1,name:'service-gateway'},{id:2,name:'service-job-b'},{id:3,name:'common-build'},{id:4,name:'service-biz-a'}];
const st = h.applyPipelineTemplate(h.PIPELINE_TEMPLATES[0], svcs);
console.log(st[0].services.length === 1 && st[3].services.length === 1);
```

预期：全部输出 `true`。

- [ ] **Step 3: Commit**

```bash
git add webapp/static/js/pages/config/config-form-helpers.js
git commit -m "feat(config): 共享表单工具（字段/分区/标签输入器/行编辑器/YAML 转换/模板常量）"
```

---

## Task 4: config-home.js（配置工作台首页 + 用户管理视图）

**Files:**
- Write: `webapp/static/js/pages/config/config-home.js`

- [ ] **Step 1: 写入完整文件**

```js
// config-home.js — 配置工作台首页（统计卡片/快速开始/最近项目/空态）+ 用户管理视图（#/config/users）
(function(){
  'use strict';
  const { touchRecent, getRecent } = window.cfgHelpers;

  // 工作台：统计卡片 + 快速开始 + 最近项目 + 空态引导
  async function renderHome(root){
    root.innerHTML = '<div class="cfg-home">' + skeletonHTML(5) + '</div>';
    try{
      const [projects, servers, users] = await Promise.all([
        api('/api/projects'), api('/api/servers'), api('/api/users'),
      ]);
      const total = {
        services: projects.reduce((a, p) => a + (p.services || 0), 0),
        environments: projects.reduce((a, p) => a + (p.environments || 0), 0),
        pipelines: projects.reduce((a, p) => a + (p.pipelines || 0), 0),
      };
      const okCnt = servers.filter(s => s.conn && s.conn.ok).length;
      const errCnt = servers.filter(s => s.conn && !s.conn.ok).length;
      const recent = getRecent().filter(r => projects.some(p => p.id === r.pid));
      const firstPid = projects.length ? projects[0].id : null;
      const goProject = pid => { location.hash = '#/config/p/' + pid; };

      const statCards = [
        { ico:'🗂️', num: projects.length, label:'项目', tip:'点击进入项目仪表盘', go: () => { if (firstPid) goProject(firstPid); } },
        { ico:'🧩', num: total.services, label:'服务', tip:'全部项目服务总数', go: () => { if (firstPid) location.hash = '#/config/p/' + firstPid + '/services'; } },
        { ico:'🌐', num: total.environments, label:'环境', tip:'全部项目环境总数', go: () => { if (firstPid) location.hash = '#/config/p/' + firstPid + '/environments'; } },
        { ico:'🔁', num: total.pipelines, label:'流水线', tip:'全部项目流水线总数', go: () => { if (firstPid) location.hash = '#/config/p/' + firstPid + '/pipelines'; } },
        { ico:'🖥️', num: servers.length, label:'服务器', sub: `SSH 正常 ${okCnt}${errCnt ? ' · 异常 ' + errCnt : ''}`, tip:'平台级资源，环境中选择绑定', go: () => { location.hash = '#/config/servers'; } },
        { ico:'👤', num: users.length, label:'用户', tip:'账号与角色管理', go: () => { location.hash = '#/config/users'; } },
      ];

      // 快速开始跳转：需项目上下文；无项目时引导新建
      const gotoAuto = key => {
        if (!firstPid){
          toastError('请先创建项目，再创建服务/环境/流水线');
          openProjectFormSafe(root);
          return;
        }
        sessionStorage.setItem('cfgAutoOpen', key);
        const subMap = { service:'services', env:'environments', pipeline:'pipelines' };
        location.hash = '#/config/p/' + firstPid + '/' + (subMap[key] || 'services');
      };
      const quickCards = [
        { ico:'🧩', title:'新建服务', desc:'注册代码仓库与构建方式，供流水线编排', time:'约 2 分钟', go: () => gotoAuto('service') },
        { ico:'🌐', title:'新建环境', desc:'模板一键填充，只需改服务器地址等差异项', time:'约 3 分钟', go: () => gotoAuto('env') },
        { ico:'🔁', title:'编排流水线', desc:'拖拽服务到阶段，自动生成发布命令', time:'约 2 分钟', go: () => gotoAuto('pipeline') },
        { ico:'🖥️', title:'添加服务器', desc:'填写 IP 与登录方式，保存时自动测试连接', time:'约 1 分钟', go: () => { location.hash = '#/config/servers'; sessionStorage.setItem('cfgAutoOpen', 'server'); } },
      ];

      root.innerHTML = `<div class="cfg-home">
        <div class="page-title">配置工作台</div>
        <div class="page-sub">可视化运维创作：先建项目，再按「服务器 → 服务 → 环境 → 流水线」顺序配置</div>
        ${projects.length === 0 ? `
        <div class="onboard-card">
          <div style="font-size:15px;font-weight:700;color:var(--text-1)">🚀 还没有项目，推荐按这个顺序开始</div>
          <div class="onboard-steps">
            <span>① 新建项目</span><span class="os-arrow">→</span>
            <span>② 添加服务器</span><span class="os-arrow">→</span>
            <span>③ 新建服务</span><span class="os-arrow">→</span>
            <span>④ 新建环境并绑定服务器</span><span class="os-arrow">→</span>
            <span>⑤ 编排流水线</span>
          </div>
          <button class="btn primary" id="cfgNewProj">＋ 新建项目</button>
        </div>` : ''}
        <div>
          <div class="section-title">平台概览 <span class="tag">共 ${projects.length} 个项目</span></div>
          <div class="stat-grid">${statCards.map(c => `
            <div class="stat-card" data-stat>
              <div class="st-ico">${c.ico}</div>
              <div class="st-num">${c.num}${c.sub ? `<span class="sub">${esc(c.sub)}</span>` : ''}</div>
              <div class="st-label">${esc(c.label)}</div>
              <div class="st-tip">${esc(c.tip || '')}</div>
            </div>`).join('')}
          </div>
        </div>
        <div>
          <div class="section-title">快速开始 <span class="tag">新手从这里下手</span></div>
          <div class="quick-grid">${quickCards.map(c => `
            <div class="quick-card" data-quick>
              <div class="qc-head"><span class="qc-ico">${c.ico}</span><span class="qc-title">${esc(c.title)}</span></div>
              <div class="qc-desc">${esc(c.desc)}</div>
              <div class="qc-time">⏱ ${esc(c.time)}</div>
            </div>`).join('')}
          </div>
        </div>
        ${recent.length ? `
        <div>
          <div class="section-title">最近使用的项目</div>
          <div class="recent-grid">${recent.map(r => `
            <div class="recent-item" data-recent="${r.pid}">
              <span class="ri-ico">🗂️</span>
              <div><div class="ri-name">${esc(r.name)}</div><div class="ri-meta">最近访问 ${fmtTime(Math.floor(r.ts / 1000))}</div></div>
            </div>`).join('')}
          </div>
        </div>` : ''}
      </div>`;

      root.querySelectorAll('[data-stat]').forEach((el, i) => el.onclick = () => statCards[i].go());
      root.querySelectorAll('[data-quick]').forEach((el, i) => el.onclick = () => quickCards[i].go());
      root.querySelectorAll('[data-recent]').forEach(el => {
        el.onclick = () => { const p = projects.find(x => x.id === Number(el.dataset.recent)); if (p){ touchRecent(p.id, p.name); goProject(p.id); } };
      });
      const np = document.getElementById('cfgNewProj');
      if (np) np.onclick = () => openProjectFormSafe(root);
    }catch(e){
      root.innerHTML = errorState('配置工作台加载失败：' + esc(e.message), 'navRetry');
    }
  }
  // 新建项目入口：cfgProject 由 Task 5 模块提供，未就绪时给出明确提示
  function openProjectFormSafe(root){
    if (!window.cfgProject || !window.cfgProject.openProjectForm){
      toastError('项目模块未就绪，请稍后重试');
      return;
    }
    window.cfgProject.openProjectForm(root, null, () => location.hash = '#/config');
  }

  // ---- 用户管理视图（#/config/users） ----
  const ROLE_CN = { admin: '管理员', operator: '操作员', viewer: '只读' };
  async function renderUsers(root){
    root.innerHTML = '<div class="cfg-home">' + skeletonHTML(3) + '</div>';
    try{
      const us = await api('/api/users');
      const me = currentUser().username;
      root.innerHTML = `<div class="cfg-home">
        <div style="display:flex;align-items:center;gap:12px">
          <div class="page-title">用户管理</div>
          <button class="btn sm ghost" onclick="location.hash='#/config'">← 返回工作台</button>
        </div>
        <div class="deploy-toolbar"><span style="flex:1"></span>
          <button class="btn sm primary" id="addUser">+ 新建用户</button>
        </div>
        <div class="panel" style="padding:0">${us.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr>
          <th>用户名</th><th>角色</th><th>启用</th><th>强制改密</th><th>操作</th></tr></thead><tbody>
          ${us.map(u => `<tr>
            <td>${esc(u.username)}${u.username === me ? ' <span class="weak">(我)</span>' : ''}</td>
            <td>${ROLE_CN[u.role] || esc(u.role)}</td>
            <td>${u.enabled ? '<span class="ok">启用</span>' : '<span class="weak">停用</span>'}</td>
            <td>${u.must_change_password ? '<span class="badge warning">是</span>' : '-'}</td>
            <td><button class="btn sm ghost" data-edit="${u.id}">编辑</button>
                <button class="btn sm danger" data-del="${u.id}">删除</button></td>
          </tr>`).join('')}</tbody></table></div>`
        : `<div class="empty-state"><div class="e-ico">👤</div><div>暂无用户</div></div>`}</div>
      </div>`;
      document.getElementById('addUser').onclick = () => openUserForm(root, null);
      root.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => openUserForm(root, us.find(x => x.id === Number(b.dataset.edit))));
      root.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
        const u = us.find(x => x.id === Number(b.dataset.del));
        if (!await confirmDialog(`确认删除用户「${u.username}」？`)) return;
        try{ await api('/api/users/' + u.id, { method: 'DELETE' }); toastSuccess('用户已删除'); await renderUsers(root); }
        catch(err){ toastError(err.message); }
      });
    }catch(e){
      root.innerHTML = errorState('用户列表加载失败：' + esc(e.message), 'navRetry');
    }
  }
  function openUserForm(root, u){
    openDrawer({ title: u ? '编辑用户：' + u.username : '新建用户', body: `
      <div class="form-grid">
        <div class="field"><label>用户名 *</label><input id="uName" value="${esc(u ? u.username : '')}" ${u ? 'disabled' : ''}></div>
        <div class="field"><label>${u ? '重置密码（留空不修改）' : '初始密码 *（至少 6 位）'}</label><input id="uPwd" type="password"></div>
        <div class="field"><label>角色</label><select id="uRole">
          ${Object.entries(ROLE_CN).map(([k, v]) => `<option value="${k}" ${(u && u.role === k) || (!u && k === 'viewer') ? 'selected' : ''}>${v}</option>`).join('')}
        </select></div>
        <div class="field check" style="align-self:end"><label><input type="checkbox" id="uEnabled" ${!u || u.enabled ? 'checked' : ''}> 启用</label></div>
      </div><div style="display:flex;gap:10px;margin-top:20px"><button class="btn primary flex-1" id="cfSubmit">${u ? '保存' : '创建'}</button></div>` });
    document.getElementById('cfSubmit').onclick = async () => {
      try{
        const name = val('uName').trim();
        const pwd = val('uPwd');
        if (!u){
          if (!name || pwd.length < 6) throw new Error('用户名必填，密码至少 6 位');
          await api('/api/users', { method: 'POST', body: JSON.stringify({ username: name, password: pwd, role: val('uRole') }) });
          toastSuccess('用户已创建（首登强制改密）');
        }else{
          const payload = { role: val('uRole'), enabled: chk('uEnabled') };
          if (pwd) payload.password = pwd;
          await api('/api/users/' + u.id, { method: 'PUT', body: JSON.stringify(payload) });
          toastSuccess('用户已更新');
        }
        const dr = document.getElementById('drawerRoot');
        dr.classList.add('hidden'); dr.innerHTML = '';
        await renderUsers(root);
      }catch(err){ toastError(err.message); }
    };
  }
  // 局部工具（与旧 config.js 同款，避免依赖被删文件）
  function val(id){ return document.getElementById(id).value; }
  function chk(id){ return document.getElementById(id).checked; }

  window.cfgHome = {
    render: async function(root, view){
      if (view === 'users'){ await renderUsers(root); return []; }
      await renderHome(root);
      return [];
    },
  };
})();
```

- [ ] **Step 2: 验证**

浏览器访问 `http://127.0.0.1:5678/screen#/config`（此时入口未切换，仍显示旧版），console 执行：

```js
const root = document.getElementById('pageRoot');
window.cfgHome.render(root, 'home');
```

预期：root 显示配置工作台（统计卡片数字与 DB 一致、快速开始 4 卡、最近项目区）。再执行 `window.cfgHome.render(root, 'users')` → 用户表格正常。console 无报错。

- [ ] **Step 3: Commit**

```bash
git add webapp/static/js/pages/config/config-home.js
git commit -m "feat(config): 配置工作台首页（统计/快速开始/最近项目/空态）+ 用户管理视图"
```

---

## Task 5: config-project.js（项目仪表盘 + 子页框架 + 各列表）

**Files:**
- Write: `webapp/static/js/pages/config/config-project.js`

职责：`#/config/p/<pid>` 项目仪表盘 + `#/config/p/<pid>/<sub>` 子页（overview/services/environments/pipelines/servers/yaml），项目 CRUD，各列表渲染。表单打开委托给各表单模块。

- [ ] **Step 1: 写入完整文件**

```js
// config-project.js — 项目仪表盘 + 子页框架 + 各列表 + 项目 CRUD + YAML 页
(function(){
  'use strict';
  const { touchRecent, getRecent } = window.cfgHelpers;
  const SUB_TABS = [
    ['overview','概览'],['services','服务'],['environments','环境'],['pipelines','流水线'],['servers','服务器'],['yaml','YAML 配置'],
  ];

  async function render(root, pid, sub){
    try{
      pid = Number(pid);
      if (!Number.isInteger(pid) || pid <= 0) throw new Error('项目 ID 无效');
      const projects = await api('/api/projects');
      const project = projects.find(p => p.id === pid);
      if (!project) throw new Error('项目不存在或已被删除');
      touchRecent(pid, project.name);
      if (!SUB_TABS.some(([k]) => k === sub)) sub = 'overview';

      // 服务器与绑定信息（供上下文条徽章与服务器子页）
      const [servers, envs] = await Promise.all([
        api('/api/servers'),
        api(`/api/projects/${pid}/environments`),
      ]);
      const bindings = await Promise.all(envs.map(async e => {
        try{ return { env: e, list: await api(`/api/environments/${e.id}/servers`) }; }
        catch(err){ return { env: e, list: [], failed: true }; }   // 请求失败不参与未绑定统计，避免删除后竞态误报
      }));
      const okCnt = servers.filter(s => s.conn && s.conn.ok).length;
      const errCnt = servers.filter(s => s.conn && !s.conn.ok).length;
      const unbindEnvs = bindings.filter(b => !b.failed && b.list.length === 0);

      const issues = [];
      if (servers.length && unbindEnvs.length) issues.push(`${unbindEnvs.length} 个环境未绑定服务器`);
      if (errCnt) issues.push(`${errCnt} 台服务器 SSH 异常`);

      root.innerHTML = `
        <div class="proj-bar">
          <div class="pb-ico">🗂️</div>
          <div>
            <div class="pb-name">${esc(project.name)} <span class="pb-code">${esc(project.code)}</span></div>
            <div class="pb-desc">${esc(project.description || '暂无描述')} · Git 组织：${esc(project.git_org || '-')}</div>
          </div>
          <div class="pb-badges">
            <span class="badge-health ${errCnt ? 'bad' : ''}">🖥️ SSH ${okCnt}/${servers.length} 正常</span>
            ${issues.map(t => `<span class="badge-issue">⚠ ${esc(t)}</span>`).join('')}
            <button class="btn sm ghost" data-editp>编辑项目</button>
          </div>
        </div>
        <div class="proj-tabs">${SUB_TABS.map(([k, v]) =>
          `<span class="pt ${k === sub ? 'on' : ''}" data-sub="${k}">${v}</span>`).join('')}
        </div>
        <div id="subBody">${skeletonHTML(4)}</div>`;

      root.querySelectorAll('[data-sub]').forEach(t => t.onclick = () => { location.hash = `#/config/p/${pid}/${t.dataset.sub}`; });
      root.querySelector('[data-editp]').onclick = () => openProjectForm(root, project);

      const sb = document.getElementById('subBody');
      const route = { services: renderServices, environments: renderEnvs, pipelines: renderPipelines, servers: renderServers, yaml: renderYaml, overview: renderOverview };
      await route[sub](sb, project);
      // 快速开始/服务器保存后自动打开新建表单
      const auto = sessionStorage.getItem('cfgAutoOpen');
      if (auto){
        sessionStorage.removeItem('cfgAutoOpen');
        if (auto === 'service' && sub === 'services') cfgServiceForm.open(sb, project, null, () => renderServices(sb, project));
        else if (auto === 'env' && sub === 'environments') cfgEnvForm.open(sb, project, null);
        else if (auto === 'pipeline' && sub === 'pipelines') cfgPipelineEditor.open(sb, project, null);
        else if (auto === 'server' && sub === 'servers') cfgServerForm.open(sb, null, () => renderServers(sb));
      }
    }catch(e){
      root.innerHTML = errorState('项目加载失败：' + esc(e.message), 'navRetry');
    }
  }

  // ---- 概览：最近发布记录 ----
  async function renderOverview(sb, project){
    try{
      const tasks = await api(`/api/tasks?project_id=${project.id}&limit=8`);
      sb.innerHTML = `
        <div class="overview-grid">
          <div class="ov-card" data-go="services"><div class="oc-num">${project.services || 0}</div><div class="oc-label">服务</div><div class="oc-desc">代码仓库与构建方式，供流水线编排</div><div class="oc-go">管理 →</div></div>
          <div class="ov-card" data-go="environments"><div class="oc-num">${project.environments || 0}</div><div class="oc-label">环境</div><div class="oc-desc">模板化配置 + 绑定服务器</div><div class="oc-go">管理 →</div></div>
          <div class="ov-card" data-go="pipelines"><div class="oc-num">${project.pipelines || 0}</div><div class="oc-label">流水线</div><div class="oc-desc">拖拽画布编排发布顺序</div><div class="oc-go">管理 →</div></div>
          <div class="ov-card" data-go="servers"><div class="oc-num">${(await api('/api/servers')).length}</div><div class="oc-label">服务器</div><div class="oc-desc">平台级资源，环境中选择绑定</div><div class="oc-go">管理 →</div></div>
        </div>
        <div class="panel" style="margin-top:var(--sp-4)">
          <div class="panel-title">最近发布记录</div>
          ${tasks.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>类型</th><th>名称</th><th>环境</th><th>版本</th><th>状态</th><th>时间</th></tr></thead><tbody>
          ${tasks.map(t => `<tr>
            <td>${t.type === 'pipeline' ? '流水线' : '服务'}</td>
            <td>${esc(t.name)}</td><td>${esc(t.env || '-')}</td><td class="num">${esc(t.version || '-')}</td>
            <td>${statusBadge(t.status)}</td><td class="num">${fmtTime(t.started_at)}</td>
          </tr>`).join('')}</tbody></table></div>`
          : `<div class="empty-state"><div class="e-ico">📋</div><div>暂无发布记录，去发布中心发起发布</div></div>`}
        </div>`;
      sb.querySelectorAll('[data-go]').forEach(c => c.onclick = () => { location.hash = `#/config/p/${project.id}/${c.dataset.go}`; });
    }catch(e){
      sb.innerHTML = errorState('概览加载失败：' + esc(e.message), 'navRetry');
    }
  }

  // ---- 服务列表 ----
  async function renderServices(sb, project){
    try{
      const svcs = await api(`/api/projects/${project.id}/services`);
      sb.innerHTML = `<div class="deploy-toolbar">
          <span class="weak" style="font-size:12px">服务（${svcs.length}）</span><span style="flex:1"></span>
          <button class="btn sm primary" id="addSvc">+ 新建服务</button>
        </div>
        <div class="panel" style="padding:0">${svcs.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr>
          <th>名称</th><th>镜像</th><th>服务名</th><th>端口</th><th>启用</th><th>操作</th></tr></thead><tbody>
          ${svcs.map(s => `<tr>
            <td>${esc(s.name)}</td><td class="num">${esc(s.image_name)}</td><td class="num">${esc(s.service_name)}</td>
            <td class="num">${s.health_port ?? '-'}</td>
            <td>${s.enabled ? '<span class="ok">启用</span>' : '<span class="weak">停用</span>'}</td>
            <td><button class="btn sm ghost" data-edit="${s.id}">编辑</button>
                <button class="btn sm danger" data-del="${s.id}">删除</button></td>
          </tr>`).join('')}</tbody></table></div>`
        : `<div class="empty-state"><div class="e-ico">🧩</div><div>暂无服务，点右上角新建</div></div>`}</div>`;
      document.getElementById('addSvc').onclick = () => cfgServiceForm.open(sb, project, null, () => renderServices(sb, project));
      sb.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => cfgServiceForm.open(sb, project, svcs.find(s => s.id === Number(b.dataset.edit)), () => renderServices(sb, project)));
      sb.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
        const s = svcs.find(x => x.id === Number(b.dataset.del));
        if (!await confirmDialog(`确认删除服务「${s.name}」？`)) return;
        try{ await api(`/api/projects/${project.id}/services/${s.id}`, { method: 'DELETE' }); toastSuccess('服务已删除'); await renderServices(sb, project); }
        catch(e){ toastError(e.message); }  // 被流水线引用时后端 400 文案直接展示
      });
    }catch(e){ sb.innerHTML = errorState('服务列表加载失败：' + esc(e.message), 'navRetry'); }
  }

  // ---- 环境列表（含绑定服务器入口，填补现有盲区） ----
  async function renderEnvs(sb, project){
    try{
      const envs = await api(`/api/projects/${project.id}/environments`);
      const binds = await Promise.all(envs.map(async e => {
        try{ return { env: e, list: await api(`/api/environments/${e.id}/servers`) }; }
        catch(err){ return { env: e, list: [], failed: true }; }   // 请求失败不参与未绑定统计，避免删除后竞态误报
      }));
      sb.innerHTML = `<div class="deploy-toolbar">
          <span class="weak" style="font-size:12px">环境（${envs.length}）</span><span style="flex:1"></span>
          <button class="btn sm primary" id="addEnv">+ 新建环境</button>
        </div>
        <div class="panel" style="padding:0">${envs.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr>
          <th>名称</th><th>默认</th><th>绑定服务器</th><th>Compose 路径</th><th>Maven Profile</th><th>操作</th></tr></thead><tbody>
          ${binds.map(({env: e, list, failed}) => `<tr>
            <td>${esc(e.name)}</td>
            <td>${e.is_default ? '<span class="badge success">默认</span>' : '-'}</td>
            <td class="num">${list.length ? `${list.length} 台 <span class="weak">(${esc(list.map(s => s.role === 'first' ? '首台' : '跟随').join('/') || '未设角色')})</span>` : (failed ? '<span class="weak">加载失败</span>' : '<span class="err">未绑定</span>')}</td>
            <td class="num">${esc(e.docker_compose_path || '-')}</td>
            <td class="num">${esc(e.maven_profile || '-')}</td>
            <td><button class="btn sm ghost" data-bind="${e.id}">绑定服务器</button>
                <button class="btn sm ghost" data-edit="${e.id}">编辑</button>
                <button class="btn sm danger" data-del="${e.id}">删除</button></td>
          </tr>`).join('')}</tbody></table></div>`
        : `<div class="empty-state"><div class="e-ico">🌐</div><div>暂无环境，点右上角新建（有模板一键填充）</div></div>`}</div>`;
      document.getElementById('addEnv').onclick = () => cfgEnvForm.open(sb, project, null);
      sb.querySelectorAll('[data-bind]').forEach(b => b.onclick = () => cfgEnvForm.open(sb, project, envs.find(x => x.id === Number(b.dataset.bind)), { step: 'bind' }));
      sb.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => cfgEnvForm.open(sb, project, envs.find(x => x.id === Number(b.dataset.edit))));
      sb.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
        const e = envs.find(x => x.id === Number(b.dataset.del));
        if (!await confirmDialog(`确认删除环境「${e.name}」？`)) return;
        try{ await api(`/api/projects/${project.id}/environments/${e.id}`, { method: 'DELETE' }); toastSuccess('环境已删除'); await renderEnvs(sb, project); }
        catch(err){ toastError(err.message); }
      });
    }catch(e){ sb.innerHTML = errorState('环境列表加载失败：' + esc(e.message), 'navRetry'); }
  }

  // ---- 流水线列表 ----
  async function renderPipelines(sb, project){
    try{
      const pls = await api(`/api/projects/${project.id}/pipelines`);
      sb.innerHTML = `<div class="deploy-toolbar">
          <span class="weak" style="font-size:12px">流水线（${pls.length}）</span><span style="flex:1"></span>
          <button class="btn sm primary" id="addPl">+ 新建流水线</button>
        </div>
        <div class="panel" style="padding:0">${pls.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr>
          <th>名称</th><th>先构建</th><th>阶段数</th><th>操作</th></tr></thead><tbody>
          ${pls.map(pl => `<tr>
            <td>${esc(pl.name)}</td>
            <td>${pl.build_first ? '<span class="badge warning">构建优先</span>' : '否'}</td>
            <td class="num">${(pl.stages || []).length}</td>
            <td><button class="btn sm ghost" data-edit="${pl.id}">编辑</button>
                <button class="btn sm danger" data-del="${pl.id}">删除</button></td>
          </tr>`).join('')}</tbody></table></div>`
        : `<div class="empty-state"><div class="e-ico">🔁</div><div>暂无流水线，点右上角用画布编排</div></div>`}</div>`;
      document.getElementById('addPl').onclick = () => cfgPipelineEditor.open(sb, project, null);
      sb.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => cfgPipelineEditor.open(sb, project, pls.find(x => x.id === Number(b.dataset.edit))));
      sb.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
        const pl = pls.find(x => x.id === Number(b.dataset.del));
        if (!await confirmDialog(`确认删除流水线「${pl.name}」？`)) return;
        try{ await api(`/api/projects/${project.id}/pipelines/${pl.id}`, { method: 'DELETE' }); toastSuccess('流水线已删除'); await renderPipelines(sb, project); }
        catch(err){ toastError(err.message); }
      });
    }catch(e){ sb.innerHTML = errorState('流水线列表加载失败：' + esc(e.message), 'navRetry'); }
  }

  // ---- 服务器列表（平台级资源；说明条 + 新建/编辑/删除） ----
  async function renderServers(sb){
    try{
      const svs = await api('/api/servers');
      const connBadge = s => {
        const c = s.conn;
        if (!c) return '<span class="weak">未知</span>';
        const tip = c.ok ? fmtTime(c.ts) : (c.error || '连接失败');
        return c.ok
          ? `<span class="ok" title="最后探测：${esc(tip)}">正常</span>`
          : `<span class="err" title="${esc(tip)}">异常</span>`;
      };
      sb.innerHTML = `<div class="deploy-toolbar">
          <span class="weak" style="font-size:12px">服务器（${svs.length}）· 平台级资源，在环境表单中选择绑定</span><span style="flex:1"></span>
          <button class="btn sm primary" id="addSrv">+ 新建服务器</button>
        </div>
        <div class="panel" style="padding:0">${svs.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr>
          <th>主机</th><th>端口</th><th>用户</th><th>角色</th><th>Nacos IP</th><th>连接状态</th><th>备注</th><th>启用</th><th>操作</th></tr></thead><tbody>
          ${svs.map(s => `<tr>
            <td class="num">${esc(s.host)}</td>
            <td class="num">${s.ssh_port ?? '-'}</td>
            <td class="num">${esc(s.ssh_user || '-')}</td>
            <td>${s.role === 'first' ? '<span class="badge success">first 首台</span>' : (s.role === 'follower' ? '<span class="badge">follower</span>' : esc(s.role || '-'))}</td>
            <td class="num">${esc(s.nacos_ip || '-')}</td>
            <td>${connBadge(s)}</td>
            <td class="muted">${esc(s.remark || '-')}</td>
            <td>${s.enabled ? '<span class="ok">启用</span>' : '<span class="weak">停用</span>'}</td>
            <td><button class="btn sm ghost" data-edit="${s.id}">编辑</button>
                <button class="btn sm danger" data-del="${s.id}">删除</button></td>
          </tr>`).join('')}</tbody></table></div>`
        : `<div class="empty-state"><div class="e-ico">🖥️</div><div>暂无服务器，点右上角新建（保存时自动测试连接）</div></div>`}</div>`;
      document.getElementById('addSrv').onclick = () => cfgServerForm.open(sb, null);
      sb.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => cfgServerForm.open(sb, svs.find(x => x.id === Number(b.dataset.edit))));
      sb.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
        const s = svs.find(x => x.id === Number(b.dataset.del));
        if (!await confirmDialog(`确认删除服务器「${s.host}」？`)) return;
        try{ await api('/api/servers/' + s.id, { method: 'DELETE' }); toastSuccess('服务器已删除'); await renderServers(sb); }
        catch(err){ toastError(err.message); }
      });
    }catch(e){ sb.innerHTML = errorState('服务器列表加载失败：' + esc(e.message), 'navRetry'); }
  }

  // ---- YAML 导入导出（沿用旧逻辑） ----
  function renderYaml(sb){
    sb.innerHTML = `<div class="panel">
        <div class="panel-title">YAML 导入 / 导出</div>
        <p class="muted" style="margin-bottom:16px;font-size:13px">
          导出：将 DB 中全部配置写入 <code>data/export/config</code>（按项目/环境拆分 YAML）。<br>
          导入：从 <code>config/</code> 目录（deploy.yaml/pipelines.yaml/projects.yaml/servers.yaml）全量重建 DB。
        </p>
        <div style="display:flex;gap:10px">
          <button class="btn primary" id="ioExport">⬇ 导出配置</button>
          <button class="btn danger" id="ioImport">⬆ 导入配置（覆盖 DB）</button>
        </div>
        <div class="io-results" id="ioResults"></div>
      </div>`;
    document.getElementById('ioExport').onclick = async () => {
      try{
        const r = await api('/api/config/export');
        document.getElementById('ioResults').innerHTML =
          `<div class="io-item"><span class="io-ok">✓</span><span>已导出 ${r.files.length} 个文件 → ${esc(r.dir)}</span></div>`;
        toastSuccess('配置已导出');
      }catch(err){ toastError(err.message); }
    };
    document.getElementById('ioImport').onclick = async () => {
      if (!await confirmDialog('导入将覆盖现有 DB 配置，确认继续？')) return;
      try{
        const r = await api('/api/config/import', { method: 'POST' });
        const items = Object.entries(r.results || {});
        document.getElementById('ioResults').innerHTML = items.length ? items.map(([k, v]) =>
          `<div class="io-item"><span class="${v.ok ? 'io-ok' : 'io-fail'}">${v.ok ? '✓' : '✗'}</span>
           <span>${esc(k)}：${esc(v.message || '')}</span></div>`).join('') : '<div class="io-item">无变更</div>';
        items.some(([, v]) => !v.ok) ? toastError('导入完成，存在失败项（见明细）') : toastSuccess('配置已导入');
      }catch(err){ toastError(err.message); }
    };
  }

  // ---- 项目 CRUD ----
  function openProjectForm(container, p, onSaved){
    openDrawer({ title: p ? '编辑项目' : '新建项目', body: `
      <div class="form-grid">
        <div class="field"><label>名称 *</label><input id="pName" value="${esc(p ? p.name : '')}"></div>
        <div class="field"><label>编码 *（字母数字 _ -，唯一，创建后不可改）</label><input id="pCode" value="${esc(p ? p.code : '')}" ${p ? 'disabled' : ''}></div>
        <div class="field full"><label>描述</label><input id="pDesc" value="${esc(p ? p.description : '')}"></div>
        <div class="field full"><label>Git 组织</label><input id="pGit" value="${esc(p ? p.git_org : '')}" placeholder="代码仓库所属组织/命名空间，发布打 tag 时使用"></div>
      </div><div style="display:flex;gap:10px;margin-top:20px"><button class="btn primary flex-1" id="cfSubmit">${p ? '保存' : '创建'}</button></div>` });
    document.getElementById('cfSubmit').onclick = async () => {
      try{
        const payload = { name: val('pName').trim(), description: val('pDesc').trim(), git_org: val('pGit').trim() };
        if (!p) payload.code = val('pCode').trim();
        if (!payload.name || (!p && !payload.code)) throw new Error('名称与编码必填');
        await api(p ? `/api/projects/${p.id}` : '/api/projects', { method: p ? 'PUT' : 'POST', body: JSON.stringify(payload) });
        toastSuccess(p ? '项目已更新' : '项目已创建');
        const dr = document.getElementById('drawerRoot');
        dr.classList.add('hidden'); dr.innerHTML = '';
        if (onSaved) onSaved();
        else location.hash = '#/config';
      }catch(e){ toastError(e.message); }
    };
  }
  async function delProject(project){
    if (!await confirmDialog(`确认删除项目「${project.name}」？将级联删除其服务/环境/流水线。`)) return;
    try{
      await api('/api/projects/' + project.id, { method: 'DELETE' });
      toastSuccess('项目已删除');
      location.hash = '#/config';
    }catch(e){ toastError(e.message); }
  }
  function val(id){ return document.getElementById(id).value; }

  window.cfgProject = {
    render: async function(root, pid, sub){ await render(root, pid, sub); return []; },
    openProjectForm,
    delProject,
  };
})();
```

- [ ] **Step 2: 验证**

console 执行（旧入口仍生效，手动验证新模块）：

```js
const root = document.getElementById('pageRoot');
window.cfgProject.render(root, 1, 'overview');
```

预期：项目上下文条（名称/描述/SSH 徽章/问题徽章）、四概览卡、最近发布表。再执行 `window.cfgProject.render(root, 1, 'environments')` → 环境列表含"绑定服务器"列与按钮。console 无报错。

- [ ] **Step 3: Commit**

```bash
git add webapp/static/js/pages/config/config-project.js
git commit -m "feat(config): 项目仪表盘与子页框架（概览/服务/环境/流水线/服务器/YAML）+ 绑定入口"
```

---

## Task 6: config-env-form.js（环境表单：模板 → 分组 → 高级模式 → 绑定服务器）

**Files:**
- Write: `webapp/static/js/pages/config/config-env-form.js`

要点：
- 新建四步（选模板→填写→绑定→保存），编辑直接从步骤 2 开始（opts.step='bind' 时从步骤 3 开始）
- 表单与 YAML 共享同一份 `st.data`，切换双向转换，不丢内容
- 保存后统一 `location.hash = '#/config/p/<pid>/environments'` 触发列表刷新（依赖 Task 1 的 subChanged 机制）
- **密码既有缺陷规避**：环境 ssh 为 JSON 整列存储，GET 已脱敏（`***`），前端拿不到原密码。编辑时若密码留空且 SSH 分区其余字段未改动 → 整个 ssh 不提交（后端保留原值）；若其余字段有改动 → 提交并 toast 明确提示"原密码将被清除，将改用密钥或环境变量 SSH_PASSWORD"。

- [ ] **Step 1: 写入完整文件**

```js
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
            const box = document.getElementById('copySel');
            box.style.display = 'block';
            box.innerHTML = `<div class="field full"><label>选择要复制的源环境</label>
              <select id="envCopySrc">${st.envs.map(e => `<option value="${e.id}">${esc(e.name)}</option>`).join('') || '<option value="">（暂无可复制环境）</option>'}</select>
              <div class="f-hint">选中后自动带入该环境的全部 6 大块配置，改名字和差异项即可</div></div>`;
            const sel = document.getElementById('envCopySrc');
            if (sel && sel.value) loadCopy(Number(sel.value));
            sel && (sel.onchange = () => loadCopy(Number(sel.value)));
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
      const basic = `<div class="form-grid">
        <div class="field"><label>环境名称 *（字母数字 _ -，保存后不可改）</label><input id="envName" value="${esc(st.data.name)}" ${env ? 'disabled' : ''} placeholder="如 prod / test / dev"></div>
        <div class="field"><label>Maven Profile</label><select id="envProfile">
          <option value="">留空自动推导</option>
          ${['prod','test','dev','custom'].map(p => `<option value="${p}" ${st.data.maven_profile === p ? 'selected' : ''}>${p}</option>`).join('')}
        </select>
        <div class="field check" style="align-self:end"><label><input type="checkbox" id="envDef" ${st.data.is_default ? 'checked' : ''}> 默认环境</label></div>
        <div class="field check" style="align-self:end"><label><input type="checkbox" id="envMon" ${st.data.monitor_enabled ? 'checked' : ''}> 启用监控</label></div>
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
        <div class="f-hint" style="margin-bottom:10px">勾选 = 绑定到本环境；顺序即发布顺序（first 首台灰度 → follower 按序跟随）。角色在服务器列表中维护，这里仅选择与排序。</div>
        <div class="cfg-section"><div class="cs-head"><span class="cs-ico">📌</span><span class="cs-title">已绑定（${bound.length} 台）</span></div><div class="cs-body" id="boundList">
          ${bound.length ? bound.map((s, i) => boundRow(s, i)).join('') : '<div class="weak" style="font-size:12px">尚未绑定任何服务器，从下方勾选添加</div>'}
        </div></div>
        <div class="cfg-section"><div class="cs-head"><span class="cs-ico">🖥️</span><span class="cs-title">平台服务器（${unbound.length} 台未绑定）</span></div><div class="cs-body">
          ${unbound.length ? `<div class="form-grid">${unbound.map(s => `
            <label class="field check" style="grid-column:1/-1;margin:0"><input type="checkbox" class="bindChk" value="${s.id}">
              ${esc(s.host)} <span class="weak">${esc(s.role === 'first' ? 'first 首台' : (s.role === 'follower' ? 'follower' : ''))}</span>
              ${s.conn ? (s.conn.ok ? '<span class="ok">SSH 正常</span>' : '<span class="err">SSH 异常</span>') : '<span class="weak">SSH 未知</span>'}
              ${s.enabled ? '' : '<span class="weak">（停用）</span>'}</label>`).join('')}</div>`
          : '<div class="weak" style="font-size:12px">平台暂无其它服务器；可到服务器页先添加</div>'}
        </div></div>
        <div class="f-hint">同一环境建议至少 1 台 first（首台灰度观察无异常后再发布其余）。</div>`;
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
        <span class="weak" style="font-size:11px">${s.role === 'first' ? 'first 首台' : (s.role === 'follower' ? 'follower 跟随' : '未设角色')}</span>
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
```

- [ ] **Step 2: 验证**

console 执行：

```js
const root = document.getElementById('pageRoot');
window.cfgProject.render(root, 1, 'environments');
```

列表渲染后执行 `window.cfgEnvForm.open(document.getElementById('subBody'), {id:1,name:'p1'}, null)`（project 对象可从 `window.cfgProject` 传入真实项目对象；建议直接点页面"新建环境"按钮验证）：

预期：
1. 新建 → 模板 5 卡 → 点"标准 Java 微服务环境" → 分组表单（6 分区 + 基本信息），字段带说明
2. 填 name（如 `test-new`）→ 高级模式开关 → YAML 出现对应内容 → 切回表单内容不丢
3. 下一步 → 勾选 1 台服务器 → 保存 → toast 成功 → 列表刷新出现新环境且绑定数正确
4. 编辑该环境 → 表单回显一致 → 绑定步骤调整顺序/解绑 → 保存 → 列表正确
5. 删除该环境（清理测试数据）

console 无报错。

- [ ] **Step 3: Commit**

```bash
git add webapp/static/js/pages/config/config-env-form.js
git commit -m "feat(config): 环境表单（模板/分组/高级模式 YAML/绑定服务器）"
```

---

## Task 7: config-pipeline-editor.js（流水线画布：服务池拖拽 + 阶段流编排 + 模板 + 实时命令预览）

**Files:**
- Write: `webapp/static/js/pages/config/config-pipeline-editor.js`（完整覆盖占位文件）

要点：
- 数据模型：`stages[].name / parallel / services[].service_id / version / previous_version`（对齐后端 `_check_pipeline_stages`）
- 拖拽用 HTML5 DnD：服务池项（`svc:<id>`）→ 阶段容器；服务块（`block:<i>:<j>`）→ 拖内排序/跨阶段移动；阶段卡（`stage:<i>`，拖 ⠿ 圆点）→ 整卡排序
- 保存后统一 `navRetry()` 刷新列表；阶段名/并行开关在保存时从 DOM 收集

- [ ] **Step 1: 写入完整文件**

```js
// config-pipeline-editor.js — 流水线画布编辑器：服务池拖拽 + 阶段流编排 + 模板 + 实时发布命令预览
(function(){
  'use strict';
  const H = window.cfgHelpers;

  function open(container, project, pl, onSaved){
    const st = {
      project, container, onSaved,
      name: pl ? pl.name : '',
      buildFirst: pl ? !!pl.build_first : true,
      stages: Array.isArray(pl && pl.stages) ? JSON.parse(JSON.stringify(pl.stages)) : [],
      services: [],
      query: '',
      verEdit: null,   // {i, j} 正在编辑版本的服务块坐标
    };
    openDrawer({ title: pl ? '编辑流水线：' + pl.name : '新建流水线', wide: true,
      body: '<div class="cfg-home">' + skeletonHTML(3) + '</div>', onClose: null });
    const bodyEl = document.getElementById('dwBody');
    (async () => {
      try{
        st.services = await api(`/api/projects/${project.id}/services`);
        render();
      }catch(e){ bodyEl.innerHTML = errorState('加载失败：' + esc(e.message), 'navRetry'); }
    })();

    const usedSet = () => {
      const s = new Set();
      st.stages.forEach(sg => (sg.services || []).forEach(x => s.add(x.service_id)));
      return s;
    };
    const svcName = id => { const x = st.services.find(s => s.id === id); return x ? x.name : ('#' + id); };

    function render(){
      const used = usedSet();
      const q = st.query.trim().toLowerCase();
      const pool = st.services.filter(s => !q || s.name.toLowerCase().includes(q));
      bodyEl.innerHTML = `
        <div style="display:flex;align-items:flex-end;gap:10px;flex-wrap:wrap;margin-bottom:14px">
          <div class="field" style="flex:1;min-width:200px;margin:0"><label>流水线名称 *</label><input id="plName" value="${esc(st.name)}" placeholder="如 full-release"></div>
          <label class="mode-switch" style="margin-bottom:12px"><input type="checkbox" id="plBuildFirst" ${st.buildFirst ? 'checked' : ''}> 构建优先（先统一构建全部镜像再发布）</label>
          <div style="margin-bottom:12px"><select id="plTpl" style="width:auto">
            <option value="">＋ 套用模板…</option>
            ${H.PIPELINE_TEMPLATES.map(t => `<option value="${t.id}">${esc(t.name)}：${esc(t.desc)}</option>`).join('')}
          </select></div>
        </div>
        <div class="pl-canvas">
          <div class="pl-pool">
            <div class="pp-title">服务池 <span class="weak" style="font-weight:400">（拖到下方阶段）</span></div>
            <div class="pp-search"><input id="plSearch" placeholder="搜索服务…" value="${esc(st.query)}"></div>
            <div class="pp-list">
              ${pool.length ? pool.map(s => `
                <div class="pl-svc ${used.has(s.id) ? 'used' : ''}" draggable="${!used.has(s.id)}" data-svc="${s.id}">
                  <span>⚙️</span><span>${esc(s.name)}</span>${used.has(s.id) ? '<span class="usd">已用</span>' : ''}
                </div>`).join('') : (st.services.length ? '<div class="weak" style="font-size:11px;padding:6px">无匹配服务</div>' : '<div class="weak" style="font-size:11px;padding:6px">暂无服务，请先到服务页创建</div>')}}
            </div>
          </div>
          <div class="pl-stages" id="plStages">
            ${st.stages.map((sg, i) => stageHTML(sg, i)).join('')}
            <div class="add-stage" id="plAddStage">＋ 添加阶段</div>
          </div>
        </div>
        <div class="pl-preview" id="plPreview"></div>
        <div class="pl-tip">阶段按从上到下顺序执行，某阶段失败则停止后续阶段；并行阶段内的服务会同时发布，请确保目标服务器不冲突。</div>
        <div style="display:flex;gap:10px;margin-top:14px"><button class="btn primary flex-1" id="plSave">✓ 保存流水线</button></div>`;
      bind();
      updatePreview();
    }

    function stageHTML(sg, i){
      const svcs = (sg.services || []).map((x, j) => {
        const ed = st.verEdit && st.verEdit.i === i && st.verEdit.j === j;
        return `
        <div class="svc-block" draggable="true" data-block="${i}:${j}">
          <span class="sb-name">${esc(svcName(x.service_id))}</span>
          <span class="sb-ver" data-ver="${i}:${j}" title="设置服务级版本（覆盖命令行 --version/--previous-version）">${x.version ? 'v' + esc(x.version) : '版本'}</span>
          <span class="sb-x" data-x="${i}:${j}">✕</span>
        </div>
        ${ed ? `
        <div class="svc-ver-ed" style="display:flex;gap:6px;align-items:center;padding:6px 8px;border:1px dashed var(--border-strong);border-radius:var(--r-ctl);flex-wrap:wrap">
          <input style="flex:1;min-width:130px" placeholder="新版本号（覆盖 --version）" value="${esc(x.version || '')}" id="svVer">
          <input style="flex:1;min-width:130px" placeholder="回滚版本（覆盖 --previous-version）" value="${esc(x.previous_version || '')}" id="svPrev">
          <button class="btn sm primary" id="svVerOk">✓</button>
          <button class="btn sm ghost" id="svVerCancel">取消</button>
        </div>` : ''}`;
      }).join('');
      return `
      <div class="stage-card" data-stage="${i}">
        <div class="sc-head">
          <span class="sc-drag" draggable="true" title="拖动调整阶段顺序">⠿</span>
          <input class="sc-name" value="${esc(sg.name || '')}" placeholder="阶段名，如 网关">
          <label class="sc-parallel"><input type="checkbox" class="sc-par" ${sg.parallel ? 'checked' : ''}> 并行</label>
          <button class="sc-del" title="删除阶段">🗑</button>
        </div>
        <div class="stage-warn">⚠ 并行阶段内的服务会同时发布，请确保目标服务器不冲突</div>
        <div class="stage-svc ${svcs ? '' : 'empty'}" data-drop="${i}">
          ${svcs || '<span>拖入服务</span>'}
        </div>
      </div>`;
    }

    function bind(){
      const plName = document.getElementById('plName');
      if (plName) plName.oninput = () => { st.name = plName.value; updatePreview(); };
      document.getElementById('plBuildFirst').onchange = e => { st.buildFirst = e.target.checked; updatePreview(); };
      // 搜索仅刷新服务池列表，避免丢失输入焦点
      document.getElementById('plSearch').oninput = e => {
        st.query = e.target.value;
        const q = st.query.trim().toLowerCase();
        const used = usedSet();
        const list = document.querySelector('.pp-list');
        if (!list) return;
        list.innerHTML = st.services.filter(s => !q || s.name.toLowerCase().includes(q)).map(s => `
          <div class="pl-svc ${used.has(s.id) ? 'used' : ''}" draggable="${!used.has(s.id)}" data-svc="${s.id}">
            <span>⚙️</span><span>${esc(s.name)}</span>${used.has(s.id) ? '<span class="usd">已用</span>' : ''}
          </div>`).join('') || (st.services.length ? '<div class="weak" style="font-size:11px;padding:6px">无匹配服务</div>' : '<div class="weak" style="font-size:11px;padding:6px">暂无服务，请先到服务页创建</div>');
        bindPool(list);
      };
      bindPool(document.querySelector('.pp-list'));

      document.getElementById('plTpl').onchange = e => {
        const t = H.PIPELINE_TEMPLATES.find(x => x.id === e.target.value);
        if (!t) return;
        e.target.value = '';
        st.stages = H.applyPipelineTemplate(t, st.services);
        st.verEdit = null;
        render();
        toastSuccess('已按服务名自动归位到模板阶段，可再手动调整');
      };
      document.getElementById('plAddStage').onclick = () => { st.stages.push({ name:'新阶段', parallel:false, services:[] }); render(); };
      document.getElementById('plSave').onclick = save;

      // ---- 阶段卡：名称/并行/删除/整卡排序 ----
      document.querySelectorAll('.stage-card').forEach(card => {
        const i = Number(card.dataset.stage);
        card.querySelector('.sc-name').oninput = e => { st.stages[i].name = e.target.value; };
        card.querySelector('.sc-par').onchange = e => {
          st.stages[i].parallel = e.target.checked;
          card.classList.toggle('parallel', e.target.checked);
        };
        card.querySelector('.sc-del').onclick = async () => {
          const cnt = (st.stages[i].services || []).length;
          if (cnt && !await confirmDialog(`阶段「${st.stages[i].name || (i + 1)}」内有 ${cnt} 个服务，删除阶段将同时移除它们，确认？`)) return;
          st.stages.splice(i, 1);
          st.verEdit = null;
          render();
        };
        card.addEventListener('dragover', e => {
          if (e.dataTransfer.types.includes('text/plain')){ e.preventDefault(); card.style.borderColor = 'var(--primary)'; }
        });
        card.addEventListener('dragleave', () => { card.style.borderColor = ''; });
        card.addEventListener('drop', e => {
          e.preventDefault(); card.style.borderColor = '';
          const t = e.dataTransfer.getData('text/plain') || '';
          if (!t.startsWith('stage:')) return;
          const from = Number(t.split(':')[1]);
          if (from === i) return;
          const [sg] = st.stages.splice(from, 1);
          st.stages.splice(i, 0, sg);
          st.verEdit = null;   // 阶段顺序变化，版本编辑器坐标失效
          render();
        });
      });
      document.querySelectorAll('.sc-drag').forEach(h => {
        h.addEventListener('dragstart', e => {
          e.dataTransfer.setData('text/plain', 'stage:' + h.closest('.stage-card').dataset.stage);
          e.dataTransfer.effectAllowed = 'move';
        });
      });

      // ---- 阶段容器：拖入（来自服务池或其它阶段） ----
      document.querySelectorAll('.stage-svc').forEach(dz => {
        dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('dragover'); });
        dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
        dz.addEventListener('drop', e => {
          e.preventDefault(); dz.classList.remove('dragover');
          const t = e.dataTransfer.getData('text/plain') || '';
          const i = Number(dz.dataset.drop);
          if (t.startsWith('svc:')){
            const sid = Number(t.split(':')[1]);
            if (usedSet().has(sid)) return;
            st.stages[i].services = st.stages[i].services || [];
            st.stages[i].services.push({ service_id: sid });
            st.verEdit = null;   // 结构变更后旧坐标失效
            render();
          }else if (t.startsWith('block:')){
            const [fi, fj] = t.split(':').slice(1).map(Number);
            if (fi === i) return;
            const [svc] = st.stages[fi].services.splice(fj, 1);
            st.stages[i].services = st.stages[i].services || [];
            st.stages[i].services.push(svc);
            st.verEdit = null;   // 结构变更后旧坐标失效
            render();
          }
        });
      });

      // ---- 服务块：拖拽源 + 作为放置目标（拖内排序/插入到指定位置） ----
      document.querySelectorAll('.svc-block').forEach(b => {
        b.addEventListener('dragstart', e => {
          e.dataTransfer.setData('text/plain', 'block:' + b.dataset.block);
          e.dataTransfer.effectAllowed = 'move';
        });
        b.addEventListener('dragover', e => { e.preventDefault(); });
        b.addEventListener('drop', e => {
          e.preventDefault();
          const t = e.dataTransfer.getData('text/plain') || '';
          const [ti, tj] = b.dataset.block.split(':').map(Number);
          if (t.startsWith('svc:')){
            const sid = Number(t.split(':')[1]);
            if (usedSet().has(sid)) return;
            st.stages[ti].services.splice(tj, 0, { service_id: sid });
            st.verEdit = null;   // 结构变更后旧坐标失效
            render();
          }else if (t.startsWith('block:')){
            const [fi, fj] = t.split(':').slice(1).map(Number);
            if (fi === ti && fj === tj) return;
            const [svc] = st.stages[fi].services.splice(fj, 1);
            // 同阶段内向前移动：删除后索引前移一位；跨阶段则索引不变
            const ins = (fi === ti && fj < tj) ? tj - 1 : tj;
            st.stages[ti].services.splice(Math.max(ins, 0), 0, svc);
            st.verEdit = null;   // 结构变更后旧坐标失效
            render();
          }
        });
      });

      // ---- 服务块：移出 / 版本设置 ----
      document.querySelectorAll('.sb-x').forEach(x => {
        x.onclick = () => {
          const [i, j] = x.dataset.x.split(':').map(Number);
          st.stages[i].services.splice(j, 1);
          st.verEdit = null;
          render();
        };
      });
      document.querySelectorAll('.sb-ver').forEach(v => {
        v.onclick = () => {
          const [i, j] = v.dataset.ver.split(':').map(Number);
          st.verEdit = (st.verEdit && st.verEdit.i === i && st.verEdit.j === j) ? null : { i, j };
          render();
        };
      });
      const verOk = document.getElementById('svVerOk');
      if (verOk) verOk.onclick = () => {
        const svc = st.stages[st.verEdit.i].services[st.verEdit.j];
        const ver = val('svVer').trim();
        const prev = val('svPrev').trim();
        if (ver) svc.version = ver; else delete svc.version;
        if (prev) svc.previous_version = prev; else delete svc.previous_version;
        st.verEdit = null;
        render();
      };
      const verCancel = document.getElementById('svVerCancel');
      if (verCancel) verCancel.onclick = () => { st.verEdit = null; render(); };
    }

    function bindPool(list){
      if (!list) return;
      list.querySelectorAll('.pl-svc').forEach(el => {
        el.addEventListener('dragstart', e => {
          e.dataTransfer.setData('text/plain', 'svc:' + el.dataset.svc);
          e.dataTransfer.effectAllowed = 'copy';
        });
      });
    }

    function updatePreview(){
      const el = document.getElementById('plPreview');
      if (!el) return;
      const hasVer = st.stages.some(sg => (sg.services || []).some(x => x.version));
      el.textContent = H.pipelineCmd(st.name || 'pipeline', st.project.id, hasVer);
      el.title = hasVer ? '存在服务级版本：命令行 --version 仅作为默认值，会被服务级版本覆盖' : '发布时通过 --version 指定版本号';
    }

    async function save(){
      if (!st.name.trim()){ toastError('流水线名称必填'); return; }
      // 从 DOM 收集阶段名（并行开关已在事件中同步到数据）
      document.querySelectorAll('.stage-card').forEach(card => {
        const i = Number(card.dataset.stage);
        st.stages[i].name = card.querySelector('.sc-name').value.trim();
      });
      const payload = { name: st.name.trim(), build_first: st.buildFirst, stages: st.stages };
      try{
        if (pl) await api(`/api/projects/${project.id}/pipelines/${pl.id}`, { method: 'PUT', body: JSON.stringify(payload) });
        else await api(`/api/projects/${project.id}/pipelines`, { method: 'POST', body: JSON.stringify(payload) });
        toastSuccess(pl ? '流水线已更新' : '流水线已创建');
        const dr = document.getElementById('drawerRoot');
        dr.classList.add('hidden'); dr.innerHTML = '';
        if (onSaved) onSaved(); else navRetry();
      }catch(e){ toastError(e.message); }
    }

    function val(id){ const el = document.getElementById(id); return el ? el.value : ''; }
  }

  window.cfgPipelineEditor = { open };
})();
```

- [ ] **Step 2: 验证**

console 执行 `window.cfgProject.render(document.getElementById('pageRoot'), 1, 'pipelines')` 后点「新建流水线」，逐步验证：
1. 套模板「全量发布」→ 服务按名自动归位到网关/基础设施/业务/定时任务四阶段，toast 提示
2. 拖拽：服务池拖入空阶段（高亮放置区）→ 已用服务置灰不可再拖 → 块拖到另一块前（拖内排序）→ 跨阶段拖动 → ⠿ 拖整卡换顺序
3. 并行开关 → 橙色警示条出现；点版本徽标 → 填 version/previous_version → ✓ 显示 v 徽标 → 预览命令出现提示
4. 阶段删除（非空二次确认）→ 服务块 ✕ 移出 → ＋ 添加阶段
5. 保存 → toast 成功 → 列表刷新（navRetry）→ 重新打开编辑 → 阶段/服务/版本/并行/构建优先全部还原
6. 删除该测试流水线（清理数据）

console 无报错。

- [ ] **Step 3: Commit**

```bash
git add webapp/static/js/pages/config/config-pipeline-editor.js
git commit -m "feat(config): 流水线画布编辑器（拖拽/模板/服务级版本/命令预览）"
```

---

## Task 8: config-service-form.js（服务表单：复制 + 分组 + 字段白话说明）

**Files:**
- Write: `webapp/static/js/pages/config/config-service-form.js`（完整覆盖占位文件）

要点：
- 顶部「从现有服务复制」下拉：带入全部字段（剔除 id/project_id/created_at/enabled），保存时后端校验服务名唯一
- 4 个分组分区（基本信息/构建配置/注册中心与任务调度/健康检查与日志），复用 H.section / H.tagInput / H.rowList
- 编辑态允许改名（后端支持且校验唯一）；保存后 `navRetry()` 刷新列表

- [ ] **Step 1: 写入完整文件**

```js
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
```

- [ ] **Step 2: 验证**

console 执行 `window.cfgProject.render(document.getElementById('pageRoot'), 1, 'services')` 后点「新建服务」/「编辑」，逐步验证：
1. 复制下拉选中现有服务 → 全部字段（含 build_order 行、nacos_groups 标签、log_error_keywords）自动带入 → 改名保存成功
2. 行编辑器：添加步骤 / 上移下移 / 删除；标签输入器回车添加、✕ 删除
3. 必填缺失 / 端口非数字 → 字段下方就地标红，不弹窗
4. 编辑已有服务 → 回显一致 → 改名保存成功；与已有服务重名 → toast 展示后端「项目内服务名已存在」
5. 删除被流水线 stages 引用的服务 → toast 展示后端「服务被流水线 stages 引用，请先调整流水线」
6. 删除测试服务（清理数据）

console 无报错。

- [ ] **Step 3: Commit**

```bash
git add webapp/static/js/pages/config/config-service-form.js
it commit -m "feat(config): 服务表单（复制带入/分组/字段说明/就地校验）"
```

---

## Task 9: config-server-form.js（服务器表单：凭据继承 + 角色卡片 + 就地校验 + 保存并测试 SSH）

**Files:**
- Write: `webapp/static/js/pages/config/config-server-form.js`（完整覆盖占位文件）
- Modify: `webapp/static/js/pages/config/config-project.js`（Task 5 中 renderServers 的 3 处 `cfgServerForm.open` 调用追加第三个参数 `() => renderServers(sb)`，让保存后刷新列表并展示引导条）

要点：
- 凭据继承开关（默认开）：开 = 不提交 ssh_* 字段（后端 NULL = 导出时继承环境级）；关 = 显示覆盖字段（用户/端口/密钥或密码）；从覆盖切回继承时显式提交 null 清空覆盖（后端为字段级更新，不提交不会清除）
- 编辑态密码已遮蔽（`***`）：留空不提交（后端保留原值）；测试时传 `use_stored_password: true` 让后端复用已存密码
- 消费 `POST /servers/test`（单接口返回 ok/message，不做后端改造）；失败时按 message 关键字给可执行建议，提供「重新测试」与「仍要保存」兑底
- 保存成功后 toast + 引导步骤条 + 「去绑定环境 →」按钮（复用 cfgAutoOpen 自动打开环境表单）

- [ ] **Step 1: 修改 Task 5 的 renderServers 调用处**（三处）

将：

```js
      document.getElementById('addSrv').onclick = () => cfgServerForm.open(sb, null);
      sb.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => cfgServerForm.open(sb, svs.find(x => x.id === Number(b.dataset.edit))));
```

改为：

```js
      const refreshSrv = () => renderServers(sb);
      document.getElementById('addSrv').onclick = () => cfgServerForm.open(sb, null, refreshSrv);
      sb.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => cfgServerForm.open(sb, svs.find(x => x.id === Number(b.dataset.edit)), refreshSrv));
```

- [ ] **Step 2: 写入完整文件**

```js
// config-server-form.js — 服务器新增/编辑：凭据继承开关 + 角色卡片 + 就地校验 + 保存并测试 SSH
(function(){
  'use strict';
  const H = window.cfgHelpers;

  function open(container, srv, onSaved){
    const st = {
      container, onSaved, srv,
      role: (srv && srv.role) || 'first',
      // 覆盖形态含 ssh_port 单独覆盖与密码禁用（''）：缺失判据会误判继承并在保存时静默清空
      inherit: !(srv && (srv.ssh_user || srv.ssh_key_file ||
        (srv.ssh_password ?? null) !== null || (srv.ssh_port ?? null) !== null)),
      authMode: (srv && srv.ssh_password && !srv.ssh_key_file) ? 'pwd' : 'key',
      others: [],
      testing: false, testOk: null, testMsg: null,
    };
    openDrawer({ title: srv ? '编辑服务器：' + srv.host : '添加服务器', wide: true,
      body: '<div class="cfg-home">' + skeletonHTML(3) + '</div>', onClose: null });
    const bodyEl = document.getElementById('dwBody');
    (async () => {
      try{
        st.others = (await api('/api/servers')).filter(x => !srv || x.id !== srv.id);
        render();
      }catch(e){ bodyEl.innerHTML = errorState('加载失败：' + esc(e.message), 'navRetry'); }
    })();

    function render(){
      const pwdMasked = srv && srv.ssh_password === '***';
      const hasCovered = srv && (srv.ssh_user || srv.ssh_key_file ||
        (srv.ssh_password ?? null) !== null || (srv.ssh_port ?? null) !== null);
      const sshBody = st.inherit
        ? `<div class="f-hint">本台服务器将使用环境级 SSH 凭据（root@22 · 密钥 ~/.ssh/id_rsa_deploy）；未绑定环境时回退默认 root/22。大部分服务器共用一套登录方式，无需逐台填写。
            ${hasCovered ? '<div style="color:var(--warning);margin-top:6px">⚠ 本台存在端口/密码覆盖配置，保存后将被清空（如需保留请取消继承）</div>' : ''}
          </div>`
        : `<div class="form-grid">
            ${H.field({ label:'登录用户', input:`<input id="sUser" value="${esc(srv ? srv.ssh_user || '' : '')}" placeholder="如 root">`, hint:'留空 = 使用环境级用户' })}
            ${H.field({ label:'SSH 端口', input:`<input id="sPort" type="number" value="${esc(srv && srv.ssh_port ? srv.ssh_port : '')}" placeholder="22">`, hint:'留空 = 使用环境级端口' })}
            <div class="field full"><label>连接方式</label>
              <select id="sAuth"><option value="key" ${st.authMode === 'key' ? 'selected' : ''}>密钥（推荐）</option><option value="pwd" ${st.authMode === 'pwd' ? 'selected' : ''}>密码</option></select>
            </div>
            ${st.authMode === 'key'
              ? H.field({ label:'私钥路径', tip:'未配置密码时使用', input:`<input id="sKey" value="${esc(srv ? srv.ssh_key_file || '' : '')}" placeholder="如 ~/.ssh/id_rsa_deploy">`, hint:'留空 = 使用环境级密钥' })
              : H.field({ label:'密码', tip:'密码优先、失败自动回退密钥', input:`<input id="sPwd" type="password" value="" placeholder="${pwdMasked ? '已遮蔽，留空不修改' : '敏感密码建议环境变量 SSH_PASSWORD'}">`, hint: pwdMasked ? '原密码已遮蔽：留空不修改，保存后保留原密码' : '留空 = 不配置密码（仅用密钥）；敏感密码建议环境变量 SSH_PASSWORD' })}
          </div>`;
      bodyEl.innerHTML = `
        <div class="form-grid">
          ${H.field({ label:'服务器 IP', required:true, tip:'SSH 连接用地址，全平台唯一', input:`<input id="sHost" value="${esc(srv ? srv.host : '')}" placeholder="如 10.0.0.11">` })}
          ${H.field({ label:'Nacos 注册 IP（nacos_ip）', tip:'NAT 环境下与外网 IP 不同才填', input:`<input id="sNacosIp" value="${esc(srv ? srv.nacos_ip || '' : '')}" placeholder="留空 = 使用服务器 IP">`, hint:'NAT 环境下服务在 Nacos 中注册的内网 IP，与上方 IP 相同则留空' })}
        </div>
        <div class="f-label">角色 * <span class="q" title="first = 最先发布并灰度观察；follower = 按绑定顺序跟随">?</span></div>
        <div class="role-cards">
          <div class="role-card ${st.role === 'first' ? 'sel' : ''}" data-role="first">
            <div class="rc-title">first 首台<span class="rc-key">first</span></div>
            <div class="rc-desc">最先发布，灰度观察无异常后再发布其余服务器。同一环境至少 1 台。</div>
          </div>
          <div class="role-card ${st.role === 'follower' ? 'sel' : ''}" data-role="follower">
            <div class="rc-title">follower 后续<span class="rc-key">follower</span></div>
            <div class="rc-desc">按绑定顺序跟随发布，用于集群扩容。</div>
          </div>
        </div>
        <div class="cfg-section"><div class="cs-head"><span class="cs-ico">🔑</span><span class="cs-title">SSH 连接凭据</span><span class="cs-desc">大部分服务器共用环境级凭据</span></div>
          <div class="cs-body">
            <label class="mode-switch" style="margin-bottom:10px"><input type="checkbox" id="sInherit" ${st.inherit ? 'checked' : ''}> 使用环境级凭据（不单独配置）</label>
            ${sshBody}
          </div>
        </div>
        <div class="field full"><label>备注</label><textarea id="sRemark" rows="2" placeholder="仅用于识别服务器用途，不影响发布逻辑">${esc(srv ? srv.remark || '' : '')}</textarea></div>
        <div id="testPanel"></div>
        <div style="display:flex;gap:10px;margin-top:16px">
          <button class="btn primary flex-1" id="srvSave">保存并测试连接</button>
          <button class="btn ghost" id="srvSaveOnly" style="display:none">仍要保存</button>
          <button class="btn ghost" id="srvRetest" style="display:none">重新测试</button>
        </div>`;

      bodyEl.querySelectorAll('.role-card').forEach(c => c.onclick = () => { st.role = c.dataset.role; render(); });
      document.getElementById('sInherit').onchange = e => { st.inherit = e.target.checked; render(); };
      const auth = document.getElementById('sAuth');
      if (auth) auth.onchange = e => { st.authMode = e.target.value; render(); };
      document.getElementById('srvSave').onclick = async () => {
        if (!validate()) return;
        const btn = document.getElementById('srvSave');
        btn.disabled = true; btn.textContent = '⏳ 测试中…';
        await runTest();
        btn.disabled = false; btn.textContent = '保存并测试连接';
        if (st.testOk) await save();
      };
      document.getElementById('srvRetest').onclick = () => { if (validate()) runTest(); };
      document.getElementById('srvSaveOnly').onclick = () => { if (validate()) save(); };
      renderTest();
    }

    // ---- 就地校验（不弹窗、光标留在原处） ----
    function validate(){
      let ok = true;
      const host = val('sHost').trim();
      if (!host){ H.fieldErr('sHost', '服务器 IP 必填'); ok = false; }
      else if (!H.ipValid(host)){ H.fieldErr('sHost', 'IP 格式错误（IPv4 各段 0~255）'); ok = false; }
      else if (st.others.some(x => x.host === host)){ H.fieldErr('sHost', '该 IP 已存在（全平台唯一）'); ok = false; }
      else H.fieldErr('sHost', '');
      const port = val('sPort').trim();
      if (!st.inherit && port && (!Number.isInteger(Number(port)) || Number(port) < 1 || Number(port) > 65535)){
        H.fieldErr('sPort', '端口范围 1~65535'); ok = false;
      }else H.fieldErr('sPort', '');
      return ok;
    }

    // ---- 收集提交载荷（对齐后端三态：不传/NULL = 继承环境级） ----
    function collectPayload(){
      const p = { host: val('sHost').trim(), role: st.role, nacos_ip: val('sNacosIp').trim(), remark: val('sRemark').trim() };
      if (st.inherit){
        // 从覆盖切回继承：显式置 null 清空覆盖（后端为字段级更新，不提交不会清除）
        p.ssh_user = null; p.ssh_port = null; p.ssh_password = null; p.ssh_key_file = null;
      }else{
        p.ssh_user = val('sUser').trim() || null;
        p.ssh_port = val('sPort').trim() ? Number(val('sPort').trim()) : null;
        if (st.authMode === 'key'){
          const kf = val('sKey').trim();
          if (kf) p.ssh_key_file = kf;
          else if (!(srv && srv.ssh_key_file)) p.ssh_key_file = null;   // 编辑态已有密钥且留空 = 保留（不提交）
          // 遮蔽密码不触碰（保留原密码）；无遮蔽时空串 = 禁用密码认证、仅用密钥
          if (!(srv && srv.ssh_password === '***')) p.ssh_password = '';
        }else{
          const pw = val('sPwd');
          if (pw) p.ssh_password = pw;
          else if (srv && srv.ssh_password === '***'){ /* 留空 = 保留原密码，不提交 */ }
          else p.ssh_password = '';   // 空串 = 该台禁用密码认证、仅用密钥
          // 切到密码模式：已有密钥保留（密码失败自动回退密钥），不提交
          if (!(srv && srv.ssh_key_file)) p.ssh_key_file = null;
        }
      }
      return p;
    }

    // ---- 连接测试（POST /servers/test，单接口总体结果） ----
    async function runTest(){
      const t = { host: val('sHost').trim() };
      if (!st.inherit){
        const user = val('sUser').trim(), port = val('sPort').trim();
        if (user) t.ssh = Object.assign(t.ssh || {}, { user });
        if (st.authMode === 'key'){ const kf = val('sKey').trim(); if (kf) t.ssh = Object.assign(t.ssh || {}, { key_file: kf }); }
        else { const pw = val('sPwd'); if (pw) t.ssh = Object.assign(t.ssh || {}, { password: pw }); }
        if (port) t.ssh_port = Number(port);
      }
      if (srv && srv.ssh_password === '***' && !st.inherit && !val('sPwd')) t.use_stored_password = true;
      st.testing = true; renderTest();
      try{
        const r = await api('/api/servers/test', { method: 'POST', body: JSON.stringify(t) });
        st.testOk = r.ok; st.testMsg = r.message;
      }catch(e){ st.testOk = false; st.testMsg = e.message; }
      st.testing = false; renderTest();
    }

    function advice(msg){
      const m = String(msg || '');
      if (/timed out|refused|unreachable|connect.*fail/i.test(m)) return '网络不可达：检查服务器是否开机、防火墙是否放行 SSH 端口（默认 22）。';
      if (/permission denied|authentication|auth|key.*denied/i.test(m)) return '认证失败：确认登录用户与凭据；推荐把 ~/.ssh/id_rsa_deploy.pub 添加到该服务器 root 的 authorized_keys 后重新测试。';
      if (/no such file|invalid.*key|parse.*key/i.test(m)) return '私钥无效：检查密钥文件路径与内容（应为 OpenSSH 格式私钥）。';
      return '连接失败：按上方错误信息排查（网络 / 端口 / 凭据），可稍后重新测试，或选择「仍要保存」稍后处理。';
    }

    function renderTest(){
      const tp = document.getElementById('testPanel');
      if (!tp) return;
      const rt = document.getElementById('srvRetest'), so = document.getElementById('srvSaveOnly');
      if (st.testing){
        tp.innerHTML = '<div class="test-panel"><div class="tp-row tp-wait">⏳ 正在测试 SSH 连接…</div></div>';
        if (rt) rt.disabled = true;
        if (so) so.disabled = true;
        return;
      }
      if (st.testMsg === null){ tp.innerHTML = ''; return; }
      tp.innerHTML = `
        <div class="test-panel">
          <div class="tp-row ${st.testOk ? 'tp-ok' : 'tp-fail'}">${st.testOk ? '✓' : '✗'} ${esc(st.testMsg)}</div>
          ${st.testOk ? '<div class="tp-advice" style="color:var(--success)">服务器连接正常，可安全保存。</div>' : `<div class="tp-advice">💡 ${esc(advice(st.testMsg))}</div>`}
        </div>`;
      rt.style.display = st.testOk ? 'none' : 'inline-block';
      so.style.display = st.testOk ? 'none' : 'inline-block';
      rt.disabled = false; so.disabled = false;
    }

    async function save(){
      const p = collectPayload();
      try{
        if (srv) await api('/api/servers/' + srv.id, { method: 'PUT', body: JSON.stringify(p) });
        else await api('/api/servers', { method: 'POST', body: JSON.stringify(p) });
        toastSuccess(srv ? '服务器已更新' : '服务器已添加');
        const dr = document.getElementById('drawerRoot');
        dr.classList.add('hidden'); dr.innerHTML = '';
        if (onSaved){ await onSaved(); showGuide(); }
        else navRetry();
      }catch(e){ toastError(e.message); }
    }

    // ---- 保存成功引导：下一步建议 + 去绑定环境 ----
    function showGuide(){
      container.insertAdjacentHTML('afterbegin', `
        <div class="guide-steps" id="srvGuide">
          <span class="gs-item done">① 添加服务器 ✓</span><span class="gs-arrow">→</span>
          <span class="gs-item">② 新建服务</span><span class="gs-arrow">→</span>
          <span class="gs-item">③ 新建环境并绑定</span><span class="gs-arrow">→</span>
          <span class="gs-item">④ 编排流水线</span>
          <button class="btn sm primary" id="goBindEnv" style="margin-left:auto">去绑定环境 →</button>
        </div>`);
      document.getElementById('goBindEnv').onclick = async () => {
        try{
          const ps = await api('/api/projects');
          const p = ps[0];
          if (p){ sessionStorage.setItem('cfgAutoOpen', 'env'); location.hash = `#/config/p/${p.id}/environments`; }
          else { toastError('还没有项目，请先创建项目'); location.hash = '#/config'; }
        }catch(e){ toastError(e.message); }
      };
    }

    function val(id){ const el = document.getElementById(id); return el ? el.value : ''; }
  }

  window.cfgServerForm = { open };
})();
```

- [ ] **Step 3: 验证**

console 执行 `window.cfgProject.render(document.getElementById('pageRoot'), 1, 'servers')` 后点「新建服务器」，逐步验证：
1. IP 留空 / 格式错（如 256.1.1.1）/ 与既有服务器重复 → 字段下方就地标红，光标不丢失
2. 角色卡片切换 first/follower 选中态；默认「使用环境级凭据」开 → 关 → 密钥/密码方式切换
3. 填真实可连服务器 IP（或编辑已有服务器）→ 「保存并测试连接」→ 测试面板显示 ✓/✗ + 耗时；失败时显示建议 + 「重新测试」「仍要保存」
4. 保存成功 → toast + 引导步骤条 + 「去绑定环境 →」→ 跳转首个项目环境页并自动打开新建环境表单（cfgAutoOpen）
5. 编辑态：密码显示"已遮蔽，留空不修改"；留空保存后再次打开仍遮蔽（密码保留）
6. 删除测试服务器（清理数据）

console 无报错。

- [ ] **Step 4: Commit**

```bash
git add webapp/static/js/pages/config/config-server-form.js webapp/static/js/pages/config/config-project.js
git commit -m "feat(config): 服务器表单（凭据继承/角色卡/就地校验/保存并测试 SSH/引导）"
```

---

## Task 10: 替换旧 config.js 入口（子路由分发 + 全量回归）

**Files:**
- Modify: `webapp/static/js/pages/config/config.js`（Task 1 移动来的旧入口文件，整体替换为分发器；旧逻辑已被 Task 4-9 新模块取代）

要点：
- 按 `#/config` 子路径分发：`p/<pid>[/<sub>]` → cfgProject；`users` → cfgHome；`servers`（工作台统计卡入口）→ 跳第一个项目服务器子页；其余 → 工作台
- 保存刷新统一走 `navRetry()`（Task 6-8 已内置），此任务不再依赖旧 config.js 的全局状态

- [ ] **Step 1: 用新入口整体覆盖 `config/config.js`**

```js
// config.js — 配置管理入口：按 hash 子路由分发到工作台 / 项目仪表盘 / 用户管理
(function(){
  'use strict';
  async function render(root){
    const parts = (location.hash || '#/config').split('?')[0].replace(/^#\/?/, '').split('/').filter(Boolean);
    if (parts[1] === 'p'){
      // 缺 pid 与非法 pid 行为统一：链接损坏直接报错，不静默回落工作台
      const pid = Number(parts[2]);
      if (!Number.isInteger(pid) || pid <= 0) throw new Error('项目 ID 无效');
      return await window.cfgProject.render(root, pid, parts[3] || 'overview');
    }
    if (parts[1] === 'users') return await window.cfgHome.render(root, 'users');
    if (parts[1] === 'servers'){
      // 平台级服务器入口（工作台统计卡）：跳转到第一个项目的服务器子页
      const ps = await api('/api/projects');
      if (ps.length){ location.hash = `#/config/p/${ps[0].id}/servers`; return []; }
      return await window.cfgHome.render(root, 'home');
    }
    return await window.cfgHome.render(root, 'home');
  }
  window.config = { render, destroy: function(){ /* 无定时器/图表 */ } };
})();
```

- [ ] **Step 2: 全量回归验证**（所有入口经真实路由，不再 console 手动调用）

1. `#/config` → 工作台：统计卡数字与 DB 一致、快速开始 4 卡、最近项目可进入仪表盘；无项目时空态引导顺序正确
2. `#/config/p/<pid>` → 仪表盘；`/services` `/environments` `/pipelines` `/servers` `/yaml` 五个子页各自正常渲染；页签切换 hash 变化强制刷新（Task 1 subChanged 机制）
3. `#/config/users` → 用户管理（新建/编辑/删除/角色）
4. `#/config/servers` → 自动跳转首个项目服务器子页
5. 表单闭环（经真实 UI 操作）：环境（模板→表单→高级模式往返→绑定→保存→编辑回显→导出 YAML 等价）、流水线（模板→拖拽→版本→保存→重开还原→命令预览）、服务（复制→保存→被引用删除被拒）、服务器（校验→测试→仍要保存→引导）
6. YAML 导入导出沿用可用；viewer 角色访问 `#/config` 被弹回 dashboard；全平台页面 console 无报错

- [ ] **Step 3: Commit**

```bash
git add webapp/static/js/pages/config/config.js
git commit -m "feat(config): 配置管理新入口（子路由分发），完成傻瓜式运维创作重构"
```
