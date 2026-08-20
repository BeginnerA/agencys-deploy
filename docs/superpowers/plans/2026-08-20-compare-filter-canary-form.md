# 发布对比环境筛选 & 灰度策略表单化配置 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 发布对比页增加环境筛选 + 快照任意选择能力；灰度策略配置从 JSON textarea 改为模板驱动的表单化编辑器。

**Architecture:** 后端扩展 `compare_snapshots` 路由支持 `old_id`/`new_id` 手动模式；前端重写 `deploy_compare.js`（环境下拉 + 快照选择面板 + 两种对比模式）和 `deploy_canary.js`（模板胶囊 + 表格化批次编辑器 + 条形图预览）。后端灰度策略 API 无需修改。

**Tech Stack:** Python Flask (后端), 原生 JavaScript (前端), SQLite (数据库), 现有组件库 (toast/drawer/enhanceSelect/emptyState)

---

## 文件结构

| 操作 | 文件路径 | 职责 |
|------|---------|------|
| 修改 | `webapp/routes/deploy_enhance_routes.py` | 扩展 `compare_snapshots` 路由支持 `old_id`/`new_id` 手动模式 |
| 重写 | `webapp/static/js/pages/deploy_compare.js` | 环境下拉 + 快照选择面板 + 两种对比模式 |
| 重写 | `webapp/static/js/pages/deploy_canary.js` | 模板选择 + 表单编辑器 + 实时预览 |
| 修改 | `webapp/static/css/pages/deploy-enhance.css` | 新增快照选择面板 + 模板胶囊 + 预览条形图样式 |
| 修改 | `tests/test_deploy_enhance.py` | 新增手动模式对比测试 |

---

### Task 1: 后端 — 扩展 compare API 支持手动模式

**Files:**
- Modify: `webapp/routes/deploy_enhance_routes.py:556-596`
- Test: `tests/test_deploy_enhance.py`

- [ ] **Step 1: 编写手动模式对比测试**

在 `tests/test_deploy_enhance.py` 的 `TestDeployEnhanceAPI` 类中新增测试方法：

```python
    def test_compare_by_snapshot_ids(self):
        """手动模式：通过 old_id/new_id 对比指定两条快照"""
        now = int(time.time())
        sid1 = dbm.create_record(self.db_path, "deployment_snapshots", dict(
            project_id=self.proj_id, env="prod", task_id=1,
            version="1.0.0", image_versions='{"auth":"auth:1.0.0"}',
            nacos_config_hash="aaa", env_vars_hash="eee",
            deployed_at=now - 200))
        sid2 = dbm.create_record(self.db_path, "deployment_snapshots", dict(
            project_id=self.proj_id, env="prod", task_id=2,
            version="1.1.0", image_versions='{"auth":"auth:1.1.0"}',
            nacos_config_hash="bbb", env_vars_hash="eee",
            deployed_at=now - 100))
        sid3 = dbm.create_record(self.db_path, "deployment_snapshots", dict(
            project_id=self.proj_id, env="test", task_id=3,
            version="1.2.0", image_versions='{"auth":"auth:1.2.0"}',
            nacos_config_hash="ccc", env_vars_hash="fff",
            deployed_at=now))
        # 跨环境对比：old=prod(sid1), new=test(sid3)
        resp = self.client.get(
            f"/api/deploy/compare?old_id={sid1}&new_id={sid3}",
            headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["old"]["version"], "1.0.0")
        self.assertEqual(data["new"]["version"], "1.2.0")

    def test_compare_manual_mode_missing_ids(self):
        """手动模式：只传一个 ID 应返回 400"""
        resp = self.client.get(
            "/api/deploy/compare?old_id=1",
            headers=self.headers)
        self.assertEqual(resp.status_code, 400)

    def test_compare_default_mode_missing_env(self):
        """默认模式：不传 env 应返回 400"""
        resp = self.client.get(
            "/api/deploy/compare?project_id=1",
            headers=self.headers)
        self.assertEqual(resp.status_code, 400)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_deploy_enhance.py::TestDeployEnhanceAPI::test_compare_by_snapshot_ids -v`
Expected: FAIL (新测试尚未实现)

- [ ] **Step 3: 修改 compare_snapshots 路由**

替换 `webapp/routes/deploy_enhance_routes.py` 中的 `compare_snapshots` 函数（第 556-596 行）：

```python
@deploy_enhance_bp.route("/deploy/compare", methods=["GET"])
@require_auth
def compare_snapshots():
    old_id = request.args.get("old_id", type=int)
    new_id = request.args.get("new_id", type=int)

    # 手动模式：old_id + new_id 都存在
    if old_id and new_id:
        old = dbm.get_record(_db_path(), "deployment_snapshots", old_id)
        new = dbm.get_record(_db_path(), "deployment_snapshots", new_id)
        if not old or not new:
            return jsonify({"error": "指定的快照不存在"}), 404
    else:
        # 手动模式参数不完整时，走默认模式
        if old_id or new_id:
            return jsonify({"error": "手动模式需要同时提供 old_id 和 new_id"}), 400
        project_id = request.args.get("project_id", type=int)
        env = request.args.get("env", "")
        if not project_id or not env:
            return jsonify({"error": "project_id 和 env 参数必填"}), 400
        rows = dbm.list_records(_db_path(), "deployment_snapshots",
                                "project_id=? AND env=?",
                                (project_id, env), "deployed_at DESC")
        if len(rows) < 2:
            return jsonify({"error": "至少需要 2 个快照才能对比"}), 400
        old = rows[1]
        new = rows[0]

    old_versions = old.get("image_versions", {})
    new_versions = new.get("image_versions", {})
    if isinstance(old_versions, str):
        try:
            old_versions = json.loads(old_versions)
        except (ValueError, TypeError):
            old_versions = {}
    if isinstance(new_versions, str):
        try:
            new_versions = json.loads(new_versions)
        except (ValueError, TypeError):
            new_versions = {}
    # 计算变更
    changes = []
    all_keys = set(old_versions.keys()) | set(new_versions.keys())
    for k in sorted(all_keys):
        old_v = old_versions.get(k, "")
        new_v = new_versions.get(k, "")
        if old_v != new_v:
            changes.append({"service": k, "old": old_v, "new": new_v})
    return jsonify({"old": {"id": old["id"], "version": old["version"],
                            "env": old.get("env", ""),
                            "deployed_at": old["deployed_at"],
                            "image_versions": old_versions},
                    "new": {"id": new["id"], "version": new["version"],
                            "env": new.get("env", ""),
                            "deployed_at": new["deployed_at"],
                            "image_versions": new_versions},
                    "changes": changes})
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_deploy_enhance.py::TestDeployEnhanceAPI -v`
Expected: 全部 PASS（包括原有的 `test_snapshots_and_compare` 和新增的 3 个测试）

- [ ] **Step 5: Commit**

```bash
git add webapp/routes/deploy_enhance_routes.py tests/test_deploy_enhance.py
git commit -m "feat: compare API supports manual mode via old_id/new_id params"
```

---

### Task 2: 前端 — 重写 deploy_compare.js

**Files:**
- Rewrite: `webapp/static/js/pages/deploy_compare.js`

- [ ] **Step 1: 重写 deploy_compare.js**

完整替换 `webapp/static/js/pages/deploy_compare.js` 内容：

```javascript
// pages/deploy_compare.js — 发布版本对比（环境筛选 + 快照任意选择）
(function(){
  let destroyed = false;
  let projects = [];
  let selectedProject = null;
  let envs = [];
  let selectedEnv = '';
  let allSnapshots = [];   // 当前项目所有环境的快照（用于快照选择面板）
  let panelOpen = false;    // 快照选择面板是否展开

  async function loadProjects(){
    projects = await api('/api/projects').catch(() => []);
    if (projects.length && !selectedProject) selectedProject = projects[0].id;
  }

  async function loadEnvs(){
    envs = [];
    if (!selectedProject) return;
    envs = await api(`/api/projects/${selectedProject}/environments`).catch(() => []);
    if (selectedEnv && !envs.some(e => e.name === selectedEnv)) selectedEnv = '';
    if (!selectedEnv && envs.length) selectedEnv = envs[0].name;
  }

  async function loadAllSnapshots(){
    if (!selectedProject) return;
    allSnapshots = await api(`/api/deploy/snapshots/${selectedProject}`).catch(() => []);
  }

  async function doCompareDefault(){
    if (!selectedProject || !selectedEnv) return;
    const box = document.getElementById('compareBox');
    if (!box) return;
    box.innerHTML = skeletonHTML(2);
    try {
      const data = await api(`/api/deploy/compare?project_id=${selectedProject}&env=${encodeURIComponent(selectedEnv)}`);
      renderCompareResult(box, data);
    } catch(e) {
      box.innerHTML = `<div class="panel">${emptyState('📊', e.message || '对比失败')}</div>`;
    }
  }

  async function doCompareManual(oldId, newId){
    const box = document.getElementById('compareBox');
    if (!box) return;
    box.innerHTML = skeletonHTML(2);
    try {
      const data = await api(`/api/deploy/compare?old_id=${oldId}&new_id=${newId}`);
      renderCompareResult(box, data);
    } catch(e) {
      box.innerHTML = `<div class="panel">${emptyState('📊', e.message || '对比失败')}</div>`;
    }
  }

  function renderCompareResult(box, data){
    const changeTag = (c) => {
      if (c === 'upgraded') return '<span class="tag-upgrade">↑ 升级</span>';
      if (c === 'downgraded') return '<span class="tag-downgrade">↓ 回滚</span>';
      return '<span class="tag-unchanged">= 未变</span>';
    };
    const diff = data.changes || [];
    const allServices = new Set();
    if (data.old && data.old.image_versions) Object.keys(data.old.image_versions).forEach(k => allServices.add(k));
    if (data.new && data.new.image_versions) Object.keys(data.new.image_versions).forEach(k => allServices.add(k));
    const rows = [];
    allServices.forEach(svc => {
      const d = diff.find(x => x.service === svc);
      const oldV = (data.old && data.old.image_versions && data.old.image_versions[svc]) || '-';
      const newV = (data.new && data.new.image_versions && data.new.image_versions[svc]) || '-';
      const change = d ? (oldV === '-' ? 'upgraded' : 'downgraded') : 'unchanged';
      rows.push({service: svc, old: oldV, new: newV, change});
    });
    box.innerHTML = `
      <div class="panel" style="padding:16px">
        <div style="display:flex;gap:24px;margin-bottom:16px;flex-wrap:wrap">
          <div><span class="dg-label">旧版本</span> <strong>${esc(data.old.version)}</strong>
            <span class="muted"> · ${esc(data.old.env || '')} · ${fmtTime(data.old.deployed_at)}</span></div>
          <div><span class="dg-label">新版本</span> <strong>${esc(data.new.version)}</strong>
            <span class="muted"> · ${esc(data.new.env || '')} · ${fmtTime(data.new.deployed_at)}</span></div>
        </div>
        <div class="tbl-wrap"><table class="tbl compare-tbl">
          <thead><tr><th>服务</th><th>旧版本</th><th>新版本</th><th>变化</th></tr></thead>
          <tbody>${rows.map(d => `<tr class="${d.change !== 'unchanged' ? 'changed' : ''}">
            <td>${esc(d.service)}</td>
            <td class="num">${esc(d.old)}</td>
            <td class="num">${esc(d.new)}</td>
            <td>${changeTag(d.change)}</td>
          </tr>`).join('')}</tbody>
        </table></div>
      </div>`;
  }

  function renderSnapshotPanel(){
    const panel = document.getElementById('snapshotPanel');
    if (!panel) return;
    if (!panelOpen) { panel.classList.add('hidden'); return; }
    panel.classList.remove('hidden');
    // 按环境分组快照
    const envGroups = {};
    allSnapshots.forEach(s => {
      const env = s.env || 'unknown';
      if (!envGroups[env]) envGroups[env] = [];
      envGroups[env].push(s);
    });
    const renderCol = (colId, title) => {
      const envKeys = Object.keys(envGroups).sort();
      if (!envKeys.length) return `<div class="snap-col"><h4>${title}</h4><p class="muted">暂无快照</p></div>`;
      return `<div class="snap-col"><h4>${title}</h4>
        ${envKeys.map(env => `
          <div class="snap-env-group">
            <div class="snap-env-label">${esc(env)}</div>
            ${envGroups[env].map(s => `
              <label class="snap-radio">
                <input type="radio" name="${colId}" value="${s.id}" data-env="${esc(env)}">
                <span class="snap-info">
                  <strong>${esc(s.version || '未知版本')}</strong>
                  <span class="muted">${fmtTime(s.deployed_at)}</span>
                </span>
              </label>`).join('')}
          </div>`).join('')}
      </div>`;
    };
    panel.innerHTML = `
      <div class="snap-panel">
        ${renderCol('snapOld', '旧版本')}
        <div style="display:flex;align-items:center;font-size:18px;color:var(--text-2)">→</div>
        ${renderCol('snapNew', '新版本')}
      </div>
      <div style="display:flex;justify-content:flex-end;margin-top:12px">
        <button class="btn primary" id="snapCompareBtn" disabled>开始对比</button>
      </div>`;
    // 绑定 radio 变更 → 启用/禁用对比按钮
    panel.querySelectorAll('input[type=radio]').forEach(r => {
      r.onchange = () => {
        const oldSel = panel.querySelector('input[name=snapOld]:checked');
        const newSel = panel.querySelector('input[name=snapNew]:checked');
        const btn = document.getElementById('snapCompareBtn');
        if (btn) {
          const sameId = oldSel && newSel && oldSel.value === newSel.value;
          btn.disabled = !oldSel || !newSel || sameId;
          if (sameId) toast('请选择两条不同的快照', 'error');
        }
      };
    });
    const cmpBtn = document.getElementById('snapCompareBtn');
    if (cmpBtn) cmpBtn.onclick = () => {
      const oldSel = panel.querySelector('input[name=snapOld]:checked');
      const newSel = panel.querySelector('input[name=snapNew]:checked');
      if (oldSel && newSel) doCompareManual(Number(oldSel.value), Number(newSel.value));
    };
  }

  window.deploy_compare = {
    render: async function(root){
      destroyed = false;
      panelOpen = false;
      await loadProjects();
      await loadEnvs();
      await loadAllSnapshots();
      if (destroyed) return [];
      root.innerHTML = `
        <div class="deploy-toolbar">
          <span class="weak" style="font-size:12px">项目</span>
          <select class="filter-sel" id="cmpProjSel">${projects.map(p =>
            `<option value="${p.id}" ${p.id === selectedProject ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select>
          <span class="weak" style="font-size:12px">环境</span>
          <select class="filter-sel" id="cmpEnvSel">${envs.map(e =>
            `<option value="${esc(e.name)}" ${e.name === selectedEnv ? 'selected' : ''}>${esc(e.name)}</option>`).join('')}</select>
          <span style="flex:1"></span>
          <button class="btn ghost" id="togglePanelBtn">📋 选择快照对比</button>
        </div>
        <div id="snapshotPanel" class="hidden"></div>
        <div id="compareBox">
          ${envs.length === 0 ? `<div class="panel">${emptyState('📊', '该项目暂无环境')}</div>`
            : !selectedEnv ? `<div class="panel">${emptyState('📊', '请选择环境')}</div>`
            : skeletonHTML(2)}
        </div>`;
      // 项目切换
      document.getElementById('cmpProjSel').onchange = async (e) => {
        selectedProject = Number(e.target.value);
        selectedEnv = '';
        await loadEnvs();
        await loadAllSnapshots();
        if (destroyed) return [];
        // 重建环境选项
        const envSel = document.getElementById('cmpEnvSel');
        if (envSel) {
          envSel.innerHTML = envs.map(e =>
            `<option value="${esc(e.name)}" ${e.name === selectedEnv ? 'selected' : ''}>${esc(e.name)}</option>`).join('');
          enhanceSelect(envSel);
        }
        if (selectedEnv) await doCompareDefault();
        else {
          const box = document.getElementById('compareBox');
          if (box) box.innerHTML = `<div class="panel">${emptyState('📊', '请选择环境')}</div>`;
        }
      };
      // 环境切换
      const envSel = document.getElementById('cmpEnvSel');
      if (envSel) {
        envSel.onchange = async (e) => {
          selectedEnv = e.target.value;
          await doCompareDefault();
        };
      }
      // 快照选择面板切换
      const toggleBtn = document.getElementById('togglePanelBtn');
      if (toggleBtn) toggleBtn.onclick = () => {
        panelOpen = !panelOpen;
        renderSnapshotPanel();
      };
      // 自动对比
      if (selectedEnv) await doCompareDefault();
      return [];
    },
    destroy: function(){ destroyed = true; }
  };
})();
```

- [ ] **Step 2: 在浏览器中测试默认模式**

打开应用，导航到发布对比页：
1. 确认工具栏显示项目选择 + 环境选择
2. 切换环境后自动加载对比结果
3. 点击「选择快照对比」展开面板
4. 在面板中选择两条不同快照，点击「开始对比」

- [ ] **Step 3: Commit**

```bash
git add webapp/static/js/pages/deploy_compare.js
git commit -m "feat: deploy compare page with env filter and manual snapshot selection"
```

---

### Task 3: 前端 — 重写 deploy_canary.js

**Files:**
- Rewrite: `webapp/static/js/pages/deploy_canary.js`

- [ ] **Step 1: 重写 deploy_canary.js**

完整替换 `webapp/static/js/pages/deploy_canary.js` 内容：

```javascript
// pages/deploy_canary.js — 灰度策略配置（模板驱动表单编辑器）
(function(){
  let destroyed = false;
  let configs = [];
  let projects = [];

  // 预设模板
  const CANARY_TEMPLATES = {
    standard: {
      name: '标准三批',
      steps: [
        { servers: 1, observe_minutes: 5, auto_advance: true },
        { servers: 2, observe_minutes: 10, auto_advance: true },
        { servers: 'all', observe_minutes: 0, auto_advance: false }
      ]
    },
    quick: {
      name: '快速两批',
      steps: [
        { servers: 1, observe_minutes: 3, auto_advance: true },
        { servers: 'all', observe_minutes: 0, auto_advance: false }
      ]
    },
    conservative: {
      name: '保守五批',
      steps: [
        { servers: 1, observe_minutes: 5, auto_advance: false },
        { servers: 2, observe_minutes: 10, auto_advance: true },
        { servers: 3, observe_minutes: 15, auto_advance: true },
        { servers: 5, observe_minutes: 10, auto_advance: true },
        { servers: 'all', observe_minutes: 0, auto_advance: false }
      ]
    },
    full: {
      name: '全量一次',
      steps: [
        { servers: 'all', observe_minutes: 0, auto_advance: false }
      ]
    }
  };

  async function loadData(){
    projects = await api('/api/projects').catch(() => []);
    configs = await api('/api/deploy/canary-configs').catch(() => []);
  }

  function renderStepsPreview(steps){
    if (!Array.isArray(steps) || !steps.length) return '';
    const maxSrv = Math.max(...steps.map(s => s.servers === 'all' ? 10 : s.servers));
    return `<div class="canary-preview">
      ${steps.map((s, i) => {
        const srv = s.servers === 'all' ? '全量' : s.servers + '台';
        const h = s.servers === 'all' ? 100 : Math.max(20, (s.servers / maxSrv) * 100);
        return `<div class="preview-bar" style="height:${h}%" title="${srv} · ${s.observe_minutes}min · ${s.auto_advance ? '自动' : '人工'}">
          <span class="preview-label">${srv}</span>
          <span class="preview-time">${s.observe_minutes}min</span>
        </div>`;
      }).join('')}
    </div>`;
  }

  function renderStepsDisplay(steps){
    if (!Array.isArray(steps)) return '-';
    return steps.map((s, i) => `
      ${i > 0 ? '<span class="canary-arrow">→</span>' : ''}
      <div class="canary-step">
        <div class="step-num">${s.servers === 'all' ? '全量' : s.servers + '台'}</div>
        <div class="step-label">观察 ${s.observe_minutes}min</div>
        <div class="step-label">${s.auto_advance ? '自动推进' : '人工确认'}</div>
      </div>`).join('');
  }

  function renderList(){
    const root = document.getElementById('pageRoot');
    const box = root.querySelector('#canaryBox');
    if (!box) return;
    const projMap = {};
    projects.forEach(p => projMap[p.id] = p.name);
    box.innerHTML = configs.length ? configs.map(c => `
      <div class="panel" style="padding:16px;margin-bottom:12px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
          <div><strong>${esc(c.name)}</strong> <span class="muted">· ${esc(projMap[c.project_id] || '全局')}</span>
          ${c.is_default ? ' <span class="tag tag-ok">默认</span>' : ''}</div>
          <div style="display:flex;gap:8px">
            ${canDeploy() ? `<button class="btn ghost sm" data-edit="${c.id}">编辑</button>` : ''}
            <button class="btn danger ghost sm" data-del="${c.id}">删除</button>
          </div>
        </div>
        <div class="canary-steps">${renderStepsDisplay(c.steps)}</div>
      </div>`).join('')
      : `<div class="panel">${emptyState('🐤', '暂无灰度策略')}</div>`;
    box.querySelectorAll('[data-del]').forEach(btn => btn.onclick = async () => {
      if (!await confirmDialog('确认删除此策略？')) return;
      await api(`/api/deploy/canary-configs/${btn.dataset.del}`, {method:'DELETE'});
      await loadData(); renderList();
    });
    box.querySelectorAll('[data-edit]').forEach(btn => btn.onclick = () => {
      const c = configs.find(x => x.id === Number(btn.dataset.edit));
      if (c) openForm(c);
    });
  }

  function openForm(existing){
    const isEdit = !!existing;
    const title = isEdit ? '编辑灰度策略' : '新建灰度策略';
    let batchRows = existing ? (existing.steps || []).slice() : [];

    const body = `
      <div class="form-grid">
        <div class="field"><label>策略名称</label><input id="canaryName" value="${esc(existing ? existing.name : '')}" placeholder="如：标准三批"></div>
        <div class="field"><label>所属项目</label>
          <select id="canaryProj">${projects.map(p =>
            `<option value="${p.id}" ${existing && p.id === existing.project_id ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select></div>
        ${!isEdit ? `<div class="field full">
          <label>快速模板</label>
          <div class="template-pills" id="templatePills">
            ${Object.entries(CANARY_TEMPLATES).map(([k, v]) =>
              `<button class="pill" data-tpl="${k}">${v.name}</button>`).join('')}
            <button class="pill" data-tpl="custom">+ 自定义</button>
          </div>
        </div>` : ''}
        <div class="field full">
          <label>批次配置</label>
          <div id="batchEditor"></div>
        </div>
        <div class="field full">
          <label>预览</label>
          <div id="batchPreview" style="min-height:60px"></div>
        </div>
        <div class="field full check"><label><input type="checkbox" id="canaryDefault" ${existing && existing.is_default ? 'checked' : ''}> 设为默认策略</label></div>
      </div>
      <div style="display:flex;gap:10px;margin-top:20px">
        <button class="btn primary flex-1" id="canarySave">保存</button>
      </div>`;

    openDrawer({ title, wide: true, body });

    function getBatchRows(){ return batchRows; }

    function renderBatchEditor(){
      const editor = document.getElementById('batchEditor');
      if (!editor) return;
      if (!batchRows.length) {
        editor.innerHTML = `<p class="muted" style="padding:8px 0">暂无批次，点击下方按钮添加</p>`;
      } else {
        editor.innerHTML = `<table class="tbl" style="margin-bottom:8px">
          <thead><tr><th style="width:40px">#</th><th>服务器数量</th><th>观察时间(分钟)</th><th>推进方式</th><th style="width:40px"></th></tr></thead>
          <tbody>${batchRows.map((r, i) => `<tr>
            <td class="num">${i + 1}</td>
            <td>${r.servers === 'all'
              ? '<input type="number" class="batch-srv" data-i="' + i + '" value="" placeholder="全部" min="1" style="width:80px"> <select class="batch-all-sel" data-i="' + i + '"><option value="all" selected>全部</option><option value="num">指定数量</option></select>'
              : '<input type="number" class="batch-srv" data-i="' + i + '" value="' + r.servers + '" min="1" style="width:80px"> <select class="batch-all-sel" data-i="' + i + '"><option value="num" selected>指定数量</option><option value="all">全部</option></select>'}</td>
            <td><input type="number" class="batch-obs" data-i="${i}" value="${r.observe_minutes}" min="0" style="width:80px"></td>
            <td><select class="batch-adv" data-i="${i}">
              <option value="true" ${r.auto_advance ? 'selected' : ''}>自动推进</option>
              <option value="false" ${!r.auto_advance ? 'selected' : ''}>人工确认</option>
            </select></td>
            <td><button class="btn danger ghost sm" data-rm="${i}">✕</button></td>
          </tr>`).join('')}</tbody>
        </table>
        <button class="btn ghost sm" id="addBatchBtn">+ 添加批次</button>`;
      }
      // 绑定事件
      editor.querySelectorAll('.batch-srv').forEach(inp => {
        inp.oninput = () => { batchRows[Number(inp.dataset.i)].servers = Number(inp.value) || 1; updatePreview(); };
      });
      editor.querySelectorAll('.batch-all-sel').forEach(sel => {
        sel.onchange = () => {
          const idx = Number(sel.dataset.i);
          if (sel.value === 'all') {
            batchRows[idx].servers = 'all';
          } else {
            batchRows[idx].servers = 1;
          }
          renderBatchEditor();
          updatePreview();
        };
      });
      editor.querySelectorAll('.batch-obs').forEach(inp => {
        inp.oninput = () => { batchRows[Number(inp.dataset.i)].observe_minutes = Number(inp.value) || 0; updatePreview(); };
      });
      editor.querySelectorAll('.batch-adv').forEach(sel => {
        sel.onchange = () => { batchRows[Number(sel.dataset.i)].auto_advance = sel.value === 'true'; };
      });
      editor.querySelectorAll('[data-rm]').forEach(btn => {
        btn.onclick = () => { batchRows.splice(Number(btn.dataset.rm), 1); renderBatchEditor(); updatePreview(); };
      });
      const addBtn = document.getElementById('addBatchBtn');
      if (addBtn) addBtn.onclick = () => {
        batchRows.push({ servers: 1, observe_minutes: 5, auto_advance: true });
        renderBatchEditor();
        updatePreview();
      };
      updatePreview();
    }

    function updatePreview(){
      const prev = document.getElementById('batchPreview');
      if (prev) prev.innerHTML = batchRows.length ? renderStepsPreview(batchRows) : '<p class="muted">暂无批次</p>';
    }

    // 模板按钮
    if (!isEdit) {
      setTimeout(() => {
        document.querySelectorAll('#templatePills [data-tpl]').forEach(btn => {
          btn.onclick = () => {
            document.querySelectorAll('#templatePills .pill').forEach(p => p.classList.remove('active'));
            btn.classList.add('active');
            const tpl = CANARY_TEMPLATES[btn.dataset.tpl];
            if (btn.dataset.tpl === 'custom') {
              batchRows = [{ servers: 1, observe_minutes: 5, auto_advance: true }];
            } else if (tpl) {
              batchRows = tpl.steps.map(s => ({...s}));
            }
            renderBatchEditor();
          };
        });
        renderBatchEditor();
      }, 0);
    } else {
      setTimeout(() => { renderBatchEditor(); }, 0);
    }

    // 保存
    setTimeout(() => {
      document.getElementById('canarySave').onclick = async () => {
        const name = document.getElementById('canaryName').value.trim();
        if (!name) { toastError('请填写策略名称'); return; }
        if (!batchRows.length) { toastError('至少需要一个批次'); return; }
        // 规范化 servers
        const steps = batchRows.map((r, i) => ({
          batch: i + 1,
          servers: r.servers === 'all' ? 'all' : Math.max(1, Number(r.servers) || 1),
          observe_minutes: Math.max(0, Number(r.observe_minutes) || 0),
          auto_advance: !!r.auto_advance
        }));
        const payload = {
          project_id: Number(document.getElementById('canaryProj').value),
          name,
          steps,
          is_default: document.getElementById('canaryDefault').checked
        };
        try {
          if (isEdit) {
            await api(`/api/deploy/canary-configs/${existing.id}`, {method:'PUT', body:JSON.stringify(payload)});
            toastSuccess('策略已更新');
          } else {
            await api('/api/deploy/canary-configs', {method:'POST', body:JSON.stringify(payload)});
            toastSuccess('策略已创建');
          }
          document.getElementById('dwClose').click();
          await loadData(); renderList();
        } catch(e) { toastError(e.message); }
      };
    }, 0);
  }

  window.deploy_canary = {
    render: async function(root){
      destroyed = false;
      await loadData();
      if (destroyed) return [];
      root.innerHTML = `
        <div class="deploy-toolbar">
          <span style="flex:1"></span>
          ${canDeploy() ? '<button class="btn primary" id="newCanaryBtn">+ 新建策略</button>' : ''}
        </div>
        <div id="canaryBox"></div>`;
      const nb = document.getElementById('newCanaryBtn');
      if (nb) nb.onclick = () => openForm(null);
      renderList();
      return [];
    },
    destroy: function(){ destroyed = true; }
  };
})();
```

- [ ] **Step 2: 添加预览条形图 CSS**

在 `webapp/static/css/pages/deploy-enhance.css` 末尾追加以下样式：

```css
/* 灰度策略预览条形图 */
.canary-preview {
  display: flex;
  align-items: flex-end;
  gap: 6px;
  height: 80px;
  padding: 8px 0;
}
.preview-bar {
  flex: 1;
  min-width: 32px;
  max-width: 60px;
  background: var(--primary, #4f8cff);
  border-radius: 4px 4px 0 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-end;
  padding: 4px 2px;
  position: relative;
  opacity: 0.8;
}
.preview-label {
  font-size: 10px;
  color: #fff;
  white-space: nowrap;
}
.preview-time {
  font-size: 9px;
  color: rgba(255,255,255,0.7);
  margin-top: 2px;
}

/* 模板胶囊按钮 */
.template-pills {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.template-pills .pill {
  padding: 4px 14px;
  border-radius: 20px;
  border: 1px solid var(--border);
  background: var(--surface-2);
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s;
}
.template-pills .pill:hover {
  border-color: var(--primary, #4f8cff);
}
.template-pills .pill.active {
  background: var(--primary, #4f8cff);
  color: #fff;
  border-color: var(--primary, #4f8cff);
}

/* 快照选择面板 */
.snap-panel {
  display: flex;
  gap: 16px;
  padding: 16px;
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 12px;
}
.snap-col {
  flex: 1;
  max-height: 300px;
  overflow-y: auto;
}
.snap-env-group {
  margin-bottom: 8px;
}
.snap-env-label {
  font-size: 11px;
  color: var(--text-2);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 4px;
  padding: 2px 6px;
  background: var(--surface-2);
  border-radius: 4px;
  display: inline-block;
}
.snap-radio {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.1s;
}
.snap-radio:hover {
  background: var(--surface-2);
}
.snap-radio input[type="radio"] {
  margin: 0;
}
.snap-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.snap-info strong {
  font-size: 13px;
}
.snap-info .muted {
  font-size: 11px;
}
```

- [ ] **Step 3: 在浏览器中测试**

打开应用，导航到灰度策略页：
1. 点击「+ 新建策略」
2. 点击模板胶囊按钮（如「标准三批」），确认批次编辑器正确填充
3. 修改某个批次的参数，确认预览条形图实时更新
4. 添加/删除批次，确认预览同步更新
5. 保存策略
6. 点击「编辑」按钮，确认数据正确回填

- [ ] **Step 4: Commit**

```bash
git add webapp/static/js/pages/deploy_canary.js webapp/static/css/pages/deploy-enhance.css
git commit -m "feat: canary strategy form editor with templates and live preview"
```

---

### Task 4: 集成测试 + 浏览器验证

**Files:**
- Test: `tests/test_deploy_enhance.py`

- [ ] **Step 1: 运行全量后端测试**

Run: `pytest tests/test_deploy_enhance.py -v`
Expected: 全部 PASS

- [ ] **Step 2: 浏览器端到端验证**

使用 chrome-devtools MCP 进行完整交互验证：

**发布对比页：**
1. 选择项目 → 环境列表正确填充
2. 切换环境 → 自动对比最近两次快照
3. 点击「选择快照对比」→ 面板展开，显示所有环境的快照
4. 选择两条不同环境的快照 → 点击「开始对比」→ 结果正确
5. 选择同一条快照 → 按钮禁用

**灰度策略页：**
1. 点击模板 → 批次行正确填充
2. 增删行 → 预览条形图同步更新
3. 保存 → 数据正确提交
4. 编辑已有策略 → 数据回填到表单

- [ ] **Step 3: 最终 Commit**

```bash
git add -A
git commit -m "feat: deploy compare env filter + canary form editor - complete"
```
