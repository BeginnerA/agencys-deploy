# 前端重设计：深蓝科技风运维平台 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 参考 Perfview 深蓝科技风运维平台 UI，将 agencys-deploy webapp 前端重构为零构建链 SPA（多页面导航 + 监控大屏复刻 + 告警中心聚合），后端 API 零改动。

**Architecture:** Flask 渲染单一壳模板 `screen.html`，前端 SPA hash 路由（`#/dashboard` 等）切换 6 个页面模块；每个页面模块导出 `render(container)` + `destroy()` 生命周期。设计系统以 CSS 变量（`tokens.css`）为唯一视觉来源；ECharts 继续从 `static/vendor/echarts.min.js` 加载。告警中心为纯前端聚合（tasks + overview 数据），"已处理"状态存 localStorage。

**Tech Stack:** 原生 HTML/CSS/JS + ECharts（无 npm、无构建链、无测试框架）。验证方式 = 启动 Flask + 浏览器/curl 冒烟。

---

## 全局约定

- **后端零改动**：不得修改 `webapp/routes/*`、`webapp/*.py`（除 `app.py` 若确需调整模板引用——本计划不需要）。
- **验证方式**：每个任务完成后的验证均以「启动 Flask + 浏览器访问」为基准。启动命令：
  ```powershell
  cd d:\work\IdeaProjects\Reid\agencys\agencys-deploy
  python webapp/app.py --port 8099
  ```
  （后台运行；无需 --monitor-interval 时监控默认 30s。验证时可用浏览器工具访问 http://127.0.0.1:8099/screen 检查 console 无报错。）
- **commit 步骤**：项目当前不在 git 仓库中（`git rev-parse` 失败）。若执行时已初始化仓库，按步骤执行 `git add`/`git commit`；否则跳过。
- **删除旧文件**：仅在所有新文件就绪、页面可运行后（Task 12）才删除旧 JS/CSS，避免中途回退困难。

## 文件结构（目标态）

```
webapp/templates/
├── login.html            # 重写：深蓝渐变 + 玻璃拟态登录卡
└── screen.html           # 重写：SPA 壳（导航/顶栏/面包屑/内容容器/全局抽屉）
webapp/static/css/
├── tokens.css            # 新建：设计系统变量（色板/字体/间距/圆角/阴影/动效）
├── base.css              # 新建：reset + 基础元素 + 工具类
├── layout.css            # 新建：侧边导航/顶部栏/面包屑/内容区/跑马灯
├── components.css        # 新建：按钮/卡片/表格/表单/徽标/抽屉/弹窗/toast/空态/骨架
├── pages/
│   ├── dashboard.css     # 新建：监控大屏
│   ├── deploy.css        # 新建：发布中心
│   ├── alerts.css        # 新建：告警中心
│   ├── logs.css          # 新建：日志中心
│   └── config.css        # 新建：配置管理
├── responsive.css        # 新建：1280 断点降级
└── screen.css            # Task 12 删除（旧样式）
webapp/static/js/
├── api.js                # 修改：保留原接口 + 补 poll() 封装
├── router.js             # 新建：hash 路由 + 导航激活 + 面包屑
├── layout.js             # 新建：顶栏/侧边导航/用户菜单/跑马灯渲染
├── components.js         # 新建：toast/confirm/empty/skeleton/badge/table 渲染函数
├── data.js               # 新建：告警聚合算法 + 健康指数计算 + 机房示例数据
├── pages/
│   ├── dashboard.js      # 新建：KPI/雷达/机房/趋势/服务器/最近发布
│   ├── devices.js        # 新建：卡片墙 + 环境切换 + 发布抽屉 + 服务器列表
│   ├── deploy.js         # 新建：任务列表/新建/详情/日志流/终止
│   ├── alerts.js         # 新建：告警列表/筛选/标记已处理/角标
│   ├── logs.js           # 新建：任务日志 + 审计日志
│   └── config.js         # 新建：六类 CRUD 标签页 + YAML 导入导出
└── （Task 12 删除：screen.js / cards.js / charts.js / panels.js / config-admin.js）
```

---

## Task 1: 设计系统 — tokens.css + base.css

**Files:**
- Create: `webapp/static/css/tokens.css`
- Create: `webapp/static/css/base.css`

- [ ] **Step 1: 创建 `webapp/static/css/tokens.css`**（设计系统唯一视觉来源，内容完整如下）

```css
/* tokens.css — 深蓝科技风设计系统变量 */
:root{
  /* 色板（规格 §3.1） */
  --bg-base:#0A142F;            /* 页面主背景 */
  --bg-panel:#0D1B33;           /* 卡片/面板背景 */
  --bg-panel-2:#112241;         /* 悬浮/次级面板 */
  --bg-hover:rgba(0,212,255,.06); /* 行/项悬浮底 */
  --border:rgba(0,212,255,.15);   /* 边框/分隔线 */
  --border-strong:rgba(0,212,255,.32);
  --primary:#00D4FF;            /* 主色青 */
  --primary-dim:rgba(0,212,255,.55);
  --success:#00E396;            /* 正常/成功 */
  --warning:#FEB019;            /* 警告 */
  --danger:#FF4560;             /* 异常/失败 */
  --accent:#FF7A45;             /* 强调橙（运行中） */
  --text-1:#E6F1FF;             /* 主文字 */
  --text-2:#8FA8C8;             /* 次级文字 */
  --text-3:#5A7296;             /* 弱化/占位 */

  /* 字体（规格 §3.2） */
  --font-sans:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;
  --font-num:"DIN Alternate","Roboto Mono",Consolas,monospace;
  --fs-11:11px; --fs-12:12px; --fs-13:13px; --fs-14:14px;
  --fs-16:16px; --fs-20:20px; --fs-24:24px; --fs-32:32px;

  /* 间距（规格 §3.3） */
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px;
  --sp-5:24px; --sp-6:32px; --sp-7:48px;

  /* 圆角 */
  --r-panel:10px; --r-card:8px; --r-ctl:6px; --r-full:999px;

  /* 阴影（规格 §3.3） */
  --shadow-panel:0 4px 16px rgba(0,0,0,.35);
  --shadow-glow:0 0 12px rgba(0,212,255,.25);

  /* 动效（规格 §3.4） */
  --ease:cubic-bezier(.4,0,.2,1);
  --t-fast:all .15s var(--ease);
  --t-smooth:all .25s var(--ease);

  /* 布局尺寸 */
  --nav-w:220px; --nav-w-collapsed:64px;
  --topbar-h:56px; --content-max:1680px;
}
```

- [ ] **Step 2: 创建 `webapp/static/css/base.css`**（reset + 基础元素 + 工具类）

```css
/* base.css — reset + 基础元素 + 工具类 */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  font-family:var(--font-sans); font-size:var(--fs-14); color:var(--text-1);
  background:var(--bg-base); line-height:1.6; overflow:hidden;
}
/* 背景氛围：顶部青蓝光晕 + 网格 */
body::before{
  content:""; position:fixed; inset:0; z-index:-1; pointer-events:none;
  background:
    radial-gradient(900px 420px at 75% -10%, rgba(0,212,255,.10), transparent 60%),
    radial-gradient(700px 380px at -10% 110%, rgba(0,122,255,.10), transparent 60%),
    linear-gradient(rgba(0,212,255,.035) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0,212,255,.035) 1px, transparent 1px);
  background-size:auto,auto,44px 44px,44px 44px;
}
a{color:var(--primary);text-decoration:none}
a:hover{text-decoration:underline}
ul,ol{list-style:none}
input,select,textarea,button{font-family:inherit;font-size:inherit;color:inherit}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-thumb{background:rgba(0,212,255,.22);border-radius:var(--r-full)}
::-webkit-scrollbar-thumb:hover{background:rgba(0,212,255,.4)}
::-webkit-scrollbar-track{background:transparent}
.hidden{display:none!important}
.num{font-family:var(--font-num);letter-spacing:.5px}
.muted{color:var(--text-2)} .weak{color:var(--text-3)}
.err{color:var(--danger)} .ok{color:var(--success)} .warn{color:var(--warning)}
.flex{display:flex;align-items:center} .flex-1{flex:1;min-width:0}
.gap-2{gap:var(--sp-2)} .gap-3{gap:var(--sp-3)} .gap-4{gap:var(--sp-4)}
/* 文本省略 */
.ellipsis{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
```

- [ ] **Step 3: 验证**

```powershell
cd d:\work\IdeaProjects\Reid\agencys\agencys-deploy
python -c "import re,pathlib; [print(f, len(f.read_text().splitlines()), 'lines') for f in [pathlib.Path('webapp/static/css/tokens.css'), pathlib.Path('webapp/static/css/base.css')]]"
```
Expected: 两个文件存在且非空。CSS 无构建链，无需编译。

- [ ] **Step 4: Commit（若 git 可用）**
```bash
git add webapp/static/css/tokens.css webapp/static/css/base.css
git commit -m "feat(webapp): 设计系统 tokens 与 base 样式"
```

---

## Task 2: SPA 壳 — screen.html 重写 + layout.css

**Files:**
- Modify: `webapp/templates/screen.html`（整体重写）
- Create: `webapp/static/css/layout.css`

- [ ] **Step 1: 重写 `webapp/templates/screen.html`**（SPA 壳，结构如下，注意 script 引用顺序）

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>智能运维平台 · 运维中心</title>
<meta name="description" content="智能运维平台运维控制台：监控大屏、发布中心、告警中心、日志中心、配置管理">
<link rel="stylesheet" href="/static/css/tokens.css">
<link rel="stylesheet" href="/static/css/base.css">
<link rel="stylesheet" href="/static/css/layout.css">
<link rel="stylesheet" href="/static/css/components.css">
<link rel="stylesheet" href="/static/css/pages/dashboard.css">
<link rel="stylesheet" href="/static/css/pages/deploy.css">
<link rel="stylesheet" href="/static/css/pages/alerts.css">
<link rel="stylesheet" href="/static/css/pages/logs.css">
<link rel="stylesheet" href="/static/css/pages/config.css">
<link rel="stylesheet" href="/static/css/responsive.css">
</head>
<body>
<div id="app">
  <aside id="sidebar"></aside>
  <div id="main">
    <header id="topbar"></header>
    <div id="breadcrumb"></div>
    <main id="content"></main>
    <footer id="marqueeBar" class="hidden"></footer>
  </div>
</div>

<!-- 全局容器：抽屉 / 弹窗 / toast -->
<div id="drawerRoot" class="hidden"></div>
<div id="modalRoot" class="hidden"></div>
<div id="toastRoot"></div>

<script>window.PLATFORM_NAME = '智能运维平台';</script>
<script src="/static/vendor/echarts.min.js"></script>
<script src="/static/js/api.js"></script>
<script src="/static/js/components.js"></script>
<script src="/static/js/data.js"></script>
<script src="/static/js/layout.js"></script>
<script src="/static/js/router.js"></script>
<script src="/static/js/pages/dashboard.js"></script>
<script src="/static/js/pages/devices.js"></script>
<script src="/static/js/pages/deploy.js"></script>
<script src="/static/js/pages/alerts.js"></script>
<script src="/static/js/pages/logs.js"></script>
<script src="/static/js/pages/config.js"></script>
<script src="/static/js/app.js"></script>
</body>
</html>
```

> 说明：页面 CSS 与 JS 全部静态加载（无懒加载，体积可控）；`app.js` 为入口（Task 4 创建）。

- [ ] **Step 2: 创建 `webapp/static/css/layout.css`**（侧边导航/顶部栏/面包屑/内容区/跑马灯）

```css
/* layout.css — 全站布局骨架 */
#app{display:flex;height:100vh;overflow:hidden}

/* ---- 侧边导航 ---- */
#sidebar{
  width:var(--nav-w); flex-shrink:0; display:flex; flex-direction:column;
  background:linear-gradient(180deg,rgba(13,27,51,.98),rgba(10,20,47,.98));
  border-right:1px solid var(--border); transition:var(--t-smooth); overflow:hidden;
}
.side-logo{
  height:var(--topbar-h); flex-shrink:0; display:flex; align-items:center; gap:10px;
  padding:0 var(--sp-4); border-bottom:1px solid var(--border);
}
.side-logo .logo-ico{
  width:30px;height:30px;flex-shrink:0;border-radius:8px;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(135deg,var(--primary),#0066FF);font-size:16px;
  box-shadow:var(--shadow-glow);
}
.side-logo .logo-name{font-size:var(--fs-16);font-weight:600;white-space:nowrap}
.side-logo .logo-sub{font-size:var(--fs-11);color:var(--text-3);letter-spacing:2px;white-space:nowrap}
#sidebar .nav-scroll{flex:1;overflow-y:auto;padding:var(--sp-3) var(--sp-2)}
.nav-group{margin-bottom:var(--sp-3)}
.nav-group-title{
  font-size:var(--fs-11);color:var(--text-3);letter-spacing:1.5px;
  padding:0 var(--sp-3);margin:var(--sp-3) 0 var(--sp-1);
}
.nav-item{
  position:relative;display:flex;align-items:center;gap:10px;
  padding:9px var(--sp-3);margin-bottom:2px;border-radius:var(--r-ctl);
  color:var(--text-2);cursor:pointer;transition:var(--t-fast);white-space:nowrap;
}
.nav-item:hover{background:var(--bg-hover);color:var(--text-1)}
.nav-item.active{
  color:var(--primary);background:linear-gradient(90deg,rgba(0,212,255,.14),transparent 85%);
}
.nav-item.active::before{
  content:"";position:absolute;left:0;top:8px;bottom:8px;width:3px;border-radius:var(--r-full);
  background:var(--primary);box-shadow:var(--shadow-glow);
}
.nav-item .nav-ico{width:18px;text-align:center;font-size:15px;flex-shrink:0}
.nav-item .nav-txt{flex:1}
.nav-badge{
  min-width:18px;height:18px;padding:0 5px;border-radius:var(--r-full);
  background:var(--danger);color:#fff;font-size:var(--fs-11);line-height:18px;
  text-align:center;font-family:var(--font-num);
}
.side-foot{padding:var(--sp-3);border-top:1px solid var(--border);font-size:var(--fs-11);color:var(--text-3)}
#sidebar.collapsed{width:var(--nav-w-collapsed)}
#sidebar.collapsed .logo-name,#sidebar.collapsed .logo-sub,
#sidebar.collapsed .nav-txt,#sidebar.collapsed .nav-group-title,
#sidebar.collapsed .nav-badge{display:none}
#sidebar.collapsed .nav-item{justify-content:center;padding:9px 0}

/* ---- 主区 ---- */
#main{flex:1;display:flex;flex-direction:column;min-width:0}
#topbar{
  height:var(--topbar-h);flex-shrink:0;display:flex;align-items:center;gap:var(--sp-4);
  padding:0 var(--sp-5);background:rgba(13,27,51,.85);backdrop-filter:blur(8px);
  border-bottom:1px solid var(--border);
}
#topbar .tb-search{
  flex:1;max-width:320px;position:relative;margin-left:auto;
}
#topbar .tb-search input{
  width:100%;padding:7px 12px 7px 32px;border:1px solid var(--border);border-radius:var(--r-full);
  background:rgba(0,212,255,.05);color:var(--text-1);outline:none;transition:var(--t-fast);
}
#topbar .tb-search input:focus{border-color:var(--primary);box-shadow:var(--shadow-glow)}
#topbar .tb-search .ico{position:absolute;left:11px;top:50%;transform:translateY(-50%);color:var(--text-3);font-size:13px}
.tb-btn{
  width:32px;height:32px;border:1px solid var(--border);border-radius:var(--r-ctl);
  background:transparent;color:var(--text-2);cursor:pointer;transition:var(--t-fast);
  display:flex;align-items:center;justify-content:center;font-size:14px;
}
.tb-btn:hover{color:var(--primary);border-color:var(--primary)}
#topbar .user-chip{display:flex;align-items:center;gap:8px;cursor:pointer;padding:5px 10px;border-radius:var(--r-ctl);transition:var(--t-fast)}
#topbar .user-chip:hover{background:var(--bg-hover)}
.user-avatar{
  width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(135deg,var(--primary),#0066FF);font-size:var(--fs-12);font-weight:600;flex-shrink:0;
}
.user-meta{line-height:1.2;text-align:left}
.user-meta .u-name{font-size:var(--fs-13);color:var(--text-1)}
.user-meta .u-role{font-size:var(--fs-11);color:var(--text-3)}
/* 用户下拉 */
#userMenu{
  position:fixed;z-index:120;min-width:150px;background:var(--bg-panel-2);
  border:1px solid var(--border);border-radius:var(--r-card);box-shadow:var(--shadow-panel);padding:6px;display:none;
}
#userMenu button{width:100%;text-align:left}
/* 下拉通用面板（搜索建议） */
.drop-panel{
  position:fixed;z-index:110;background:var(--bg-panel-2);border:1px solid var(--border);
  border-radius:var(--r-card);box-shadow:var(--shadow-panel);padding:var(--sp-2);display:none;max-height:360px;overflow-y:auto;
}
.drop-item{padding:7px 10px;border-radius:var(--r-ctl);cursor:pointer;transition:var(--t-fast);font-size:var(--fs-13)}
.drop-item:hover{background:var(--bg-hover);color:var(--primary)}
.drop-item .k{color:var(--text-3);font-size:var(--fs-11);margin-left:6px}

/* ---- 面包屑 ---- */
#breadcrumb{
  display:flex;align-items:center;gap:6px;padding:10px var(--sp-5) 0;
  font-size:var(--fs-12);color:var(--text-3);
}
#breadcrumb .sep{opacity:.5}
#breadcrumb .cur{color:var(--primary)}

/* ---- 内容区 ---- */
#content{flex:1;overflow-y:auto;padding:var(--sp-4) var(--sp-5) var(--sp-6)}
.page-inner{max-width:var(--content-max);margin:0 auto}

/* ---- 底部跑马灯 ---- */
#marqueeBar{
  flex-shrink:0;display:flex;align-items:center;gap:10px;padding:7px var(--sp-5);
  border-top:1px solid var(--border);background:rgba(13,27,51,.9);font-size:var(--fs-12);
  color:var(--text-2);white-space:nowrap;overflow:hidden;
}
#marqueeBar .mq-label{color:var(--primary);flex-shrink:0}
#marqueeBar .mq-text{flex:1;overflow:hidden;text-overflow:ellipsis}
#marqueeBar .mq-close{cursor:pointer;color:var(--text-3);flex-shrink:0;border:none;background:none;font-size:14px}
#marqueeBar .mq-close:hover{color:var(--danger)}
```

- [ ] **Step 3: 验证**

启动 Flask，访问 `http://127.0.0.1:8099/screen`：
```powershell
cd d:\work\IdeaProjects\Reid\agencys\agencys-deploy
python webapp/app.py --port 8099
```
Expected: 页面骨架渲染（侧边栏/顶栏/面包屑/内容区为空但存在），无 404 静态资源（tokens/base/layout/components 及 pages/*.css 均存在；components.css 与各页面 CSS 尚未创建 → 用浏览器确认 `GET /static/css/components.css` 返回 404 属预期，Task 3 补齐）。**注意**：为避免中途 404，可先创建空占位 CSS 文件，或跳过此步直接进入 Task 3 后一并验证。推荐：Task 2 与 Task 3 完成后统一验证。

- [ ] **Step 4: Commit（若 git 可用）**
```bash
git add webapp/templates/screen.html webapp/static/css/layout.css
git commit -m "feat(webapp): SPA 壳模板与布局样式"
```

---

## Task 3: 通用组件 — components.css + components.js

**Files:**
- Create: `webapp/static/css/components.css`
- Create: `webapp/static/js/components.js`

- [ ] **Step 1: 创建 `webapp/static/css/components.css`**（按钮/卡片/表格/表单/徽标/抽屉/弹窗/toast/空态/骨架）

```css
/* components.css — 通用组件样式 */
/* ---- 按钮 ---- */
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:6px;
  padding:8px 16px;border:1px solid var(--border);border-radius:var(--r-ctl);
  background:transparent;color:var(--text-1);cursor:pointer;font-size:var(--fs-13);
  transition:var(--t-fast);white-space:nowrap;user-select:none;
}
.btn:hover{border-color:var(--primary);color:var(--primary)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn.primary{background:linear-gradient(135deg,var(--primary),#0090FF);border-color:transparent;color:#04121F;font-weight:600}
.btn.primary:hover{box-shadow:var(--shadow-glow);filter:brightness(1.1)}
.btn.danger{border-color:rgba(255,69,96,.5);color:var(--danger)}
.btn.danger:hover{background:rgba(255,69,96,.12);border-color:var(--danger)}
.btn.ghost{color:var(--text-2)}
.btn.sm{padding:4px 10px;font-size:var(--fs-12)}
.btn.lg{padding:11px 22px;font-size:var(--fs-14)}

/* ---- 面板 / 卡片 ---- */
.panel{
  background:linear-gradient(180deg,rgba(17,34,65,.55),rgba(13,27,51,.85));
  border:1px solid var(--border);border-radius:var(--r-panel);
  box-shadow:var(--shadow-panel);padding:var(--sp-4);transition:var(--t-smooth);
}
.panel:hover{border-color:var(--border-strong)}
.panel-title{
  display:flex;align-items:center;gap:8px;font-size:var(--fs-14);font-weight:600;
  margin-bottom:var(--sp-3);color:var(--text-1);
}
.panel-title::before{content:"";width:3px;height:14px;border-radius:var(--r-full);background:var(--primary);box-shadow:var(--shadow-glow)}
.panel-title .pt-right{margin-left:auto;font-weight:400}

/* ---- 表格 ---- */
.tbl{width:100%;border-collapse:collapse;font-size:var(--fs-13)}
.tbl th{
  text-align:left;padding:9px 12px;font-weight:500;color:var(--text-2);
  border-bottom:1px solid var(--border);font-size:var(--fs-12);white-space:nowrap;
}
.tbl td{padding:9px 12px;border-bottom:1px solid rgba(0,212,255,.08);vertical-align:middle}
.tbl tbody tr{transition:var(--t-fast)}
.tbl tbody tr:hover{background:var(--bg-hover)}
.tbl .empty-row td{text-align:center;color:var(--text-3);padding:24px}

/* ---- 徽标 / 状态点 ---- */
.badge{
  display:inline-block;padding:2px 9px;border-radius:var(--r-full);
  font-size:var(--fs-11);line-height:1.5;white-space:nowrap;
}
.badge.success{background:rgba(0,227,150,.14);color:var(--success);border:1px solid rgba(0,227,150,.4)}
.badge.running{background:rgba(255,122,69,.14);color:var(--accent);border:1px solid rgba(255,122,69,.4);animation:blink 1.2s infinite}
.badge.pending{background:rgba(143,168,200,.14);color:var(--text-2);border:1px solid rgba(143,168,200,.4)}
.badge.failed{background:rgba(255,69,96,.14);color:var(--danger);border:1px solid rgba(255,69,96,.4)}
.badge.warning{background:rgba(254,176,25,.14);color:var(--warning);border:1px solid rgba(254,176,25,.4)}
.badge.critical{background:rgba(255,69,96,.2);color:#FF7B8D;border:1px solid rgba(255,69,96,.6)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
.dot.ok{background:var(--success);box-shadow:0 0 6px var(--success)}
.dot.bad{background:var(--danger);box-shadow:0 0 6px var(--danger)}
.dot.warn{background:var(--warning);box-shadow:0 0 6px var(--warning);animation:blink 1.2s infinite}
.dot.gray{background:var(--text-3)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.35}}
@keyframes flash{
  0%{background:rgba(0,212,255,.18)} 100%{background:transparent}
}
.flash{animation:flash 1.2s ease-out}

/* ---- 表单 ---- */
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:var(--sp-3) var(--sp-4)}
.form-grid .full{grid-column:1/-1}
.field label{display:block;font-size:var(--fs-12);color:var(--text-2);margin-bottom:4px}
.field input,.field select,.field textarea{
  width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:var(--r-ctl);
  background:rgba(0,212,255,.04);color:var(--text-1);outline:none;transition:var(--t-fast);
  font-size:var(--fs-13);
}
.field input:focus,.field select:focus,.field textarea:focus{border-color:var(--primary);box-shadow:var(--shadow-glow)}
.field textarea{min-height:72px;resize:vertical;font-family:var(--font-num)}
.field .hint{font-size:var(--fs-11);color:var(--text-3);margin-top:2px}
.field.check{display:flex;align-items:center;gap:8px}
.field.check input{width:auto}
/* 环境 chip / 项目 tab */
.chip{
  display:inline-flex;align-items:center;gap:5px;padding:4px 14px;border-radius:var(--r-full);
  border:1px solid var(--border);color:var(--text-2);cursor:pointer;font-size:var(--fs-12);
  transition:var(--t-fast);background:transparent;
}
.chip:hover{color:var(--primary);border-color:var(--primary)}
.chip.active{background:rgba(0,212,255,.12);color:var(--primary);border-color:var(--primary)}
.tab-btn{
  padding:6px 16px;border:1px solid var(--border);background:transparent;color:var(--text-2);
  border-radius:var(--r-ctl) var(--r-ctl) 0 0;cursor:pointer;font-size:var(--fs-13);transition:var(--t-fast);
}
.tab-btn.active{background:var(--bg-panel-2);color:var(--primary);border-color:var(--primary);border-bottom-color:var(--bg-panel-2)}

/* ---- 抽屉 ---- */
.drawer-mask{position:fixed;inset:0;background:rgba(4,10,25,.6);backdrop-filter:blur(2px);z-index:100}
.drawer{
  position:fixed;top:0;right:0;bottom:0;width:520px;max-width:92vw;z-index:101;
  background:linear-gradient(180deg,var(--bg-panel-2),var(--bg-panel));
  border-left:1px solid var(--border);box-shadow:-8px 0 30px rgba(0,0,0,.5);
  display:flex;flex-direction:column;animation:slideIn .25s var(--ease);
}
.drawer.wide{width:760px}
@keyframes slideIn{from{transform:translateX(40px);opacity:0}to{transform:none;opacity:1}}
.drawer-head{display:flex;align-items:center;gap:10px;padding:14px var(--sp-5);border-bottom:1px solid var(--border);flex-shrink:0}
.drawer-head b{font-size:var(--fs-16)}
.drawer-head .dh-right{margin-left:auto;display:flex;gap:8px}
.drawer-body{flex:1;overflow-y:auto;padding:var(--sp-4) var(--sp-5)}

/* ---- 弹窗 ---- */
.modal-mask{position:fixed;inset:0;background:rgba(4,10,25,.65);z-index:200;display:flex;align-items:center;justify-content:center}
.modal-card{
  width:420px;max-width:92vw;background:linear-gradient(180deg,var(--bg-panel-2),var(--bg-panel));
  border:1px solid var(--border);border-radius:var(--r-panel);box-shadow:var(--shadow-panel);
  padding:var(--sp-5);animation:slideIn .25s var(--ease);
}
.modal-card h3{margin-bottom:var(--sp-4);font-size:var(--fs-16)}

/* ---- toast ---- */
#toastRoot{position:fixed;top:70px;left:50%;transform:translateX(-50%);z-index:300;display:flex;flex-direction:column;gap:8px;align-items:center}
.toast{
  padding:9px 18px;border-radius:var(--r-ctl);font-size:var(--fs-13);
  background:var(--bg-panel-2);border:1px solid var(--border-strong);
  box-shadow:var(--shadow-panel);animation:slideIn .25s var(--ease);max-width:520px;
}
.toast.error{border-color:rgba(255,69,96,.6);color:#FF8A99}
.toast.success{border-color:rgba(0,227,150,.6);color:var(--success)}

/* ---- 空态 / 错误态 / 骨架 ---- */
.empty-state{text-align:center;padding:40px 20px;color:var(--text-3)}
.empty-state .e-ico{font-size:34px;margin-bottom:10px;opacity:.6}
.empty-state .e-btn{margin-top:12px}
.error-state{text-align:center;padding:30px 20px}
.error-state .e-ico{font-size:30px;margin-bottom:8px}
.skeleton{position:relative;overflow:hidden;background:rgba(0,212,255,.06);border-radius:var(--r-card)}
.skeleton::after{
  content:"";position:absolute;inset:0;
  background:linear-gradient(90deg,transparent,rgba(0,212,255,.12),transparent);
  animation:shimmer 1.4s infinite;
}
@keyframes shimmer{from{transform:translateX(-100%)}to{transform:translateX(100%)}}

/* ---- 列表项 ---- */
.list-item{display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:var(--r-ctl);cursor:pointer;transition:var(--t-fast);font-size:var(--fs-13)}
.list-item:hover{background:var(--bg-hover)}
.list-item .li-main{flex:1;min-width:0}
.list-item .li-sub{font-size:var(--fs-11);color:var(--text-3)}
```

- [ ] **Step 2: 创建 `webapp/static/js/components.js`**（通用渲染函数，全部挂 window 供各页面调用）

```javascript
// components.js — 通用组件：toast / confirm / modal / drawer / 空态 / 骨架 / 表格工具
function toast(msg, type){
  const box = document.createElement('div');
  box.className = 'toast ' + (type || '');
  box.textContent = msg;
  document.getElementById('toastRoot').appendChild(box);
  setTimeout(() => { box.style.opacity = '0'; box.style.transition = 'opacity .3s'; }, 2600);
  setTimeout(() => box.remove(), 3000);
}
function toastError(msg){ toast(msg, 'error'); }
function toastSuccess(msg){ toast(msg, 'success'); }

// 确认弹窗：resolve(true/false)
function confirmDialog(text, title){
  return new Promise(resolve => {
    const root = document.getElementById('modalRoot');
    root.classList.remove('hidden');
    root.innerHTML = `
      <div class="modal-mask" id="cfMask">
        <div class="modal-card">
          <h3>${title || '操作确认'}</h3>
          <p style="margin-bottom:24px;color:var(--text-2)">${text}</p>
          <div style="display:flex;gap:10px;justify-content:flex-end">
            <button class="btn" id="cfNo">取消</button>
            <button class="btn danger" id="cfYes">确认</button>
          </div>
        </div>
      </div>`;
    const close = (v) => { root.classList.add('hidden'); root.innerHTML = ''; resolve(v); };
    document.getElementById('cfNo').onclick = () => close(false);
    document.getElementById('cfYes').onclick = () => close(true);
    document.getElementById('cfMask').onclick = (e) => { if(e.target.id === 'cfMask') close(false); };
  });
}

// 表单抽屉：{title, body, wide, onClose} → 返回容器元素；onClose 返回 Promise 时 await 后再销毁
function openDrawer(opts){
  const root = document.getElementById('drawerRoot');
  root.classList.remove('hidden');
  root.innerHTML = `
    <div class="drawer-mask" id="dwMask"></div>
    <div class="drawer ${opts.wide ? 'wide' : ''}">
      <div class="drawer-head"><b>${opts.title}</b>
        <div class="dh-right"><button class="btn sm ghost" id="dwClose">关闭</button></div>
      </div>
      <div class="drawer-body" id="dwBody">${opts.body}</div>
    </div>`;
  const close = async () => {
    if (opts.onClose) { await opts.onClose(); }
    root.classList.add('hidden'); root.innerHTML = '';
  };
  document.getElementById('dwClose').onclick = close;
  document.getElementById('dwMask').onclick = close;
  return document.getElementById('dwBody');
}

// 空态
function emptyState(icon, text, btnText, btnFn){
  return `<div class="empty-state"><div class="e-ico">${icon}</div>
    <div>${text}</div>${btnText ? `<button class="btn sm primary e-btn" id="esBtn">${btnText}</button>` : ''}</div>`;
}
// 错误态（面板内局部错误 + 重试）
function errorState(text, retryFn){
  return `<div class="error-state"><div class="e-ico">⚠️</div>
    <div class="muted">${text || '数据加载失败'}</div>
    <button class="btn sm e-btn" onclick="${retryFn}()">重试</button></div>`;
}
// 骨架屏：count 个面板块
function skeletonHTML(count){
  return Array.from({length: count || 4}, () =>
    `<div class="skeleton" style="height:${Math.round(90 + Math.random()*40)}px;margin-bottom:14px"></div>`).join('');
}

// 状态徽标：task.status → badge
function statusBadge(status){
  const map = {success:'success', failed:'failed', running:'running', pending:'pending'};
  const label = {success:'成功', failed:'失败', running:'运行中', pending:'等待中'}[status] || status;
  return `<span class="badge ${map[status] || 'pending'}">${label}</span>`;
}
// 服务状态点 + 文案
function svcStatusHTML(st){
  const map = {online:['ok','在线'], not_deployed:['gray','未部署'], probe_failed:['warn','探测失败'], error:['bad','异常']};
  const [cls, txt] = map[st.status] || ['gray','未知'];
  return `<span class="dot ${cls}"></span>${txt}`;
}
// 时间格式化：ts(秒) → "MM-DD HH:mm:ss"
function fmtTime(ts){
  if (!ts) return '-';
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
// 相对时间："3 分钟前"
function relTime(ts){
  if (!ts) return '-';
  const diff = Math.max(0, (Date.now()/1000) - ts);
  if (diff < 60) return '刚刚';
  if (diff < 3600) return Math.floor(diff/60) + ' 分钟前';
  if (diff < 86400) return Math.floor(diff/3600) + ' 小时前';
  return Math.floor(diff/86400) + ' 天前';
}
// HTML 转义（所有插值用户/后端数据处使用）
function esc(s){
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
```

- [ ] **Step 3: 验证**

启动 Flask 后访问 `http://127.0.0.1:8099/screen`，浏览器 console 执行：
```javascript
// 手工验证组件函数存在且可用
typeof toast === 'function' && typeof confirmDialog === 'function' && typeof openDrawer === 'function'
```
Expected: `true`；`/static/css/components.css` 返回 200。

- [ ] **Step 4: Commit（若 git 可用）**
```bash
git add webapp/static/css/components.css webapp/static/js/components.js
git commit -m "feat(webapp): 通用组件样式与渲染函数"
```


---

## Task 4: 前端基础设施 — api.js 扩展 + router.js + layout.js + app.js

**Files:**
- Modify: `webapp/static/js/api.js`（追加 poll 封装，保留原函数不动）
- Create: `webapp/static/js/router.js`
- Create: `webapp/static/js/layout.js`
- Create: `webapp/static/js/app.js`（入口）

- [ ] **Step 1: 修改 `webapp/static/js/api.js`** — 在文件末尾追加（不改动既有 `token/currentUser/api/logout` 四函数，兼容旧代码引用）

```javascript
// 追加：轮询封装（错误静默，避免轮询失败刷屏 toast）
async function poll(fn, intervalMs){
  const tick = async () => { try{ await fn(); }catch(e){ /* 轮询失败静默 */ } };
  await tick();
  return setInterval(tick, intervalMs);
}
// 追加：当前用户（含 role 快捷判断）
function isAdmin(){ return currentUser().role === 'admin'; }
function canDeploy(){ return ['admin','operator'].includes(currentUser().role); }
```

- [ ] **Step 2: 创建 `webapp/static/js/router.js`**（hash 路由 + 导航激活 + 面包屑）

```javascript
// router.js — SPA hash 路由
const ROUTES = [
  { path:'dashboard', group:'运维监控', icon:'📊', name:'监控中心', page:'dashboard', crumbs:['运维中心','监控中心'] },
  { path:'devices',   group:'运维监控', icon:'🖥️', name:'项目总览', page:'devices',   crumbs:['运维中心','项目总览'] },
  { path:'deploy',    group:'运维操作', icon:'🚀', name:'发布中心', page:'deploy',    crumbs:['运维操作','发布中心'] },
  { path:'alerts',    group:'运维操作', icon:'🔔', name:'告警中心', page:'alerts',    crumbs:['运维操作','告警中心'] },
  { path:'logs',      group:'运维操作', icon:'📜', name:'日志中心', page:'logs',      crumbs:['运维操作','日志中心'] },
  { path:'config',    group:'系统管理', icon:'⚙️', name:'配置管理', page:'config',    crumbs:['系统管理','配置管理'], adminOnly:true },
];
const PAGE_MODULES = { dashboard, devices, deploy, alerts, logs, config }; // 各 pages/*.js 全局挂载
let currentRoute = null;
let activeTimers = [];

function getRoute(){
  const hash = location.hash.replace(/^#\/?/, '').split('?')[0] || 'dashboard';
  return ROUTES.find(r => r.path === hash) || ROUTES[0];
}
function visibleRoutes(){
  return ROUTES.filter(r => !r.adminOnly || isAdmin());
}
// 路由切换：销毁旧页 → 渲染新页
async function navigate(force){
  const route = getRoute();
  if (!force && currentRoute && currentRoute.path === route.path) return;
  activeTimers.forEach(t => clearInterval(t));
  activeTimers = [];
  if (currentRoute && PAGE_MODULES[currentRoute.page] && PAGE_MODULES[currentRoute.page].destroy){
    try{ PAGE_MODULES[currentRoute.page].destroy(); }catch(e){ console.warn('destroy err', e); }
  }
  currentRoute = route;
  renderNavActive();
  renderBreadcrumb();
  const container = document.getElementById('content');
  container.innerHTML = '<div class="page-inner" id="pageRoot"></div>';
  const root = document.getElementById('pageRoot');
  root.innerHTML = skeletonHTML(5);
  try{
    const timers = await PAGE_MODULES[route.page].render(root);
    (timers || []).forEach(t => activeTimers.push(t));
  }catch(e){
    console.error('page render failed', e);
    root.innerHTML = errorState('页面加载失败：' + e.message, 'navigate');
  }
}
// 导航渲染 + 激活态（app.js 在 DOM 就绪后调用）
function renderSidebar(){
  const sb = document.getElementById('sidebar');
  const groups = {};
  visibleRoutes().forEach(r => { (groups[r.group] = groups[r.group] || []).push(r); });
  const nav = Object.entries(groups).map(([g, items]) => `
    <div class="nav-group">
      <div class="nav-group-title">${g}</div>
      ${items.map(r => `
        <div class="nav-item ${r.path === currentRoute.path ? 'active' : ''}" data-path="${r.path}">
          <span class="nav-ico">${r.icon}</span><span class="nav-txt">${r.name}</span>
          ${r.path === 'alerts' ? '<span class="nav-badge hidden" id="alertBadge">0</span>' : ''}
        </div>`).join('')}
    </div>`).join('');
  sb.innerHTML = `
    <div class="side-logo">
      <div class="logo-ico">⚡</div>
      <div><div class="logo-name">${window.PLATFORM_NAME}</div>
      <div class="logo-sub">DEPLOY CONSOLE</div></div>
    </div>
    <div class="nav-scroll">${nav}</div>
    <div class="side-foot">© 2026 运维平台</div>`;
  sb.querySelectorAll('.nav-item').forEach(item => {
    item.onclick = () => { location.hash = '#/' + item.dataset.path; };
  });
}
function renderNavActive(){
  document.querySelectorAll('#sidebar .nav-item').forEach(i =>
    i.classList.toggle('active', i.dataset.path === currentRoute.path));
}
function renderBreadcrumb(){
  document.getElementById('breadcrumb').innerHTML =
    currentRoute.crumbs.map((c, i) => i === currentRoute.crumbs.length - 1
      ? `<span class="cur">${c}</span>`
      : `<span>${c}</span><span class="sep">/</span>`).join('');
}
// 供 alerts 页更新角标
window.updateAlertBadge = function(count){
  const badge = document.getElementById('alertBadge');
  if (!badge) return;
  if (count > 0){ badge.classList.remove('hidden'); badge.textContent = count > 99 ? '99+' : count; }
  else badge.classList.add('hidden');
};
```

- [ ] **Step 3: 创建 `webapp/static/js/layout.js`**（顶部栏 / 用户菜单 / 搜索 / 跑马灯）

```javascript
// layout.js — 顶部栏 + 用户菜单 + 全局搜索 + 跑马灯
function renderTopbar(){
  const u = currentUser();
  const roleName = {admin:'管理员', operator:'操作员', viewer:'只读'}[u.role] || u.role;
  const tb = document.getElementById('topbar');
  tb.innerHTML = `
    <button class="tb-btn" id="navToggle" title="折叠导航">☰</button>
    <div class="tb-search">
      <span class="ico">🔍</span>
      <input id="globalSearch" placeholder="搜索服务 / 任务 #id ...">
      <div class="drop-panel" id="searchDrop"></div>
    </div>
    <button class="tb-btn" id="refreshBtn" title="手动刷新监控">⟳</button>
    <div class="user-chip" id="userChip">
      <div class="user-avatar">${esc((u.username || '?').slice(0,1).toUpperCase())}</div>
      <div class="user-meta"><div class="u-name">${esc(u.username || '未知')}</div>
      <div class="u-role">${roleName}</div></div>
    </div>
    <div id="userMenu">
      <button class="btn ghost sm" id="menuPwd" style="width:100%;justify-content:flex-start">🔑 修改密码</button>
      <button class="btn ghost sm" id="menuLogout" style="width:100%;justify-content:flex-start">⏻ 退出登录</button>
    </div>`;
  const toggleNav = () => document.getElementById('sidebar').classList.toggle('collapsed');
  document.getElementById('navToggle').onclick = toggleNav;
  document.getElementById('refreshBtn').onclick = async () => {
    try{ await api('/api/monitor/refresh', {method:'POST'}); toastSuccess('监控探测已刷新'); }
    catch(e){ toastError(e.message); }
  };
  // 用户菜单
  const chip = document.getElementById('userChip');
  const menu = document.getElementById('userMenu');
  chip.onclick = (e) => {
    e.stopPropagation();
    const r = chip.getBoundingClientRect();
    menu.style.display = menu.style.display === 'block' ? 'none' : 'block';
    menu.style.top = (r.bottom + 6) + 'px';
    menu.style.right = (window.innerWidth - r.right) + 'px';
  };
  document.addEventListener('click', () => { menu.style.display = 'none'; });
  document.getElementById('menuLogout').onclick = logout;
  document.getElementById('menuPwd').onclick = () => openPwdModal();
  // 全局搜索
  const input = document.getElementById('globalSearch');
  const drop = document.getElementById('searchDrop');
  let searchTimer = null;
  input.addEventListener('input', () => {
    clearTimeout(searchTimer);
    const kw = input.value.trim();
    if (!kw){ drop.style.display = 'none'; return; }
    searchTimer = setTimeout(async () => {
      try{
        const tasks = await api('/api/tasks?limit=100').catch(() => []);
        const projects = await api('/api/projects').catch(() => []);
        const tList = tasks.filter(t => t.name.includes(kw) || String(t.id) === kw).slice(0, 6);
        const sList = [];
        for (const p of projects){
          const svcs = await api(`/api/projects/${p.id}/services`).catch(() => []);
          svcs.filter(s => s.name.includes(kw)).slice(0, 4).forEach(s =>
            sList.push({label: s.name, sub: `${p.name} 服务`, goto: () => {
              localStorage.setItem('dev_filter_project', p.id); location.hash = '#/devices';
            }}));
        }
        const rows = [
          ...tList.map(t => ({label: `#${t.id} ${t.name}`, sub: t.env || '', goto: () => openTaskLogDrawer(t.id)})),
          ...sList,
        ];
        if (!rows.length){ drop.innerHTML = '<div class="drop-item">无匹配结果</div>'; }
        else {
          drop.innerHTML = rows.map(r => `<div class="drop-item">${esc(r.label)}<span class="k">${esc(r.sub)}</span></div>`).join('');
          [...drop.children].forEach((el, i) => el.onclick = () => { drop.style.display='none'; input.value=''; rows[i].goto(); });
        }
        const r = input.getBoundingClientRect();
        drop.style.top = (r.bottom + 6) + 'px';
        drop.style.left = r.left + 'px';
        drop.style.width = Math.max(280, r.width) + 'px';
        drop.style.display = 'block';
      }catch(e){ /* 搜索失败静默 */ }
    }, 300);
  });
  input.addEventListener('keydown', e => { if(e.key === 'Escape'){ drop.style.display = 'none'; input.value=''; } });
  document.addEventListener('click', (e) => { if(!input.contains(e.target) && !drop.contains(e.target)) drop.style.display='none'; });
}
// 修改密码弹窗（首登强制改密复用；改密成功前业务 API 均 403）
function openPwdModal(){
  const root = document.getElementById('modalRoot');
  root.classList.remove('hidden');
  root.innerHTML = `
    <div class="modal-mask" id="pwdMask">
      <div class="modal-card">
        <h3>修改密码</h3>
        <div class="field" style="margin-bottom:12px"><label>原密码</label>
          <input type="password" id="pwdOld" placeholder="原密码"></div>
        <div class="field" style="margin-bottom:20px"><label>新密码（至少 6 位）</label>
          <input type="password" id="pwdNew" placeholder="新密码"></div>
        <div style="display:flex;gap:10px;justify-content:flex-end">
          <button class="btn" id="pwdCancel">取消</button>
          <button class="btn primary" id="pwdOk">确认修改</button>
        </div>
      </div>
    </div>`;
  const close = () => { root.classList.add('hidden'); root.innerHTML = ''; };
  document.getElementById('pwdMask').onclick = (e) => { if(e.target.id === 'pwdMask') close(); };
  document.getElementById('pwdCancel').onclick = close;
  document.getElementById('pwdOk').onclick = async () => {
    try{
      await api('/api/auth/password', {method:'POST', body: JSON.stringify({
        old_password: document.getElementById('pwdOld').value,
        new_password: document.getElementById('pwdNew').value})});
      const u = currentUser(); u.must_change_password = 0;
      localStorage.setItem('user', JSON.stringify(u));
      close(); toastSuccess('密码已修改');
    }catch(e){ toastError(e.message); }
  };
}
// 跑马灯：渲染最近发布事件
function renderMarquee(latest){
  const bar = document.getElementById('marqueeBar');
  if (bar.dataset.closed === '1') return;
  bar.classList.remove('hidden');
  bar.innerHTML = latest
    ? `<span class="mq-label">最近发布</span><span class="mq-text">#${latest.id} ${esc(latest.name)}（${esc(latest.env||'prod')}）→ ${esc(latest.version||latest.previous_version||'-')}，${esc(latest.status)}，${fmtTime(latest.started_at)}</span>
       <button class="mq-close" id="mqClose">✕</button>`
    : `<span class="mq-label">最近发布</span><span class="mq-text">等待发布事件...</span>
       <button class="mq-close" id="mqClose">✕</button>`;
  document.getElementById('mqClose').onclick = () => { bar.classList.add('hidden'); bar.dataset.closed = '1'; };
}
```

- [ ] **Step 4: 创建 `webapp/static/js/app.js`**（SPA 入口）

```javascript
// app.js — SPA 入口
(function init(){
  if (!token()){ location.href = '/'; return; }
  renderSidebar();
  renderTopbar();
  if (currentUser().must_change_password) openPwdModal();
  navigate(true);
  window.addEventListener('hashchange', () => navigate(false));
  // 底部跑马灯：3s 轮询最新任务
  poll(async () => {
    const tasks = await api('/api/tasks?limit=1');
    renderMarquee(tasks[0]);
  }, 3000);
})();
```

- [ ] **Step 5: 验证**

浏览器访问 `http://127.0.0.1:8099/screen`：
- Expected: 左侧导航渲染（运维监控/运维操作两组；admin 登录含系统管理）、顶部栏含搜索框与用户 chip、面包屑显示"运维中心 / 监控中心"
- Expected: 无 token 访问自动跳 `/`
- console 无 JS 语法错误（页面模块 `dashboard` 未定义导致的报错属预期——Task 6 实现）

- [ ] **Step 6: Commit（若 git 可用）**
```bash
git add webapp/static/js/api.js webapp/static/js/router.js webapp/static/js/layout.js webapp/static/js/app.js
git commit -m "feat(webapp): SPA 路由与布局基础设施"
```

---

## Task 5: 登录页重设计 — login.html 重写

**Files:**
- Modify: `webapp/templates/login.html`（整体重写）

- [ ] **Step 1: 重写 `webapp/templates/login.html`**（深蓝渐变 + 网格光效 + 玻璃拟态登录卡，规格 §11）

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>智能运维平台 · 登录</title>
<meta name="description" content="智能运维平台运维控制台登录">
<link rel="stylesheet" href="/static/css/tokens.css">
<link rel="stylesheet" href="/static/css/base.css">
<style>
  body.login-body{display:flex;align-items:center;justify-content:center;overflow:hidden}
  .login-wrap{width:400px;max-width:92vw;position:relative;z-index:1}
  .login-card{
    background:linear-gradient(160deg,rgba(17,34,65,.72),rgba(13,27,51,.88));
    border:1px solid var(--border);border-radius:16px;box-shadow:var(--shadow-panel);
    backdrop-filter:blur(14px);padding:44px 40px 38px;text-align:center;
  }
  .login-logo{
    width:56px;height:56px;margin:0 auto 14px;border-radius:14px;
    background:linear-gradient(135deg,var(--primary),#0066FF);display:flex;align-items:center;justify-content:center;
    font-size:28px;box-shadow:var(--shadow-glow);
  }
  .login-title{font-size:24px;font-weight:700;letter-spacing:1px}
  .login-sub{font-size:11px;color:var(--text-3);letter-spacing:3px;margin:6px 0 32px}
  .login-card .field{margin-bottom:16px;text-align:left}
  .login-card .field input{
    width:100%;padding:11px 14px;border:1px solid var(--border);border-radius:8px;
    background:rgba(0,212,255,.05);color:var(--text-1);outline:none;transition:var(--t-fast);
  }
  .login-card .field input:focus{border-color:var(--primary);box-shadow:var(--shadow-glow)}
  .login-card .field input::placeholder{color:var(--text-3)}
  .login-btn{
    width:100%;padding:12px;border:none;border-radius:8px;cursor:pointer;font-size:15px;font-weight:600;
    background:linear-gradient(135deg,var(--primary),#0090FF);color:#04121F;transition:var(--t-fast);
  }
  .login-btn:hover{box-shadow:var(--shadow-glow);filter:brightness(1.1)}
  .login-btn:disabled{opacity:.6;cursor:wait}
  .login-err{display:none;margin:0 0 12px;padding:8px 12px;border-radius:6px;
    background:rgba(255,69,96,.12);border:1px solid rgba(255,69,96,.45);color:#FF8A99;font-size:13px;text-align:left}
  .login-foot{font-size:11px;color:var(--text-3);margin-top:20px;letter-spacing:1px}
</style>
</head>
<body class="login-body">
<div class="login-wrap">
  <div class="login-card">
    <div class="login-logo">⚡</div>
    <div class="login-title">智能运维平台</div>
    <div class="login-sub">DEPLOY &amp; OPERATIONS CONSOLE</div>
    <div class="login-err" id="err"></div>
    <div class="field"><input id="username" placeholder="用户名" autocomplete="username"></div>
    <div class="field"><input id="password" type="password" placeholder="密码" autocomplete="current-password"></div>
    <button class="login-btn" id="loginBtn">登 录</button>
    <div class="login-foot">INTERNAL OPS TOOL · AUTHORIZED ACCESS ONLY</div>
  </div>
</div>
<script>
(function(){
  const btn = document.getElementById('loginBtn');
  function showErr(msg){ const e = document.getElementById('err'); e.textContent = msg; e.style.display = 'block'; }
  async function login(){
    const username = document.getElementById('username').value.trim();
    const password = document.getElementById('password').value;
    if(!username || !password){ showErr('请输入用户名和密码'); return; }
    btn.disabled = true; btn.textContent = '登录中...';
    try{
      const resp = await fetch('/api/auth/login', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({username, password})});
      const data = await resp.json().catch(() => ({}));
      if(resp.ok){
        localStorage.setItem('token', data.token);
        localStorage.setItem('user', JSON.stringify(data.user));
        location.href = '/screen#/dashboard';
      }else{
        showErr(data.error || '登录失败，请重试');
      }
    }catch(e){
      showErr('网络异常，请检查服务是否可用');
    }finally{
      btn.disabled = false; btn.textContent = '登 录';
    }
  }
  btn.onclick = login;
  document.getElementById('password').addEventListener('keydown', e => { if(e.key === 'Enter') login(); });
})();
</script>
</body>
</html>
```

> 说明：登录页独立于 SPA；内联样式仅限登录页自身。品牌名此处静态写死，与 `screen.html` 的 `window.PLATFORM_NAME` 保持一致。

- [ ] **Step 2: 验证**

浏览器访问 `http://127.0.0.1:8099/`：
- Expected: 深蓝渐变玻璃拟态登录卡；空提交显示"请输入用户名和密码"；错误密码显示后端 error 文案；正确登录跳 `/screen#/dashboard`
- 登录后顶部栏用户 chip 显示用户名与角色名

- [ ] **Step 3: Commit（若 git 可用）**
```bash
git add webapp/templates/login.html
git commit -m "feat(webapp): 深蓝科技风登录页重设计"
```

---

## Task 6: 监控大屏 — pages/dashboard.js + pages/dashboard.css

**Files:**
- Create: `webapp/static/css/pages/dashboard.css`
- Create: `webapp/static/js/pages/dashboard.js`

**页面布局**（规格 §6）：

```
┌─────────────────────────────────────────────────────┐
│ KPI 条：项目 / 服务 / 环境 / 服务器 / 任务 / 成功率      │
├───────────┬───────────────────┬──────────────────────┤
│ 雷达健康    │   发布趋势          │   最近发布（右侧列表）   │
│ 指数       │  (7日/30日切换)     │                       │
├───────────┴───────────────────┴──────────────────────┤
│ 机房环境检测：温度/湿度/功耗/风扇 4 仪表盘                 │
├───────────────────────────────────────────────────────┤
│ 服务器状态表                                            │
└───────────────────────────────────────────────────────┘
```

- [ ] **Step 1: 创建 `webapp/static/css/pages/dashboard.css`**

```css
/* dashboard.css — 监控大屏 */
.kpi-row{display:grid;grid-template-columns:repeat(6,1fr);gap:var(--sp-3);margin-bottom:var(--sp-4)}
.kpi-card{
  background:linear-gradient(180deg,rgba(17,34,65,.6),rgba(13,27,51,.9));
  border:1px solid var(--border);border-radius:var(--r-card);padding:14px 16px;
  position:relative;overflow:hidden;transition:var(--t-smooth);
}
.kpi-card:hover{border-color:var(--border-strong);transform:translateY(-2px)}
.kpi-card .k-ico{position:absolute;right:12px;top:12px;font-size:22px;opacity:.35}
.kpi-card .k-num{font-family:var(--font-num);font-size:28px;font-weight:700;line-height:1.2}
.kpi-card .k-label{font-size:12px;color:var(--text-2);margin-top:2px}
.kpi-card::after{
  content:"";position:absolute;left:0;right:0;bottom:0;height:2px;
  background:linear-gradient(90deg,var(--primary),transparent);opacity:.7;
}
.dash-grid{display:grid;grid-template-columns:340px 1fr 300px;gap:var(--sp-4);margin-bottom:var(--sp-4)}
.dash-grid .chart-box{min-height:300px}
.env-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:var(--sp-3);margin-bottom:var(--sp-4)}
.env-card{height:190px}
.demo-tag{
  display:inline-block;padding:1px 8px;border-radius:var(--r-full);font-size:10px;
  background:rgba(254,176,25,.15);color:var(--warning);border:1px solid rgba(254,176,25,.4);margin-left:8px;
}
.trend-tabs{display:flex;gap:6px}
.trend-tabs .tab-btn{padding:3px 12px;font-size:12px}
.rlist .list-item{border-bottom:1px solid rgba(0,212,255,.06)}
.rlist .list-item:last-child{border-bottom:none}
.tbl-wrap{overflow-x:auto}
```

> 注意：上面代码块**不要**包含任何 `</style>` 闭合标签——这是纯 CSS 文件。

- [ ] **Step 2: 创建 `webapp/static/js/pages/dashboard.js`**（模块导出 render/destroy）

```javascript
// pages/dashboard.js — 监控大屏（KPI/雷达/机房/趋势/服务器/最近发布）
(function(){
  let charts = {};      // ECharts 实例
  let envTimer = null;  // 机房仪表盘模拟刷新
  let dataTimer = null; // 大屏数据轮询
  let overview = null;  // dashboard/overview 缓存
  let trendMode = 7;

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

  // ---- 健康指数（规格 §6.2：真实加权 + 示例兜底）----
  function healthRadar(o, mon){
    const svcs = mon && mon.services ? Object.values(mon.services) : [];
    const online = svcs.filter(s => s.status === 'online').length;
    const rate = o.success_rate === null ? 0.92 : o.success_rate / 100;
    const stability = svcs.length ? Math.min(1, (online / svcs.length) * 1.25) : 0.85;
    const demo = svcs.length === 0;   // 无真实服务数据 → 标注演示
    return { demo, values: [
      Math.round(stability * 100), 88, 90, 95, Math.round(rate * 100), 87] };
  }

  // ---- 机房环境（规格 §6.3：示例 + 扰动）----
  const envSeed = { temp: 24.5, humi: 48, power: 62, fan: 76 };
  function envValues(){
    const jitter = v => Math.max(5, Math.min(99, v + (Math.random() - 0.5) * 4));
    return { temp: jitter(envSeed.temp), humi: jitter(envSeed.humi),
             power: jitter(envSeed.power), fan: jitter(envSeed.fan) };
  }
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

  // ---- 服务器状态表（规格 §6.5）----
  function serverTable(servers){
    if (!servers || !servers.length){
      return `<div class="panel"><div class="panel-title">服务器状态</div>
        <div class="empty-state"><div class="e-ico">🖥️</div>
        <div>实时探测未启用或暂无数据</div>
        <div class="weak" style="margin-top:6px;font-size:12px">启动时加 --monitor-interval 30 启用探测调度</div></div></div>`;
    }
    return `<div class="panel"><div class="panel-title">服务器状态 <span class="muted" style="font-weight:400;font-size:12px">（${servers.length} 台）</span></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr>
        <th>主机</th><th>SSH 状态</th><th>容器数</th><th>最后探测</th></tr></thead><tbody>
        ${servers.map(s => `<tr>
          <td><span class="dot ${s.ssh_ok ? 'ok' : 'bad'}"></span><span class="num">${esc(s.host)}</span></td>
          <td>${s.ssh_ok ? '<span class="ok">连通</span>' : '<span class="err">失败</span>'}</td>
          <td class="num">${s.containers ?? '-'}</td>
          <td class="muted">${s.error ? esc(s.error) : fmtTime(s.ts)}</td></tr>`).join('')}
      </tbody></table></div></div>`;
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

  // ---- 模块导出（router 约定：render 返回定时器数组）----
  window.dashboard = {
    render: async function(root){
      root.innerHTML = skeletonHTML(6);
      const load = async () => {
        try{
          overview = await api('/api/dashboard/overview');
          let mon = null;
          try{
            const projects = await api('/api/projects');
            if (projects.length){
              const envs = await api(`/api/projects/${projects[0].id}/environments`);
              const env = (envs.find(e => e.is_default) || envs[0]);
              if (env) mon = await api(`/api/monitor/overview?project_id=${projects[0].id}&env=${env.name}`).catch(() => null);
            }
          }catch(e){ mon = null; }
          renderAll(mon);
        }catch(e){ root.innerHTML = errorState('监控数据加载失败：' + e.message, 'navigate'); }
      };
      await load();
      dataTimer = setInterval(load, 5000);
      return [dataTimer];
    },
    destroy: function(){
      Object.values(charts).forEach(c => { try{ c.dispose(); }catch(e){} });
      charts = {};
      if (envTimer) clearInterval(envTimer);
      if (dataTimer) clearInterval(dataTimer);
      envTimer = dataTimer = null;
    },
  };

  function renderAll(mon){
    const root = document.getElementById('pageRoot');
    const radar = healthRadar(overview, mon);
    const envs = envValues();
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
      <div class="env-grid">
        ${[['温度','℃',30,'#FF7A45'],['湿度','%',100,'#00D4FF'],['功耗','kW',100,'#FEB019'],['风扇','%',100,'#00E396']]
          .map(([name, unit, max, color], i) => `
          <div class="panel env-card"><div class="panel-title" style="font-size:13px">${name}<span class="demo-tag">演示</span></div>
            <div id="gauge${i}" style="height:150px"></div></div>`).join('')}
      </div>
      ${serverTable(mon && mon.servers)}
    `;
    // 趋势 tab 切换
    root.querySelectorAll('.trend-tabs .tab-btn').forEach(b => b.onclick = () => {
      trendMode = Number(b.dataset.days);
      document.querySelectorAll('.trend-tabs .tab-btn').forEach(x => x.classList.toggle('active', x === b));
      renderTrend();
    });
    // 任务点击 → 日志抽屉（Task 8 提供 openTaskLogDrawer）
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
    // 机房 4 仪表盘
    const envSeries = [['温度', envs.temp, '℃', 30, '#FF7A45'], ['湿度', envs.humi, '%', 100, '#00D4FF'],
                       ['功耗', envs.power, 'kW', 100, '#FEB019'], ['风扇', envs.fan, '%', 100, '#00E396']];
    envSeries.forEach(([name, v, unit, max, color], i) => {
      charts['g' + i] = echarts.init(document.getElementById('gauge' + i));
      charts['g' + i].setOption(gaugeOption(name, v, unit, max, color));
    });
    // 5s 模拟扰动
    if (envTimer) clearInterval(envTimer);
    envTimer = setInterval(() => {
      const nv = envValues();
      envSeries.forEach(([name, , unit, max, color], i) => {
        charts['g' + i] && charts['g' + i].setOption(
          gaugeOption(name, nv[['temp','humi','power','fan'][i]], unit, max, color));
      });
    }, 5000);
  }
  function renderTrend(){
    const trend = trendMode === 7 ? (overview.trend7 || {}) : (overview.trend30 || {});
    charts.trend.setOption(trendOption(trend));
  }
})();
```

> 说明：健康指数算法最终由 Task 9 的 `data.js` 提供 `computeHealth()`（规格 §8 文件结构）；Task 9 会同步把本文件 `healthRadar` 替换为 `computeHealth(overview, mon)` 调用，输出结构保持一致（`{demo, values}`）。

- [ ] **Step 3: 验证**

浏览器访问 `http://127.0.0.1:8099/screen#/dashboard`：
- Expected: KPI 条 6 项渲染真实数据；雷达图/趋势图/4 仪表盘渲染；5s 后仪表盘数值轻微扰动；服务器状态表（无数据时显示"实时探测未启用"提示条）
- Expected: 切到 `#/devices` 再切回 `#/dashboard`，图表重新初始化且 console 无 `There is a chart instance already initialized` 警告（destroy 生效）
- 浏览器窗口缩放后图表不塌陷（容器高度固定）

- [ ] **Step 4: Commit（若 git 可用）**
```bash
git add webapp/static/css/pages/dashboard.css webapp/static/js/pages/dashboard.js
git commit -m "feat(webapp): 监控中心大屏（KPI/雷达/机房/趋势/服务器）"
```


---

## Task 7: 项目总览 — pages/devices.js

**Files:**
- Create: `webapp/static/js/pages/devices.js`

**页面结构**（规格 §5 项目总览）：项目 Tab + 环境 chip + 服务卡片墙（状态/版本/实例/发布入口）+ 服务器列表。

- [ ] **Step 1: 创建 `webapp/static/js/pages/devices.js`**（复用 `openTaskLogDrawer` 与发布抽屉逻辑）

```javascript
// pages/devices.js — 项目总览（项目/环境切换 + 服务卡片墙 + 发布抽屉 + 服务器列表）
(function(){
  let state = { projects: [], currentProject: null, envs: [], currentEnv: null };
  let timer = null;
  let monitorCache = null;
  let tasksCache = [];

  // 服务卡片：状态灯优先级 发布中 > 探测结果（沿用旧 dotClass 语义）
  function dotClass(svc, runningTask){
    if (runningTask) return 'warn';
    const st = svc.status;
    if (st === 'online') return 'ok';
    if (st === 'error') return 'bad';
    if (st === 'probe_failed') return 'warn';
    return 'gray';
  }
  function svcLabel(st, run){
    if (run) return '发布中';
    const map = {online:'在线', error:'异常', not_deployed:'未部署', probe_failed:'探测失败'};
    return map[st.status] || '未知';
  }

  async function loadProjects(){
    state.projects = await api('/api/projects');
    // 支持全局搜索跳转指定项目（layout.js 写入 dev_filter_project）
    const filter = localStorage.getItem('dev_filter_project');
    if (filter) localStorage.removeItem('dev_filter_project');
    if (!state.currentProject && state.projects.length){
      const want = filter ? state.projects.find(p => p.id === Number(filter)) : null;
      await switchProject(want ? want.id : state.projects[0].id);
    }
  }
  async function switchProject(pid){
    const p = state.projects.find(x => x.id === pid);
    if (!p) return;
    state.currentProject = p;
    state.envs = await api(`/api/projects/${pid}/environments`);
    state.currentEnv = (state.envs.find(e => e.is_default) || state.envs[0] || {}).name || null;
    render();
  }
  function switchEnv(name){ state.currentEnv = name; render(); }

  async function loadData(){
    if (!state.currentProject) return;
    const [services, monitor] = await Promise.all([
      api(`/api/projects/${state.currentProject.id}/services`),
      api(`/api/monitor/overview?project_id=${state.currentProject.id}&env=${state.currentEnv||''}`).catch(() => null),
    ]);
    monitorCache = monitor;
    tasksCache = await api(`/api/tasks?project_id=${state.currentProject.id}&limit=20`).catch(() => []);
    renderCards(services);
    renderServers();
  }
  function renderCards(services){
    const svcStatus = (monitorCache && monitorCache.services) || {};
    const running = tasksCache.filter(t => t.status === 'running');
    const root = document.getElementById('pageRoot');
    const wall = root.querySelector('#svcWall');
    if (!wall) return;
    wall.innerHTML = services.map(s => {
      const st = svcStatus[s.name] || {};
      const run = running.find(r => r.type === 'project' && r.name === s.name && r.env === state.currentEnv);
      const ver = st.version || '—';
      const inst = st.instances !== undefined && st.instances !== -1 ? st.instances : '—';
      const ts = st.ts ? fmtTime(st.ts) : '';
      const dotCls = dotClass(st, run);
      return `<div class="svc-card ${run ? 'publishing' : ''}" data-id="${s.id}">
        <div class="svc-head"><span class="dot ${dotCls}"></span><b class="ellipsis">${esc(s.name)}</b>
          ${s.enabled ? '' : '<span class="weak" style="font-size:11px">(停用)</span>'}</div>
        <div class="svc-meta">
          <div><span class="weak">镜像</span> <span class="num">${esc(ver)}</span></div>
          <div><span class="weak">实例</span> <span class="num">${inst}</span>
               <span class="weak" style="margin-left:10px">状态</span> ${svcLabel(st, run)}</div>
          <div><span class="weak">端口</span> <span class="num">${s.health_port || '-'}</span>
               ${ts ? `<span class="weak" style="margin-left:10px">探测</span> ${ts}` : ''}</div>
        </div>
        <div class="svc-foot">
          ${canDeploy() ? `<button class="btn sm primary" data-pub="${s.id}">发布</button>
          <button class="btn sm ghost" data-roll="${s.id}">回滚</button>` : '<span class="weak" style="font-size:11px">只读账号</span>'}
        </div>
      </div>`;
    }).join('') || emptyState('📭', '该项目暂无服务');
    wall.querySelectorAll('[data-pub]').forEach(b => b.onclick = () => openDeployDrawer(Number(b.dataset.pub), false));
    wall.querySelectorAll('[data-roll]').forEach(b => b.onclick = () => openDeployDrawer(Number(b.dataset.roll), true));
  }
  function renderServers(){
    const root = document.getElementById('pageRoot');
    const box = root.querySelector('#serverBox');
    const servers = (monitorCache && monitorCache.servers) || [];
    box.innerHTML = servers.length
      ? `<div class="panel"><div class="panel-title">服务器列表（${servers.length}）</div>
          <div class="tbl-wrap"><table class="tbl"><thead><tr>
            <th>主机</th><th>SSH</th><th>容器数</th><th>最后探测</th></tr></thead><tbody>
            ${servers.map(s => `<tr>
              <td><span class="dot ${s.ssh_ok ? 'ok' : 'bad'}"></span><span class="num">${esc(s.host)}</span></td>
              <td>${s.ssh_ok ? '<span class="ok">连通</span>' : '<span class="err">失败</span>'}</td>
              <td class="num">${s.containers ?? '-'}</td>
              <td class="muted">${s.error ? esc(s.error) : fmtTime(s.ts)}</td></tr>`).join('')}
          </tbody></table></div></div>`
      : `<div class="panel"><div class="panel-title">服务器列表</div>
          <div class="empty-state"><div class="e-ico">🖥️</div><div>实时探测未启用或暂无数据</div></div></div>`;
  }

  // ---- 发布抽屉（规格 §9：卡片 → 抽屉表单 → 提交跳发布中心）----
  async function openDeployDrawer(sid, rollback){
    const pid = state.currentProject.id;
    const [services, pipelines] = await Promise.all([
      api(`/api/projects/${pid}/services`),
      api(`/api/projects/${pid}/pipelines`),
    ]);
    const svc = services.find(s => s.id === sid);
    const envs = state.envs;
    const body = openDrawer({ title: (rollback ? '回滚 ' : '发布 ') + (svc ? svc.name : ''), wide:false, body: `
      <div class="form-grid">
        <div class="field full"><label>发布类型</label>
          <select id="dpType">
            <option value="project" ${!rollback ? 'selected' : ''}>服务发布</option>
            <option value="pipeline" ${rollback ? 'selected' : ''}>流水线发布</option>
          </select></div>
        <div class="field full"><label>服务 / 流水线</label>
          <select id="dpRef">${rollback
            ? pipelines.map(p => `<option value="pipeline:${p.id}">流水线 · ${esc(p.name)}</option>`).join('')
            : services.map(s => `<option value="project:${s.id}" ${s.id === sid ? 'selected' : ''}>服务 · ${esc(s.name)}</option>`).join('')}
          </select></div>
        <div class="field"><label>环境</label>
          <select id="dpEnv">${envs.map(e => `<option ${e.name === state.currentEnv ? 'selected' : ''}>${esc(e.name)}</option>`).join('')}</select></div>
        <div class="field"><label>版本号（可留空）</label><input id="dpVersion" placeholder="如 1.0.3"></div>
        <div class="field"><label>回滚目标版本（回滚时填写）</label><input id="dpPrevVersion" placeholder="回滚到的版本"></div>
        <div class="field check"><label><input type="checkbox" id="dpAuto"> 自动版本号</label></div>
        <div class="field check"><label><input type="checkbox" id="dpSkip"> 跳过构建</label></div>
      </div>
      <div style="display:flex;gap:10px;margin-top:20px">
        <button class="btn primary flex-1" id="dpSubmit">${rollback ? '确认回滚' : '确认发布'}</button>
      </div>` });
    document.getElementById('dpSubmit').onclick = async () => {
      const typeSel = document.getElementById('dpType').value;
      const ref = document.getElementById('dpRef').value;
      const env = document.getElementById('dpEnv').value;
      const version = document.getElementById('dpVersion').value.trim();
      const previous_version = document.getElementById('dpPrevVersion').value.trim();
      const auto_version = document.getElementById('dpAuto').checked;
      const skip_build = document.getElementById('dpSkip').checked;
      try{
        if (auto_version && (version || previous_version)) throw new Error('自动版本与显式版本互斥');
        if (rollback && !version && !previous_version) throw new Error('回滚需指定版本');
        const [t, rid] = ref.split(':');
        const resp = await api('/api/tasks', {method:'POST', body: JSON.stringify({
          project_id: pid, type: t, ref_id: Number(rid), env, version, previous_version,
          rollback, auto_version, skip_build})});
        toastSuccess(`任务 #${resp.id} 已创建`);
        document.getElementById('drawerRoot').classList.add('hidden');
        location.hash = '#/deploy';
      }catch(e){ toastError(e.message); }
    };
  }

  // ---- 模块导出 ----
  window.devices = {
    render: async function(root){
      root.innerHTML = skeletonHTML(4);
      await loadProjects();
      return [];
    },
    destroy: function(){ if (timer) clearInterval(timer); timer = null; },
  };

  function render(){
    const root = document.getElementById('pageRoot');
    root.innerHTML = `
      <div class="panel" style="margin-bottom:var(--sp-4);padding:12px 16px">
        <div class="flex gap-3" style="flex-wrap:wrap">
          <span class="weak" style="font-size:12px">项目</span>
          ${state.projects.map(p => `<span class="chip ${p.id === state.currentProject.id ? 'active' : ''}" data-pid="${p.id}">${esc(p.name)}</span>`).join('')}
          <span style="width:1px;height:20px;background:var(--border);margin:0 4px"></span>
          <span class="weak" style="font-size:12px">环境</span>
          ${state.envs.map(e => `<span class="chip ${e.name === state.currentEnv ? 'active' : ''}" data-env="${esc(e.name)}">${esc(e.name)}</span>`).join('')}
        </div>
      </div>
      <div id="svcWall" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:var(--sp-4);margin-bottom:var(--sp-4)"></div>
      <div id="serverBox"></div>`;
    root.querySelectorAll('.chip[data-pid]').forEach(c => c.onclick = () => switchProject(Number(c.dataset.pid)));
    root.querySelectorAll('.chip[data-env]').forEach(c => c.onclick = () => switchEnv(c.dataset.env));
    await loadData();
    // 3s 轮询卡片状态（发布中任务状态变化）
    if (timer) clearInterval(timer);
    timer = setInterval(async () => { try{ await loadData(); }catch(e){} }, 3000);
  }
})();
```

- [ ] **Step 2: 补充 `webapp/static/css/pages/devices.css`** 与 `screen.html` 引用（服务卡片样式；如未建文件则并入 `components.css` 的 `.svc-card`）

```css
/* pages/devices.css — 项目总览（若选择并入 components.css 则忽略本文件） */
.svc-card{
  background:linear-gradient(180deg,rgba(17,34,65,.6),rgba(13,27,51,.9));
  border:1px solid var(--border);border-radius:var(--r-card);padding:14px 16px;
  transition:var(--t-smooth);display:flex;flex-direction:column;gap:10px;
}
.svc-card:hover{border-color:var(--border-strong);transform:translateY(-2px)}
.svc-card.publishing{border-color:rgba(255,122,69,.6);box-shadow:0 0 12px rgba(255,122,69,.18)}
.svc-head{display:flex;align-items:center;gap:8px;font-size:var(--fs-14)}
.svc-meta{font-size:var(--fs-12);color:var(--text-1);line-height:1.9}
.svc-meta .weak{color:var(--text-3)}
.svc-foot{display:flex;gap:8px;margin-top:2px}
```

> 实现决策：为控制文件数，将 `.svc-card` 系列样式追加到 `webapp/static/css/components.css` 末尾（页面特有但体积小）；不新建 `pages/devices.css`。`screen.html` 无需新增引用。

- [ ] **Step 3: 验证**

浏览器访问 `http://127.0.0.1:8099/screen#/devices`：
- Expected: 项目 chip 与环境 chip 渲染；切项目/环境后卡片墙与服务器列表刷新；卡片显示状态点/镜像版本/实例数/端口
- Expected: 点击"发布"弹出抽屉；填环境/版本提交后 toast 提示任务已创建并跳转 `#/deploy`（Task 8 未实现前跳转后显示错误态可接受）
- Expected: 无服务项目显示"该项目暂无服务"空态
- Expected: viewer 角色不显示"发布/回滚"按钮（只读账号提示）

- [ ] **Step 4: Commit（若 git 可用）**
```bash
git add webapp/static/js/pages/devices.js webapp/static/css/components.css
git commit -m "feat(webapp): 项目总览（卡片墙+发布抽屉）"
```

---

## Task 8: 发布中心 — pages/deploy.js + pages/deploy.css

**Files:**
- Create: `webapp/static/js/pages/deploy.js`
- Create: `webapp/static/css/pages/deploy.css`

**页面结构**（规格 §9）：任务列表表格（新建按钮 + 状态筛选）+ 任务详情抽屉（日志流 + 终止）。

- [ ] **Step 1: 创建 `webapp/static/css/pages/deploy.css`**

```css
/* deploy.css — 发布中心 */
.deploy-toolbar{display:flex;align-items:center;gap:var(--sp-3);margin-bottom:var(--sp-4);flex-wrap:wrap}
.deploy-toolbar .chip{font-size:12px}
.task-tbl .num{font-family:var(--font-num)}
.task-name{cursor:pointer;color:var(--text-1);transition:var(--t-fast)}
.task-name:hover{color:var(--primary)}
/* 任务详情抽屉 */
.log-console{
  background:#050B18;border:1px solid var(--border);border-radius:var(--r-ctl);
  padding:12px;height:calc(100vh - 370px);overflow-y:auto;
  font-family:var(--font-num);font-size:12px;line-height:1.7;white-space:pre-wrap;word-break:break-all;
}
.log-console .tail{color:var(--primary);animation:blink 1.2s infinite}
.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px 20px;margin-bottom:16px;font-size:13px}
.detail-grid .dg-label{color:var(--text-3);font-size:12px}
.task-drawer-actions{display:flex;gap:10px;margin-top:12px}
```

- [ ] **Step 2: 创建 `webapp/static/js/pages/deploy.js`**

```javascript
// pages/deploy.js — 发布中心（任务列表 + 新建 + 详情日志流 + 终止）
(function(){
  let timer = null;
  let statusFilter = 'all';
  let tasks = [];

  async function loadTasks(){
    tasks = await api('/api/tasks?limit=200');
    renderTable();
  }
  function renderTable(){
    const root = document.getElementById('pageRoot');
    const list = tasks.filter(t => statusFilter === 'all' || t.status === statusFilter);
    const box = root.querySelector('#taskBox');
    if (!box) return;
    box.innerHTML = list.length ? `
      <div class="panel" style="padding:0">
        <div class="tbl-wrap"><table class="tbl task-tbl"><thead><tr>
          <th>#</th><th>任务</th><th>类型</th><th>环境</th><th>版本</th><th>状态</th><th>启动时间</th><th>耗时</th></tr></thead><tbody>
          ${list.map(t => `<tr>
            <td class="num">${t.id}</td>
            <td><span class="task-name" data-task="${t.id}">${esc(t.name)}</span></td>
            <td>${t.type === 'pipeline' ? '流水线' : '服务'}</td>
            <td>${esc(t.env || '-')}</td>
            <td class="num">${esc(t.version || t.previous_version || '-')}</td>
            <td>${statusBadge(t.status)}</td>
            <td class="num muted">${fmtTime(t.started_at)}</td>
            <td class="num muted">${t.finished_at && t.started_at ? ((t.finished_at - t.started_at) + 's') : (t.status === 'running' ? '进行中' : '-')}</td>
          </tr>`).join('')}
        </tbody></table></div></div>`
      : `<div class="panel">${emptyState('📋', '暂无任务' + (statusFilter !== 'all' ? '（当前筛选下）' : ''))}</div>`;
    box.querySelectorAll('.task-name').forEach(el => el.onclick = () => openTaskDetail(Number(el.dataset.task)));
  }

  // ---- 任务详情 + 日志流（offset 增量追尾，running→终态停止）----
  async function openTaskDetail(id){
    const task = await api('/api/tasks/' + id);
    const body = openDrawer({ title: `任务 #${id} ${esc(task.name)}`, wide: true, body: `
      <div class="detail-grid">
        <div><div class="dg-label">状态</div><div style="margin-top:2px">${statusBadge(task.status)}</div></div>
        <div><div class="dg-label">类型</div><div style="margin-top:2px">${task.type === 'pipeline' ? '流水线' : '服务'}</div></div>
        <div><div class="dg-label">环境</div><div class="num" style="margin-top:2px">${esc(task.env || '-')}</div></div>
        <div><div class="dg-label">版本 → 回滚目标</div><div class="num" style="margin-top:2px">${esc(task.version || '-')} → ${esc(task.previous_version || '-')}</div></div>
        <div><div class="dg-label">启动</div><div class="num" style="margin-top:2px">${fmtTime(task.started_at)}</div></div>
        <div><div class="dg-label">提交人</div><div style="margin-top:2px">${esc(task.user || '')}</div></div>
      </div>
      <div class="log-console" id="taskLog">加载中...</div>
      ${['running','pending'].includes(task.status) && canDeploy()
        ? `<div class="task-drawer-actions"><button class="btn danger" id="killBtn">终止任务</button></div>` : ''}
    `});
    streamLog(id, task);
    const killBtn = document.getElementById('killBtn');
    if (killBtn) killBtn.onclick = async () => {
      if (!await confirmDialog(`确认终止任务 #${id}？终止后不可恢复。`)) return;
      try{
        const r = await api(`/api/tasks/${id}/kill`, {method:'POST'});
        toastSuccess(r.killed ? '已发送终止信号' : '任务无法终止（可能已结束）');
        loadTasks();
      }catch(e){ toastError(e.message); }
    };
  }
  function streamLog(id, task){
    const logBox = document.getElementById('taskLog');
    if (!logBox) return;
    let offset = 0;
    const tick = async () => {
      try{
        const d = await api(`/api/tasks/${id}/log?offset=${offset}`);
        if (d.log){ logBox.textContent += d.log; logBox.scrollTop = logBox.scrollHeight; }
        offset = d.offset;
        if (['success','failed','killed'].includes(d.status)){
          logBox.innerHTML += `<span class="tail">\n── 任务已结束（${d.status}）──</span>`;
          clearInterval(timer); timer = null;
          if (task.status !== d.status){ task.status = d.status; loadTasks(); }
        }
      }catch(e){ logBox.textContent += '\n[日志拉取失败] ' + e.message; clearInterval(timer); timer = null; }
    };
    tick();
    if (['running','pending'].includes(task.status)){
      timer = setInterval(tick, 2000);
    }
  }

  // ---- 新建发布（复用 devices 的抽屉逻辑，保持单一入口）----
  function openNewDeploy(){
    toast('请在「项目总览」选择服务发起发布', '');
    location.hash = '#/devices';
  }

  // ---- 模块导出 ----
  window.deploy = {
    render: async function(root){
      root.innerHTML = skeletonHTML(3);
      await loadTasks();
      const statuses = [['all','全部'],['running','运行中'],['pending','等待中'],['success','成功'],['failed','失败']];
      root.innerHTML = `
        <div class="deploy-toolbar">
          <span class="weak" style="font-size:12px">状态</span>
          ${statuses.map(([k, v]) => `<span class="chip ${k === statusFilter ? 'active' : ''}" data-st="${k}">${v}</span>`).join('')}
          <span style="flex:1"></span>
          ${canDeploy() ? '<button class="btn primary" id="newTaskBtn">+ 新建发布</button>' : ''}
        </div>
        <div id="taskBox"></div>`;
      root.querySelectorAll('.chip[data-st]').forEach(c => c.onclick = () => {
        statusFilter = c.dataset.st;
        document.querySelectorAll('.chip[data-st]').forEach(x => x.classList.toggle('active', x === c));
        renderTable();
      });
      const nb = document.getElementById('newTaskBtn');
      if (nb) nb.onclick = openNewDeploy;
      timer = setInterval(loadTasks, 5000);
      return [timer];
    },
    destroy: function(){ if (timer) clearInterval(timer); timer = null; },
  };
})();
```

> 说明：`openTaskLogDrawer(id)` 全局函数（供 dashboard/layout 搜索复用）直接使用 `openTaskDetail`：
```javascript
// 追加到 deploy.js 末尾（IIFE 外部）
window.openTaskLogDrawer = function(id){
  document.querySelector('#deploy .nav-item') && (location.hash = '#/deploy');
  // 直接调用页面内函数：通过模块句柄中转
  deployPage.openTaskDetailSafe(id);
};
```
> 实现时请把 IIFE 内部 `openTaskDetail` 同时暴露为 `window.deployPage = { openTaskDetailSafe: openTaskDetail }`（在 IIFE 末尾追加一行：`window.deployPage = { openTaskDetailSafe: openTaskDetail };`），保证 dashboard 点击任务可打开日志抽屉且不依赖当前路由。

- [ ] **Step 3: 验证**

浏览器访问 `http://127.0.0.1:8099/screen#/deploy`：
- Expected: 任务表格渲染（#/任务名/类型/环境/版本/状态/时间/耗时）；状态 chip 筛选生效
- Expected: 点击任务名打开宽抽屉；日志增量追尾；运行中任务 2s 自动刷新；结束后显示"任务已结束"尾部标记
- Expected: 运行中任务显示"终止任务"按钮，确认弹窗后调用 kill API 并刷新列表
- Expected: 从 dashboard 最近发布列表点击任务可打开日志抽屉（跨页复用）

- [ ] **Step 4: Commit（若 git 可用）**
```bash
git add webapp/static/js/pages/deploy.js webapp/static/css/pages/deploy.css
git commit -m "feat(webapp): 发布中心（列表/详情/日志流/终止）"
```

---

## Task 9: 告警中心 — data.js + pages/alerts.js + pages/alerts.css

**Files:**
- Create: `webapp/static/js/data.js`（告警聚合 + 健康指数算法）
- Create: `webapp/static/js/pages/alerts.js`
- Create: `webapp/static/css/pages/alerts.css`

**聚合逻辑**（规格 §7）：15s 轮询 `tasks?limit=500` + `dashboard/overview`；规则：failed 任务（回滚失败=严重/发布失败=警告）、running 超 30 分钟=超时警告、`probe_failed`/`not_deployed` 且 enabled=服务异常警告；"已处理"存 localStorage `alert_acked_<taskId>`。

- [ ] **Step 1: 创建 `webapp/static/js/data.js`**

```javascript
// data.js — 告警聚合算法 + 健康指数计算（规格 §7/§6.2）
const ALERT_ACK_PREFIX = 'alert_acked_';

// 生成告警：返回 [{key, level, source, title, detail, ts, status}]
// level: critical(严重)/warning(警告)；status: unacked/acked
function computeAlerts(tasks, overview, monitorByProject){
  const alerts = [];
  const now = Date.now() / 1000;
  const seen = {};   // 去重 key
  const push = (key, level, source, title, detail, ts) => {
    if (seen[key]) return;
    seen[key] = 1;
    const acked = localStorage.getItem(ALERT_ACK_PREFIX + key) === '1';
    alerts.push({ key, level, source, title, detail, ts, status: acked ? 'acked' : 'unacked' });
  };
  // 规则 1：失败任务（type 含 rollback → 严重）
  tasks.forEach(t => {
    if (t.status !== 'failed') return;
    const isRollback = t.type === 'rollback' || (t.name || '').includes('回滚');
    push('task_failed_' + t.id,
         isRollback ? 'critical' : 'warning',
         'task',
         `任务 #${t.id} ${t.name} 发布失败`,
         `环境 ${t.env || '-'} · 版本 ${t.version || t.previous_version || '-'}${isRollback ? ' · 回滚失败' : ''}`,
         t.finished_at || t.started_at || now);
  });
  // 规则 2：running 超 30 分钟
  tasks.forEach(t => {
    if (t.status !== 'running' || !t.started_at) return;
    const runningSec = now - t.started_at;
    if (runningSec > 30 * 60){
      push('task_timeout_' + t.id, 'warning', 'task',
           `任务 #${t.id} ${t.name} 运行超时`,
           `已运行 ${Math.round(runningSec / 60)} 分钟（>30 分钟）`, t.started_at);
    }
  });
  // 规则 3：服务异常（probe_failed / not_deployed 且服务 enabled）
  Object.entries(monitorByProject || {}).forEach(([projectName, mon]) => {
    const svcs = (mon && mon.services) || {};
    Object.entries(svcs).forEach(([svcName, st]) => {
      if (st.status !== 'probe_failed' && st.status !== 'not_deployed') return;
      push('svc_' + projectName + '_' + svcName, 'warning', 'service',
           `服务 ${svcName}（${projectName}）${st.status === 'probe_failed' ? '探测失败' : '未部署'}`,
           st.status === 'probe_failed' ? 'SSH 探测无响应，请检查服务与网络' : '服务尚未部署到目标环境', st.ts || now);
    });
  });
  // 按时间倒序
  alerts.sort((a, b) => (b.ts || 0) - (a.ts || 0));
  return alerts;
}
// 标记已处理
function ackAlert(key){ localStorage.setItem(ALERT_ACK_PREFIX + key, '1'); }
function unackAlert(key){ localStorage.removeItem(ALERT_ACK_PREFIX + key); }

// 健康指数（规格 §6.2）：{demo, values[6]} —— 稳定性/性能/资源/安全/可用性/响应
function computeHealth(overview, mon){
  const svcs = mon && mon.services ? Object.values(mon.services) : [];
  const online = svcs.filter(s => s.status === 'online').length;
  const rate = overview.success_rate === null ? 0.92 : overview.success_rate / 100;
  const stability = svcs.length ? Math.min(1, (online / svcs.length) * 1.25) : 0.85;
  const demo = svcs.length === 0;
  return { demo, values: [
    Math.round(stability * 100), 88, 90, 95, Math.round(rate * 100), 87] };
}
```

- [ ] **Step 2: 创建 `webapp/static/css/pages/alerts.css`**

```css
/* alerts.css — 告警中心 */
.alert-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:var(--sp-3);margin-bottom:var(--sp-4)}
.alert-stat{background:linear-gradient(180deg,rgba(17,34,65,.6),rgba(13,27,51,.9));
  border:1px solid var(--border);border-radius:var(--r-card);padding:14px 18px}
.alert-stat .as-num{font-family:var(--font-num);font-size:26px;font-weight:700}
.alert-stat .as-label{font-size:12px;color:var(--text-2)}
.alert-stat.unacked .as-num{color:var(--danger)}
.alert-row{display:flex;align-items:center;gap:12px;padding:12px 14px;border:1px solid var(--border);
  border-radius:var(--r-card);margin-bottom:10px;background:linear-gradient(180deg,rgba(17,34,65,.45),rgba(13,27,51,.7));transition:var(--t-smooth)}
.alert-row:hover{border-color:var(--border-strong)}
.alert-row .ar-main{flex:1;min-width:0}
.alert-row .ar-title{font-size:14px;font-weight:600}
.alert-row .ar-detail{font-size:12px;color:var(--text-2);margin-top:2px}
.alert-row .ar-time{font-size:11px;color:var(--text-3);white-space:nowrap}
.alert-row.acked{opacity:.55}
.alert-row.acked .ar-title{text-decoration:line-through}
```

- [ ] **Step 3: 创建 `webapp/static/js/pages/alerts.js`**

```javascript
// pages/alerts.js — 告警中心（聚合 + 筛选 + 标记已处理 + 导航角标）
(function(){
  let timer = null;
  let alerts = [];
  let levelFilter = 'all';
  let showFilter = 'unacked';   // unacked / acked / all

  async function loadAlerts(){
    const [tasks, overview] = await Promise.all([
      api('/api/tasks?limit=500').catch(() => []),
      api('/api/dashboard/overview').catch(() => null),
    ]);
    // 拉取各项目 monitor 数据（仅取有服务状态的项目，最多遍历全部项目）
    const projects = await api('/api/projects').catch(() => []);
    const monByProject = {};
    for (const p of projects){
      try{
        const envs = await api(`/api/projects/${p.id}/environments`);
        const env = (envs.find(e => e.is_default) || envs[0]);
        if (!env) continue;
        const mon = await api(`/api/monitor/overview?project_id=${p.id}&env=${env.name}`);
        if (mon && mon.services && Object.keys(mon.services).length) monByProject[p.name] = mon;
      }catch(e){ /* 单项目监控失败跳过 */ }
    }
    alerts = computeAlerts(tasks, overview, monByProject);
    render();
  }
  function render(){
    const root = document.getElementById('pageRoot');
    const unacked = alerts.filter(a => a.status === 'unacked').length;
    const acked = alerts.filter(a => a.status === 'acked').length;
    const filtered = alerts.filter(a =>
      (levelFilter === 'all' || a.level === levelFilter) &&
      (showFilter === 'all' || a.status === showFilter));
    root.innerHTML = `
      <div class="alert-stats">
        <div class="alert-stat unacked"><div class="as-num">${unacked}</div><div class="as-label">未处理</div></div>
        <div class="alert-stat"><div class="as-num">${acked}</div><div class="as-label">已处理</div></div>
        <div class="alert-stat"><div class="as-num">${alerts.length}</div><div class="as-label">全部</div></div>
      </div>
      <div class="deploy-toolbar">
        <span class="weak" style="font-size:12px">级别</span>
        ${[['all','全部'],['critical','严重'],['warning','警告']].map(([k, v]) =>
          `<span class="chip ${k === levelFilter ? 'active' : ''}" data-lv="${k}">${v}</span>`).join('')}
        <span style="width:1px;height:20px;background:var(--border);margin:0 4px"></span>
        <span class="weak" style="font-size:12px">状态</span>
        ${[['unacked','未处理'],['acked','已处理'],['all','全部']].map(([k, v]) =>
          `<span class="chip ${k === showFilter ? 'active' : ''}" data-sh="${k}">${v}</span>`).join('')}
        <span style="flex:1"></span>
      </div>
      <div id="alertList">
        ${filtered.length ? filtered.map(a => `
          <div class="alert-row ${a.status}" data-key="${a.key}">
            <span class="badge ${a.level === 'critical' ? 'critical' : 'warning'}">${a.level === 'critical' ? '严重' : '警告'}</span>
            <div class="ar-main">
              <div class="ar-title">${esc(a.title)}</div>
              <div class="ar-detail">${esc(a.detail)}</div>
            </div>
            <span class="ar-time">${fmtTime(a.ts)}</span>
            ${a.status === 'unacked'
              ? '<button class="btn sm primary" data-ack="1">标记已处理</button>'
              : '<button class="btn sm ghost" data-ack="0">恢复未处理</button>'}
          </div>`).join('')
          : `<div class="panel">${emptyState('🎉', '暂无告警，一切正常')}</div>`}
      </div>`;
    root.querySelectorAll('.chip[data-lv]').forEach(c => c.onclick = () => {
      levelFilter = c.dataset.lv;
      document.querySelectorAll('.chip[data-lv]').forEach(x => x.classList.toggle('active', x === c));
      render();
    });
    root.querySelectorAll('.chip[data-sh]').forEach(c => c.onclick = () => {
      showFilter = c.dataset.sh;
      document.querySelectorAll('.chip[data-sh]').forEach(x => x.classList.toggle('active', x === c));
      render();
    });
    root.querySelectorAll('[data-ack]').forEach(btn => btn.onclick = () => {
      const key = btn.closest('.alert-row').dataset.key;
      if (btn.dataset.ack === '1') ackAlert(key); else unackAlert(key);
      toastSuccess(btn.dataset.ack === '1' ? '已标记处理' : '已恢复未处理');
      loadAlerts();
    });
    updateAlertBadge(unacked);
  }

  // ---- 模块导出 ----
  window.alerts = {
    render: async function(root){
      root.innerHTML = skeletonHTML(3);
      await loadAlerts();
      timer = setInterval(loadAlerts, 15000);
      return [timer];
    },
    destroy: function(){
      if (timer) clearInterval(timer);
      timer = null;
    },
  };
})();
```

> 说明：角标 `updateAlertBadge(unacked)` 由 router.js 提供；切页后角标保持上次值（规格仅要求告警页内实时）。

- [ ] **Step 4: 同步修改 dashboard.js** — 用 `data.js` 的 `computeHealth` 替换内联 `healthRadar`（保持 `{demo, values}` 结构不变）：

```javascript
// dashboard.js 内删除整个 healthRadar 函数，renderAll 中改用：
const radar = computeHealth(overview, mon);
```

- [ ] **Step 5: 验证**

浏览器访问 `http://127.0.0.1:8099/screen#/alerts`：
- Expected: 三个统计卡（未处理/已处理/全部）渲染；有失败任务/异常服务时出现告警行（级别徽标正确：回滚失败=严重）
- Expected: 标记已处理后行变灰划线、统计数更新、左侧导航角标数减少；刷新页面后状态保持（localStorage）
- Expected: 15s 轮询自动刷新；`#/dashboard` 页雷达仍正常（computeHealth 迁移无回归）
- Expected: 制造 running 任务 30 分钟以上（可在浏览器 console 直接调用 `localStorage` 模拟或用真实超时任务）——若无现成数据，验证规则 2 时可临时修改阈值至 0 分钟测试后还原

- [ ] **Step 6: Commit（若 git 可用）**
```bash
git add webapp/static/js/data.js webapp/static/js/pages/alerts.js webapp/static/css/pages/alerts.css webapp/static/js/pages/dashboard.js
git commit -m "feat(webapp): 告警中心聚合与导航角标"
```

---

## Task 10: 日志中心 — pages/logs.js + pages/logs.css

**Files:**
- Create: `webapp/static/css/pages/logs.css`
- Create: `webapp/static/js/pages/logs.js`

**页面结构**（规格 §5 日志中心）：上半面板 = 任务日志（任务选择 + 状态徽标 + 自动追尾 + 全量刷新）；下半面板 = 审计日志（仅 admin）。接口：`tasks?limit=200`、`tasks/<id>/log?offset=`（增量）、`/api/audit?limit=200`（admin）。

- [ ] **Step 1: 创建 `webapp/static/css/pages/logs.css`**

```css
/* logs.css — 日志中心 */
.logs-console{height:420px;min-height:260px}/* 任务日志控制台 */
#logTaskSel{min-width:340px;padding:7px 10px;border:1px solid var(--border);border-radius:var(--r-ctl);
  background:rgba(0,212,255,.04);color:var(--text-1);outline:none;transition:var(--t-fast)}
#logTaskSel:focus{border-color:var(--primary);box-shadow:var(--shadow-glow)}
.audit-detail{max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-2);font-size:var(--fs-12);cursor:default}
.audit-detail:hover{color:var(--primary)}
```

- [ ] **Step 2: 创建 `webapp/static/js/pages/logs.js`**（模块导出 render/destroy；日志追尾复用 deploy.js 的 offset 增量语义）

```javascript
// pages/logs.js — 日志中心（任务日志查询追尾 + 审计日志）
(function(){
  let timer = null;       // 追尾定时器
  let tasks = [];
  let currentId = null;
  let offset = 0;         // 当前日志拉取偏移

  const ACTION_CN = {
    login:'登录', login_failed:'登录失败', change_password:'修改密码',
    create_user:'创建用户', update_user:'更新用户', delete_user:'删除用户',
    create_project:'创建项目', update_project:'更新项目', delete_project:'删除项目',
    create_service:'创建服务', update_service:'更新服务', delete_service:'删除服务',
    create_environment:'创建环境', update_environment:'更新环境', delete_environment:'删除环境',
    bind_server:'绑定服务器', unbind_server:'解绑服务器', reorder_servers:'调整服务器顺序',
    create_server:'创建服务器', update_server:'更新服务器', delete_server:'删除服务器',
    create_pipeline:'创建流水线', update_pipeline:'更新流水线', delete_pipeline:'删除流水线',
    import_config:'导入配置', export_config:'导出配置',
    deploy:'发布', rollback:'回滚', kill_task:'终止任务',
  };
  const TARGET_CN = {user:'用户', project:'项目', service:'服务', environment:'环境',
                     server:'服务器', pipeline:'流水线', config:'配置', task:'任务'};

  function logBox(){ return document.getElementById('logBox'); }
  function stopTail(){ if (timer){ clearInterval(timer); timer = null; } }

  async function loadTaskList(){
    tasks = await api('/api/tasks?limit=200');
    const sel = document.getElementById('logTaskSel');
    if (!sel) return;
    sel.innerHTML = '<option value="">— 选择任务 —</option>' + tasks.map(t =>
      `<option value="${t.id}" ${t.id === currentId ? 'selected' : ''}>#${t.id} ${esc(t.name)} [${esc(t.env || '-')}] ${t.status}</option>`
    ).join('');
  }
  // 全量拉取：offset 循环直至任务终态或 1000 轮（防死循环）
  async function fetchFull(){
    stopTail();
    if (!currentId){ logBox().textContent = '请选择任务'; return; }
    logBox().textContent = '加载中...';
    offset = 0;
    let text = '';
    let status = '';
    for (let i = 0; i < 1000; i++){
      const d = await api(`/api/tasks/${currentId}/log?offset=${offset}`);
      if (d.log) text += d.log;
      offset = d.offset;
      status = d.status;
      if (['success','failed','killed'].includes(d.status)) break;
    }
    logBox().textContent = text || '（无日志输出）';
    logBox().scrollTop = logBox().scrollHeight;
    document.getElementById('logTaskStatus').innerHTML = statusBadge(status);
    if (['running','pending'].includes(status)) startTail();
  }
  // 追尾：3s 增量拉取，终态自动停止
  function startTail(){
    stopTail();
    timer = setInterval(async () => {
      try{
        const d = await api(`/api/tasks/${currentId}/log?offset=${offset}`);
        if (d.log){ logBox().textContent += d.log; logBox().scrollTop = logBox().scrollHeight; }
        offset = d.offset;
        if (['success','failed','killed'].includes(d.status)){
          stopTail();
          document.getElementById('logTaskStatus').innerHTML = statusBadge(d.status);
          loadTaskList();
        }
      }catch(e){ stopTail(); }
    }, 3000);
  }

  // ---- 审计日志（admin）----
  function fmtDetail(detail){
    try{ const s = JSON.stringify(JSON.parse(detail || '{}'));
      return s.length > 60 ? s.slice(0, 60) + '…' : s; }
    catch(e){ return detail || '-'; }
  }
  async function renderAudit(){
    const tbody = document.getElementById('auditBody');
    if (!tbody) return;
    let rows = [];
    try{ rows = await api('/api/audit?limit=200'); }catch(e){ /* 权限/失败静默，空表 */ }
    tbody.innerHTML = rows.length ? rows.map(r => `<tr>
      <td class="num muted">${fmtTime(r.created_at)}</td>
      <td>${esc(r.username || '-')}</td>
      <td>${ACTION_CN[r.action] || esc(r.action)}</td>
      <td class="muted">${TARGET_CN[r.target_type] || esc(r.target_type || '-')}${r.target_id ? ' #' + r.target_id : ''}</td>
      <td class="audit-detail" title="${esc(fmtDetail(r.detail))}">${esc(fmtDetail(r.detail))}</td>
    </tr>`).join('')
      : '<tr><td colspan="5" class="empty-row">暂无审计记录</td></tr>';
  }

  // ---- 模块导出 ----
  window.logs = {
    render: async function(root){
      root.innerHTML = `
        <div class="panel" style="margin-bottom:var(--sp-4)">
          <div class="panel-title">任务日志</div>
          <div class="flex gap-3" style="margin-bottom:12px;flex-wrap:wrap">
            <select id="logTaskSel"></select>
            <span id="logTaskStatus"></span>
            <span style="flex:1"></span>
            <button class="btn sm" id="logRefresh">⟳ 全量刷新</button>
          </div>
          <div class="log-console logs-console" id="logBox">请选择任务</div>
        </div>
        ${isAdmin() ? `
        <div class="panel">
          <div class="panel-title">审计日志</div>
          <div class="tbl-wrap"><table class="tbl"><thead><tr>
            <th>时间</th><th>用户</th><th>动作</th><th>对象</th><th>详情</th></tr></thead>
            <tbody id="auditBody"><tr><td colspan="5" class="empty-row">加载中...</td></tr></tbody>
          </table></div>
        </div>` : ''}`;
      const sel = document.getElementById('logTaskSel');
      if (sel) sel.onchange = () => {
        currentId = sel.value ? Number(sel.value) : null;
        document.getElementById('logTaskStatus').innerHTML = '';
        fetchFull();
      };
      const rb = document.getElementById('logRefresh');
      if (rb) rb.onclick = fetchFull;
      await loadTaskList();
      if (isAdmin()) renderAudit();
      return [];
    },
    destroy: function(){ stopTail(); },
  };
})();
```

- [ ] **Step 3: 验证**

浏览器访问 `http://127.0.0.1:8099/screen#/logs`：
- Expected: 任务下拉列出最近 200 个任务；选择运行中任务后日志增量追尾（3s），选择已完成任务一次性全量展示
- Expected: 追尾期间任务结束 → 自动停止并显示终态徽标；「全量刷新」重新拉取全部日志
- Expected: admin 可见审计日志表（时间/用户/动作中文映射/对象/详情截断，hover 显示完整）；viewer 角色不渲染审计面板

- [ ] **Step 4: Commit（若 git 可用）**
```bash
git add webapp/static/js/pages/logs.js webapp/static/css/pages/logs.css
git commit -m "feat(webapp): 日志中心（任务日志追尾+审计日志）"
```

---

## Task 11: 配置管理 — pages/config.js + pages/config.css

**Files:**
- Create: `webapp/static/css/pages/config.css`
- Create: `webapp/static/js/pages/config.js`

**页面结构**（规格 §5/§9 配置管理，功能与字段完全对齐后端 `config_routes.py`）：顶层 tab = 项目管理 / 服务器管理 / 用户管理 / YAML 导入导出；项目管理内先选项目（左侧项目列表），再切子 tab = 服务 / 环境 / 流水线；所有编辑走抽屉表单；删除一律 `confirmDialog` 二次确认；服务被流水线引用时展示后端错误提示（toast）。

- [ ] **Step 1: 创建 `webapp/static/css/pages/config.css`**

```css
/* config.css — 配置管理 */
.cfg-layout{display:grid;grid-template-columns:220px 1fr;gap:var(--sp-4);align-items:start}
.cfg-side{display:flex;flex-direction:column;gap:2px;padding:var(--sp-2)}
.cfg-side .list-item{border-radius:var(--r-ctl)}
.cfg-side .list-item.active{background:rgba(0,212,255,.1);color:var(--primary)}
.cfg-toolbar{display:flex;align-items:center;gap:var(--sp-3);margin-bottom:var(--sp-4);flex-wrap:wrap}
.cfg-toolbar .chip{font-size:12px}
.cfg-hint{font-size:var(--fs-11);color:var(--text-3);margin-top:2px}
.io-results{margin-top:var(--sp-4)}
.io-results .io-item{display:flex;gap:8px;padding:6px 0;border-bottom:1px dashed var(--border);font-size:var(--fs-13)}
.io-results .io-item .io-ok{color:var(--success)}
.io-results .io-item .io-fail{color:var(--danger)}
```

- [ ] **Step 2: 创建 `webapp/static/js/pages/config.js`**（六类 CRUD + 导入导出；全文件一次创建）

```javascript
// pages/config.js — 配置管理（项目/服务/环境/服务器/流水线/用户 CRUD + YAML 导入导出，admin）
(function(){
  let topTab = 'projects';     // projects / servers / users / io
  let pid = null;              // 当前项目 id
  let subTab = 'services';     // services / envs / pipelines
  let projects = [];

  // ---- 通用工具 ----
  const tabs = (list, active, key) => `<div class="cfg-toolbar">${list.map(([k, v]) =>
    `<span class="chip ${k === active ? 'active' : ''}" data-${key}="${k}">${v}</span>`).join('')}</div>`;
  const listToText = arr => (arr || []).join('\n');
  const textToList = v => String(v || '').split('\n').map(s => s.trim()).filter(Boolean);
  const jsonToText = o => JSON.stringify(o || {}, null, 2);
  function textToJson(v, label){
    const o = JSON.parse(v);
    if (!o || typeof o !== 'object' || Array.isArray(o)) throw new Error(label + ' 必须为 JSON 对象');
    return o;
  }
  function val(id){ return document.getElementById(id).value; }
  function chk(id){ return document.getElementById(id).checked; }
  const submitBtn = (txt) => `<div style="display:flex;gap:10px;margin-top:20px">
    <button class="btn primary flex-1" id="cfSubmit">${txt}</button></div>`;

  // ---- 顶层渲染与分发 ----
  async function render(){
    const root = document.getElementById('pageRoot');
    root.innerHTML = `
      <div class="deploy-toolbar">
        ${[['projects','项目管理'],['servers','服务器管理'],['users','用户管理'],['io','YAML 导入导出']]
          .map(([k, v]) => `<span class="chip ${k === topTab ? 'active' : ''}" data-top="${k}">${v}</span>`).join('')}
      </div>
      <div id="cfgBody">${skeletonHTML(3)}</div>`;
    root.querySelectorAll('.chip[data-top]').forEach(c => c.onclick = () => {
      topTab = c.dataset.top; render();
    });
    const body = document.getElementById('cfgBody');
    if (topTab === 'projects') await renderProjects(body);
    else if (topTab === 'servers') await renderServers(body);
    else if (topTab === 'users') await renderUsers(body);
    else renderIO(body);
  }

  // ================= 项目 =================
  async function renderProjects(body){
    projects = await api('/api/projects');
    if (!pid && projects.length) pid = projects[0].id;
    body.innerHTML = `
      <div class="cfg-layout">
        <div class="panel cfg-side">
          <div class="panel-title" style="font-size:13px">项目（${projects.length}）</div>
          ${projects.map(p => `<div class="list-item ${p.id === pid ? 'active' : ''}" data-pid="${p.id}">
            <div class="li-main ellipsis">${esc(p.name)} <span class="weak">${esc(p.code)}</span></div>
            <button class="btn sm ghost" style="padding:2px 6px;font-size:11px" data-editp="${p.id}">编辑</button>
            <button class="btn sm danger" style="padding:2px 6px;font-size:11px" data-delp="${p.id}">删除</button></div>`).join('') || emptyState('🗂️', '暂无项目')}
          <button class="btn sm primary" id="addProject" style="margin-top:10px">+ 新建项目</button>
        </div>
        <div>
          ${pid ? tabs([['services','服务'],['envs','环境'],['pipelines','流水线']], subTab, 'sub') + `<div id="subBody"></div>` : '<div class="panel">请先创建或选择项目</div>'}
        </div>
      </div>`;
    body.querySelectorAll('.list-item[data-pid]').forEach(it => it.onclick = () => { pid = Number(it.dataset.pid); renderProjects(body); });
    body.querySelectorAll('[data-editp]').forEach(b => b.onclick = (e) => { e.stopPropagation(); openProjectForm(body, projects.find(x => x.id === Number(b.dataset.editp))); });
    body.querySelectorAll('[data-delp]').forEach(b => b.onclick = (e) => { e.stopPropagation(); delProject(body, projects.find(x => x.id === Number(b.dataset.delp))); });
    body.querySelectorAll('.chip[data-sub]').forEach(c => c.onclick = () => { subTab = c.dataset.sub; renderProjects(body); });
    document.getElementById('addProject').onclick = () => openProjectForm(body, null);
    if (pid){
      const sb = document.getElementById('subBody');
      if (subTab === 'services') await renderServices(sb);
      else if (subTab === 'envs') await renderEnvs(sb);
      else await renderPipelines(sb);
    }
  }
  function openProjectForm(body, p){
    openDrawer({ title: p ? '编辑项目' : '新建项目', body: `
      <div class="form-grid">
        <div class="field"><label>名称 *</label><input id="pName" value="${esc(p ? p.name : '')}"></div>
        <div class="field"><label>编码 *（字母数字 _ -，唯一）</label><input id="pCode" value="${esc(p ? p.code : '')}" ${p ? 'disabled' : ''}></div>
        <div class="field full"><label>描述</label><input id="pDesc" value="${esc(p ? p.description : '')}"></div>
        <div class="field full"><label>Git 组织</label><input id="pGit" value="${esc(p ? p.git_org : '')}"></div>
      </div>${submitBtn(p ? '保存' : '创建')}` });
    document.getElementById('cfSubmit').onclick = async () => {
      try{
        const payload = { name: val('pName').trim(), description: val('pDesc').trim(), git_org: val('pGit').trim() };
        if (!p) payload.code = val('pCode').trim();
        if (!payload.name || (!p && !payload.code)) throw new Error('名称与编码必填');
        await api(p ? `/api/projects/${p.id}` : '/api/projects', { method: p ? 'PUT' : 'POST', body: JSON.stringify(payload) });
        toastSuccess(p ? '项目已更新' : '项目已创建');
        document.getElementById('drawerRoot').classList.add('hidden');
        await renderProjects(body);
      }catch(e){ toastError(e.message); }
    };
  }
  async function delProject(body, p){
    if (!await confirmDialog(`确认删除项目「${p.name}」？将级联删除其服务/环境/流水线。`)) return;
    try{
      await api('/api/projects/' + p.id, { method: 'DELETE' });
      if (pid === p.id) pid = null;
      toastSuccess('项目已删除');
      await renderProjects(body);
    }catch(e){ toastError(e.message); }
  }

  // ================= 服务（当前项目） =================
  async function renderServices(sb){
    const svcs = await api(`/api/projects/${pid}/services`);
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
      : `<div class="empty-state"><div class="e-ico">🧩</div><div>暂无服务</div></div>`}</div>`;
    document.getElementById('addSvc').onclick = () => openServiceForm(sb, null);
    sb.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => openServiceForm(sb, svcs.find(s => s.id === Number(b.dataset.edit))));
    sb.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
      const s = svcs.find(x => x.id === Number(b.dataset.del));
      if (!await confirmDialog(`确认删除服务「${s.name}」？`)) return;
      try{ await api(`/api/projects/${pid}/services/${s.id}`, { method: 'DELETE' }); toastSuccess('服务已删除'); renderServices(sb); }
      catch(e){ toastError(e.message); }  // 被流水线引用时后端返回 400 文案直接展示
    });
  }
  const SVC_LIST_FIELDS = [['build_order','构建顺序（每行一个）'],['nacos_groups','Nacos 分组（每行一个）'],
                           ['dubbo_services','Dubbo 服务（每行一个）'],['log_error_keywords','日志错误关键字（每行一个）']];
  function openServiceForm(sb, s){
    openDrawer({ title: s ? '编辑服务：' + s.name : '新建服务', wide: true, body: `
      <div class="form-grid">
        <div class="field"><label>名称 *</label><input id="sName" value="${esc(s ? s.name : '')}"></div>
        <div class="field"><label>镜像名 *</label><input id="sImage" value="${esc(s ? s.image_name : '')}"></div>
        <div class="field"><label>服务名 *（容器/注册名）</label><input id="sSvc" value="${esc(s ? s.service_name : '')}"></div>
        <div class="field"><label>健康端口</label><input id="sPort" type="number" value="${s ? s.health_port : 0}"></div>
        <div class="field"><label>Docker 上下文</label><input id="sCtx" value="${esc(s ? s.docker_context : '')}"></div>
        <div class="field"><label>Dockerfile</label><input id="sDockerfile" value="${esc(s ? s.dockerfile : '')}"></div>
        <div class="field"><label>SnailJob 分组</label><input id="sSnail" value="${esc(s ? s.snailjob_group : '')}"></div>
        <div class="field"><label>健康检查 URL</label><input id="sHealthUrl" value="${esc(s ? s.health_url : '')}"></div>
        <div class="field"><label>清理日志目录</label><input id="sClear" value="${esc(s ? s.clear_log_dir : '')}"></div>
        <div class="field"><label>Git 仓库</label><input id="sGit" value="${esc(s ? s.git_repo : '')}"></div>
        <div class="field check" style="grid-column:1/-1"><label><input type="checkbox" id="sEnabled" ${!s || s.enabled ? 'checked' : ''}> 启用</label></div>
        ${SVC_LIST_FIELDS.map(([k, label]) => `<div class="field"><label>${label}</label>
          <textarea id="s${k}" style="min-height:56px">${esc(listToText(s && s[k]))}</textarea></div>`).join('')}
      </div>${submitBtn(s ? '保存' : '创建')}` });
    document.getElementById('cfSubmit').onclick = async () => {
      try{
        const body = {
          name: val('sName').trim(), image_name: val('sImage').trim(), service_name: val('sSvc').trim(),
          docker_context: val('sCtx').trim(), dockerfile: val('sDockerfile').trim(),
          snailjob_group: val('sSnail').trim(), health_url: val('sHealthUrl').trim(),
          clear_log_dir: val('sClear').trim(), git_repo: val('sGit').trim(),
          health_port: Number(val('sPort')), enabled: chk('sEnabled'),
        };
        if (!body.name || !body.image_name || !body.service_name) throw new Error('名称/镜像名/服务名必填');
        SVC_LIST_FIELDS.forEach(([k]) => body[k] = textToList(val('s' + k)));
        await api(`/api/projects/${pid}/services${s ? '/' + s.id : ''}`, { method: s ? 'PUT' : 'POST', body: JSON.stringify(body) });
        toastSuccess(s ? '服务已更新' : '服务已创建');
        document.getElementById('drawerRoot').classList.add('hidden');
        renderServices(sb);
      }catch(e){ toastError(e.message); }
    };
  }

  // ================= 环境（当前项目） =================
  const ENV_JSON_FIELDS = [['registry','镜像仓库'],['nacos','Nacos'],['mysql_snailjob','SnailJob 数据库'],
                           ['ssh','SSH 连接'],['build','构建配置'],['deploy','部署配置']];
  async function renderEnvs(sb){
    const envs = await api(`/api/projects/${pid}/environments`);
    sb.innerHTML = `<div class="deploy-toolbar">
        <span class="weak" style="font-size:12px">环境（${envs.length}）</span><span style="flex:1"></span>
        <button class="btn sm primary" id="addEnv">+ 新建环境</button>
      </div>
      <div class="panel" style="padding:0">${envs.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr>
        <th>名称</th><th>默认</th><th>监控</th><th>Compose 路径</th><th>Maven Profile</th><th>操作</th></tr></thead><tbody>
        ${envs.map(e => `<tr>
          <td>${esc(e.name)}</td>
          <td>${e.is_default ? '<span class="badge success">默认</span>' : '-'}</td>
          <td>${e.monitor_enabled ? '<span class="ok">启用</span>' : '<span class="weak">停用</span>'}</td>
          <td class="num">${esc(e.docker_compose_path || '-')}</td>
          <td class="num">${esc(e.maven_profile || '-')}</td>
          <td><button class="btn sm ghost" data-edit="${e.id}">编辑</button>
              <button class="btn sm danger" data-del="${e.id}">删除</button></td>
        </tr>`).join('')}</tbody></table></div>`
      : `<div class="empty-state"><div class="e-ico">🌐</div><div>暂无环境</div></div>`}</div>`;
    document.getElementById('addEnv').onclick = () => openEnvForm(sb, null);
    sb.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => openEnvForm(sb, envs.find(e => e.id === Number(b.dataset.edit))));
    sb.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
      const e = envs.find(x => x.id === Number(b.dataset.del));
      if (!await confirmDialog(`确认删除环境「${e.name}」？`)) return;
      try{ await api(`/api/projects/${pid}/environments/${e.id}`, { method: 'DELETE' }); toastSuccess('环境已删除'); renderEnvs(sb); }
      catch(err){ toastError(err.message); }
    });
  }
  function openEnvForm(sb, e){
    openDrawer({ title: e ? '编辑环境：' + e.name : '新建环境', wide: true, body: `
      <div class="form-grid">
        <div class="field"><label>名称 *（字母数字 _ -）</label><input id="eName" value="${esc(e ? e.name : '')}"></div>
        <div class="field check" style="align-self:end"><label><input type="checkbox" id="eDefault" ${e && e.is_default ? 'checked' : ''}> 默认环境</label></div>
        <div class="field check" style="align-self:end"><label><input type="checkbox" id="eMonitor" ${!e || e.monitor_enabled ? 'checked' : ''}> 启用监控</label></div>
        <div class="field"><label>Docker Compose 路径</label><input id="eCompose" value="${esc(e ? e.docker_compose_path : '')}"></div>
        <div class="field"><label>Maven Profile</label><input id="eProfile" value="${esc(e ? e.maven_profile : '')}"></div>
        ${ENV_JSON_FIELDS.map(([k, label]) => `<div class="field full"><label>${label}（JSON）</label>
          <textarea id="e${k}" style="min-height:64px;font-family:var(--font-num);font-size:12px">${esc(jsonToText(e && e[k]))}</textarea></div>`).join('')}
      </div>${submitBtn(e ? '保存' : '创建')}` });
    document.getElementById('cfSubmit').onclick = async () => {
      try{
        const body = {
          name: val('eName').trim(), is_default: chk('eDefault'), monitor_enabled: chk('eMonitor'),
          docker_compose_path: val('eCompose').trim(), maven_profile: val('eProfile').trim(),
        };
        if (!body.name) throw new Error('名称必填');
        ENV_JSON_FIELDS.forEach(([k, label]) => body[k] = textToJson(val('e' + k), label));
        await api(`/api/projects/${pid}/environments${e ? '/' + e.id : ''}`, { method: e ? 'PUT' : 'POST', body: JSON.stringify(body) });
        toastSuccess(e ? '环境已更新' : '环境已创建');
        document.getElementById('drawerRoot').classList.add('hidden');
        renderEnvs(sb);
      }catch(err){ toastError(err.message); }
    };
  }

  // ================= 流水线（当前项目） =================
  async function renderPipelines(sb){
    const pls = await api(`/api/projects/${pid}/pipelines`);
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
      : `<div class="empty-state"><div class="e-ico">🔁</div><div>暂无流水线</div></div>`}</div>`;
    document.getElementById('addPl').onclick = () => openPipelineForm(sb, null);
    sb.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => openPipelineForm(sb, pls.find(x => x.id === Number(b.dataset.edit))));
    sb.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
      const pl = pls.find(x => x.id === Number(b.dataset.del));
      if (!await confirmDialog(`确认删除流水线「${pl.name}」？`)) return;
      try{ await api(`/api/projects/${pid}/pipelines/${pl.id}`, { method: 'DELETE' }); toastSuccess('流水线已删除'); renderPipelines(sb); }
      catch(err){ toastError(err.message); }
    });
  }
  function openPipelineForm(sb, pl){
    openDrawer({ title: pl ? '编辑流水线：' + pl.name : '新建流水线', wide: true, body: `
      <div class="form-grid">
        <div class="field"><label>名称 *</label><input id="plName" value="${esc(pl ? pl.name : '')}"></div>
        <div class="field check" style="align-self:end"><label><input type="checkbox" id="plBuild" ${!pl || pl.build_first ? 'checked' : ''}> 先构建后发布</label></div>
        <div class="field full"><label>Stages（JSON）</label>
          <div class="cfg-hint">示例：[
{"name":"构建","services":[{"service_id":1}]},
{"name":"部署","services":[{"service_id":2}]}]</div>
          <textarea id="plStages" style="min-height:180px;font-family:var(--font-num);font-size:12px">${esc(jsonToText(pl && pl.stages))}</textarea></div>
      </div>${submitBtn(pl ? '保存' : '创建')}` });
    document.getElementById('cfSubmit').onclick = async () => {
      try{
        const body = { name: val('plName').trim(), build_first: chk('plBuild') };
        if (!body.name) throw new Error('名称必填');
        const stages = textToJson(val('plStages'), 'Stages');
        if (!Array.isArray(stages)) throw new Error('Stages 必须为 JSON 数组');
        body.stages = stages;
        await api(`/api/projects/${pid}/pipelines${pl ? '/' + pl.id : ''}`, { method: pl ? 'PUT' : 'POST', body: JSON.stringify(body) });
        toastSuccess(pl ? '流水线已更新' : '流水线已创建');
        document.getElementById('drawerRoot').classList.add('hidden');
        renderPipelines(sb);
      }catch(err){ toastError(err.message); }
    };
  }

  // ================= 服务器 =================
  async function renderServers(body){
    const svs = await api('/api/servers');
    body.innerHTML = `<div class="deploy-toolbar">
        <span class="weak" style="font-size:12px">服务器（${svs.length}）</span><span style="flex:1"></span>
        <button class="btn sm primary" id="addSrv">+ 新建服务器</button>
      </div>
      <div class="panel" style="padding:0">${svs.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr>
        <th>主机</th><th>端口</th><th>用户</th><th>Nacos IP</th><th>备注</th><th>启用</th><th>操作</th></tr></thead><tbody>
        ${svs.map(s => `<tr>
          <td class="num">${esc(s.host)}</td>
          <td class="num">${s.ssh_port ?? '-'}</td>
          <td class="num">${esc(s.ssh_user || '-')}</td>
          <td class="num">${esc(s.nacos_ip || '-')}</td>
          <td class="muted">${esc(s.remark || '-')}</td>
          <td>${s.enabled ? '<span class="ok">启用</span>' : '<span class="weak">停用</span>'}</td>
          <td><button class="btn sm ghost" data-edit="${s.id}">编辑</button>
              <button class="btn sm danger" data-del="${s.id}">删除</button></td>
        </tr>`).join('')}</tbody></table></div>`
      : `<div class="empty-state"><div class="e-ico">🖥️</div><div>暂无服务器</div></div>`}</div>`;
    document.getElementById('addSrv').onclick = () => openServerForm(body, null);
    body.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => openServerForm(body, svs.find(x => x.id === Number(b.dataset.edit))));
    body.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
      const s = svs.find(x => x.id === Number(b.dataset.del));
      if (!await confirmDialog(`确认删除服务器「${s.host}」？`)) return;
      try{ await api('/api/servers/' + s.id, { method: 'DELETE' }); toastSuccess('服务器已删除'); renderServers(body); }
      catch(err){ toastError(err.message); }
    });
  }
  function openServerForm(body, s){
    openDrawer({ title: s ? '编辑服务器：' + s.host : '新建服务器', body: `
      <div class="form-grid">
        <div class="field"><label>主机 *</label><input id="rHost" value="${esc(s ? s.host : '')}"></div>
        <div class="field"><label>SSH 端口</label><input id="rPort" type="number" value="${s && s.ssh_port ? s.ssh_port : ''}"></div>
        <div class="field"><label>SSH 用户</label><input id="rUser" value="${esc(s ? s.ssh_user : '')}"></div>
        <div class="field"><label>SSH 密码（${s && s.ssh_password ? '已设置' : '留空继承环境级'}）</label><input id="rPwd" type="password" value="" placeholder="${s && s.ssh_password === '***' ? '已遮蔽，留空不修改' : '留空继承环境级'}"></div>
        <div class="field"><label>SSH 密钥文件</label><input id="rKey" value="${esc(s ? s.ssh_key_file : '')}"></div>
        <div class="field"><label>Nacos IP</label><input id="rNacos" value="${esc(s ? s.nacos_ip : '')}"></div>
        <div class="field full"><label>备注</label><input id="rRemark" value="${esc(s ? s.remark : '')}"></div>
        <div class="field check"><label><input type="checkbox" id="rEnabled" ${!s || s.enabled ? 'checked' : ''}> 启用</label></div>
      </div>${submitBtn(s ? '保存' : '创建')}` });
    document.getElementById('cfSubmit').onclick = async () => {
      try{
        const body = {
          host: val('rHost').trim(), ssh_user: val('rUser').trim(), nacos_ip: val('rNacos').trim(),
          remark: val('rRemark').trim(), enabled: chk('rEnabled'),
        };
        if (!body.host) throw new Error('主机必填');
        const port = val('rPort').trim();
        if (port) body.ssh_port = Number(port);
        const pwd = val('rPwd');
        if (pwd) body.ssh_password = pwd;
        const key = val('rKey').trim();
        if (key) body.ssh_key_file = key;
        await api('/api/servers' + (s ? '/' + s.id : ''), { method: s ? 'PUT' : 'POST', body: JSON.stringify(body) });
        toastSuccess(s ? '服务器已更新' : '服务器已创建');
        document.getElementById('drawerRoot').classList.add('hidden');
        renderServers(body);
      }catch(err){ toastError(err.message); }
    };
  }

  // ================= 用户 =================
  const ROLE_CN = { admin: '管理员', operator: '操作员', viewer: '只读' };
  async function renderUsers(body){
    const us = await api('/api/users');
    const me = currentUser().username;
    body.innerHTML = `<div class="deploy-toolbar">
        <span class="weak" style="font-size:12px">用户（${us.length}）</span><span style="flex:1"></span>
        <button class="btn sm primary" id="addUser">+ 新建用户</button>
      </div>
      <div class="panel" style="padding:0">${us.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr>
        <th>用户名</th><th>角色</th><th>启用</th><th>强制改密</th><th>操作</th></tr></thead><tbody>
        ${us.map(u => `<tr>
          <td>${esc(u.username)}${u.username === me ? ' <span class="weak">(我)</span>' : ''}</td>
          <td>${ROLE_CN[u.role] || u.role}</td>
          <td>${u.enabled ? '<span class="ok">启用</span>' : '<span class="weak">停用</span>'}</td>
          <td>${u.must_change_password ? '<span class="badge warning">是</span>' : '-'}</td>
          <td><button class="btn sm ghost" data-edit="${u.id}">编辑</button>
              <button class="btn sm danger" data-del="${u.id}">删除</button></td>
        </tr>`).join('')}</tbody></table></div>`
      : `<div class="empty-state"><div class="e-ico">👤</div><div>暂无用户</div></div>`}</div>`;
    document.getElementById('addUser').onclick = () => openUserForm(body, null);
    body.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => openUserForm(body, us.find(x => x.id === Number(b.dataset.edit))));
    body.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
      const u = us.find(x => x.id === Number(b.dataset.del));
      if (!await confirmDialog(`确认删除用户「${u.username}」？`)) return;
      try{ await api('/api/users/' + u.id, { method: 'DELETE' }); toastSuccess('用户已删除'); renderUsers(body); }
      catch(err){ toastError(err.message); }  // 不能删除自己（后端 400 文案）
    });
  }
  function openUserForm(body, u){
    openDrawer({ title: u ? '编辑用户：' + u.username : '新建用户', body: `
      <div class="form-grid">
        <div class="field"><label>用户名 *</label><input id="uName" value="${esc(u ? u.username : '')}" ${u ? 'disabled' : ''}></div>
        <div class="field"><label>${u ? '重置密码（留空不修改）' : '初始密码 *（至少 6 位）'}</label><input id="uPwd" type="password"></div>
        <div class="field"><label>角色</label><select id="uRole">
          ${Object.entries(ROLE_CN).map(([k, v]) => `<option value="${k}" ${u && u.role === k ? 'selected' : ''}>${v}</option>`).join('')}
        </select></div>
        <div class="field check" style="align-self:end"><label><input type="checkbox" id="uEnabled" ${!u || u.enabled ? 'checked' : ''}> 启用</label></div>
      </div>${submitBtn(u ? '保存' : '创建')}` });
    document.getElementById('cfSubmit').onclick = async () => {
      try{
        const name = val('uName').trim();
        const pwd = val('uPwd');
        if (!u){
          if (!name || pwd.length < 6) throw new Error('用户名必填，密码至少 6 位');
          await api('/api/users', { method: 'POST', body: JSON.stringify({ username: name, password: pwd, role: val('uRole') }) });
          toastSuccess('用户已创建（首登强制改密）');
        }else{
          const body = { role: val('uRole'), enabled: chk('uEnabled') };
          if (pwd) body.password = pwd;
          await api('/api/users/' + u.id, { method: 'PUT', body: JSON.stringify(body) });
          toastSuccess('用户已更新');
        }
        document.getElementById('drawerRoot').classList.add('hidden');
        renderUsers(body);
      }catch(err){ toastError(err.message); }
    };
  }

  // ================= YAML 导入导出 =================
  function renderIO(body){
    body.innerHTML = `<div class="panel">
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
        toastSuccess('配置已导入');
      }catch(err){ toastError(err.message); }
    };
  }

  // ---- 模块导出 ----
  window.config = {
    render: async function(root){ await render(); return []; },
    destroy: function(){ /* 无定时器/图表 */ },
  };
})();
```

> 说明：环境编辑时后端对 SSH 密码做了脱敏（`***`），提交时若未改动需避免把 `***` 写回——环境 JSON 为对象级编辑，如需保留原值请直接以原值提交；服务器密码字段留空即不修改（编辑态）。

- [ ] **Step 3: 验证**

浏览器访问 `http://127.0.0.1:8099/screen#/config`（admin 登录）：
- Expected: 顶层 4 个 tab 可切换；项目管理左侧项目列表 + 右侧服务/环境/流水线子 tab 联动
- Expected: 六类各建/改/删一次：项目（名称/编码必填校验）、服务（列表字段 textarea 每行一个转数组）、环境（JSON 子对象 textarea 校验）、流水线（stages JSON 数组校验、引用不存在服务报后端错误）、服务器（密码留空不覆盖）、用户（新建强制改密标记；不能删除自己）
- Expected: 删除均弹 confirmDialog；被流水线引用的服务删除显示后端 400 文案 toast
- Expected: 导入/导出按钮可用，导出列出文件数，导入展示每项 ok/message；viewer/operator 角色侧栏无「配置管理」入口（路由 adminOnly）

- [ ] **Step 4: Commit（若 git 可用）**
```bash
git add webapp/static/js/pages/config.js webapp/static/css/pages/config.css
git commit -m "feat(webapp): 配置管理（六类 CRUD+导入导出）"
```

---

## Task 12: 响应式降级 + 清理旧文件 + 全量回归

**Files:**
- Create: `webapp/static/css/responsive.css`
- Delete: `webapp/static/js/screen.js` / `cards.js` / `charts.js` / `panels.js` / `config-admin.js`
- Delete: `webapp/static/css/screen.css`

**说明**：`screen.html`（Task 2）已引用 `responsive.css`；Task 5 重写的 `login.html` 仅引用 tokens/base（内联样式），删除 `screen.css` 无引用残留。所有新页面文件已就绪，此时才删旧文件（全局约定第 3 条）。

- [ ] **Step 1: 创建 `webapp/static/css/responsive.css`**（1280 断点降级，规格 §10；选择器与 layout.css/dashboard.css 定义一致）

```css
/* responsive.css — 1280 断点降级（规格 §10） */
@media (max-width: 1600px){
  .dash-grid{grid-template-columns:300px 1fr}
  .dash-grid > div:last-child{grid-column:1/-1}
}
@media (max-width: 1280px){
  /* 导航折叠为 64px 图标模式（等价 #sidebar.collapsed 视觉） */
  #sidebar{width:var(--nav-w-collapsed)}
  #sidebar .logo-name,#sidebar .logo-sub,
  #sidebar .nav-txt,#sidebar .nav-group-title,#sidebar .nav-badge{display:none}
  #sidebar .nav-item{justify-content:center;padding:9px 0}
  #sidebar .nav-item.active::before{left:0}
  /* KPI 条 6 列 → 3 列换行 */
  .kpi-row{grid-template-columns:repeat(3,1fr)}
  /* 大屏网格纵向堆叠 */
  .dash-grid{grid-template-columns:1fr}
  .env-grid{grid-template-columns:repeat(2,1fr)}
  /* 表单抽屉单列 */
  .form-grid{grid-template-columns:1fr}
  /* 配置页左右布局改上下 */
  .cfg-layout{grid-template-columns:1fr}
  /* 内容区横向滚动兑底 */
  #content{overflow-x:auto}
  .page-inner{min-width:960px}
}
```

- [ ] **Step 2: 删除旧 JS/CSS**

删除以下文件（新前端已不再引用）：
```text
webapp/static/js/screen.js
webapp/static/js/cards.js
webapp/static/js/charts.js
webapp/static/js/panels.js
webapp/static/js/config-admin.js
webapp/static/css/screen.css
```

- [ ] **Step 3: 全量回归冒烟**（规格 §13 测试计划）

```powershell
cd d:\work\IdeaProjects\Reid\agencys\agencys-deploy
python webapp/app.py --port 8099
```

浏览器按序验证（每步检查 console 无报错、Network 无 404）：
1. 登录页 `/` → 深蓝玻璃卡登录成功 → `/screen#/dashboard`
2. 六页面导航切换：dashboard / devices / deploy / alerts / logs / config（admin）→ 面包屑正确、内容渲染
3. 发布闭环：项目总览卡片发布 → 跳发布中心 → 任务出现 → 详情日志追尾 → 状态流转
4. 告警闭环：制造失败任务 → 告警中心未处理告警 → 标记已处理 → 角标数更新
5. 配置 CRUD：六类各建/改/删一次 + YAML 导入导出按钮可用
6. 权限：viewer 不见「配置管理」与「审计日志」；operator 可发布不可配置
7. 响应式：窗口缩至 1280 检查导航折叠、KPI 换行、网格降级；内容区可横向滚动
8. 回归：`/api/*` 全部可被新前端调用（无 404/500）；旧 API 响应兼容

- [ ] **Step 4: Commit（若 git 可用）**
```bash
git add webapp/static/css/responsive.css
git add -A webapp/static/js webapp/static/css
git commit -m "feat(webapp): 响应式降级并移除旧前端文件"
```

---

## 自检（Self-Review）

对照规格逐项核对：

**1. 规格覆盖**
- §3 设计系统 → Task 1（tokens.css/base.css）✅
- §4 布局骨架（侧边导航/顶栏/面包屑/跑马灯/用户菜单/搜索/折叠）→ Task 2（layout.css）+ Task 4（layout.js/router.js）✅
- §5 页面映射：监控中心 Task 6 / 项目总览 Task 7 / 发布中心 Task 8 / 告警中心 Task 9 / 日志中心 Task 10 / 配置管理 Task 11 ✅
- §6 大屏复刻：KPI Task 6 / 雷达 Task 6+9（computeHealth）/ 机房 gauge Task 6 / 趋势 Task 6 / 服务器表 Task 6 / 最近发布 Task 6 ✅
- §7 告警聚合：三条规则 + ack/unack + 角标 → Task 9 ✅
- §8 组件拆分：components.css/js Task 3；data.js Task 9；页面模块 render/destroy 各 Task ✅
- §9 交互：发布抽屉 Task 7 / 日志追尾 Task 8+10 / kill 二次确认 Task 8 / 配置删除二次确认 Task 11 / 401 与错误处理 Task 3-4 ✅
- §10 响应式 → Task 12（responsive.css）✅
- §11 登录页 → Task 5 ✅
- §12 错误态/空态/监控未启用提示 → Task 3（errorState/emptyState）+ Task 6 服务器表 ✅
- §13 测试计划 → 各任务验证 + Task 12 全量回归 ✅

**2. 占位符扫描**：无 TBD/TODO；所有代码块完整可执行。✅

**3. 类型一致性**：
- 全局函数：`api/poll/isAdmin/canDeploy`（Task 4）→ 各页面引用一致 ✅
- 组件函数：`toast/toastError/toastSuccess/confirmDialog/openDrawer/emptyState/errorState/skeletonHTML/statusBadge/fmtTime/esc`（Task 3）→ 全部页面一致引用 ✅
- 路由：`PAGE_MODULES = {dashboard, devices, deploy, alerts, logs, config}` 与各页面 `window.*` 导出名一致（Task 4/6-11）✅
- `window.updateAlertBadge`（Task 4）与 alerts.js 调用一致（Task 9）✅
- `window.openTaskLogDrawer` / `window.deployPage.openTaskDetailSafe`（Task 8）与 dashboard/layout 搜索调用一致 ✅
- `data.js`：`computeAlerts(tasks, overview, monitorByProject)` 签名与 alerts.js 传参（monByProject 对象）一致；`computeHealth` 输出 `{demo, values}` 与 dashboard.js 使用一致（Task 9 同步替换）✅
- 接口字段：services `health_port/enabled`、environments `is_default/monitor_enabled`、pipelines `stages/build_first`、servers `ssh_*`、users `role/enabled/must_change_password`、audit `action/target_type/detail` 均与 `config_routes.py`/`auth_routes.py`/`dash_routes.py` 实际返回对齐 ✅
- CSS 类名：`.log-console`（deploy.css）被 logs.css 复用；`.svc-card`（components.css Task 7 追加）与 devices.js 一致 ✅

**4. 已知边界**（规格 §14）：雷达/机房为演示数据并标注；告警 ack 存 localStorage 单浏览器；品牌名经 `window.PLATFORM_NAME` 配置；项目当前非 git 仓库时跳过 commit 步骤。✅
