"""日志采集器：SSH 增量拉取 docker logs + FTS5 索引 + 归档清理"""
import gzip
import os
import re
import shlex
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db as dbm
from .alert_engine import get_engine

# docker logs 时间戳解析正则
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
        if rest and rest[0] == '.':
            rest = rest[1:]  # skip '.'
            rest = rest.lstrip('0123456789')  # strip nanosecond digits
            if rest and rest[0] == 'Z':
                rest = rest[1:]
            if rest and rest[0] == ' ':
                rest = rest[1:]
        elif rest and rest[0] == 'Z':
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
    """日志采集器（后台守护线程）"""

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
                now = time.time()
                if now - last_archive_check >= 3600:
                    self._archive_and_cleanup()
                    last_archive_check = now
            except Exception as e:
                print(f"[log-collector] 采集周期异常: {e!r}", file=sys.stderr)
            self._stop.wait(self.interval)

    def _collect_round(self):
        """单轮采集：遍历所有启用了监控的环境 × 服务器 × 容器"""
        if not self.ssh_func:
            return
        # 本轮启用的日志关键词规则（每轮加载一次，供所有容器复用）
        self._current_kw_rules = self._load_log_keyword_rules()
        conn = dbm.get_conn(self.db_path)
        try:
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
        self._current_kw_rules = None

    def _collect_server(self, env_name: str, env_id: int, env_ssh: dict, server: dict):
        """采集单台服务器的所有容器日志"""
        from .monitor import _merge_server_ssh
        ssh_cfg = _merge_server_ssh(env_ssh, server)
        host = server["host"]
        port = server.get("ssh_port") or 22
        try:
            out = self.ssh_func(host, port, ssh_cfg,
                                "docker ps --format '{{.Names}}|{{.Image}}'", timeout=10)
        except Exception:
            return
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
        safe_container = shlex.quote(container)
        if last_ts:
            cmd = f"docker logs --since '{last_ts}' -t {safe_container} 2>&1 | head -{self.max_lines}"
        else:
            cmd = f"docker logs --since 5m -t {safe_container} 2>&1 | head -{self.max_lines}"

        try:
            out = self.ssh_func(host, port, ssh_cfg, cmd, timeout=30)
            self._fail_counts.pop(key, None)
        except Exception:
            self._fail_counts[key] = self._fail_counts.get(key, 0) + 1
            return

        if not out or not out.strip():
            return

        lines = out.splitlines(keepends=True)
        if not lines:
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        service = container
        file_path = _build_log_file_path(self.log_dir, env_name, service, today)

        self._append_to_file(file_path, lines)

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
            self._scan_keywords_in_entries(entries, env_name, env_id)
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
                    "INSERT INTO log_entries (ts, env, service, container, file_path, line_offset, line_count, content, keywords) "
                    "VALUES (?, ?, ?, '', ?, ?, ?, ?, '[]')",
                    (ts, env, service, file_path, offset, count, content)
                )
                rowid = cursor.lastrowid
                conn.execute(
                    "INSERT INTO log_fts (rowid, ts, env, service, content) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (rowid, str(ts), env, service, content)
                )
            conn.commit()
        finally:
            conn.close()

    def _load_log_keyword_rules(self) -> list:
        """加载所有已启用的 log_keyword 类型告警规则"""
        try:
            conn = dbm.get_conn(self.db_path)
            try:
                rows = conn.execute(
                    "SELECT * FROM alert_rules "
                    "WHERE metric_type='log_keyword' AND enabled=1"
                ).fetchall()
            finally:
                conn.close()
            return [dbm._deserialize("alert_rules", dict(r)) for r in rows]
        except Exception:
            return []

    def _scan_keywords_in_entries(self, entries: list, env_name: str, env_id: int):
        """扫描日志条目中的关键词，命中时触发告警"""
        rules = getattr(self, '_current_kw_rules', None)
        if not rules:
            return
        engine = get_engine()
        if engine is None:
            return
        service = entries[0][2] if entries else ""
        for rule in rules:
            metric_raw = (rule.get("metric") or "").strip()
            if not metric_raw:
                continue
            keywords = [kw.strip() for kw in metric_raw.split(",") if kw.strip()]
            if not keywords:
                continue
            # 扫描每条日志，找到第一个命中即触发（同规则同服务由指纹去重）
            for ts, env, svc, content, file_path, offset, count in entries:
                if not content:
                    continue
                for kw in keywords:
                    if kw in content:
                        ctx = [content]
                        engine.trigger_log_alert(
                            rule, svc, env_name, content, ctx, env_id,
                            matched_keyword=kw)
                        break  # 本条日志只触发一次（取首个命中关键词）
                else:
                    continue
                break  # 本规则已触发，不再扫描后续日志行（指纹去重，同规则同服务只告警一次）

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
                    "UPDATE log_scan_state SET last_ts=?, last_line=?, updated_at=? WHERE id=?",
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
        """归档过期日志 + 清理过期 FTS5 条目"""
        conn = dbm.get_conn(self.db_path)
        try:
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

        for env_dir in Path(collect_base).iterdir():
            if not env_dir.is_dir():
                continue
            env_name = env_dir.name
            for svc_dir in env_dir.iterdir():
                if not svc_dir.is_dir():
                    continue
                service = svc_dir.name
                for log_file in svc_dir.glob("*.log"):
                    date_str = log_file.stem
                    if date_str <= retention_cutoff:
                        archive_dir = Path(archive_base) / env_name / service
                        archive_dir.mkdir(parents=True, exist_ok=True)
                        archive_path = archive_dir / (log_file.name + ".gz")
                        with open(log_file, "rb") as f_in:
                            with gzip.open(archive_path, "wb") as f_out:
                                shutil.copyfileobj(f_in, f_out)
                        log_file.unlink()
                        self._cleanup_fts_for_file(env_name, service, str(log_file))

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
