# Alerts 页前端改造实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有纯前端动态计算的 alerts 页改造为消费后端 15 条告警 API 的完整告警中心（列表/操作/规则管理/静默窗口/历史统计）

**Architecture:** 完全重写 `webapp/static/js/pages/alerts.js`，拆分为 4 个 Tab（告警列表/规则管理/静默窗口/历史统计），每个 Tab 独立加载数据与交互。告警列表消费 `/api/alerts` + ack/resolve 操作；规则管理消费 `/api/alerts/rules` CRUD + toggle；静默窗口消费 `/api/alerts/silences` CRUD；历史统计消费 `/api/alerts/stats` 用 ECharts 渲染趋势图。

**Tech Stack:** 原生 HTML/CSS/JS + ECharts（已有），零新依赖

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `webapp/static/js/pages/alerts.js` | 重写 | 告警中心页（4 Tab：列表/规则/静默/统计） |

---

### Task 1: 告警列表 Tab（消费 /api/alerts + ack/resolve）

**Files:**
- Modify: `webapp/static/js/pages/alerts.js`

- [ ] **Step 1: 设计告警列表 UI 结构**

告警列表 Tab 包含：
- 统计卡片（firing/acked/resolved 数量）
- 筛选栏（级别 P0-P3 / 状态 firing/acked/resolved / 关键词搜索）
- 告警列表（每条：级别徽标、状态、主题、描述、目标信息、首次/最后出现时间、ack/resolve 按钮）
- 分页（后端分页，page/page_size）

HTML 模板（动态生成）：
```javascript
<div class="alert-stats">
  <div class="alert-stat firing"><div class="as-num">${firingCount}</div><div class="as-label">活跃</div></div>
  <div class="alert-stat acked"><div class="as-num">${ackedCount}</div><div class="as-label">已确认</div></div>
  <div class="alert-stat resolved"><div class="as-num">${resolvedCount}</div><div class="as-label">已解决</div></div>
</div>
<div class="alert-toolbar">
  <select id="alSevFilter">...</select>
  <select id="alStatusFilter">...</select>
  <input id="alKeyword" placeholder="关键词搜索">
</div>
<div id="alertList">...</div>
<div class="pagination">...</div>
```

- [ ] **Step 2: 实现数据加载（GET /api/alerts）**

```javascript
async function loadAlerts() {
  const params = new URLSearchParams({
    page: currentPage,
    page_size: 20,
  });
  if (sevFilter !== 'all') params.set('severity', sevFilter);
  if (statusFilter !== 'all') params.set('status', statusFilter);
  if (keyword) params.set('keyword', keyword);
  
  const data = await api(`/api/alerts?${params}`);
  alerts = data.rows;
  total = data.total;
  renderAlertList();
}
```

- [ ] **Step 3: 实现 ack/resolve 操作**

```javascript
async function ackAlert(id) {
  await api(`/api/alerts/${id}/ack`, { method: 'POST' });
  toastSuccess('已确认');
  loadAlerts();
}

async function resolveAlert(id) {
  await api(`/api/alerts/${id}/resolve`, { method: 'POST' });
  toastSuccess('已解决');
  loadAlerts();
}
```

- [ ] **Step 4: 渲染告警列表**

每条告警渲染：
```javascript
<div class="alert-row" data-id="${a.id}">
  <span class="badge ${a.severity.toLowerCase()}">${a.severity}</span>
  <span class="status-badge ${a.status}">${statusLabel(a.status)}</span>
  <div class="ar-main">
    <div class="ar-title">${esc(a.subject)}</div>
    <div class="ar-detail">${esc(a.description)}</div>
    <div class="ar-meta">
      <span>目标: ${formatTargetInfo(a.target_info)}</span>
      <span>首次: ${fmtTime(a.first_seen_at)}</span>
      <span>最后: ${fmtTime(a.last_seen_at)}</span>
      <span>通知次数: ${a.notify_count}</span>
    </div>
  </div>
  <div class="ar-actions">
    ${a.status === 'firing' ? '<button class="btn sm primary" onclick="ackAlert(${a.id})">确认</button>' : ''}
    ${a.status !== 'resolved' ? '<button class="btn sm" onclick="resolveAlert(${a.id})">解决</button>' : ''}
  </div>
</div>
```

- [ ] **Step 5: 浏览器验证**

启动 app：`python webapp/app.py --monitor-interval 30`（后台）
打开浏览器：`http://localhost:8099`
导航到告警页，验证：
- 统计卡片显示 firing/acked/resolved 数量
- 筛选栏工作（级别/状态/关键词）
- 告警列表渲染（级别徽标、状态、主题、描述、时间）
- ack/resolve 按钮工作（点击后状态变化、列表刷新）
- 分页工作（翻页）

- [ ] **Step 6: 提交**

```bash
git add webapp/static/js/pages/alerts.js
git commit -m "feat(alerts): 告警列表 Tab（消费后端 API + ack/resolve 操作）"
```

---

### Task 2: 规则管理 Tab（规则 CRUD + 启停）

**Files:**
- Modify: `webapp/static/js/pages/alerts.js`

- [ ] **Step 1: 设计规则管理 UI 结构**

规则管理 Tab 包含：
- 规则列表（每条：名称、类型、指标、阈值、级别、渠道、启用状态、操作按钮）
- 新建规则按钮（打开抽屉）
- 编辑/删除/启停按钮

HTML 模板：
```javascript
<div class="rule-toolbar">
  <button class="btn primary" onclick="openRuleDrawer()">新建规则</button>
</div>
<div id="ruleList">...</div>
```

- [ ] **Step 2: 实现规则列表加载（GET /api/alerts/rules）**

```javascript
async function loadRules() {
  rules = await api('/api/alerts/rules');
  renderRuleList();
}
```

- [ ] **Step 3: 实现规则抽屉（新建/编辑表单）**

抽屉表单字段：
- 名称（必填）
- 类型（metric/status/log_keyword）
- 指标（cpu/mem/disk/load 或 service_down/task_failed）
- 比较符（</<=/>/>=）
- 阈值（数字）
- 持续时间（秒）
- 作用域（all/project/env/server/service + IDs）
- 级别（P0/P1/P2/P3）
- 渠道（多选：email/wecom/webhook/sms_aliyun/feishu/dingtalk）
- 升级间隔（分钟）
- 启用（checkbox）
- 备注

提交逻辑：
```javascript
async function saveRule() {
  const body = collectFormData();
  if (editingRuleId) {
    await api(`/api/alerts/rules/${editingRuleId}`, { method: 'PUT', body: JSON.stringify(body) });
  } else {
    await api('/api/alerts/rules', { method: 'POST', body: JSON.stringify(body) });
  }
  toastSuccess('保存成功');
  closeRuleDrawer();
  loadRules();
}
```

- [ ] **Step 4: 实现规则启停（POST /api/alerts/rules/{id}/toggle）**

```javascript
async function toggleRule(id) {
  await api(`/api/alerts/rules/${id}/toggle`, { method: 'POST' });
  loadRules();
}
```

- [ ] **Step 5: 实现规则删除（DELETE /api/alerts/rules/{id}）**

```javascript
async function deleteRule(id) {
  if (!confirm('确定删除该规则？')) return;
  await api(`/api/alerts/rules/${id}`, { method: 'DELETE' });
  toastSuccess('已删除');
  loadRules();
}
```

- [ ] **Step 6: 浏览器验证**

导航到规则管理 Tab，验证：
- 规则列表渲染（5 条种子规则）
- 新建规则（填写表单，提交，列表刷新）
- 编辑规则（打开抽屉，修改，提交）
- 启停规则（点击启停按钮，状态变化）
- 删除规则（确认对话框，删除后列表刷新）

- [ ] **Step 7: 提交**

```bash
git add webapp/static/js/pages/alerts.js
git commit -m "feat(alerts): 规则管理 Tab（CRUD + 启停）"
```

---

### Task 3: 静默窗口 Tab（静默 CRUD）

**Files:**
- Modify: `webapp/static/js/pages/alerts.js`

- [ ] **Step 1: 设计静默窗口 UI 结构**

静默窗口 Tab 包含：
- 窗口列表（每条：名称、类型、时间、作用域、启用状态、操作按钮）
- 新建窗口按钮（打开抽屉）
- 编辑/删除/启停按钮

HTML 模板：
```javascript
<div class="silence-toolbar">
  <button class="btn primary" onclick="openSilenceDrawer()">新建静默窗口</button>
</div>
<div id="silenceList">...</div>
```

- [ ] **Step 2: 实现窗口列表加载（GET /api/alerts/silences）**

```javascript
async function loadSilences() {
  silences = await api('/api/alerts/silences');
  renderSilenceList();
}
```

- [ ] **Step 3: 实现窗口抽屉（新建/编辑表单）**

抽屉表单字段：
- 名称（必填）
- 类型（one_time/recurring）
- 起止时间（one_time: epoch 秒；recurring: HH:MM）
- 周期窗口（recurring: weekdays 多选）
- 作用域（rules/envs/servers 多选）
- 启用（checkbox）
- 备注

提交逻辑：
```javascript
async function saveSilence() {
  const body = collectFormData();
  if (editingSilenceId) {
    await api(`/api/alerts/silences/${editingSilenceId}`, { method: 'PUT', body: JSON.stringify(body) });
  } else {
    await api('/api/alerts/silences', { method: 'POST', body: JSON.stringify(body) });
  }
  toastSuccess('保存成功');
  closeSilenceDrawer();
  loadSilences();
}
```

- [ ] **Step 4: 实现窗口删除（DELETE /api/alerts/silences/{id}）**

```javascript
async function deleteSilence(id) {
  if (!confirm('确定删除该窗口？')) return;
  await api(`/api/alerts/silences/${id}`, { method: 'DELETE' });
  toastSuccess('已删除');
  loadSilences();
}
```

- [ ] **Step 5: 浏览器验证**

导航到静默窗口 Tab，验证：
- 窗口列表渲染
- 新建窗口（填写表单，提交，列表刷新）
- 编辑窗口（打开抽屉，修改，提交）
- 删除窗口（确认对话框，删除后列表刷新）

- [ ] **Step 6: 提交**

```bash
git add webapp/static/js/pages/alerts.js
git commit -m "feat(alerts): 静默窗口 Tab（CRUD）"
```

---

### Task 4: 历史统计 Tab（ECharts 趋势图）

**Files:**
- Modify: `webapp/static/js/pages/alerts.js`

- [ ] **Step 1: 设计历史统计 UI 结构**

历史统计 Tab 包含：
- 时间范围选择（近 7 天 / 14 天 / 30 天）
- ECharts 趋势图（按天/级别聚合）

HTML 模板：
```javascript
<div class="stats-toolbar">
  <select id="statsDays">
    <option value="7">近 7 天</option>
    <option value="14">近 14 天</option>
    <option value="30">近 30 天</option>
  </select>
</div>
<div id="statsChart" style="width:100%;height:400px"></div>
```

- [ ] **Step 2: 实现趋势数据加载（GET /api/alerts/stats）**

```javascript
async function loadStats() {
  const days = document.getElementById('statsDays').value;
  const data = await api(`/api/alerts/stats?days=${days}`);
  renderStatsChart(data);
}
```

- [ ] **Step 3: 实现 ECharts 渲染**

```javascript
function renderStatsChart(data) {
  const chart = echarts.init(document.getElementById('statsChart'));
  const dates = data.map(d => d.date);
  const p0 = data.map(d => d.P0 || 0);
  const p1 = data.map(d => d.P1 || 0);
  const p2 = data.map(d => d.P2 || 0);
  const p3 = data.map(d => d.P3 || 0);
  
  chart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['P0', 'P1', 'P2', 'P3'] },
    xAxis: { type: 'category', data: dates },
    yAxis: { type: 'value' },
    series: [
      { name: 'P0', type: 'bar', stack: 'total', data: p0, itemStyle: { color: '#ff4d4f' } },
      { name: 'P1', type: 'bar', stack: 'total', data: p1, itemStyle: { color: '#fa8c16' } },
      { name: 'P2', type: 'bar', stack: 'total', data: p2, itemStyle: { color: '#fadb14' } },
      { name: 'P3', type: 'bar', stack: 'total', data: p3, itemStyle: { color: '#1890ff' } },
    ],
  });
}
```

- [ ] **Step 4: 浏览器验证**

导航到历史统计 Tab，验证：
- 时间范围选择工作
- ECharts 趋势图渲染（按天/级别堆叠柱状图）
- 数据正确（与 /api/alerts/stats 返回一致）

- [ ] **Step 5: 提交**

```bash
git add webapp/static/js/pages/alerts.js
git commit -m "feat(alerts): 历史统计 Tab（ECharts 趋势图）"
```

---

### Task 5: Tab 切换 + 端到端联调

**Files:**
- Modify: `webapp/static/js/pages/alerts.js`

- [ ] **Step 1: 实现 Tab 切换**

```javascript
let currentTab = 'alerts';

function render() {
  const root = document.getElementById('pageRoot');
  root.innerHTML = `
    <div class="tab-bar">
      <button class="tab ${currentTab === 'alerts' ? 'active' : ''}" onclick="switchTab('alerts')">告警列表</button>
      <button class="tab ${currentTab === 'rules' ? 'active' : ''}" onclick="switchTab('rules')">规则管理</button>
      <button class="tab ${currentTab === 'silences' ? 'active' : ''}" onclick="switchTab('silences')">静默窗口</button>
      <button class="tab ${currentTab === 'stats' ? 'active' : ''}" onclick="switchTab('stats')">历史统计</button>
    </div>
    <div id="tabContent"></div>
  `;
  loadTabContent();
}

function switchTab(tab) {
  currentTab = tab;
  render();
}

async function loadTabContent() {
  const content = document.getElementById('tabContent');
  if (currentTab === 'alerts') await renderAlertsTab(content);
  else if (currentTab === 'rules') await renderRulesTab(content);
  else if (currentTab === 'silences') await renderSilencesTab(content);
  else if (currentTab === 'stats') await renderStatsTab(content);
}
```

- [ ] **Step 2: 端到端联调**

启动 app：`python webapp/app.py --monitor-interval 30`（后台）
打开浏览器：`http://localhost:8099`
导航到告警页，验证：
- 4 个 Tab 切换工作
- 告警列表 Tab：统计卡片、筛选、列表、ack/resolve、分页
- 规则管理 Tab：列表、新建、编辑、启停、删除
- 静默窗口 Tab：列表、新建、编辑、删除
- 历史统计 Tab：时间范围选择、ECharts 趋势图

- [ ] **Step 3: 提交**

```bash
git add webapp/static/js/pages/alerts.js
git commit -m "feat(alerts): Tab 切换 + 端到端联调"
```

---

## 执行顺序

1. Task 1 → Task 2 → Task 3 → Task 4 → Task 5
2. 每个 Task 完成后浏览器验证，确保功能正常
3. 全部完成后端到端联调
