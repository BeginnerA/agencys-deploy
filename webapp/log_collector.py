"""日志采集器：SSH 增量拉取 docker logs + FTS5 索引 + 归档清理"""
import gzip
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db as dbm
from .alert_engine import get_engine

# docker logs 时间戳解析正则（含可选纳秒/微秒部分）
_DOCKER_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?"
)


def _parse_ts(line: str):
    """解析行首 docker 时间戳：返回 (epoch_seconds, 微秒, 剩余内容)。
    无时间戳或解析失败时返回 (0, 0, line)。"""
    if not line:
        return 0, 0, ""
    m = _DOCKER_TS_RE.match(line)
    if not m:
        return 0, 0, line
    frac = m.group(7) or ""
    us = int((frac[1:] + "000000")[:6]) if frac else 0
    try:
        dt = datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4)), int(m.group(5)), int(m.group(6)), us,
            tzinfo=timezone.utc
        )
    except (ValueError, OverflowError):
        return 0, 0, line
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
    return ts, us, rest


def _parse_docker_log_line(line: str):
    """解析 docker logs -t 输出行：提取时间戳 + 内容。
    格式：2026-08-18T10:30:45.123456789Z <content>
    返回 (epoch_seconds, content)。无时间戳时 ts=0。"""
    ts, _, rest = _parse_ts(line)
    return ts, rest


def _cursor_from_ts(ts: int, us: int) -> str:
    """由行时间戳构造下轮采集游标：微秒精度 +1μs 跳过边界。

    docker logs --since 为含边界的 >= 过滤，游标直接取最后一行时间戳会导致
    该行（及同秒所有行）每轮重复采集；+1μs 后既不重复也不遗漏。"""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=us)
    return (dt + timedelta(microseconds=1)).isoformat()


def _build_log_file_path(base_dir: str, env: str, service: str, date_str: str) -> str:
    """构建日志文件路径：base_dir/collect/env/service/YYYY-MM-DD.log"""
    return os.path.join(base_dir, "collect", env, service, f"{date_str}.log")


def _marker(token: str, container: str) -> str:
    """单通道批量采集的分段标记（在输出中独占一行）"""
    return f"@@LOGCUT:{token}:{container}"


def _split_marker_output(out: str, token: str, containers: list) -> dict:
    """按 @@LOGCUT 标记把单通道批量输出切分为 {container: 输出文本}

    仅认同行首完整匹配的标记行（token 每轮随机，日志内容碰撞概率可忽略）；
    标记缺失的容器值为 None（由调用方记失败，下轮重试）；
    标记存在但无日志行 → 空字符串（正常空拉取）。
    """
    prefix = f"@@LOGCUT:{token}:"
    chunks, buf, current = {}, None, None
    for line in out.splitlines(keepends=True):
        if line.startswith(prefix):
            current = line.rstrip("\r\n")[len(prefix):]
            buf = []
            chunks[current] = buf
            continue
        if current is not None:
            buf.append(line)
    return {c: ("".join(chunks[c]) if c in chunks else None) for c in containers}


class LogCollector:
    """日志采集器（后台守护线程）"""

    def __init__(self, db_path: str, log_dir: str, interval: int = 30,
                 ssh_func=None, max_lines_per_container: int = 5000,
                 monitor=None):
        self.db_path = db_path
        self.log_dir = log_dir
        self.interval = interval
        self.ssh_func = ssh_func  # 可注入测试 mock；生产环境用 monitor._ssh_exec
        self.max_lines = max_lines_per_container
        self.monitor = monitor  # MonitorScheduler 实例，复用其容器列表缓存
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
        """主循环：每 interval 秒执行一轮采集 + 每小时归档检查 + FTS 维护"""
        last_archive_check = 0
        while not self._stop.is_set():
            try:
                self._collect_round()
                now = time.time()
                if now - last_archive_check >= 3600:
                    self._archive_and_cleanup()
                    self._fts_maintenance()  # 每日一次：FTS optimize + 空间回收
                    last_archive_check = now
            except Exception as e:
                print(f"[log-collector] 采集周期异常: {e!r}", file=sys.stderr)
            self._stop.wait(self.interval)

    def _collect_round(self):
        """单轮采集：全部启用监控的环境按「主机」聚合后去重采集

        同一主机被多个环境绑定时（共用测试机），旧实现每个环境各拉一遍同主机
        全部容器日志（5 环境 × 29 容器 = 每轮 145 次 docker logs）；现按
        (host, port) 聚合，每容器每轮仅 1 次 docker logs，输出同时分发到各
        绑定环境（文件/FTS/游标仍按环境隔离，上层语义不变）；
        同名环境（跨项目 prod/test）共用同一日志目录与游标，按环境名去重
        避免同一份日志被重复追加/重复索引。
        """
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

        # 按主机聚合环境：jobs[(host, port)] = {"host","port","ssh_cfg","envs"}
        from .monitor import _merge_server_ssh
        jobs = {}
        for env_row in envs:
            env_name = env_row["name"]
            env_id = env_row["id"]
            try:
                env_ssh = json.loads(env_row["ssh"] or "{}")
            except (ValueError, TypeError):
                env_ssh = {}
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
                srv = dict(srv)
                key = (srv["host"], int(srv.get("ssh_port") or 22))
                job = jobs.get(key)
                if job is None:
                    job = {"host": srv["host"], "port": key[1],
                           "ssh_cfg": _merge_server_ssh(env_ssh, srv),
                           "envs": []}
                    jobs[key] = job
                job["envs"].append((env_name, env_id))
        if not jobs:
            self._current_kw_rules = None
            return
        # 主机间并发 8（SSH 限流，防连接风暴）；_fail_counts 的 key 含 host，
        # 各 key 仅由对应主机线程读写，且修改在 _lock 内完成（get+1+set 非原子）
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
            futures = [pool.submit(self._collect_host, job) for job in jobs.values()]
            for fut in futures:
                try:
                    # _collect_host 内部 SSH 命令已有超时，此处仅防意外
                    fut.result(timeout=360)
                except Exception:
                    pass
        self._current_kw_rules = None

    def _collect_host(self, job: dict):
        """采集单台主机：一次 SSH 单通道批量拉取所有容器增量日志

        优先复用 monitor 探测缓存的容器列表（省去独立 docker ps SSH 连接）；
        缓存不可用时退化为自己执行 docker ps。
        每个容器前打印唯一 @@LOGCUT 标记，单条命令一次拉取全部容器；输出按
        标记切分后分发给绑定该主机的各环境（原来的每容器、每环境各一条
        SSH 命令/channel → 每主机 1 条命令）。
        """
        from .monitor import _ssh_exec_multi
        host, port = job["host"], job["port"]
        ssh_cfg = job["ssh_cfg"]
        env_pairs = job["envs"]
        # 同名环境（跨项目 prod/test）共用同一日志目录与游标：按环境名去重，
        # 避免同一份日志被重复追加/重复索引
        out_envs = []
        seen_names = set()
        for env_name, env_id in env_pairs:
            if env_name in seen_names:
                continue
            seen_names.add(env_name)
            out_envs.append((env_name, env_id))
        # 优先复用 monitor 探测缓存的容器列表
        containers = None
        if self.monitor:
            cached = self.monitor._server_containers.get(host)
            if cached and (time.time() - cached["ts"]) < max(self.interval * 2, 60):
                containers = cached["containers"]
        if containers is None:
            # 缓存不可用（首轮或 monitor 未覆盖此服务器），退化为自己 docker ps
            try:
                outs = _ssh_exec_multi(host, port, ssh_cfg,
                                       ["docker ps --format '{{.Names}}|{{.Image}}'"],
                                       timeout=10)
            except Exception:
                return
            containers = []
            for line in outs[0].splitlines():
                if "|" in line:
                    name = line.split("|", 1)[0].strip()
                    if name:
                        containers.append(name)
        if not containers:
            return
        # 各环境游标：批量读取（每环境 1 次查询）
        states_by_env = {env_name: self._load_scan_states(env_name, host)
                         for env_name, _ in out_envs}
        # 单通道批量：每容器前打印唯一分段标记（token 每轮随机，防日志内容碰撞）
        token = secrets.token_hex(4)
        parts, metas = [], []
        for container in containers:
            # 取各环境最早游标（最小时间窗），任一环境缺失游标 → 首次窗口 5m，
            # 保证所有分发环境都不丢新日志（游标偏差仅在启用先后不同时出现，
            # 下一轮即收敛）
            cursors = [st.get(container, {}).get("last_ts", "")
                       for st in states_by_env.values()]
            valid = [c for c in cursors if c]
            last_ts = min(valid) if len(valid) == len(out_envs) else ""
            safe_container = shlex.quote(container)
            # 截断用 tail（保留最新）而非 head（保留最旧）：增量窗口内超量时
            # head 会丢弃最新日志且游标推进越过它们（永久丢失）；tail 只丢弃
            # 窗口内较旧行，最新日志与游标连续性不受影响
            if last_ts:
                logs_cmd = (f"docker logs --since '{last_ts}' -t "
                            f"{safe_container} 2>&1 | tail -n {self.max_lines}")
            else:
                logs_cmd = (f"docker logs --since 5m -t "
                            f"{safe_container} 2>&1 | tail -n {self.max_lines}")
            parts.append("echo " + shlex.quote(_marker(token, container)))
            parts.append(logs_cmd)
            metas.append((container, last_ts))
        cmd = "; ".join(parts)
        # 批量总超时随容器数放宽（单容器通常 <1s）
        timeout = max(30, min(300, 10 + 4 * len(metas)))
        try:
            # 单连接单命令顺序拉取（原为每容器一条命令，逐条 channel）
            out = self.ssh_func(host, port, ssh_cfg, cmd, timeout=timeout)
        except Exception:
            with self._lock:
                for container, _ in metas:
                    for env_name, _ in out_envs:
                        k = (env_name, host, container)
                        self._fail_counts[k] = self._fail_counts.get(k, 0) + 1
            return
        chunks = _split_marker_output(out, token, [c for c, _ in metas])
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for container, _ in metas:
            out_container = chunks.get(container)
            if out_container is None:
                # 标记缺失（命令中途失败/容器消失）：记失败，下轮重试
                with self._lock:
                    for env_name, _ in out_envs:
                        k = (env_name, host, container)
                        self._fail_counts[k] = self._fail_counts.get(k, 0) + 1
                continue
            with self._lock:
                for env_name, _ in out_envs:
                    self._fail_counts.pop((env_name, host, container), None)
            for env_name, env_id in out_envs:
                st = (states_by_env.get(env_name) or {}).get(container, {})
                # 跨日（或首次采集）：新日期文件，文件级偏移基准重置为 0
                file_offset = (st.get("file_offset", 0)
                               if st.get("file_date") == today else 0)
                self._collect_container_output(env_name, env_id, host, container,
                                               out_container,
                                               st.get("last_ts") or "", file_offset)

    def _load_scan_states(self, env_name: str, host: str) -> dict:
        """批量读取服务器所有容器采集游标：
        {container: {"last_ts", "file_offset", "file_date"}}（1 次连接）
        file_offset 为文件级累计逻辑字节偏移（log_scan_state.last_line）"""
        conn = dbm.get_conn(self.db_path)
        try:
            rows = conn.execute(
                "SELECT container, last_ts, last_line, file_date FROM log_scan_state "
                "WHERE env=? AND server=?",
                (env_name, host)
            ).fetchall()
            return {r["container"]: {
                "last_ts": r["last_ts"] or "",
                "file_offset": r["last_line"] or 0,
                "file_date": r["file_date"] or "",
            } for r in rows}
        finally:
            conn.close()

    def _collect_container(self, env_name: str, env_id: int, host: str,
                           ssh_cfg: dict, container: str, port: int):
        """采集单个容器（独立 SSH，兼容单容器路径）；批量采集走 _collect_host"""
        key = (env_name, host, container)
        states = self._load_scan_states(env_name, host)
        st = states.get(container, {})
        last_ts = st.get("last_ts", "")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        safe_container = shlex.quote(container)
        # 截断用 tail（保留最新）而非 head（保留最旧），原因见 _collect_server
        if last_ts:
            cmd = f"docker logs --since '{last_ts}' -t {safe_container} 2>&1 | tail -n {self.max_lines}"
        else:
            cmd = f"docker logs --since 5m -t {safe_container} 2>&1 | tail -n {self.max_lines}"
        try:
            out = self.ssh_func(host, port, ssh_cfg, cmd, timeout=30)
            with self._lock:
                self._fail_counts.pop(key, None)
        except Exception:
            with self._lock:
                self._fail_counts[key] = self._fail_counts.get(key, 0) + 1
            return
        # 跨日（或首次采集）：新日期文件，文件级偏移基准重置为 0
        file_offset = st.get("file_offset", 0) if st.get("file_date") == today else 0
        self._collect_container_output(env_name, env_id, host, container, out,
                                       last_ts, file_offset)

    def _collect_container_output(self, env_name: str, env_id: int, host: str,
                                  container: str, out: str, last_ts: str,
                                  file_offset: int = 0):
        """处理单容器采集输出：解析行 → 归档文件 → FTS 索引 → 关键词扫描 → 游标

        file_offset: 该容器当日日志文件的“文件级逻辑字节偏移”基准（跨日重置 0）。
        line_offset 语义与 log_routes.log_context 的定位逻辑一致：
        从文件头逐行累加源行字节长度（无 \r）得到的文件级偏移。
        """
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
        line_offset = file_offset
        last_line_ts = last_ts
        for i, raw_line in enumerate(lines):
            ts, us, content = _parse_ts(raw_line.rstrip("\n"))
            if ts > 0:
                # 游标推进到最后一行时间戳 +1μs（跳过 --since 含边界导致的重复）
                last_line_ts = _cursor_from_ts(ts, us)
            entries.append((ts or int(time.time()), env_name, service, content,
                            file_path, line_offset, 1))
            line_offset += len(raw_line.encode("utf-8", "ignore"))

        if entries:
            rowids = self._index_log_entries(entries)
            self._scan_keywords_in_entries(entries, env_name, env_id, rowids)
            self._update_scan_state(env_name, host, container, service,
                                    last_line_ts or "", line_offset, today)

    def _append_to_file(self, file_path: str, lines: list):
        """追加日志行到文件（自动创建目录）"""
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "a", encoding="utf-8") as f:
            f.writelines(lines)

    def _index_log_entries(self, entries: list) -> list:
        """批量写入 log_entries + 同步 FTS5；返回每条日志的 rowid（与 entries 一一对应）

        FTS 行用 executemany 一次提交，减少 Python→SQLite 往返次数
        （log_entries 需逐条 INSERT 取 lastrowid）
        """
        conn = dbm.get_conn(self.db_path)
        rowids = []
        fts_rows = []
        try:
            for ts, env, service, content, file_path, offset, count in entries:
                cursor = conn.execute(
                    "INSERT INTO log_entries (ts, env, service, container, file_path, line_offset, line_count, content, keywords) "
                    "VALUES (?, ?, ?, '', ?, ?, ?, ?, '[]')",
                    (ts, env, service, file_path, offset, count, content)
                )
                rowids.append(cursor.lastrowid)
                fts_rows.append((cursor.lastrowid, str(ts), env, service, content))
            conn.executemany(
                "INSERT INTO log_fts (rowid, ts, env, service, content) "
                "VALUES (?, ?, ?, ?, ?)",
                fts_rows
            )
            conn.commit()
            return rowids
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

    def _scan_keywords_in_entries(self, entries: list, env_name: str, env_id: int,
                                  rowids: list = None):
        """扫描日志条目中的关键词，命中时触发告警

        rowids: _index_log_entries 返回的 rowid 列表，与 entries 一一对应；
        传入后命中行携带 entry_id/hit_ts，供告警详情展示日志上下文。

        排除关键词：命中包含关键词后，检查当前行及邻近行（上下文窗口 ±5 条），
        若窗口内任一条目命中排除关键词则跳过不告警。
        这样可正确处理多行堆栈跟踪中异常信息与排除词分布在不同行的场景。
        """
        rules = getattr(self, '_current_kw_rules', None)
        if not rules:
            return
        engine = get_engine()
        if engine is None:
            return
        service = entries[0][2] if entries else ""
        # 排除关键词上下文窗口大小（检查前后各 _EXCLUDE_CTX 条）
        _EXCLUDE_CTX = 5
        for rule in rules:
            metric_raw = (rule.get("metric") or "").strip()
            if not metric_raw:
                continue
            keywords = [kw.strip() for kw in metric_raw.split(",") if kw.strip()]
            if not keywords:
                continue
            # 解析排除关键词
            exclude_raw = (rule.get("exclude_keyword") or "").strip()
            exclude_kws = [k.strip() for k in exclude_raw.split(",") if k.strip()] if exclude_raw else []
            # 扫描每条日志，找到第一个命中即触发（同规则同服务由指纹去重）
            for i, (ts, env, svc, content, file_path, offset, count) in enumerate(entries):
                if not content:
                    continue
                matched_kw = None
                excluded = False
                for kw in keywords:
                    if kw in content:
                        # 检查排除关键词：当前行 + 邻近行（上下文窗口）
                        if exclude_kws:
                            lo = max(0, i - _EXCLUDE_CTX)
                            hi = min(len(entries), i + _EXCLUDE_CTX + 1)
                            if any(ekw in entries[j][3] for j in range(lo, hi) for ekw in exclude_kws):
                                excluded = True
                                break
                        matched_kw = kw
                        break
                if excluded:
                    continue  # 被排除，继续扫描下一行日志
                if matched_kw:
                    ctx = [content]
                    engine.trigger_log_alert(
                        rule, svc, env_name, content, ctx, env_id,
                        matched_keyword=matched_kw,
                        entry_id=rowids[i] if rowids and i < len(rowids) else None,
                        hit_ts=ts if rowids else None)
                    break  # 本规则已触发，不再扫描后续日志行（指纹去重）

    def _update_scan_state(self, env: str, server: str, container: str,
                           service: str, last_ts: str, last_line: int,
                           file_date: str = ""):
        """更新采集游标（upsert）；last_line 为文件级累计逻辑字节偏移（跨日重置）"""
        conn = dbm.get_conn(self.db_path)
        try:
            existing = conn.execute(
                "SELECT id FROM log_scan_state WHERE env=? AND server=? AND container=?",
                (env, server, container)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE log_scan_state SET last_ts=?, last_line=?, file_date=?, updated_at=? WHERE id=?",
                    (last_ts, last_line, file_date, int(time.time()), existing["id"])
                )
            else:
                conn.execute(
                    "INSERT INTO log_scan_state (env, server, container, service, last_ts, last_line, file_date, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (env, server, container, service, last_ts, last_line, file_date, int(time.time()))
                )
            conn.commit()
        finally:
            conn.close()

    def _archive_and_cleanup(self):
        """归档过期日志 + 清理过期 FTS5 条目（每轮自动执行；实现见模块级
        _archive_and_cleanup_files，与手动清理共用同一套策略）"""
        return _archive_and_cleanup_files(self.db_path, self.log_dir)

    def _fts_maintenance(self):
        """每日一次 FTS5 optimize + incremental_vacuum（控制 DB 膨胀与碎片）

        optimize 合并 FTS 增量段（降低查询放大、减少文件碎片）；
        incremental_vacuum 回收 freelist 空闲页（auto_vacuum=INCREMENTAL 的库
        生效；存量库先执行 scripts/db_vacuum.py 转换）；并按 log_archive_days
        上限兜底清理孤儿日志行（归档清扫仅按 file_path 精确匹配，路径变化时
        会永久残留）。日期记录在 platform_settings.log_fts_optimize_date。
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn = dbm.get_conn(self.db_path)
        try:
            row = conn.execute(
                "SELECT value FROM platform_settings WHERE key='log_fts_optimize_date'"
            ).fetchone()
            if row:
                try:
                    if json.loads(row["value"]) == today:
                        return  # 当日已执行
                except (ValueError, TypeError):
                    pass
            # 兜底清理：文件归档清扫按 file_path 精确匹配，路径迁移等场景会残留
            # 孤儿日志行（永久占空间）；按 archive_days 上限删除，防止 DB 复胀
            try:
                crow = conn.execute(
                    "SELECT value FROM platform_settings"
                    " WHERE key='log_archive_days'"
                ).fetchone()
                archive_days = int(json.loads(crow["value"])) if crow else 30
            except (ValueError, TypeError):
                archive_days = 30
            cur = conn.execute(
                "DELETE FROM log_entries WHERE ts < ?",
                (int(time.time()) - archive_days * 86400,))
            if cur.rowcount:
                conn.execute("INSERT INTO log_fts(log_fts) VALUES('rebuild')")
                print(f"[log-collector] 已清理孤儿日志行 {cur.rowcount} 条",
                      file=sys.stderr)
            conn.commit()
            conn.execute("INSERT INTO log_fts(log_fts) VALUES('optimize')")
            conn.commit()
            conn.execute("PRAGMA incremental_vacuum(2000)")
            conn.commit()
            now = int(time.time())
            conn.execute(
                "INSERT INTO platform_settings (key, value, updated_at) "
                "VALUES ('log_fts_optimize_date', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (json.dumps(today), now))
            conn.commit()
        except Exception as e:
            print(f"[log-collector] FTS 维护失败: {e!r}", file=sys.stderr)
        finally:
            conn.close()


def _cleanup_fts_for_file(db_path: str, env: str, service: str, file_path: str):
    """清理指定文件的 FTS5 + log_entries 条目"""
    conn = dbm.get_conn(db_path)
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


def _archive_and_cleanup_files(db_path: str, log_dir: str) -> dict:
    """归档过期日志文件 + 清理对应索引条目（自动轮与手动清理共用）

    collect/ 下超过 log_retention_days 的 .log 压缩到 archive/ 后删除原文件
    （并按 file_path 清理索引行）；archive/ 下超过 log_archive_days 的 .gz
    永久删除。返回 {"archived": n, "purged": n}。
    """
    conn = dbm.get_conn(db_path)
    try:
        retention_days = 7
        row = conn.execute(
            "SELECT value FROM platform_settings WHERE key='log_retention_days'"
        ).fetchone()
        if row:
            try:
                retention_days = int(json.loads(row["value"]))
            except (ValueError, TypeError):
                pass
        archive_days = 30
        row = conn.execute(
            "SELECT value FROM platform_settings WHERE key='log_archive_days'"
        ).fetchone()
        if row:
            try:
                archive_days = int(json.loads(row["value"]))
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

    collect_base = os.path.join(log_dir, "collect")
    archive_base = os.path.join(log_dir, "archive")
    archived = purged = 0

    if not os.path.isdir(collect_base):
        return {"archived": archived, "purged": purged}

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
                    archived += 1
                    _cleanup_fts_for_file(db_path, env_name, service, str(log_file))

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
                        purged += 1
    return {"archived": archived, "purged": purged}


# 手动清理互斥：防止 Web 端并发触发两次清理；与采集线程的并发由
# busy_timeout + 幂等清理语义兑底（每日自动轮面对同样的并发场景）
_manual_cleanup_lock = threading.Lock()


def run_manual_cleanup(db_path: str, log_dir: str) -> dict:
    """手动触发一次性日志存储清理（Web 端「立即清理」入口）

    与采集器每日自动维护同一套策略，立即执行：
    1) 删除超 log_retention_days 的索引行并整体重建 FTS（替代逐行删除）；
    2) 文件归档清扫：collect 超期 → archive 压缩；archive 超期 → 删除；
    3) 孤儿行兑底（archive_days 上限）+ FTS optimize + incremental_vacuum
       全量回收空闲页；顺手写入当日维护标记（当日自动维护跳过）。
    返回统计 dict；并发调用未抢到锁时返回 {"busy": True}。
    """
    if not _manual_cleanup_lock.acquire(blocking=False):
        return {"busy": True}
    try:
        settings = {}
        conn = dbm.get_conn(db_path)
        try:
            for key, default in (("log_retention_days", 7),
                                 ("log_archive_days", 30)):
                row = conn.execute(
                    "SELECT value FROM platform_settings WHERE key=?", (key,)
                ).fetchone()
                try:
                    settings[key] = int(json.loads(row["value"])) if row else default
                except (ValueError, TypeError):
                    settings[key] = default
        finally:
            conn.close()
        retention_days = settings["log_retention_days"]
        archive_days = settings["log_archive_days"]
        now = int(time.time())

        def _db_size():
            try:
                return os.path.getsize(db_path)
            except OSError:
                return 0

        size_before = _db_size()

        # 1) 超保留期索引行：DELETE + FTS rebuild（高效路径，行级删除仅用于归档文件）
        deleted_entries = 0
        conn = dbm.get_conn(db_path)
        try:
            cur = conn.execute(
                "DELETE FROM log_entries WHERE ts < ?",
                (now - retention_days * 86400,))
            deleted_entries = cur.rowcount or 0
            if deleted_entries:
                conn.execute("INSERT INTO log_fts(log_fts) VALUES('rebuild')")
            conn.commit()
        finally:
            conn.close()

        # 2) 文件归档清扫（与自动轮共用实现）
        files = _archive_and_cleanup_files(db_path, log_dir)

        orphan_deleted = 0
        conn = dbm.get_conn(db_path)
        try:
            # 3) 孤儿行兑底（用户把 retention 调得比 archive 大时仍能收口）
            cur = conn.execute(
                "DELETE FROM log_entries WHERE ts < ?",
                (now - archive_days * 86400,))
            orphan_deleted = cur.rowcount or 0
            if orphan_deleted:
                conn.execute("INSERT INTO log_fts(log_fts) VALUES('rebuild')")
            conn.commit()
            conn.execute("INSERT INTO log_fts(log_fts) VALUES('optimize')")
            conn.commit()
            try:
                # 无参 = 全量回收可回收空闲页（auto_vacuum=INCREMENTAL 的库）
                conn.execute("PRAGMA incremental_vacuum")
                conn.commit()
            except sqlite3.OperationalError:
                pass  # 未转换的旧库不可增量回收，可用 scripts/db_vacuum.py 处理
            # 当日维护标记：手动已做，自动维护当日跳过
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            conn.execute(
                "INSERT INTO platform_settings (key, value, updated_at) "
                "VALUES ('log_fts_optimize_date', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (json.dumps(today), int(time.time())))
            conn.commit()
        finally:
            conn.close()

        return {
            "retention_days": retention_days,
            "archive_days": archive_days,
            "deleted_entries": deleted_entries,
            "orphan_deleted": orphan_deleted,
            "archived_files": files["archived"],
            "purged_files": files["purged"],
            "db_size_before": size_before,
            "db_size_after": _db_size(),
        }
    finally:
        _manual_cleanup_lock.release()


_collector = None


def get_collector() -> "LogCollector":
    global _collector
    return _collector


def init_collector(app) -> None:
    """启动日志采集器（依赖 monitor 的 SSH 函数，复用其容器缓存）"""
    global _collector
    if _collector is not None:
        return
    from .monitor import _ssh_exec, get_monitor
    log_dir = app.config.get("LOG_DIR") or str(
        Path(app.root_path).parent / "data" / "logs")
    mon_interval = app.config.get("MONITOR_INTERVAL", 60)
    log_interval = mon_interval * 2  # 日志采集频率为监控的一半，减少 SSH 连接数
    _collector = LogCollector(app.config["DB_PATH"], log_dir, interval=log_interval,
                              ssh_func=_ssh_exec, monitor=get_monitor())
    _collector.start()
    print(f"[log-collector] 日志采集器已启动，采集周期 {log_interval}s")
