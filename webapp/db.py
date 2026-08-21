"""SQLite 数据访问（通用部署平台版）：泛型 CRUD + JSON 序列化 + 删除保护 + 审计 + 告警系统"""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    code TEXT UNIQUE NOT NULL,
    description TEXT DEFAULT '',
    git_org TEXT DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    image_name TEXT NOT NULL,
    service_name TEXT NOT NULL,
    docker_context TEXT DEFAULT '',
    dockerfile TEXT DEFAULT '',
    build_order TEXT DEFAULT '[]',
    nacos_groups TEXT DEFAULT '[]',
    dubbo_services TEXT DEFAULT '[]',
    snailjob_group TEXT DEFAULT '',
    health_url TEXT DEFAULT '',
    health_port INTEGER DEFAULT 0,
    log_error_keywords TEXT DEFAULT '[]',
    clear_log_dir TEXT DEFAULT '',
    git_repo TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    UNIQUE(project_id, name)
);
CREATE TABLE IF NOT EXISTS environments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    is_default INTEGER DEFAULT 0,
    registry TEXT DEFAULT '{}',
    nacos TEXT DEFAULT '{}',
    mysql_snailjob TEXT DEFAULT '{}',
    ssh TEXT DEFAULT '{}',
    build TEXT DEFAULT '{}',
    deploy TEXT DEFAULT '{}',
    docker_compose_path TEXT DEFAULT '',
    maven_profile TEXT DEFAULT '',
    monitor_enabled INTEGER DEFAULT 1,
    UNIQUE(project_id, name)
);
CREATE TABLE IF NOT EXISTS servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL,
    -- ssh 系列列 NULL = 未配置（导出时继承环境级 ssh），显式值（含空串）才是覆盖
    ssh_port INTEGER,
    ssh_user TEXT,
    ssh_password TEXT,
    ssh_key_file TEXT,
    nacos_ip TEXT DEFAULT '',
    remark TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    UNIQUE(host)
);
CREATE TABLE IF NOT EXISTS env_servers (
    env_id INTEGER NOT NULL REFERENCES environments(id),
    server_id INTEGER NOT NULL REFERENCES servers(id),
    order_no INTEGER DEFAULT 0,
    PRIMARY KEY (env_id, server_id)
);
CREATE TABLE IF NOT EXISTS pipelines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    build_first INTEGER DEFAULT 1,
    stages TEXT DEFAULT '[]',
    UNIQUE(project_id, name)
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',
    must_change_password INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    type TEXT NOT NULL,
    ref_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    env TEXT DEFAULT '',
    version TEXT DEFAULT '',
    previous_version TEXT DEFAULT '',
    extra TEXT DEFAULT '',
    action TEXT DEFAULT 'deploy',
    status TEXT DEFAULT 'pending',
    log_file TEXT DEFAULT '',
    created_by INTEGER DEFAULT 0,
    started_at INTEGER,
    finished_at INTEGER
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER DEFAULT 0,
    username TEXT DEFAULT '',
    action TEXT NOT NULL,
    target_type TEXT DEFAULT '',
    target_id INTEGER DEFAULT 0,
    detail TEXT DEFAULT '{}',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS alert_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    metric_type TEXT NOT NULL DEFAULT 'metric',
    metric TEXT DEFAULT '',
    exclude_keyword TEXT DEFAULT '',
    compare TEXT DEFAULT '>',
    threshold REAL DEFAULT 0,
    duration_seconds INTEGER DEFAULT 0,
    scope TEXT DEFAULT '{"type":"all","ids":[]}',
    severity TEXT DEFAULT 'P2',
    channels TEXT DEFAULT '[]',
    escalation_interval INTEGER DEFAULT 30,
    enabled INTEGER DEFAULT 1,
    remark TEXT DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER DEFAULT 0,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'firing',
    severity TEXT NOT NULL DEFAULT 'P2',
    subject TEXT NOT NULL,
    description TEXT DEFAULT '',
    target_info TEXT DEFAULT '{}',
    first_seen_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    ack_by TEXT DEFAULT '',
    ack_at INTEGER,
    resolved_by TEXT DEFAULT '',
    resolved_at INTEGER,
    resolved_type TEXT DEFAULT '',
    notify_count INTEGER DEFAULT 0,
    escalation_level INTEGER DEFAULT 0,
    escalation_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_active
    ON alerts(fingerprint) WHERE status != 'resolved';
CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL,
    channel TEXT NOT NULL,
    target TEXT DEFAULT '',
    payload TEXT DEFAULT '',
    ok INTEGER DEFAULT 0,
    detail TEXT DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS silence_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'one_time',
    start_at TEXT DEFAULT '0',
    end_at TEXT DEFAULT '0',
    weekdays TEXT DEFAULT '[]',
    scope TEXT DEFAULT '{"rules":[],"envs":[],"servers":[]}',
    enabled INTEGER DEFAULT 1,
    remark TEXT DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS platform_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    value TEXT DEFAULT '',
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS log_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    env TEXT NOT NULL DEFAULT '',
    service TEXT NOT NULL DEFAULT '',
    container TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL DEFAULT '',
    line_offset INTEGER NOT NULL DEFAULT 0,
    line_count INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL DEFAULT '',
    keywords TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_log_entries_env_svc_ts
    ON log_entries(env, service, ts, id);
CREATE TABLE IF NOT EXISTS log_scan_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    env TEXT NOT NULL DEFAULT '',
    server TEXT NOT NULL DEFAULT '',
    container TEXT NOT NULL DEFAULT '',
    service TEXT NOT NULL DEFAULT '',
    last_ts TEXT DEFAULT '',
    last_line INTEGER DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_log_scan_state_key
    ON log_scan_state(env, server, container);
CREATE VIRTUAL TABLE IF NOT EXISTS log_fts USING fts5(
    ts, env, service, content, content='log_entries', content_rowid='id'
);
CREATE TABLE IF NOT EXISTS deploy_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    type TEXT NOT NULL DEFAULT 'once',
    name TEXT NOT NULL,
    ref_type TEXT DEFAULT 'project',
    ref_id INTEGER DEFAULT 0,
    env TEXT DEFAULT '',
    version TEXT DEFAULT '',
    previous_version TEXT DEFAULT '',
    extra TEXT DEFAULT '[]',
    schedule_at INTEGER DEFAULT 0,
    cron_expr TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    last_run_at INTEGER DEFAULT 0,
    next_run_at INTEGER DEFAULT 0,
    created_by INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS release_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,
    start_at INTEGER NOT NULL,
    end_at INTEGER NOT NULL,
    block_level TEXT NOT NULL DEFAULT 'warn',
    repeat_yearly INTEGER DEFAULT 0,
    created_by INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS service_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    source_service TEXT NOT NULL,
    target_service TEXT NOT NULL,
    dep_type TEXT NOT NULL DEFAULT 'manual',
    created_at INTEGER NOT NULL,
    UNIQUE(project_id, source_service, target_service)
);
CREATE TABLE IF NOT EXISTS deployment_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    env TEXT NOT NULL DEFAULT '',
    task_id INTEGER DEFAULT 0,
    version TEXT DEFAULT '',
    image_versions TEXT DEFAULT '{}',
    nacos_config_hash TEXT DEFAULT '',
    env_vars_hash TEXT DEFAULT '',
    deployed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS canary_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    steps TEXT NOT NULL DEFAULT '[]',
    is_default INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS response_time_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    env_id INTEGER NOT NULL DEFAULT 0,
    service TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT '',
    response_ms REAL NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rth_env_svc_ts
    ON response_time_history(env_id, service, created_at);
CREATE TABLE IF NOT EXISTS monitor_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    env_id INTEGER NOT NULL DEFAULT 0,
    host TEXT NOT NULL DEFAULT '',
    swap REAL,
    net_rx REAL,
    net_tx REAL,
    disk_read REAL,
    disk_write REAL,
    tcp_established INTEGER,
    tcp_time_wait INTEGER,
    tcp_close_wait INTEGER,
    restart_count INTEGER DEFAULT 0,
    uptime_hours REAL,
    docker_images_bytes INTEGER,
    docker_containers_bytes INTEGER,
    docker_volumes_bytes INTEGER,
    ssl_days REAL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ms_env_host_ts
    ON monitor_snapshots(env_id, host, created_at);
"""

# JSON 字段（读写时自动序列化/反序列化）
JSON_FIELDS = {
    "services": {"build_order", "nacos_groups", "dubbo_services", "log_error_keywords"},
    "environments": {"registry", "nacos", "mysql_snailjob", "ssh", "build", "deploy"},
    "pipelines": {"stages"},
    "alert_rules": {"scope", "channels"},
    "alerts": {"target_info"},
    "silence_windows": {"weekdays", "scope"},
    "tasks": {"extra"},
    "log_entries": {"keywords"},
    "deploy_schedules": {"extra"},
    "deployment_snapshots": {"image_versions"},
    "canary_configs": {"steps"},
}

# 表名白名单（泛型 CRUD 的标识符护栏，防 SQL 注入）
TABLES = frozenset(("projects", "services", "environments", "servers",
                    "env_servers", "pipelines", "users", "tasks", "audit_logs",
                    "alert_rules", "alerts", "alert_events",
                    "silence_windows", "platform_settings",
                    "log_entries", "log_scan_state",
                    "deploy_schedules", "release_windows",
                    "service_dependencies", "deployment_snapshots",
                    "canary_configs",
                    "response_time_history", "monitor_snapshots"))


def _check_table(table: str):
    if table not in TABLES:
        raise ValueError(f"非法表名: {table}")


def get_conn(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL 下并发读写（采集线程写 + 前端查询读）互不阻塞；
    # busy_timeout 避免多线程瞬时写冲突直接抛 SQLITE_BUSY
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(db_path: str):
    conn = get_conn(db_path)
    try:
        # WAL 为数据库级持久设置（写入库文件头），初始化时开启一次即可
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn):
    """存量库结构迁移（幂等）：services 补日志相关列，tasks 补列，log_entries 补 content 列等。
    services 加列先于其余迁移执行，避免提前 return 跳过加列。"""
    srv_cols = {r["name"] for r in conn.execute("PRAGMA table_info(services)")}
    if "log_retention_days" not in srv_cols:
        conn.execute("ALTER TABLE services ADD COLUMN log_retention_days INTEGER")
    if "log_collect_interval" not in srv_cols:
        conn.execute("ALTER TABLE services ADD COLUMN log_collect_interval INTEGER")
    task_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    # tasks 补列顺序：先加被引用列（extra/action 的 UPDATE 依赖 extra），再加其余列
    if "project_id" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN project_id INTEGER DEFAULT 0")
    if "ref_id" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN ref_id INTEGER DEFAULT 0")
    if "created_by" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN created_by INTEGER DEFAULT 0")
    if "extra" not in task_cols:
        # 任务运行参数（--skip-build/--rollback/--auto-version）持久化，供重新运行恢复
        conn.execute("ALTER TABLE tasks ADD COLUMN extra TEXT DEFAULT ''")
    if "action" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN action TEXT DEFAULT 'deploy'")
        # 存量数据：extra 含 --rollback 的记录回填为 rollback（extra 列必须先加好）
        conn.execute("UPDATE tasks SET action='rollback' WHERE extra LIKE '%--rollback%'")
    # log_entries 补 content 列（FTS5 外部内容模式要求内容表有 content 列）
    log_cols = {r["name"] for r in conn.execute("PRAGMA table_info(log_entries)")}
    if "content" not in log_cols:
        conn.execute("ALTER TABLE log_entries ADD COLUMN content TEXT NOT NULL DEFAULT ''")
    # alert_rules 补 exclude_keyword 列（日志关键词排除规则）
    ar_cols = {r["name"] for r in conn.execute("PRAGMA table_info(alert_rules)")}
    if "exclude_keyword" not in ar_cols:
        conn.execute("ALTER TABLE alert_rules ADD COLUMN exclude_keyword TEXT DEFAULT ''")
    # servers 表 role 列已废弃（发布顺序由 env_servers.order_no 决定），存量库删除该列
    srv_cols_now = {r["name"] for r in conn.execute("PRAGMA table_info(servers)")}
    if "role" in srv_cols_now:
        conn.execute("ALTER TABLE servers DROP COLUMN role")


def _serialize(table: str, fields: dict) -> dict:
    out = dict(fields)
    for k in JSON_FIELDS.get(table, set()):
        if k in out and not isinstance(out[k], str):
            out[k] = json.dumps(out[k], ensure_ascii=False)
    return out


def _deserialize(table: str, row: dict) -> dict:
    out = dict(row)
    for k in JSON_FIELDS.get(table, set()):
        if k in out and out[k]:
            try:
                out[k] = json.loads(out[k])
            except (TypeError, ValueError):
                pass
    return out


# ---------- 泛型 CRUD ----------

def get_record(db_path: str, table: str, record_id: int):
    _check_table(table)
    conn = get_conn(db_path)
    try:
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
        return _deserialize(table, dict(row)) if row else None
    finally:
        conn.close()


def list_records(db_path: str, table: str, where: str = "", params: tuple = (),
                 order: str = "id"):
    _check_table(table)
    conn = get_conn(db_path)
    try:
        sql = f"SELECT * FROM {table}"
        if where:
            sql += f" WHERE {where}"
        sql += f" ORDER BY {order}"
        rows = conn.execute(sql, params).fetchall()
        return [_deserialize(table, dict(r)) for r in rows]
    finally:
        conn.close()


def create_record(db_path: str, table: str, fields: dict) -> int:
    _check_table(table)
    fields = _serialize(table, fields)
    cols = ", ".join(fields.keys())
    marks = ", ".join("?" * len(fields))
    conn = get_conn(db_path)
    try:
        cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})",
                           tuple(fields.values()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_record(db_path: str, table: str, record_id: int, fields: dict) -> bool:
    _check_table(table)
    fields = _serialize(table, fields)
    if not fields:
        return False
    sets = ", ".join(f"{k}=?" for k in fields)
    conn = get_conn(db_path)
    try:
        cur = conn.execute(f"UPDATE {table} SET {sets} WHERE id=?",
                           tuple(fields.values()) + (record_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------- 删除保护 ----------

def delete_project(db_path: str, project_id: int) -> tuple:
    """返回 (ok, message)；项目下有服务/流水线/环境时禁止删除"""
    conn = get_conn(db_path)
    try:
        for table in ("services", "pipelines", "environments"):
            n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                             (project_id,)).fetchone()[0]
            if n:
                return False, f"项目下仍有 {n} 个{table}，请先删除"
        conn.execute("DELETE FROM tasks WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        conn.commit()
        return True, ""
    finally:
        conn.close()


def delete_environment(db_path: str, env_id: int) -> tuple:
    conn = get_conn(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM env_servers WHERE env_id=?",
                         (env_id,)).fetchone()[0]
        if n:
            return False, f"环境仍绑定 {n} 台服务器，请先解绑"
        # 注：tasks.env 存环境名而非环境 id，历史任务不阻止环境删除
        conn.execute("DELETE FROM environments WHERE id=?", (env_id,))
        conn.commit()
        return True, ""
    finally:
        conn.close()


def delete_server(db_path: str, server_id: int) -> tuple:
    conn = get_conn(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM env_servers WHERE server_id=?",
                         (server_id,)).fetchone()[0]
        if n:
            return False, f"服务器被 {n} 个环境引用，请先解绑"
        conn.execute("DELETE FROM servers WHERE id=?", (server_id,))
        conn.commit()
        return True, ""
    finally:
        conn.close()


def delete_record(db_path: str, table: str, record_id: int) -> bool:
    """泛型删除（users/services/pipelines 等无删除保护的简单表）；返回是否删除了记录"""
    _check_table(table)
    conn = get_conn(db_path)
    try:
        cur = conn.execute(f"DELETE FROM {table} WHERE id=?", (record_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------- 域查询 ----------

def list_services(db_path: str, project_id: int, enabled_only: bool = False) -> list:
    where = "project_id=?"
    params = [project_id]
    if enabled_only:
        where += " AND enabled=1"
    return list_records(db_path, "services", where, tuple(params), "id")


def list_environments(db_path: str, project_id: int) -> list:
    return list_records(db_path, "environments", "project_id=?", (project_id,), "id")


def list_pipelines(db_path: str, project_id: int) -> list:
    return list_records(db_path, "pipelines", "project_id=?", (project_id,), "id")


def get_service_by_name(db_path: str, project_id: int, name: str):
    rows = list_records(db_path, "services", "project_id=? AND name=?",
                        (project_id, name))
    return rows[0] if rows else None


def get_pipeline_by_name(db_path: str, project_id: int, name: str):
    rows = list_records(db_path, "pipelines", "project_id=? AND name=?",
                        (project_id, name))
    return rows[0] if rows else None


def get_env_by_name(db_path: str, project_id: int, name: str):
    rows = list_records(db_path, "environments", "project_id=? AND name=?",
                        (project_id, name))
    return rows[0] if rows else None


def get_default_env(db_path: str, project_id: int):
    rows = list_records(db_path, "environments",
                        "project_id=? AND is_default=1", (project_id,))
    if rows:
        return rows[0]
    rows = list_records(db_path, "environments", "project_id=?", (project_id,))
    return rows[0] if rows else None


def list_env_servers(db_path: str, env_id: int) -> list:
    """环境绑定的服务器（按 order_no 排序），每个元素含 server 字段 + order_no"""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT s.*, es.order_no FROM servers s "
            "JOIN env_servers es ON es.server_id=s.id "
            "WHERE es.env_id=? AND s.enabled=1 ORDER BY es.order_no",
            (env_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_server_envs(db_path: str, server_id: int) -> list:
    """服务器绑定的环境（按 order_no 排序）；供连接测试回退环境级 ssh 配置"""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT e.* FROM environments e "
            "JOIN env_servers es ON es.env_id=e.id "
            "WHERE es.server_id=? ORDER BY es.order_no",
            (server_id,)).fetchall()
        return [_deserialize("environments", dict(r)) for r in rows]
    finally:
        conn.close()


def bind_server(db_path: str, env_id: int, server_id: int, order_no: int = 0):
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT OR REPLACE INTO env_servers (env_id,server_id,order_no) "
                     "VALUES (?,?,?)", (env_id, server_id, order_no))
        conn.commit()
    finally:
        conn.close()


def unbind_server(db_path: str, env_id: int, server_id: int):
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM env_servers WHERE env_id=? AND server_id=?",
                     (env_id, server_id))
        conn.commit()
    finally:
        conn.close()


def reorder_env_servers(db_path: str, env_id: int, server_ids: list) -> None:
    """批量重排环境绑定顺序，单事务提交（避免逐条 commit 的部分重排）"""
    conn = get_conn(db_path)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO env_servers (env_id,server_id,order_no) "
            "VALUES (?,?,?)",
            [(env_id, sid, idx) for idx, sid in enumerate(server_ids)])
        conn.commit()
    finally:
        conn.close()


# ---------- 任务 ----------

def create_task(db_path: str, project_id: int, type_: str, ref_id: int, name: str,
                env: str, version: str, previous_version: str, created_by: int,
                extra: list = None, action: str = 'deploy') -> int:
    fields = dict(project_id=project_id, type=type_, ref_id=ref_id, name=name,
                  env=env, version=version, previous_version=previous_version,
                  extra=extra or [], action=action, status="pending",
                  created_by=created_by, started_at=int(time.time()))
    return create_record(db_path, "tasks", fields)


def update_task(db_path: str, task_id: int, **fields) -> bool:
    """白名单字段更新；返回是否更新成功"""
    allowed = {"status", "log_file", "started_at", "finished_at", "version", "previous_version"}
    return update_record(db_path, "tasks", task_id, {k: v for k, v in fields.items()
                                                      if k in allowed})


def list_tasks(db_path: str, project_id: int = None, limit: int = 100) -> list:
    conn = get_conn(db_path)
    try:
        if project_id:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE project_id=? ORDER BY id DESC LIMIT ?",
                (project_id, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?",
                                (limit,)).fetchall()
        return [_deserialize("tasks", dict(r)) for r in rows]
    finally:
        conn.close()


def get_task(db_path: str, task_id: int):
    return get_record(db_path, "tasks", task_id)


def delete_task(db_path: str, task_id: int) -> tuple:
    """删除已完成的任务及其日志文件；返回 (ok, message)"""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return False, "任务不存在"
        if dict(row)["status"] in ("running", "pending"):
            return False, "运行中或等待中的任务不能删除"
        log_file = dict(row).get("log_file", "")
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()
    import os
    if log_file:
        try: os.remove(log_file)
        except OSError: pass
    return True, ""


# ---------- 审计 ----------

def create_audit(db_path: str, user_id: int, username: str, action: str,
                 target_type: str = "", target_id: int = 0, detail: dict = None):
    create_record(db_path, "audit_logs", dict(
        user_id=user_id, username=username, action=action,
        target_type=target_type, target_id=target_id,
        detail=json.dumps(detail or {}, ensure_ascii=False),
        created_at=int(time.time())))


def list_audit_logs(db_path: str, limit: int = 100) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_audit_logs_paged(db_path: str, page: int = 1, page_size: int = 20) -> tuple:
    """返回 (rows, total)；page 从 1 开始"""
    conn = get_conn(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        offset = (page - 1) * page_size
        rows = conn.execute(
            "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ? OFFSET ?",
            (page_size, offset)).fetchall()
        return [dict(r) for r in rows], total
    finally:
        conn.close()


# ---------- 用户 ----------

def get_user_by_name(db_path: str, username: str):
    rows = list_records(db_path, "users", "username=?", (username,))
    return rows[0] if rows else None


def list_users(db_path: str) -> list:
    return list_records(db_path, "users", "", (), "id")


def project_count(db_path: str) -> int:
    conn = get_conn(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    finally:
        conn.close()


# ---------- 监控时序数据 ----------

def save_response_time(db_path: str, env_id: int, service: str,
                       host: str, response_ms: float, ok: bool):
    """持久化健康检查响应时间（保留 7 天，每次写入清理过期）"""
    now = int(time.time())
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO response_time_history"
            " (env_id, service, host, response_ms, ok, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (env_id, service, host, response_ms, int(ok), now))
        conn.execute(
            "DELETE FROM response_time_history WHERE created_at < ?",
            (now - 7 * 86400,))
        conn.commit()
    finally:
        conn.close()


def get_response_time_history(db_path: str, env_id: int = None,
                            service: str = None, hours: int = 24) -> list:
    """查询响应时间历史（默认最近 24h）"""
    since = int(time.time()) - hours * 3600
    where_parts = ["created_at >= ?"]
    params = [since]
    if env_id:
        where_parts.append("env_id = ?")
        params.append(env_id)
    if service:
        where_parts.append("service = ?")
        params.append(service)
    where = " AND ".join(where_parts)
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM response_time_history WHERE {where}"
            " ORDER BY created_at DESC LIMIT 500", params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def save_monitor_snapshot(db_path: str, env_id: int, host: str, data: dict):
    """保存一轮监控快照（新指标：swap/network/diskio/tcp/restart/docker_df/ssl）"""
    now = int(time.time())
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO monitor_snapshots"
            " (env_id, host, swap, net_rx, net_tx, disk_read, disk_write,"
            " tcp_established, tcp_time_wait, tcp_close_wait,"
            " restart_count, uptime_hours,"
            " docker_images_bytes, docker_containers_bytes, docker_volumes_bytes,"
            " ssl_days, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (env_id, host,
             data.get("swap"), data.get("net_rx"), data.get("net_tx"),
             data.get("disk_read"), data.get("disk_write"),
             data.get("tcp", {}).get("ESTABLISHED"),
             data.get("tcp", {}).get("TIME_WAIT"),
             data.get("tcp", {}).get("CLOSE_WAIT"),
             data.get("restart_count", 0), data.get("uptime_hours"),
             data.get("docker_df", {}).get("images"),
             data.get("docker_df", {}).get("containers"),
             data.get("docker_df", {}).get("volumes"),
             data.get("ssl_days"), now))
        # 保留 3 天
        conn.execute(
            "DELETE FROM monitor_snapshots WHERE created_at < ?",
            (now - 3 * 86400,))
        conn.commit()
    finally:
        conn.close()


def get_monitor_latest(db_path: str, env_id: int = None,
                       hours: int = 24) -> list:
    """查询最新监控快照（每个 host 取最新一条）"""
    since = int(time.time()) - hours * 3600
    conn = get_conn(db_path)
    try:
        if env_id:
            rows = conn.execute(
                "SELECT m.* FROM monitor_snapshots m"
                " INNER JOIN ("
                "   SELECT host, MAX(created_at) AS max_ts"
                "   FROM monitor_snapshots WHERE created_at >= ? AND env_id = ?"
                "   GROUP BY host"
                ") t ON m.host = t.host AND m.created_at = t.max_ts"
                " ORDER BY m.host", (since, env_id)).fetchall()
        else:
            rows = conn.execute(
                "SELECT m.* FROM monitor_snapshots m"
                " INNER JOIN ("
                "   SELECT host, MAX(created_at) AS max_ts"
                "   FROM monitor_snapshots WHERE created_at >= ?"
                "   GROUP BY host"
                ") t ON m.host = t.host AND m.created_at = t.max_ts"
                " ORDER BY m.host", (since,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
