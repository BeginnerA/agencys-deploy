# 日志中心（阶段二）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现日志中心后端采集/存储/检索 + 前端服务日志 Tab，支持 SSH 增量采集 docker logs、FTS5 全文检索、上下文查看、归档清理。

**Architecture:** 新增 `log_collector.py`（后台守护线程，30s 周期 SSH 拉取 docker logs 增量，追加到本地文件 + FTS5 索引），`log_routes.py`（检索 API + 上下文 API），前端 logs.js 新增第 3 个 Tab「服务日志」。复用 monitor.py 的 SSH 模式（`_ssh_exec` + `_merge_server_ssh`），复用 db.py 的幂等迁移模式。

**Tech Stack:** Python 标准库 + SQLite FTS5 + paramiko（已有依赖）+ 原生 HTML/CSS/JS

---

## 文件结构

| 文件 | 动作 | 职责 |
|------|------|------|
| `webapp/db.py` | 修改 | 新增 log_entries + log_scan_state 表定义 + FTS5 虚拟表 + TABLES 白名单 |
| `webapp/log_collector.py` | 新建 | SSH 增量采集 + 文件追加 + FTS5 索引 + 关键词检测 + 归档清理 |
| `webapp/routes/log_routes.py` | 新建 | 检索 API + 上下文 API + 服务列表 API |
| `webapp/static/js/pages/logs.js` | 修改 | 新增第 3 个 Tab「服务日志」（检索 + 上下文抽屉） |
| `webapp/static/css/pages/logs.css` | 修改 | 服务日志 Tab 样式 |
| `webapp/app.py` | 修改 | 启动 LogCollector 守护线程 |
| `tests/test_log_collector.py` | 新建 | 采集/索引/归档/检索单元测试 |
| `tests/test_log_routes.py` | 新建 | 检索/上下文 API 单元测试 |

---

### Task 1: 数据层迁移（log_entries + log_scan_state + FTS5）

**Files:**
- Modify: `webapp/db.py`（SCHEMA + TABLES + JSON_FIELDS）
- Test: `tests/test_db_extensions.py`（追加测试）

- [ ] **Step 1: 写失败测试**

在 `tests/test_db_extensions.py` 末尾追加：

```python
class TestLogTables(unittest.TestCase):
    """日志中心表结构验证"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_log_entries_table_exists(self):
        conn = dbm.get_conn(self.db)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='log_entries'"
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            conn.close()

    def test_log_scan_state_table_exists(self):
        conn = dbm.get_conn(self.db)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='log_scan_state'"
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            conn.close()

    def test_log_fts_table_exists(self):
        conn = dbm.get_conn(self.db)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='log_fts'"
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            conn.close()

    def test_log_entries_in_tables_whitelist(self):
        self.assertIn("log_entries", dbm.TABLES)
        self.assertIn("log_scan_state", dbm.TABLES)
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m unittest tests.test_db_extensions.TestLogTables -v`
Expected: FAIL（表不存在 / TABLES 白名单缺失）

- [ ] **Step 3: 实现迁移**

修改 `webapp/db.py`：

1. SCHEMA 末尾（`"""` 之前）追加：

```sql
CREATE TABLE IF NOT EXISTS log_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    env TEXT NOT NULL DEFAULT '',
    service TEXT NOT NULL DEFAULT '',
    container TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL DEFAULT '',
    line_offset INTEGER NOT NULL DEFAULT 0,
    line_count INTEGER NOT NULL DEFAULT 0,
    keywords TEXT DEFAULT '[]'
);
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
```

2. TABLES 白名单追加 `"log_entries", "log_scan_state"`

3. JSON_FIELDS 追加 `"log_entries": {"keywords"}`

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m unittest tests.test_db_extensions.TestLogTables -v`
Expected: 4 tests PASS

- [ ] **Step 5: 运行全量测试确认无回归**

Run: `python -m unittest discover -s tests`
Expected: 全部 PASS（43+ tests）

- [ ] **Step 6: 提交**

```bash
git add webapp/db.py tests/test_db_extensions.py
git commit -m "feat(log-center): 数据层迁移（log_entries + log_scan_state + FTS5）"
```

---

### Task 2: 日志采集器核心（log_collector.py）

**Files:**
- Create: `webapp/log_collector.py`
- Test: `tests/test_log_collector.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_log_collector.py`：

```python
"""日志采集器单元测试"""
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from webapp import db as dbm
from webapp.log_collector import LogCollector, _parse_docker_log_line, _build_log_file_path


class TestParseDockerLogLine(unittest.TestCase):
    """docker logs 行解析"""

    def test_parse_with_timestamp(self):
        line = "2026-08-18T10:30:45.123456789Z ERROR something failed"
        ts, content = _parse_docker_log_line(line)
        self.assertEqual(ts, 1755510645)  # 2026-08-18T10:30:45 UTC
        self.assertEqual(content, "ERROR something failed")

    def test_parse_without_timestamp(self):
        line = "plain log line without timestamp"
        ts, content = _parse_docker_log_line(line)
        self.assertEqual(ts, 0)
        self.assertEqual(content, line)

    def test_parse_empty_line(self):
        ts, content = _parse_docker_log_line("")
        self.assertEqual(ts, 0)
        self.assertEqual(content, "")


class TestBuildLogFilePath(unittest.TestCase):
    """日志文件路径构建"""

    def test_build_path(self):
        base = "/tmp/logs"
        path = _build_log_file_path(base, "prod", "api-service", "2026-08-18")
        self.assertEqual(path, "/tmp/logs/collect/prod/api-service/2026-08-18.log")


class TestLogCollector(unittest.TestCase):
    """采集器核心逻辑"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.log_dir = os.path.join(self.tmp, "logs")
        self.collector = LogCollector(self.db, self.log_dir, ssh_func=self._fake_ssh)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_ssh(self, host, port, ssh_cfg, cmd, timeout=10):
        """模拟 SSH 返回 docker logs 输出"""
        return self._fake_ssh_output

    def test_append_to_file_creates_directory(self):
        """文件追加：自动创建目录"""
        path = os.path.join(self.log_dir, "collect", "prod", "api", "2026-08-18.log")
        self.collector._append_to_file(path, ["line1\n", "line2\n"])
        self.assertTrue(os.path.isfile(path))
        content = Path(path).read_text(encoding="utf-8")
        self.assertIn("line1", content)
        self.assertIn("line2", content)

    def test_update_scan_state_insert_and_update(self):
        """游标更新：首次插入 + 后续更新"""
        self.collector._update_scan_state("prod", "10.0.0.1", "api-container", "api",
                                          "2026-08-18T10:30:00Z", 100)
        conn = dbm.get_conn(self.db)
        try:
            row = conn.execute(
                "SELECT * FROM log_scan_state WHERE env='prod' AND server='10.0.0.1'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["last_ts"], "2026-08-18T10:30:00Z")
            self.assertEqual(row["last_line"], 100)
        finally:
            conn.close()
        # 更新
        self.collector._update_scan_state("prod", "10.0.0.1", "api-container", "api",
                                          "2026-08-18T10:31:00Z", 200)
        conn = dbm.get_conn(self.db)
        try:
            row = conn.execute(
                "SELECT * FROM log_scan_state WHERE env='prod' AND server='10.0.0.1'"
            ).fetchone()
            self.assertEqual(row["last_ts"], "2026-08-18T10:31:00Z")
            self.assertEqual(row["last_line"], 200)
        finally:
            conn.close()

    def test_index_log_lines_writes_fts(self):
        """FTS5 索引：写入 log_entries + log_fts"""
        path = os.path.join(self.log_dir, "test.log")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("line0\nline1\nline2\n", encoding="utf-8")
        entries = [
            (1755510645, "prod", "api", "line1", path, 1, 1),
            (1755510646, "prod", "api", "line2", path, 2, 1),
        ]
        self.collector._index_log_entries(entries)
        conn = dbm.get_conn(self.db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0]
            self.assertEqual(count, 2)
            # FTS5 检索
            fts_count = conn.execute(
                "SELECT COUNT(*) FROM log_fts WHERE log_fts MATCH 'line1'"
            ).fetchone()[0]
            self.assertEqual(fts_count, 1)
        finally:
            conn.close()
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m unittest tests.test_log_collector -v`
Expected: FAIL（ModuleNotFoundError: log_collector）

- [ ] **Step 3: 实现 log_collector.py**

创建 `webapp/log_collector.py`：

```python
"""日志采集器：SSH 增量拉取 docker logs + FTS5 索引 + 归档清理"""
import gzip
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db as dbm

# docker logs --since 时间戳格式解析
_DOCKER_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})"
)


def _parse_docker_log_line(line: str):
    """解析 docker logs -t 输出行：提取时间戳 + 内容。
    格式：2026-08-18T10:30:45.123456789Z <content>
    返回 (epoch_seconds, content)。无时间戳时 ts=0。"""
    if not line:
        return 0, ""
    m = _DOCKER_TS_RE.match(line)
    if not m:
        return 0, line
    try:
        dt = datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4)), int(m.group(5)), int(m.group(6)),
            tzinfo=timezone.utc
        )
        ts = int(dt.timestamp())
        # 跳过时间戳部分（含可选纳秒 + Z + 空格）
        rest = line[m.end():]
        # 跳过 .123456789Z 部分
        if rest and rest[0] == '.':
            rest = rest.lstrip('0123456789')
            if rest and rest[0] == 'Z':
                rest = rest[1:]
            if rest and rest[0] == ' ':
                rest = rest[1:]
        return ts, rest
    except (ValueError, OverflowError):
        return 0, line


def _build_log_file_path(base_dir: str, env: str, service: str, date_str: str) -> str:
    """构建日志文件路径：base_dir/collect/env/service/YYYY-MM-DD.log"""
    return os.path.join(base_dir, "collect", env, service, f"{date_str}.log")


class LogCollector:
    """日志采集器（后台守护线程）：SSH 增量拉取 + FTS5 索引 + 归档清理"""

    def __init__(self, db_path: str, log_dir: str, interval: int = 30,
                 ssh_func=None, max_lines_per_container: int = 5000):
        self.db_path = db_path
        self.log_dir = log_dir
        self.interval = interval
        self.ssh_func = ssh_func  # 可注入测试 mock；生产环境用 monitor._ssh_exec
        self.max_lines = max_lines_per_container
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        # 采集容错：连续失败计数 {(env, server, container): count}
        self._fail_counts = {}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="LogCollector")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run_loop(self):
        """主循环：每 interval 秒执行一轮采集 + 每日归档检查"""
        last_archive_check = 0
        while not self._stop.is_set():
            try:
                self._collect_round()
                # 每日归档检查（距上次 >= 3600s）
                now = time.time()
                if now - last_archive_check >= 3600:
                    self._archive_and_cleanup()
                    last_archive_check = now
            except Exception:
                pass  # 单轮异常不退出线程
            self._stop.wait(self.interval)

    def _collect_round(self):
        """单轮采集：遍历所有环境 × 服务器 × 容器，增量拉取 docker logs"""
        if not self.ssh_func:
            return
        conn = dbm.get_conn(self.db_path)
        try:
            # 查询所有启用了监控的环境
            envs = conn.execute(
                "SELECT e.id, e.name, e.ssh, p.name as project "
                "FROM environments e JOIN projects p ON e.project_id = p.id "
                "WHERE e.monitor_enabled = 1"
            ).fetchall()
        finally:
            conn.close()

        for env_row in envs:
            env_name = env_row["name"]
            env_id = env_row["id"]
            env_ssh = {}
            try:
                import json
                env_ssh = json.loads(env_row["ssh"] or "{}")
            except (ValueError, TypeError):
                pass
            if not env_ssh:
                continue
            # 查询该环境关联的服务器
            conn = dbm.get_conn(self.db_path)
            try:
                servers = conn.execute(
                    "SELECT s.host, s.ssh_port, s.ssh_user, s.ssh_password, s.ssh_key_file "
                    "FROM servers s JOIN env_servers es ON s.id = es.server_id "
                    "WHERE es.env_id = ? AND s.enabled = 1",
                    (env_id,)
                ).fetchall()
            finally:
                conn.close()

            for srv in servers:
                self._collect_server(env_name, env_id, env_ssh, dict(srv))

    def _collect_server(self, env_name: str, env_id: int, env_ssh: dict, server: dict):
        """采集单台服务器的所有容器日志"""
        from .monitor import _merge_server_ssh
        ssh_cfg = _merge_server_ssh(env_ssh, server)
        host = server["host"]
        port = server.get("ssh_port") or 22
        # 获取该服务器上的容器列表
        try:
            out = self.ssh_func(host, port, ssh_cfg,
                                "docker ps --format '{{.Names}}|{{.Image}}'", timeout=10)
        except Exception:
            return  # SSH 失败跳过本服务器
        containers = []
        for line in out.splitlines():
            if "|" in line:
                name = line.split("|", 1)[0].strip()
                if name:
                    containers.append(name)

        for container in containers:
            self._collect_container(env_name, env_id, host, ssh_cfg, container, port)

    def _collect_container(self, env_name: str, env_id: int, host: str,
                           ssh_cfg: dict, container: str, port: int):
        """采集单个容器：增量 docker logs --since"""
        key = (env_name, host, container)
        # 读取游标
        conn = dbm.get_conn(self.db_path)
        try:
            state = conn.execute(
                "SELECT last_ts, last_line FROM log_scan_state "
                "WHERE env=? AND server=? AND container=?",
                (env_name, host, container)
            ).fetchone()
        finally:
            conn.close()

        last_ts = state["last_ts"] if state else ""
        # 构建 docker logs 命令
        if last_ts:
            cmd = f"docker logs --since '{last_ts}' -t --no-color {container} 2>&1 | head -{self.max_lines}"
        else:
            # 首次采集：最近 5 分钟
            cmd = f"docker logs --since 5m -t --no-color {container} 2>&1 | head -{self.max_lines}"

        try:
            out = self.ssh_func(host, port, ssh_cfg, cmd, timeout=30)
            self._fail_counts.pop(key, None)  # 成功重置失败计数
        except Exception:
            self._fail_counts[key] = self._fail_counts.get(key, 0) + 1
            return  # 单容器失败不阻塞其他

        if not out or not out.strip():
            return

        lines = out.splitlines(keepends=True)
        if not lines:
            return

        # 解析 + 追加文件 + 索引
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # 推断 service 名（容器名通常 = service_name）
        service = container
        file_path = _build_log_file_path(self.log_dir, env_name, service, today)

        # 追加到文件
        self._append_to_file(file_path, lines)

        # 解析行 + 索引
        entries = []
        line_offset = 0
        last_line_ts = last_ts
        for i, raw_line in enumerate(lines):
            ts, content = _parse_docker_log_line(raw_line.rstrip("\n"))
            if ts > 0:
                last_line_ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            entries.append((ts or int(time.time()), env_name, service, content,
                            file_path, line_offset, 1))
            line_offset += len(raw_line.encode("utf-8", "ignore"))

        if entries:
            self._index_log_entries(entries)
            self._update_scan_state(env_name, host, container, service,
                                    last_line_ts or "", line_offset)

    def _append_to_file(self, file_path: str, lines: list):
        """追加日志行到文件（自动创建目录）"""
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "a", encoding="utf-8") as f:
            f.writelines(lines)

    def _index_log_entries(self, entries: list):
        """批量写入 log_entries + 同步 FTS5"""
        conn = dbm.get_conn(self.db_path)
        try:
            for ts, env, service, content, file_path, offset, count in entries:
                cursor = conn.execute(
                    "INSERT INTO log_entries (ts, env, service, container, file_path, line_offset, line_count, keywords) "
                    "VALUES (?, ?, ?, '', ?, ?, ?, '[]')",
                    (ts, env, service, file_path, offset, count)
                )
                rowid = cursor.lastrowid
                # 同步 FTS5
                conn.execute(
                    "INSERT INTO log_fts (rowid, ts, env, service, content) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (rowid, str(ts), env, service, content)
                )
            conn.commit()
        finally:
            conn.close()

    def _update_scan_state(self, env: str, server: str, container: str,
                           service: str, last_ts: str, last_line: int):
        """更新采集游标（upsert）"""
        conn = dbm.get_conn(self.db_path)
        try:
            existing = conn.execute(
                "SELECT id FROM log_scan_state WHERE env=? AND server=? AND container=?",
                (env, server, container)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE log_scan_state SET last_ts=?, last_line=?, updated_at=? "
                    "WHERE id=?",
                    (last_ts, last_line, int(time.time()), existing["id"])
                )
            else:
                conn.execute(
                    "INSERT INTO log_scan_state (env, server, container, service, last_ts, last_line, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (env, server, container, service, last_ts, last_line, int(time.time()))
                )
            conn.commit()
        finally:
            conn.close()

    def _archive_and_cleanup(self):
        """归档过期日志 + 清理过期 FTS5 条目 + 删除过期归档"""
        conn = dbm.get_conn(self.db_path)
        try:
            # 获取全局保留期（默认 7 天）
            settings = conn.execute(
                "SELECT value FROM platform_settings WHERE key='log_retention_days'"
            ).fetchone()
            retention_days = 7
            if settings:
                try:
                    import json
                    retention_days = int(json.loads(settings["value"]))
                except (ValueError, TypeError):
                    pass

            # 获取归档期（默认 30 天）
            settings = conn.execute(
                "SELECT value FROM platform_settings WHERE key='log_archive_days'"
            ).fetchone()
            archive_days = 30
            if settings:
                try:
                    import json
                    archive_days = int(json.loads(settings["value"]))
                except (ValueError, TypeError):
                    pass
        finally:
            conn.close()

        now = time.time()
        retention_cutoff = datetime.fromtimestamp(
            now - retention_days * 86400, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        archive_cutoff = datetime.fromtimestamp(
            now - archive_days * 86400, tz=timezone.utc
        ).strftime("%Y-%m-%d")

        collect_base = os.path.join(self.log_dir, "collect")
        archive_base = os.path.join(self.log_dir, "archive")

        if not os.path.isdir(collect_base):
            return

        # 遍历 collect 目录
        for env_dir in Path(collect_base).iterdir():
            if not env_dir.is_dir():
                continue
            env_name = env_dir.name
            for svc_dir in env_dir.iterdir():
                if not svc_dir.is_dir():
                    continue
                service = svc_dir.name
                for log_file in svc_dir.glob("*.log"):
                    date_str = log_file.stem  # YYYY-MM-DD
                    if date_str <= retention_cutoff:
                        # 归档：gzip 压缩到 archive 目录
                        archive_dir = Path(archive_base) / env_name / service
                        archive_dir.mkdir(parents=True, exist_ok=True)
                        archive_path = archive_dir / (log_file.name + ".gz")
                        with open(log_file, "rb") as f_in:
                            with gzip.open(archive_path, "wb") as f_out:
                                shutil.copyfileobj(f_in, f_out)
                        log_file.unlink()
                        # 清理 FTS5 + log_entries
                        self._cleanup_fts_for_file(env_name, service, str(log_file))
                    elif date_str <= archive_cutoff:
                        pass  # 在保留期内，不动
                # 清理过期归档
                for gz_file in svc_dir.glob("*.gz") if svc_dir.name == service else []:
                    pass  # 归档目录清理在下面

        # 清理过期归档
        if os.path.isdir(archive_base):
            for env_dir in Path(archive_base).iterdir():
                if not env_dir.is_dir():
                    continue
                for svc_dir in env_dir.iterdir():
                    if not svc_dir.is_dir():
                        continue
                    for gz_file in svc_dir.glob("*.gz"):
                        date_str = gz_file.stem.replace(".log", "")
                        if date_str <= archive_cutoff:
                            gz_file.unlink()

    def _cleanup_fts_for_file(self, env: str, service: str, file_path: str):
        """清理指定文件的 FTS5 + log_entries 条目"""
        conn = dbm.get_conn(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id FROM log_entries WHERE env=? AND service=? AND file_path=?",
                (env, service, file_path)
            ).fetchall()
            for row in rows:
                conn.execute("DELETE FROM log_fts WHERE rowid=?", (row["id"],))
                conn.execute("DELETE FROM log_entries WHERE id=?", (row["id"],))
            conn.commit()
        finally:
            conn.close()


_collector = None


def get_collector() -> "LogCollector":
    global _collector
    return _collector


def init_collector(app) -> None:
    """启动日志采集器（依赖 monitor 的 SSH 函数）"""
    global _collector
    if _collector is not None:
        return
    from .monitor import _ssh_exec
    log_dir = str(Path(app.root_path).parent / "data" / "logs")
    interval = app.config.get("MONITOR_INTERVAL", 30)
    _collector = LogCollector(app.config["DB_PATH"], log_dir, interval=interval,
                              ssh_func=_ssh_exec)
    _collector.start()
    print(f"[log-collector] 日志采集器已启动，采集周期 {interval}s")
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m unittest tests.test_log_collector -v`
Expected: 6 tests PASS

- [ ] **Step 5: 提交**

```bash
git add webapp/log_collector.py tests/test_log_collector.py
git commit -m "feat(log-center): 日志采集器核心（SSH 增量采集 + FTS5 索引 + 归档清理）"
```

---

### Task 3: 日志检索 API（log_routes.py）

**Files:**
- Create: `webapp/routes/log_routes.py`
- Test: `tests/test_log_routes.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_log_routes.py`：

```python
"""日志检索 API 单元测试"""
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from webapp import db as dbm
from webapp.app import create_app


class TestLogRoutes(unittest.TestCase):
    """日志检索 API 测试"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db = os.path.join(cls.tmp, "test.db")
        # monkeypatch DATA_DIR
        import webapp
        cls._orig_data_dir = webapp.DATA_DIR
        webapp.DATA_DIR = Path(cls.tmp)
        webapp.__init__.DATA_DIR = Path(cls.tmp)
        # 重建 app
        cls.app = create_app()
        cls.app.config["DB_PATH"] = cls.db
        dbm.init_db(cls.db)
        cls.client = cls.app.test_client()
        # 登录
        cls.client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        cls.client.post("/api/auth/password", json={"new_password": "test1234"})
        cls.client.post("/api/auth/login", json={"username": "admin", "password": "test1234"})

    @classmethod
    def tearDownClass(cls):
        import webapp
        webapp.DATA_DIR = cls._orig_data_dir
        webapp.__init__.DATA_DIR = cls._orig_data_dir
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _insert_log_entries(self):
        """插入测试数据"""
        conn = dbm.get_conn(self.db)
        try:
            for i in range(5):
                cursor = conn.execute(
                    "INSERT INTO log_entries (ts, env, service, container, file_path, line_offset, line_count, keywords) "
                    "VALUES (?, ?, ?, '', ?, 0, 1, '[]')",
                    (int(time.time()) - i * 60, "prod", "api-service", f"/logs/prod/api-service/2026-08-18.log")
                )
                rowid = cursor.lastrowid
                conn.execute(
                    "INSERT INTO log_fts (rowid, ts, env, service, content) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (rowid, str(int(time.time()) - i * 60), "prod", "api-service",
                     f"ERROR something failed line {i}")
                )
            conn.commit()
        finally:
            conn.close()

    def test_search_logs_empty(self):
        """检索：空结果"""
        resp = self.client.get("/api/logs?env=prod&service=api-service&keyword=notfound")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["total"], 0)

    def test_search_logs_with_results(self):
        """检索：有结果"""
        self._insert_log_entries()
        resp = self.client.get("/api/logs?env=prod&service=api-service&keyword=ERROR")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertGreater(data["total"], 0)
        self.assertIn("rows", data)

    def test_search_logs_pagination(self):
        """检索：分页"""
        self._insert_log_entries()
        resp = self.client.get("/api/logs?env=prod&service=api-service&page=1&page_size=2")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertLessEqual(len(data["rows"]), 2)

    def test_context_api_missing_params(self):
        """上下文 API：缺少参数返回 400"""
        resp = self.client.get("/api/logs/context")
        self.assertEqual(resp.status_code, 400)

    def test_services_list(self):
        """服务列表 API"""
        resp = self.client.get("/api/logs/services")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIsInstance(data, list)
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m unittest tests.test_log_routes -v`
Expected: FAIL（API 路由不存在）

- [ ] **Step 3: 实现 log_routes.py**

创建 `webapp/routes/log_routes.py`：

```python
"""日志中心 API：检索 + 上下文 + 服务列表"""
import os
import time
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from .. import db as dbm
from ..auth import require_auth

log_bp = Blueprint("log", __name__)


@log_bp.route("/api/logs")
@require_auth
def search_logs():
    """日志检索：FTS5 全文 + 环境/服务/时间范围过滤"""
    env = request.args.get("env", "")
    service = request.args.get("service", "")
    keyword = request.args.get("keyword", "")
    from_ts = request.args.get("from", 0, type=int)
    to_ts = request.args.get("to", 0, type=int)
    page = max(1, request.args.get("page", 1, type=int))
    page_size = min(200, max(1, request.args.get("page_size", 50, type=int)))

    db_path = current_app.config["DB_PATH"]
    conn = dbm.get_conn(db_path)
    try:
        # 构建查询
        conditions = []
        params = []

        if env:
            conditions.append("le.env = ?")
            params.append(env)
        if service:
            conditions.append("le.service = ?")
            params.append(service)
        if from_ts:
            conditions.append("le.ts >= ?")
            params.append(from_ts)
        if to_ts:
            conditions.append("le.ts <= ?")
            params.append(to_ts)

        if keyword:
            # FTS5 检索
            fts_conditions = []
            fts_params = []
            fts_sql = "SELECT rowid FROM log_fts WHERE log_fts MATCH ?"
            fts_params.append(keyword)
            fts_rows = conn.execute(fts_sql, fts_params).fetchall()
            if not fts_rows:
                return jsonify({"rows": [], "total": 0})
            rowids = [r["rowid"] for r in fts_rows]
            placeholders = ",".join("?" * len(rowids))
            conditions.append(f"le.id IN ({placeholders})")
            params.extend(rowids)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        # 总数
        total = conn.execute(
            f"SELECT COUNT(*) FROM log_entries le WHERE {where_clause}", params
        ).fetchone()[0]

        # 分页
        offset = (page - 1) * page_size
        rows = conn.execute(
            f"SELECT le.id, le.ts, le.env, le.service, le.container, le.file_path, "
            f"le.line_offset, le.line_count, le.keywords "
            f"FROM log_entries le WHERE {where_clause} "
            f"ORDER BY le.ts DESC LIMIT ? OFFSET ?",
            params + [page_size, offset]
        ).fetchall()

        return jsonify({
            "rows": [dict(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        })
    finally:
        conn.close()


@log_bp.route("/api/logs/context")
@require_auth
def log_context():
    """上下文查看：按文件 + 行偏移取前后 N 行"""
    env = request.args.get("env", "")
    service = request.args.get("service", "")
    entry_id = request.args.get("id", 0, type=int)
    lines = min(50, max(1, request.args.get("lines", 5, type=int)))

    if not entry_id:
        return jsonify({"error": "缺少参数 id"}), 400

    db_path = current_app.config["DB_PATH"]
    conn = dbm.get_conn(db_path)
    try:
        entry = conn.execute(
            "SELECT * FROM log_entries WHERE id=?", (entry_id,)
        ).fetchone()
        if not entry:
            return jsonify({"error": "日志条目不存在"}), 404

        file_path = entry["file_path"]
        if not os.path.isfile(file_path):
            return jsonify({"error": "日志文件不存在", "file": file_path}), 404

        # 读取文件上下文
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            all_lines = f.readlines()

        offset = entry["line_offset"]
        # 找到目标行号（按字节偏移近似）
        byte_pos = 0
        target_line = 0
        for i, line in enumerate(all_lines):
            if byte_pos >= offset:
                target_line = i
                break
            byte_pos += len(line.encode("utf-8", "ignore"))

        # 取前后 N 行
        start = max(0, target_line - lines)
        end = min(len(all_lines), target_line + lines + 1)
        context_lines = all_lines[start:end]

        return jsonify({
            "file": file_path,
            "target_line": target_line,
            "start": start,
            "end": end,
            "lines": context_lines,
        })
    finally:
        conn.close()


@log_bp.route("/api/logs/services")
@require_auth
def log_services():
    """服务列表：返回所有已采集的环境 × 服务组合"""
    db_path = current_app.config["DB_PATH"]
    conn = dbm.get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT env, service FROM log_entries ORDER BY env, service"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()
```

- [ ] **Step 4: 注册蓝图**

修改 `webapp/__init__.py`：

在 `from .routes.alert_routes import alert_bp` 后追加：

```python
from .routes.log_routes import log_bp
```

在 `app.register_blueprint(alert_bp, url_prefix="/api")` 后追加：

```python
app.register_blueprint(log_bp, url_prefix="/api")
```

- [ ] **Step 5: 运行测试验证通过**

Run: `python -m unittest tests.test_log_routes -v`
Expected: 5 tests PASS

- [ ] **Step 6: 运行全量测试确认无回归**

Run: `python -m unittest discover -s tests`
Expected: 全部 PASS

- [ ] **Step 7: 提交**

```bash
git add webapp/routes/log_routes.py webapp/__init__.py tests/test_log_routes.py
git commit -m "feat(log-center): 日志检索 API + 上下文 API + 服务列表 API"
```

---

### Task 4: 前端服务日志 Tab（logs.js + logs.css）

**Files:**
- Modify: `webapp/static/js/pages/logs.js`
- Modify: `webapp/static/css/pages/logs.css`

- [ ] **Step 1: 修改 logs.js 新增第 3 个 Tab**

在 `logs.js` 的 `tab` 变量定义处追加 `'service'` 选项：

```javascript
let tab = 'task';  // task / audit / service
```

在 toolbar 渲染处追加第 3 个 Tab：

```javascript
${[['task','任务日志'],['audit','审计日志'],['service','服务日志']]
  .filter(([k]) => k === 'task' || k === 'audit' && isAdmin() || k === 'service')
  .map(([k, v]) => `<span class="chip ${k === tab ? 'active' : ''}" data-tab="${k}">${v}</span>`).join('')}
```

在 `renderTab()` 函数中追加 `tab === 'service'` 分支：

```javascript
if (tab === 'service') {
  body.innerHTML = `
    <div class="panel">
      <div class="panel-title">服务日志</div>
      <div class="flex gap-3" style="margin-bottom:12px;flex-wrap:wrap;align-items:center">
        <span class="weak" style="font-size:12px">环境</span>
        <select class="filter-sel" id="svcLogEnvSel">
          <option value="">全部环境</option>
        </select>
        <span class="weak" style="font-size:12px">服务</span>
        <select class="filter-sel" id="svcLogSvcSel">
          <option value="">全部服务</option>
        </select>
        <span class="weak" style="font-size:12px">关键词</span>
        <input type="text" class="filter-input" id="svcLogKeyword" placeholder="ERROR / Exception / ..." style="width:200px">
        <button class="btn sm" id="svcLogSearch">检索</button>
      </div>
      <div id="svcLogResults">
        <div class="muted" style="text-align:center;padding:40px">请输入检索条件</div>
      </div>
    </div>
    <div id="svcLogContextDrawer" class="drawer-overlay" style="display:none">
      <div class="drawer right" style="width:600px">
        <div class="drawer-header">
          <span>日志上下文</span>
          <button class="btn sm" id="svcLogCtxClose">关闭</button>
        </div>
        <div class="drawer-body">
          <pre id="svcLogCtxContent" class="log-console" style="height:500px;overflow:auto"></pre>
        </div>
      </div>
    </div>`;
  // 加载环境/服务列表
  loadServiceLogOptions();
  // 绑定事件
  document.getElementById('svcLogSearch').onclick = () => searchServiceLogs();
  document.getElementById('svcLogCtxClose').onclick = () => {
    document.getElementById('svcLogContextDrawer').style.display = 'none';
  };
  return;
}
```

追加辅助函数：

```javascript
async function loadServiceLogOptions() {
  const services = await api('/api/logs/services').catch(() => []);
  const envSel = document.getElementById('svcLogEnvSel');
  const svcSel = document.getElementById('svcLogSvcSel');
  if (!envSel || !svcSel) return;
  const envSet = new Set(services.map(s => s.env));
  envSel.innerHTML = '<option value="">全部环境</option>' +
    [...envSet].map(e => `<option value="${esc(e)}">${esc(e)}</option>`).join('');
  envSel.onchange = () => {
    const env = envSel.value;
    const filtered = env ? services.filter(s => s.env === env) : services;
    svcSel.innerHTML = '<option value="">全部服务</option>' +
      filtered.map(s => `<option value="${esc(s.service)}">${esc(s.service)}</option>`).join('');
  };
}

async function searchServiceLogs() {
  const env = document.getElementById('svcLogEnvSel')?.value || '';
  const service = document.getElementById('svcLogSvcSel')?.value || '';
  const keyword = document.getElementById('svcLogKeyword')?.value || '';
  if (!keyword && !env && !service) {
    toastError('请至少选择一个筛选条件');
    return;
  }
  const results = document.getElementById('svcLogResults');
  if (results) results.innerHTML = '<div class="muted" style="text-align:center;padding:20px">检索中...</div>';
  try {
    const params = new URLSearchParams();
    if (env) params.set('env', env);
    if (service) params.set('service', service);
    if (keyword) params.set('keyword', keyword);
    params.set('page_size', '100');
    const data = await api('/api/logs?' + params);
    renderServiceLogResults(data);
  } catch (e) {
    if (results) results.innerHTML = `<div class="panel">检索失败：${esc(e.message)}</div>`;
  }
}

function renderServiceLogResults(data) {
  const results = document.getElementById('svcLogResults');
  if (!results) return;
  if (!data.rows || data.rows.length === 0) {
    results.innerHTML = '<div class="muted" style="text-align:center;padding:40px">无匹配结果</div>';
    return;
  }
  results.innerHTML = `
    <div class="muted" style="font-size:12px;margin-bottom:8px">共 ${data.total} 条，显示前 ${data.rows.length} 条</div>
    <div class="tbl-wrap">
      <table class="tbl">
        <thead><tr><th style="width:140px">时间</th><th style="width:80px">环境</th><th style="width:120px">服务</th><th>内容</th><th style="width:60px">操作</th></tr></thead>
        <tbody>
          ${data.rows.map(r => `
            <tr>
              <td class="num muted">${fmtTime(r.ts)}</td>
              <td>${esc(r.env)}</td>
              <td>${esc(r.service)}</td>
              <td class="svc-log-content" title="${esc(r.file_path)}">${esc((r.content || '').slice(0, 120))}</td>
              <td><button class="btn sm" onclick="window._showLogContext(${r.id})">上下文</button></td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>`;
}

// 全局函数供 onclick 调用
window._showLogContext = async function(entryId) {
  const drawer = document.getElementById('svcLogContextDrawer');
  const content = document.getElementById('svcLogCtxContent');
  if (!drawer || !content) return;
  drawer.style.display = 'block';
  content.textContent = '加载中...';
  try {
    const data = await api(`/api/logs/context?id=${entryId}&lines=10`);
    if (data.error) {
      content.textContent = data.error;
      return;
    }
    content.textContent = data.lines.join('');
  } catch (e) {
    content.textContent = '加载失败：' + e.message;
  }
};
```

- [ ] **Step 2: 修改 logs.css 追加服务日志 Tab 样式**

```css
/* 服务日志 Tab */
.svc-log-content{max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-2);font-size:var(--fs-12);cursor:default}
.svc-log-content:hover{color:var(--primary)}
#svcLogContextDrawer .drawer-body{padding:0}
#svcLogCtxContent{margin:0;padding:var(--sp-4);font-size:var(--fs-12);line-height:1.6;background:var(--bg-2);border-radius:var(--r-ctl)}
```

- [ ] **Step 3: 浏览器验证**

启动 app，打开浏览器，访问日志页，切换到「服务日志」Tab，验证：
- Tab 切换正常
- 检索表单渲染正确
- 无 JS 报错

- [ ] **Step 4: 提交**

```bash
git add webapp/static/js/pages/logs.js webapp/static/css/pages/logs.css
git commit -m "feat(log-center): 前端服务日志 Tab（检索 + 上下文抽屉）"
```

---

### Task 5: 启动集成（app.py 集成 LogCollector）

**Files:**
- Modify: `webapp/app.py`

- [ ] **Step 1: 修改 app.py 启动 LogCollector**

在 `init_engine(app)` 之后追加：

```python
from webapp.log_collector import init_collector
init_collector(app)
```

- [ ] **Step 2: 运行全量测试确认无回归**

Run: `python -m unittest discover -s tests`
Expected: 全部 PASS

- [ ] **Step 3: 提交**

```bash
git add webapp/app.py
git commit -m "feat(log-center): 启动集成（LogCollector 守护线程）"
```

---

## 测试计划

1. **单元测试**：Task 1-3 的测试覆盖数据层/采集器/API
2. **集成验证**：Task 4-5 浏览器验证 Tab 切换 + 检索流程
3. **回归测试**：每个 Task 完成后 `python -m unittest discover -s tests` 全量通过

## 假设

- SSH 凭据复用 monitor.py 的 `_merge_server_ssh` 模式
- FTS5 扩展在 SQLite 标准构建中可用（Python 3.x 默认启用）
- 日志文件路径：`data/logs/collect/<env>/<service>/YYYY-MM-DD.log`
- 归档路径：`data/logs/archive/<env>/<service>/YYYY-MM-DD.log.gz`
