# 项目总览服务卡片服务重启功能设计规格

## 概述

在「项目总览」（设备总览，devices 页）的服务卡片上新增「重启」操作，以异步任务方式重启当前环境指定服务的全部容器实例，并内置重启后的应用健康检查。重启任务复用现有任务体系（tasks 表 + 后台单 worker + 日志流 + 终止/删除/重新运行），与发布/回滚一致受发布窗口约束。

## 背景

### 现有能力

- 服务卡片（`webapp/static/js/pages/devices.js`）：项目/环境切换 + 服务卡片墙，卡片现有「发布」「回滚」「日志」按钮，操作类按钮按 `canDeploy()`（admin/operator）显隐
- 任务体系（`webapp/task_manager.py`）：单 worker 串行执行，`_run_task` 通过 `build_cli` 组装命令后 `subprocess.Popen` 调 `deploy.py`/`deploy_pipeline.py`；日志写 `data/logs/task_N.log`；支持终止/重新运行/删除
- SSH 连接池（`webapp/monitor.py`）：`_ssh_pool`（paramiko 客户端复用）、`_ssh_exec`/`_ssh_exec_multi`（远端命令执行）、`_merge_server_ssh`（服务器级凭据优先、回退环境级）、`_probe_health_url`（HTTP 健康检查）
- 服务 → 容器名映射：services 表 `service_name` 字段；环境服务器清单：`dbm.list_env_servers(db_path, env_id)`（按 order_no 排序，含 ssh_user/ssh_password/ssh_key_file 服务器级字段）
- 发布窗口：`deploy_scheduler.check_release_window(db_path, project_id)`，block 级 403 / warn 级 409 `window_warn`
- 审计：`audit(action, target_type, target_id, detail)` 装饰器
- tasks 表 `type` 为自由文本（无 CHECK 约束），`action` 默认 `'deploy'`

### 目标

1. 服务卡片提供「重启」按钮（admin/operator 可见）
2. 重启走任务系统异步执行，复用任务列表/日志抽屉/终止/删除/重新运行
3. 重启范围：当前环境绑定的全部服务器上运行该 `service_name` 容器的所有实例
4. 每台服务器 `docker restart` 后做应用健康检查（配置了 `health_port` 时），超时仅警告不判失败
5. 与发布/回滚一致受发布窗口约束、写审计日志

## 设计决策

| 决策点 | 结论 | 理由 |
| --- | --- | --- |
| 执行方式 | 任务系统异步（`type='restart'`） | 用户指定；复用全套任务能力 |
| 重启命令 | `docker restart <container>` | 直接操作容器、秒级完成；无需 compose 文件路径（部署链路才需要） |
| 健康检查 | 重启成功后轮询 `health_port`+`health_url` 最多 60s；超时仅警告 | 应用启动慢属常见，不因此判任务失败 |
| 失败判定 | 任一台 `docker restart` 失败 → 任务 `failed`；容器不存在 → 跳过；全部跳过 → `failed`；仅健康检查超时 → `success`（日志警告） | 与 monitor 单台容错风格一致 |
| 发布窗口 | 与发布一致：block 403、warn 409 二次确认 | 重启同为变更操作 |
| 并发 | 单 worker 串行排队，无需额外互斥 | 现有任务体系天然保证 |

## 后端设计

### 1. 任务创建（`webapp/routes/deploy_routes.py`）

`POST /api/tasks` 增加 `type='restart'` 分支：

- `ref_id` 必须解析为该项目下 services 记录（复用 `_resolve_ref`），否则 404
- `env` 必填且必须属于该项目（`dbm.get_env_by_name(db_path, project_id, env)`），否则 400
- 窗口检查（复用现有逻辑，位于分支前，对 restart 同样生效）
- `dbm.create_task(..., type_='restart', action='restart', version='', previous_version='')`
- `submit_task` 提交执行；`audit("restart", "service", ref_id, {"name": rec["name"], "env": env})`
- 校验沿用现有 `_bool_field` 风格；`rollback/auto_version/skip_build` 对 restart 强制为 false/缺省

### 2. 执行器（`webapp/task_manager.py`）

- `build_cli`：`type='restart'` 时返回空列表（不产 CLI），`_run_task` 按类型分发：
  - `pipeline` / `project` → 现有子进程路径（不变）
  - `restart` → 线程内直接执行 `_run_restart(db_path, task)`，日志写入 `log_file`
- `_run_restart` 执行流程：
  1. 取服务：`dbm.get_record(db_path, "services", task["ref_id"])` → `service_name`（容器名）
  2. 取环境：`dbm.get_env_by_name(db_path, task["project_id"], task["env"])` → `env_id`；服务器清单 `dbm.list_env_servers(db_path, env_id)`；环境级 ssh 凭据 `env.get("ssh")`
  3. `ThreadPoolExecutor` 并发处理每台服务器，单台流程：
     - `_merge_server_ssh(env_ssh, server)` 合并凭据
     - 容器存在性：`docker ps -a --format '{{.Names}}' | grep -Fx <service_name>`（-a 含停止容器，容器存在即重启；-F 字面量、-x 整行精确匹配，与 monitor 探测的容器名精确比较语义一致，避免子串误匹配如 hqb-auth 命中 hqb-auth-bak），无输出 → 日志 `[host] 容器不存在，跳过`，该台记 skipped
     - `docker restart <service_name>`（timeout 60s），失败 → 记 failed 并记录原因
     - 重启成功且服务配置了 `health_port > 0`：轮询 `_probe_health_url(host, health_port, health_url)` 最多 60s（间隔 5s）；通过 → ok；超时 → ok（日志标注 `[host] 健康检查超时（应用启动慢？），仍视为成功`）
  4. 汇总：全部成功/跳过且至少一台成功 → `success`；全部跳过（未找到容器）→ `failed`（日志注明）；任一台 docker restart 失败 → `failed`
  5. 每台一行日志：`[host] 重启成功（健康检查 Xms）/ 容器不存在 / 重启失败: <原因>`
- 依赖注入：`_run_restart` 直接 `from . import monitor as monitor_mod` 复用 `_ssh_pool`、`_ssh_exec_multi`、`_merge_server_ssh`、`_probe_health_url`（同进程，无跨进程问题）

### 3. 边界与错误处理

| 场景 | 处理 |
| --- | --- |
| env 不属于项目 | 创建接口 400 |
| ref_id 非本项目服务 | 创建接口 404（复用 `_resolve_ref`） |
| 服务停用（enabled=0） | 允许重启（停用仅影响发布列表展示） |
| 某台 SSH 失败/凭据缺失 | 单台 failed，不影响其他台 |
| 全部服务器 SSH 失败 | 任务 failed |
| 容器不存在（单台） | 跳过，不算失败 |
| 全部容器不存在 | 任务 failed，日志注明 |
| 健康检查超时 | 成功 + 警告日志 |
| 重启期间监控探测 | 容器先停后起，monitor 轮询可能短暂显示异常，属预期；卡片「发布中」状态覆盖 |

## 前端设计

### 1. 总览服务卡片（`webapp/static/js/pages/devices.js`）

- foot 按钮区：`canDeploy()` 时在「回滚」旁增加 `<button class="btn sm ghost" data-restart="${s.id}">⭯ 重启</button>`
- 点击流程：
  1. `confirmDialog('确认重启服务「name」？环境：currentEnv，将重启全部实例')`
  2. `POST /api/tasks {type:'restart', project_id, ref_id, env, force_window:false}`
  3. 成功 → `toastSuccess('任务 #id 已创建')`，`location.hash = '#/deploy'`（与发布/回滚一致）
  4. `window_warn` → 二次确认后 `force_window:true` 重发（与发布/回滚同款交互，复用同一模式代码结构）
- 卡片状态：running 任务匹配条件扩展为
  `r.type === 'project' && r.name === s.name && r.env === state.currentEnv` 或
  `r.type === 'restart' && r.ref_id === s.id && r.env === state.currentEnv`
  命中 → 卡片 `publishing` 高亮 + 状态灯 warn + 标签「发布中」（重启期间不误显示在线）
- 绑定：`wall.querySelectorAll('[data-restart]').forEach(b => b.onclick = ...)`（stopPropagation 无需，卡片无整卡点击）

### 2. 发布中心（`webapp/static/js/pages/deploy.js`）

- 操作列（列表 + 详情抽屉）：`action === 'restart'` → `<span class="tag tag-info">重启</span>`
- 类型列：`type === 'restart'` → 「重启」；详情抽屉「发布版本」行 restart 显示 `-`
- 操作筛选 chips：`[['all','全部'],['deploy','发布'],['rollback','回滚'],['restart','重启']]`
- 重新运行：`rerunTask` 按原参数 POST `type='restart'`，后端新分支直接支持，前端逻辑无需改
- 版本列现有逻辑（version/previous_version 空 → '-'）已覆盖 restart

### 3. 样式（`webapp/static/css/components.css`）

- 新增 `tag-info` 变体（青蓝色，与 `tag-warn` 橙色区分）：
  ```css
  .tag.tag-info{background:rgba(0,212,255,.12);color:var(--primary);border:1px solid rgba(0,212,255,.35)}
  ```

## 测试计划（新增 `tests/test_restart_task.py`）

1. **路由层**（mock `submit_task`/`create_task`）：
   - 创建 restart：合法 → 201；env 不属于项目 → 400；ref_id 非服务/越权 → 404
   - block 窗口 → 403；warn 窗口不带 force_window → 409
2. **执行层**（mock `_ssh_exec_multi`/`_probe_health_url`）：
   - 多服务器：容器存在 → restart → 健康检查通过 → success
   - 单台 restart 失败 → failed（其余台日志保留）
   - 全部容器不存在 → failed
   - 健康检查超时 → success + 警告日志
   - 日志文件写入断言（每台一行、汇总行）
3. **task_manager 分发**：`type='restart'` 不产 CLI、走 `_run_restart`；`project/pipeline` 现有行为回归

## 涉及文件

| 文件 | 改动 |
| --- | --- |
| `webapp/routes/deploy_routes.py` | `/api/tasks` 支持 restart 分支 |
| `webapp/task_manager.py` | 类型分发 + `_run_restart` 执行器 |
| `webapp/static/js/pages/devices.js` | 卡片重启按钮 + 状态匹配扩展 |
| `webapp/static/js/pages/deploy.js` | 标签/类型列/筛选适配 |
| `webapp/static/css/components.css` | `tag-info` 变体 |
| `tests/test_restart_task.py` | 新增测试 |
