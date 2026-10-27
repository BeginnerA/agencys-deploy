# M5 观测面 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `jar_service` 补齐观测面：systemd 进程状态与资源指标、journalctl 日志采集、每主机版本视图，并让设备页实时展示。

**Architecture:** 新增纯函数模块 `webapp/jar_probe.py` 负责「探测命令构造 / 输出解析 / 跨轮 CPU 差分 / 每主机聚合」；监控调度器在第一轮 SSH 批命令中追加单元探测，把结果切片为服务级 `hosts` 视图并只在版本变化时写穿 `service_host_state`；日志采集器在同一条主机批命令中追加 `journalctl -o json`，转换成带时间戳前缀的行后复用现有「文件归档 → FTS → 关键词告警 → 游标」管线（扫描键 `systemd:<unit>`）；新增只读 API 合并 DB 快照与实时视图，设备页用纯渲染函数展示每主机抽屉。

**Tech Stack:** Python 3 / Flask / SQLite / paramiko（已有）+ 原生 JS（无构建步骤）+ pytest/unittest + Node 无头用例。

**Spec:** `docs/superpowers/specs/2026-10-24-multi-runtime-deploy-design.md`（§3.4 进程观测、§6.3 journal 日志、§11 每主机版本视图）

## Global Constraints

- 单实例 jar 发布存在停机窗口，文档与 UI 均不得承诺零停机。
- 所有测试必须桩掉 SSH/SFTP/systemctl/journalctl/docker，禁止真实网络与真实服务器。
- 测试不得读写仓库 `data/`：一律用 `tempfile.mkdtemp()`，Flask 用例用 `create_app(data_dir=...)` 注入。
- 不新增第三方依赖；`scripts/quality_gate.py` 中 ruff/node 缺失时保持 SKIP。
- 不执行 push；每个任务只做本地 commit（用户明确要求推送时才推送）。
- 权威版本状态仍在目标机（`.deploy_version` + `CURRENT`）；`service_host_state` 只是展示缓存。
- 日志服务键统一为 `service_name or name`；journal 采集的扫描键统一为 `systemd:<unit>`，文件目录用 `<unit>`（Windows 文件名不能含 `:`）。
- 单元名统一由 `derive_unit_name()` 派生（`unit_name → image_name → 服务名 → app`），禁止再复制三元表达式。

## File Structure

| 文件 | 职责 |
| --- | --- |
| `deploy_strategies/jar_config.py` | 新增 `derive_unit_name()`（派生口径唯一来源） |
| `webapp/db.py` | 新增 `record_host_state_live()`（只更新当前版本/时间，保留回滚信息） |
| `webapp/task_manager.py`、`webapp/routes/config_routes.py` | 改用统一单元名派生 |
| `webapp/jar_probe.py`（新） | 单元探测命令、输出解析、CPU 差分、每主机聚合（纯函数） |
| `webapp/monitor.py` | 主机批探测追加单元命令；环境级 jar 服务聚合、健康检查接入、版本写穿 |
| `webapp/log_collector.py` | journal 纯函数 + 采集器集成（同一条主机批命令） |
| `webapp/routes/log_routes.py` | 服务列表/尾查对空 `service_name` 回退服务名 |
| `webapp/routes/config_routes.py`、`webapp/static/js/jar_hosts.js`（新） | `GET .../hosts` 只读 API + 纯渲染表格 |
| `webapp/static/js/pages/devices.js`、`webapp/templates/screen.html` | 卡片状态/版本/主机数、主机抽屉、脚本引入与版本号 |
| `tests/test_jar_probe.py` 等 | 纯函数与集成回归 |
| `README.md`、`docs/usage.md` | 观测能力文档 |

---

### Task 1: 统一单元名派生 + 版本直播写穿

**Files:**
- Modify: `deploy_strategies/jar_config.py`（文件末尾）
- Modify: `webapp/db.py`（`upsert_host_state` 之后）
- Modify: `webapp/task_manager.py:355`
- Modify: `webapp/routes/config_routes.py:880-885`
- Test: `tests/test_jar_config.py`、`tests/test_jar_db.py`

**Interfaces:**
- Produces:
  - `derive_unit_name(runtime_config, image_name: str = "", service_name: str = "") -> str`
  - `record_host_state_live(db_path: str, project_id: int, env: str, service: str, host: str, current_version: str) -> None`

- [ ] **Step 1: 写失败测试**

在 `tests/test_jar_config.py` 的导入中补 `derive_unit_name`：

```python
from deploy_strategies.jar_config import (
    artifact_filename, derive_unit_name, parse_runtime_config, sha256_file,
    validate_runtime_config)
```

在 `ArtifactTest` 之后新增：

```python
class DeriveUnitNameTest(unittest.TestCase):
    def test_priority(self):
        self.assertEqual(derive_unit_name(
            {"unit_name": "hqb-auth"}, "img", "svc"), "hqb-auth")
        self.assertEqual(derive_unit_name({"unit_name": ""}, "img", "svc"), "img")
        self.assertEqual(derive_unit_name({}, "", "svc"), "svc")
        self.assertEqual(derive_unit_name({}, "", ""), "app")

    def test_accepts_json_string(self):
        self.assertEqual(derive_unit_name('{"unit_name": "u1"}', "img"), "u1")
        self.assertEqual(derive_unit_name("{bad json", "img"), "img")
```

在 `tests/test_jar_db.py` 的 `JarDbTest` 中新增：

```python
    def test_host_state_live_keeps_rollback_info(self):
        dbm.upsert_host_state(self.db, self.pid, "prod", "auth", "10.0.0.1",
                              "1.0.1", "1.0.0", 7)
        dbm.record_host_state_live(self.db, self.pid, "prod", "auth",
                                   "10.0.0.1", "1.0.2")
        rows = dbm.list_host_state(self.db, self.pid, "prod", "auth")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["current_version"], "1.0.2")
        self.assertEqual(rows[0]["previous_version"], "1.0.0")  # 直播写穿不清回滚基线
        self.assertEqual(rows[0]["last_task_id"], 7)

    def test_host_state_live_inserts_when_missing(self):
        dbm.record_host_state_live(self.db, self.pid, "prod", "auth",
                                   "10.0.0.2", "2.0.0")
        rows = dbm.list_host_state(self.db, self.pid, "prod", "auth")
        self.assertEqual([r["host"] for r in rows], ["10.0.0.2"])
        self.assertEqual(rows[0]["current_version"], "2.0.0")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_jar_config.py tests/test_jar_db.py -q`
Expected: FAIL/ERROR，提示 `derive_unit_name` / `record_host_state_live` 未定义。

- [ ] **Step 3: 实现**

`deploy_strategies/jar_config.py` 末尾追加：

```python
def derive_unit_name(runtime_config, image_name: str = "",
                     service_name: str = "") -> str:
    """平台统一口径：unit_name → image_name → 服务名 → app（不含 .service）"""
    cfg = _as_dict(runtime_config)
    return str(cfg.get("unit_name") or image_name or service_name or "app")
```

`webapp/db.py` 的 `upsert_host_state()` 之后追加：

```python
def record_host_state_live(db_path: str, project_id: int, env: str, service: str,
                           host: str, current_version: str) -> None:
    """监控轮询写穿当前版本：只更新 current_version，保留回滚基线与任务号"""
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO service_host_state "
            "(project_id, env, service, host, current_version, previous_version, "
            " last_task_id, updated_at) VALUES (?,?,?,?,?, '', 0, ?) "
            "ON CONFLICT(project_id, env, service, host) DO UPDATE SET "
            "current_version=excluded.current_version, updated_at=excluded.updated_at",
            (project_id, env or "", service, host, current_version or "",
             int(time.time())))
        conn.commit()
    finally:
        conn.close()
```

`webapp/task_manager.py` 第 355 行：

```python
            from deploy_strategies.jar_config import derive_unit_name
            unit = derive_unit_name(cfg, rec.get("image_name"), rec["name"])
```

（删除原有的 `unit = cfg["unit_name"] or ...` 与同函数内的 `parse_runtime_config` 导入可保留，`cfg` 仍用于 `app_home` 校验。）

`webapp/routes/config_routes.py` 的 `_unit_target()`：

```python
def _unit_target(rec) -> tuple:
    from deploy_strategies.jar_config import derive_unit_name, parse_runtime_config
    import service_unit
    cfg = parse_runtime_config(rec.get("runtime_config"))
    unit = derive_unit_name(cfg, rec.get("image_name"), rec["name"])
    return unit, service_unit.render_unit(cfg, unit)
```

- [ ] **Step 4: 运行相关测试**

Run: `python -m pytest tests/test_jar_config.py tests/test_jar_db.py tests/test_config_routes_jar.py tests/test_restart_task.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add deploy_strategies/jar_config.py webapp/db.py webapp/task_manager.py webapp/routes/config_routes.py tests/test_jar_config.py tests/test_jar_db.py
git commit -m "feat(jar): unify unit name derivation and live host-state write"
```

---

### Task 2: 纯函数探测模块 `webapp/jar_probe.py`

**Files:**
- Create: `webapp/jar_probe.py`
- Test: `tests/test_jar_probe.py`（新建）

**Interfaces:**
- Produces:
  - `UNIT_PROPS: tuple[str, ...]`
  - `build_probe_cmd(units: dict, token: str = None) -> tuple[str, str]`（`units: {unit: app_home | {"app_home": ...}}`）
  - `parse_probe_output(out: str, token: str, expected_units=()) -> dict`（`{"uptime": float, "clk_tck": int, "units": {unit: raw}}`）
  - `build_view(raw: dict, prev: dict = None, now: float = None, proc_uptime: float = 0.0, clk_tck: int = 100) -> dict`
  - `compute_cpu_pct(prev, pid, cpu_ticks, now, clk_tck=100) -> float | None`
  - `aggregate_hosts(hosts: list) -> dict`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_jar_probe.py`：

```python
"""jar_probe 纯函数：命令构造、输出解析、CPU 差分与每主机聚合"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import jar_probe


def _stat_line(pid=4242, utime=500, stime=200, rss_pages=20480):
    # 剥掉 "pid (comm) " 后：rest[0]=state, rest[11]=utime, rest[12]=stime,
    # rest[21]=rss（对应 /proc/<pid>/stat 的 3/14/15/24 字段）
    rest = ["S"] + ["0"] * 10 + [str(utime), str(stime)] + ["0"] * 8 + [str(rss_pages)]
    return f"{pid} (java app) " + " ".join(rest)


SAMPLE = "\n".join([
    "UPTIME:10000.00 20000.00",
    "CLK:100",
    "@@JARUNIT:tok:0",
    "LoadState=loaded",
    "ActiveState=active",
    "SubState=running",
    "MainPID=4242",
    "NRestarts=2",
    "ActiveEnterTimestampMonotonic=9600000000",
    "VER:1.0.1",
    "LINK:/opt/auth/releases/1.0.1",
    "STAT:" + _stat_line(),
    "@@JARUNIT:tok:1",
    "LoadState=not-found",
    "ActiveState=inactive",
    "SubState=dead",
    "MainPID=0",
    "NRestarts=0",
    "ActiveEnterTimestampMonotonic=0",
    "VER:",
    "LINK:",
])


class BuildProbeCmdTest(unittest.TestCase):
    def test_units_and_versions_are_quoted(self):
        cmd, token = jar_probe.build_probe_cmd(
            {"hqb-auth": "/opt/auth", "hqb api": {"app_home": "/opt/a b"}},
            token="tok")
        self.assertEqual(token, "tok")
        self.assertIn("@@JARUNIT:tok:0", cmd)
        self.assertIn("@@JARUNIT:tok:1", cmd)
        self.assertIn("systemctl show -p LoadState", cmd)
        self.assertIn("cat /opt/auth/.deploy_version", cmd)
        self.assertIn("readlink /opt/auth/CURRENT", cmd)
        self.assertIn("ghcq".replace("ghcq", "'hqb api'"), cmd)  # shlex.quote
        self.assertIn("'/opt/a b'", cmd)

    def test_units_order_is_stable(self):
        cmd, _ = jar_probe.build_probe_cmd({"b": "/b", "a": "/a"}, token="t")
        self.assertLess(cmd.index(":0"), cmd.index(":1"))


class ParseProbeOutputTest(unittest.TestCase):
    def test_parses_all_fields(self):
        parsed = jar_probe.parse_probe_output(SAMPLE, "tok",
                                             ["hqb-auth", "other"])
        self.assertEqual(parsed["uptime"], 10000.0)
        self.assertEqual(parsed["clk_tck"], 100)
        u = parsed["units"]["hqb-auth"]
        self.assertEqual(u["load_state"], "loaded")
        self.assertEqual(u["active_state"], "active")
        self.assertEqual(u["pid"], 4242)
        self.assertEqual(u["restarts"], 2)
        self.assertEqual(u["active_enter_mono"], 9600000000.0)
        self.assertEqual(u["version"], "1.0.1")
        self.assertEqual(u["link_version"], "1.0.1")
        self.assertEqual(u["cpu_ticks"], 700)
        self.assertEqual(u["rss_pages"], 20480)
        self.assertEqual(parsed["units"]["other"]["load_state"], "not-found")

    def test_missing_expected_unit_gets_not_found(self):
        parsed = jar_probe.parse_probe_output("", "tok", ["ghost"])
        self.assertEqual(parsed["units"]["ghost"]["load_state"], "not-found")
        self.assertEqual(parsed["units"]["ghost"]["version"], "")


class BuildViewTest(unittest.TestCase):
    def _raw(self, **kw):
        raw = {"load_state": "loaded", "active_state": "active",
               "sub_state": "running", "pid": 4242, "restarts": 1,
               "active_enter_mono": 3600_000_000, "version": "1.0.1",
               "link_version": "", "cpu_ticks": 700, "rss_pages": 25600}
        raw.update(kw)
        return raw

    def test_online_view(self):
        view = jar_probe.build_view(self._raw(), proc_uptime=7200.0, now=100.0)
        self.assertEqual(view["status"], "online")
        self.assertEqual(view["version"], "1.0.1")
        self.assertEqual(view["uptime_hours"], 1.0)
        self.assertEqual(view["rss_mb"], 100.0)
        self.assertIn("sample", view)

    def test_version_falls_back_to_current_link(self):
        view = jar_probe.build_view(self._raw(version="", link_version="1.0.9"))
        self.assertEqual(view["version"], "1.0.9")

    def test_not_found_and_stopped(self):
        self.assertEqual(jar_probe.build_view(
            self._raw(load_state="not-found", active_state="inactive")
        )["status"], "not_deployed")
        self.assertEqual(jar_probe.build_view(
            self._raw(active_state="failed", sub_state="failed")
        )["status"], "stopped")

    def test_no_uptime_when_monotonic_missing(self):
        view = jar_probe.build_view(self._raw(active_enter_mono=0))
        self.assertIsNone(view["uptime_hours"])


class CpuPctTest(unittest.TestCase):
    def test_normal_delta(self):
        prev = {"pid": 1, "cpu_ticks": 100, "ts": 100.0}
        self.assertEqual(jar_probe.compute_cpu_pct(prev, 1, 250, 103.0, 100), 50.0)

    def test_pid_change_or_no_baseline(self):
        self.assertIsNone(jar_probe.compute_cpu_pct(None, 1, 100, 2.0))
        self.assertIsNone(jar_probe.compute_cpu_pct(
            {"pid": 1, "cpu_ticks": 10, "ts": 1.0}, 2, 20, 2.0))
        self.assertIsNone(jar_probe.compute_cpu_pct(
            {"pid": 1, "cpu_ticks": 10, "ts": 1.0}, 1, 5, 2.0))


class AggregateHostsTest(unittest.TestCase):
    def test_status_precedence_and_version(self):
        agg = jar_probe.aggregate_hosts([
            {"host": "b", "status": "stopped", "version": "1.0.0"},
            {"host": "a", "status": "online", "version": "1.0.1"},
        ])
        self.assertEqual([h["host"] for h in agg["hosts"]], ["a", "b"])
        self.assertEqual(agg["status"], "online")
        self.assertEqual(agg["version"], "1.0.1")
        self.assertEqual(agg["online_hosts"], 1)

    def test_empty(self):
        agg = jar_probe.aggregate_hosts([])
        self.assertEqual(agg, {"status": "unknown", "version": "",
                               "hosts": [], "online_hosts": 0})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_jar_probe.py -q`
Expected: FAIL，`No module named 'webapp.jar_probe'`。

- [ ] **Step 3: 实现 `webapp/jar_probe.py`**

```python
"""jar_service 进程观测纯函数：探测命令、输出解析、CPU 差分与主机聚合

不触碰 SSH/Flask：监控调度器负责执行命令，本模块只把文本变成结构化视图，
便于在无服务器环境下完整单测。
"""
import secrets
import shlex
import time

# systemctl show 的固定属性集；--value 会丢属性名，这里保留 Key=Value 便于解析
UNIT_PROPS = ("LoadState", "ActiveState", "SubState", "MainPID",
              "NRestarts", "ActiveEnterTimestampMonotonic")

_STATUS_RANK = {"online": 4, "probe_failed": 3, "stopped": 2,
                "not_deployed": 1, "unknown": 0}


def build_probe_cmd(units: dict, token: str = None) -> tuple:
    """构造单通道批量单元探测命令，返回 (cmd, token)

    units: {unit: app_home} 或 {unit: {"app_home": ...}}。
    每个单元输出：systemctl 属性、.deploy_version、CURRENT 链接、进程 stat。
    """
    token = token or secrets.token_hex(4)
    parts = ['echo "UPTIME:$(cat /proc/uptime 2>/dev/null)"',
             'echo "CLK:$(getconf CLK_TCK 2>/dev/null || echo 100)"']
    for idx, (unit, info) in enumerate(units.items()):
        app_home = info if isinstance(info, str) else (info or {}).get("app_home", "")
        u = shlex.quote(unit)
        props = " ".join(f"-p {p}" for p in UNIT_PROPS)
        parts.append(f"echo '@@JARUNIT:{token}:{idx}'")
        parts.append(f"systemctl show {props} {u} 2>/dev/null || true")
        if app_home:
            h = shlex.quote(str(app_home).rstrip("/"))
            parts.append(f"echo \"VER:$(cat {h}/.deploy_version 2>/dev/null | head -n1)\"")
            parts.append(f"echo \"LINK:$(readlink {h}/CURRENT 2>/dev/null)\"")
        else:
            parts.append('echo "VER:"')
            parts.append('echo "LINK:"')
        parts.append(
            f"P=$(systemctl show -p MainPID --value {u} 2>/dev/null); "
            f"if [ -n \"$P\" ] && [ \"$P\" != 0 ]; then "
            f"echo \"STAT:$(cat /proc/$P/stat 2>/dev/null)\"; fi")
    return "; ".join(parts), token


def _empty_unit() -> dict:
    return {"load_state": "not-found", "active_state": "inactive",
            "sub_state": "dead", "pid": 0, "restarts": 0,
            "active_enter_mono": 0.0, "version": "", "link_version": "",
            "cpu_ticks": 0, "rss_pages": 0}


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_stat(text: str) -> dict:
    """/proc/<pid>/stat → CPU 累计 tick 与 RSS 页数（comm 可能含空格）"""
    rest = text.rsplit(")", 1)[-1].split()
    try:
        return {"cpu_ticks": int(rest[11]) + int(rest[12]),
                "rss_pages": int(rest[21])}
    except (IndexError, ValueError):
        return {}


def parse_probe_output(out: str, token: str, expected_units=()) -> dict:
    """把 build_probe_cmd 的输出解析为 {uptime, clk_tck, units}"""
    result = {"uptime": 0.0, "clk_tck": 100, "units": {}}
    prefix = f"@@JARUNIT:{token}:"
    index_to_unit = {str(i): u for i, u in enumerate(expected_units)}
    current = None
    for raw in (out or "").splitlines():
        line = raw.strip()
        if line.startswith(prefix):
            current = index_to_unit.get(line[len(prefix):])
            if current:
                result["units"][current] = _empty_unit()
            continue
        if line.startswith("UPTIME:"):
            try:
                result["uptime"] = float(line[7:].split()[0])
            except (ValueError, IndexError):
                pass
            continue
        if line.startswith("CLK:"):
            result["clk_tck"] = max(1, _to_int(line[4:].strip(), 100))
            continue
        if not current:
            continue
        unit = result["units"][current]
        if line.startswith("STAT:"):
            unit.update(_parse_stat(line[5:]))
        elif line.startswith("VER:"):
            unit["version"] = line[4:].strip()
        elif line.startswith("LINK:"):
            link = line[5:].strip()
            if link:
                unit["link_version"] = link.rstrip("/").rsplit("/", 1)[-1]
        elif "=" in line:
            key, value = line.split("=", 1)
            key = key.strip()
            if key == "LoadState":
                unit["load_state"] = value.strip()
            elif key == "ActiveState":
                unit["active_state"] = value.strip()
            elif key == "SubState":
                unit["sub_state"] = value.strip()
            elif key == "MainPID":
                unit["pid"] = _to_int(value, 0)
            elif key == "NRestarts":
                unit["restarts"] = _to_int(value, 0)
            elif key == "ActiveEnterTimestampMonotonic":
                unit["active_enter_mono"] = _to_float(value, 0.0)
    for unit in expected_units:
        result["units"].setdefault(unit, _empty_unit())
    return result


def compute_cpu_pct(prev, pid, cpu_ticks, now, clk_tck: int = 100):
    """跨轮 CPU 使用率：pid 变化或计数回绕返回 None，避免误报"""
    if not prev or not pid or cpu_ticks is None:
        return None
    if prev.get("pid") != pid:
        return None
    dt = float(now) - float(prev.get("ts") or 0)
    delta = int(cpu_ticks) - int(prev.get("cpu_ticks") or 0)
    if dt <= 0 or delta < 0:
        return None
    return round(delta / max(1, int(clk_tck)) / dt * 100, 1)


def build_view(raw: dict, prev: dict = None, now: float = None,
               proc_uptime: float = 0.0, clk_tck: int = 100) -> dict:
    """单单元原始输出 → 展示视图（cpu 需要上一轮样本，sample 供调度器保存）"""
    now = time.time() if now is None else now
    raw = raw or {}
    load_state = (raw.get("load_state") or "").strip().lower()
    active_state = (raw.get("active_state") or "").strip().lower()
    if not load_state or load_state in ("not-found", "masked"):
        status_value = "not_deployed"
    elif active_state == "active":
        status_value = "online"
    else:
        status_value = "stopped"
    version = (raw.get("version") or "").strip() or (raw.get("link_version") or "")
    mono_sec = _to_float(raw.get("active_enter_mono"), 0.0) / 1_000_000
    uptime_hours = None
    if (status_value == "online" and proc_uptime > 0 and mono_sec > 0
            and proc_uptime >= mono_sec):
        uptime_hours = round((proc_uptime - mono_sec) / 3600, 2)
    pid = _to_int(raw.get("pid"), 0)
    cpu_ticks = _to_int(raw.get("cpu_ticks"), 0)
    rss_pages = _to_int(raw.get("rss_pages"), 0)
    return {
        "status": status_value,
        "version": version,
        "pid": pid,
        "uptime_hours": uptime_hours,
        "restarts": _to_int(raw.get("restarts"), 0),
        "cpu": compute_cpu_pct(prev, pid, cpu_ticks, now, clk_tck),
        "rss_mb": round(rss_pages * 4096 / 1024 / 1024, 1) if rss_pages else None,
        "sub_state": (raw.get("sub_state") or "").strip(),
        "sample": {"pid": pid, "cpu_ticks": cpu_ticks, "ts": now},
    }


def aggregate_hosts(hosts: list) -> dict:
    """每主机视图 → 服务级聚合：状态取最高优先级，版本取首个在线主机"""
    rows = sorted(hosts or [], key=lambda h: h.get("host", ""))
    best = "unknown"
    for row in rows:
        status_value = row.get("status") or "unknown"
        if _STATUS_RANK.get(status_value, 0) > _STATUS_RANK.get(best, 0):
            best = status_value
    version = next((h.get("version") for h in rows
                    if h.get("status") == "online" and h.get("version")), "")
    if not version:
        version = next((h.get("version") for h in rows if h.get("version")), "")
    return {"status": best, "version": version, "hosts": rows,
            "online_hosts": sum(1 for h in rows
                                if h.get("status") == "online")}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_jar_probe.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add webapp/jar_probe.py tests/test_jar_probe.py
git commit -m "feat(jar): add pure probe parser and per-host aggregation"
```

---

### Task 3: 监控主机批探测追加单元状态

**Files:**
- Modify: `webapp/monitor.py:220-300`（`_probe_server_full`）
- Modify: `webapp/monitor.py:809-853`（`_probe_envs` / `_probe_hosts`）
- Modify: `tests/test_monitor_perf.py:203-204`（fake 签名）
- Test: `tests/test_monitor_host_jar.py`（新建）

**Interfaces:**
- Consumes: `jar_probe.build_probe_cmd()`, `jar_probe.parse_probe_output()`
- Produces:
  - `_probe_server_full(..., units: dict = None)`，结果新增 `jar_probe` 键（`{"uptime", "clk_tck", "units"}`）
  - `_probe_envs` 的 host job 新增 `"jar_units": {unit: {"app_home": str}}`

- [ ] **Step 1: 调整既有 fake 签名并写新测试**

`tests/test_monitor_perf.py` 的 fake：

```python
        def fake_probe(host, port, ssh_cfg, service_names, timeout=10,
                       skip_extended=False, units=None):
            calls.append({"host": host, "service_names": set(service_names),
                          "skip_extended": skip_extended, "units": units})
```

新建 `tests/test_monitor_host_jar.py`：

```python
"""监控主机探测：jar 单元命令并入同一 SSH 批次 + 环境作业收集单元"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import db as dbm
from webapp.monitor import MonitorScheduler, _probe_server_full


class ProbeServerFullUnitsTest(unittest.TestCase):
    def test_units_use_third_command(self):
        calls = []

        def fake_multi(host, port, ssh_cfg, cmds, timeout=10):
            calls.append(list(cmds))
            return ["svc|Up 1 hour|img\n@@STATS@@\n", "cpu 1 2 3 4\n", (
                "UPTIME:100.00 0.00\nCLK:100\n"
                "@@JARUNIT:tok:0\n"
                "LoadState=loaded\nActiveState=active\nSubState=running\n"
                "MainPID=42\nNRestarts=0\nActiveEnterTimestampMonotonic=0\n"
                "VER:1.0.0\nLINK:/opt/auth/releases/1.0.0\n")]

        with mock.patch("webapp.monitor._ssh_exec_multi", side_effect=fake_multi):
            res = _probe_server_full("1.2.3.4", 22, {}, set(),
                                     units={"auth": {"app_home": "/opt/auth"}})
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), 3)
        self.assertIn("systemctl show", calls[0][2])
        self.assertEqual(res["jar_probe"]["units"]["auth"]["version"], "1.0.0")
        self.assertEqual(res["jar_probe"]["units"]["auth"]["pid"], 42)

    def test_no_units_keeps_two_commands(self):
        calls = []

        def fake_multi(host, port, ssh_cfg, cmds, timeout=10):
            calls.append(list(cmds))
            return ["", "cpu 1 2 3 4\n"]

        with mock.patch("webapp.monitor._ssh_exec_multi", side_effect=fake_multi):
            res = _probe_server_full("1.2.3.4", 22, {}, set())
        self.assertEqual(len(calls[0]), 2)
        self.assertEqual(res["jar_probe"], {})


class EnvJobsCollectUnitsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jar_host_")
        self.db = os.path.join(self.tmp, "t.db")
        dbm.init_db(self.db)
        self.pid = dbm.create_record(self.db, "projects", dict(
            name="p", code="p", description="", git_org="", created_at=0))
        self.env = dbm.create_record(self.db, "environments", dict(
            project_id=self.pid, name="prod", is_default=1, registry="{}",
            nacos="{}", mysql_snailjob="{}", ssh="{}", build="{}", deploy="{}",
            monitor_enabled=1))
        dbm.create_record(self.db, "services", dict(
            project_id=self.pid, name="auth", image_name="", service_name="",
            deploy_mode="jar_service",
            runtime_config={"app_home": "/opt/auth", "port": 8080},
            docker_context="", dockerfile="", build_order="[]",
            nacos_groups="[]", dubbo_services="[]", snailjob_group="",
            health_url="", health_port=0, log_error_keywords="[]",
            clear_log_dir="", git_repo="", enabled=1))
        srv = dbm.create_record(self.db, "servers", dict(
            host="1.2.3.4", ssh_port=22, ssh_user="root", ssh_password="pw",
            ssh_key_file="", nacos_ip="", remark="", enabled=1))
        dbm.bind_server(self.db, self.env, srv)
        self.seen = {}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_jar_units_go_to_host_job(self):
        def fake_probe(host, port, ssh_cfg, service_names, timeout=10,
                       skip_extended=False, units=None):
            self.seen["units"] = units
            return {"ssh_ok": False, "containers": {}, "containers_stopped": {},
                    "container_list": [], "container_status": {},
                    "resources": {}, "host_metrics": {}, "extended": {},
                    "jar_probe": {}, "error": "down"}

        sched = MonitorScheduler(self.db, 30)
        with mock.patch("webapp.monitor._probe_server_full",
                        side_effect=fake_probe):
            sched._cycle()
        self.assertEqual(self.seen["units"], {"auth": {"app_home": "/opt/auth"}})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_monitor_host_jar.py tests/test_monitor_perf.py -q`
Expected: FAIL（`units` 不被接受 / `jar_probe` 缺失）。

- [ ] **Step 3: 修改 `webapp/monitor.py`**

文件顶部新增导入：

```python
from .jar_probe import build_probe_cmd, parse_probe_output
```

`_probe_server_full` 签名与函数体：

```python
def _probe_server_full(host: str, port: int, ssh_cfg: dict, service_names: set,
                       timeout: int = 10, skip_extended: bool = False,
                       units: dict = None) -> dict:
    """单连接探测整台服务器：容器 + 主机指标 + 扩展指标 + jar/systemd 单元

    units: {unit: {"app_home": ...}}，非空时在第一批命令末尾追加单元探测，
    结果写入 result["jar_probe"]（uptime/clk_tck/units）。
    """
```

结果初始化加 `"jar_probe": {}`：

```python
    result = {"ssh_ok": False, "containers": {}, "container_list": [],
              "containers_stopped": {}, "container_status": {}, "resources": {},
              "host_metrics": {}, "extended": {}, "jar_probe": {}, "error": ""}
```

第一轮命令改为：

```python
        unit_cmd, unit_token = (build_probe_cmd(units) if units else (None, None))
        cmds = [base_cmd, host_cmd] + ([unit_cmd] if unit_cmd else [])
        outs = _ssh_exec_multi(host, port, ssh_cfg, cmds, timeout=timeout)
        base_out, hm_out = outs[0], outs[1]
        if unit_cmd:
            result["jar_probe"] = parse_probe_output(
                outs[2] if len(outs) > 2 else "", unit_token, list(units))
```

（删除原来的 `base_out, hm_out = _ssh_exec_multi(...)` 一行。）

`_probe_envs` 的 host job 初始化：

```python
                    job = {"host": s["host"], "port": key[1],
                           "ssh_cfg": _merge_server_ssh(env_ssh, s),
                           "service_names": set(), "jar_units": {}}
                    host_jobs[key] = job
                job["service_names"].update(svc_names)
```

在「服务名并集」处只收集容器服务，并追加 jar 单元：

```python
            svc_names = [svc["service_name"] for svc in services
                         if svc.get("service_name")
                         and (svc.get("deploy_mode") or "container") == "container"]
```

在 `_probe_envs` 的环境循环前加载 jar 服务：

```python
        from deploy_strategies.jar_config import (derive_unit_name,
                                                  parse_runtime_config)
        jar_by_project = {}
        for proj, env in env_tasks:
            for svc in dbm.list_services(self._db_path, proj["id"],
                                          enabled_only=True):
                if (svc.get("deploy_mode") or "container") != "jar_service":
                    continue
                cfg = parse_runtime_config(svc.get("runtime_config"))
                unit = derive_unit_name(cfg, svc.get("image_name"), svc["name"])
                jar_by_project.setdefault(proj["id"], []).append(
                    {"unit": unit, "app_home": cfg["app_home"]})
```

为避免每环境重复查库，先把每个 env 的服务列表取到 `env_ctxs`，再统一构建 `jar_by_project`（实现时按下列顺序：先循环 `env_tasks` 构 `env_ctxs` 与 `jar_by_project`，再循环 `env_ctxs` 构 `host_jobs`）：

```python
        for ctx in env_ctxs:
            for s in ctx["servers"]:
                key = (s["host"], int(s.get("ssh_port") or 22))
                job = host_jobs[key]
                for info in jar_by_project.get(ctx["proj"]["id"], []):
                    job["jar_units"].setdefault(
                        info["unit"], {"app_home": info["app_home"]})
```

`_probe_hosts` 传参：

```python
            futures = {pool.submit(_probe_server_full, job["host"], job["port"],
                                   job["ssh_cfg"], job["service_names"],
                                   10, skip_extended,
                                   job.get("jar_units") or None): key
                       for key, job in host_jobs.items()}
```

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/test_monitor_host_jar.py tests/test_monitor_perf.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add webapp/monitor.py tests/test_monitor_host_jar.py tests/test_monitor_perf.py
git commit -m "feat(monitor): probe systemd units in the host SSH batch"
```

---

### Task 4: 环境级 jar 聚合、健康检查与版本写穿

**Files:**
- Modify: `webapp/monitor.py:754-763`（`__init__` 状态）
- Modify: `webapp/monitor.py:951-953`（容器循环跳过 jar）
- Modify: `webapp/monitor.py:978` 之后（新增 `_merge_jar_services`）
- Modify: `webapp/monitor.py:1009-1035`（Nacos 回退、健康端口回退）
- Test: `tests/test_monitor_jar.py`（新建）

**Interfaces:**
- Consumes: `jar_probe.build_view()`, `jar_probe.aggregate_hosts()`, `dbm.record_host_state_live()`
- Produces: `result["services"][name]` 新增 `hosts`、`online_hosts`、`unit`、`health_port` 字段

- [ ] **Step 1: 写失败测试**

新建 `tests/test_monitor_jar.py`：

```python
"""监控环境级 jar_service：进程视图、版本写穿、健康检查端口回退"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import db as dbm
from webapp.monitor import MonitorScheduler


def _raw(active="active", pid=4242, cpu_ticks=1000, version="1.0.1"):
    return {"load_state": "loaded" if active != "not-found" else "not-found",
            "active_state": active, "sub_state": "running" if active == "active" else "dead",
            "pid": pid if active == "active" else 0, "restarts": 1,
            "active_enter_mono": 9600_000_000.0, "version": version,
            "link_version": "", "cpu_ticks": cpu_ticks, "rss_pages": 25600}


class MonitorJarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mon_jar_")
        self.db = os.path.join(self.tmp, "t.db")
        dbm.init_db(self.db)
        self.pid = dbm.create_record(self.db, "projects", dict(
            name="p", code="p", description="", git_org="", created_at=0))
        self.env = dbm.create_record(self.db, "environments", dict(
            project_id=self.pid, name="prod", is_default=1, registry="{}",
            nacos="{}", mysql_snailjob="{}", ssh="{}", build="{}", deploy="{}",
            monitor_enabled=1))
        dbm.create_record(self.db, "services", dict(
            project_id=self.pid, name="auth", image_name="", service_name="",
            deploy_mode="jar_service",
            runtime_config={"app_home": "/opt/auth", "port": 8080},
            docker_context="", dockerfile="", build_order="[]",
            nacos_groups="[]", dubbo_services="[]", snailjob_group="",
            health_url="", health_port=0, log_error_keywords="[]",
            clear_log_dir="", git_repo="", enabled=1))
        srv = dbm.create_record(self.db, "servers", dict(
            host="1.2.3.4", ssh_port=22, ssh_user="root", ssh_password="pw",
            ssh_key_file="", nacos_ip="", remark="", enabled=1))
        dbm.bind_server(self.db, self.env, srv)
        self.cpu_ticks = 1000
        self.active = "active"
        self.version = "1.0.1"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_probe(self, host, port, ssh_cfg, service_names, timeout=10,
                    skip_extended=False, units=None):
        return {"ssh_ok": True, "containers": {}, "containers_stopped": {},
                "container_list": [], "container_status": {}, "resources": {},
                "host_metrics": {"cpu": 10.0, "mem": 20.0, "disk": 30.0, "load": 1.0},
                "extended": {}, "error": "",
                "jar_probe": {"uptime": 10000.0, "clk_tck": 100,
                              "units": {"auth": _raw(self.active, cpu_ticks=self.cpu_ticks,
                                                     version=self.version)}}}

    def test_online_view_and_single_write(self):
        sched = MonitorScheduler(self.db, 30)
        with mock.patch("webapp.monitor._probe_server_full",
                        side_effect=self._fake_probe), \
                mock.patch("webapp.monitor._probe_health_via_ssh", return_value=[]), \
                mock.patch("webapp.db.record_host_state_live") as live:
            sched._cycle()
            ov = sched.get_overview(self.pid, "prod")
            st = ov["services"]["auth"]
            self.assertEqual(st["status"], "online")
            self.assertEqual(st["version"], "1.0.1")
            self.assertEqual(st["unit"], "auth")
            self.assertEqual(st["online_hosts"], 1)
            self.assertEqual(len(st["hosts"]), 1)
            self.assertEqual(st["hosts"][0]["pid"], 4242)
            self.assertEqual(st["hosts"][0]["rss_mb"], 100.0)
            self.assertEqual(st["hosts"][0]["uptime_hours"], 0.11)
            self.assertEqual(st["instances"], 1)  # Nacos 不可用时回退在线主机数
            live.assert_called_once()
            args = live.call_args[0]
            self.assertEqual(args[1:], (self.pid, "prod", "auth", "1.2.3.4", "1.0.1"))

            # 第二轮版本不变 → 不再写库
            sched._cycle()
            self.assertEqual(live.call_count, 1)

    def test_second_round_computes_cpu(self):
        sched = MonitorScheduler(self.db, 30)
        with mock.patch("webapp.monitor._probe_server_full",
                        side_effect=self._fake_probe), \
                mock.patch("webapp.monitor._probe_health_via_ssh", return_value=[]):
            sched._cycle()
            self.cpu_ticks = 3000
            sched._cycle()
        host = sched.get_overview(self.pid, "prod")["services"]["auth"]["hosts"][0]
        self.assertIsNotNone(host["cpu"])
        self.assertGreater(host["cpu"], 0)

    def test_stopped_version_still_written(self):
        self.active = "failed"
        sched = MonitorScheduler(self.db, 30)
        with mock.patch("webapp.monitor._probe_server_full",
                        side_effect=self._fake_probe), \
                mock.patch("webapp.monitor._probe_health_via_ssh", return_value=[]), \
                mock.patch("webapp.db.record_host_state_live") as live:
            sched._cycle()
        self.assertEqual(sched.get_overview(self.pid, "prod")["services"]["auth"]["status"],
                         "stopped")
        live.assert_called_once()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_monitor_jar.py -q`
Expected: FAIL（服务状态仍为 `not_deployed`，无 `hosts` 字段，写穿未调用）。

- [ ] **Step 3: 实现**

`MonitorScheduler.__init__` 追加：

```python
        self._jar_prev = {}      # {(host, unit): {pid, cpu_ticks, ts}} CPU 差分基准
        self._host_versions = {}  # {(project_id, env, service, host): version} 写穿去重
```

容器分支跳过 jar：

```python
            for svc in services:
                if (svc.get("deploy_mode") or "container") in ("static_archive",
                                                               "jar_service"):
                    continue  # 静态托管/纯 Java 进程没有容器，走各自的探测分支
```

在 `result["extended_metrics"] = per_server_extended` 之后插入：

```python
        jar_services = [svc for svc in services
                        if (svc.get("deploy_mode") or "container") == "jar_service"]
        if jar_services:
            self._merge_jar_services(env, jar_services, servers, host_results,
                                     result, health_hosts)
```

新增方法（放在 `_probe_env` 之前）：

```python
    def _merge_jar_services(self, env: dict, jar_services: list, servers: list,
                            host_results: dict, result: dict,
                            health_hosts: dict):
        """jar_service：每主机 systemd 进程视图 → 服务聚合 + 版本写穿

        版本权威在目标机（.deploy_version / CURRENT）；只有版本变化才写
        service_host_state，避免 30s 轮询写放大。CPU 需要上一轮样本，
        首次探测为 None，第二轮起才有值。
        """
        from deploy_strategies.jar_config import (derive_unit_name,
                                                  parse_runtime_config)
        from . import jar_probe
        for svc in jar_services:
            cfg = parse_runtime_config(svc.get("runtime_config"))
            unit = derive_unit_name(cfg, svc.get("image_name"), svc["name"])
            rows = []
            for s in servers:
                key = (s["host"], int(s.get("ssh_port") or 22))
                r = host_results.get(key) or {}
                probe = r.get("jar_probe") or {}
                raw = (probe.get("units") or {}).get(unit)
                if not r.get("ssh_ok"):
                    rows.append({"host": s["host"], "status": "probe_failed",
                                 "version": "", "pid": 0, "uptime_hours": None,
                                 "restarts": 0, "cpu": None, "rss_mb": None,
                                 "unit": unit})
                    continue
                if not raw:
                    rows.append({"host": s["host"], "status": "not_deployed",
                                 "version": "", "pid": 0, "uptime_hours": None,
                                 "restarts": 0, "cpu": None, "rss_mb": None,
                                 "unit": unit})
                    continue
                prev_key = (s["host"], unit)
                with self._lock:
                    prev = self._jar_prev.get(prev_key)
                view = jar_probe.build_view(
                    raw, prev=prev,
                    proc_uptime=probe.get("uptime") or 0.0,
                    clk_tck=probe.get("clk_tck") or 100)
                sample = view.pop("sample", None)
                if sample:
                    with self._lock:
                        self._jar_prev[prev_key] = sample
                view["host"] = s["host"]
                rows.append(view)
                if view["status"] == "online":
                    health_hosts.setdefault(svc["name"], []).append(s["host"])
                version = view.get("version") or ""
                if version and view["status"] in ("online", "stopped"):
                    cache_key = (env.get("project_id"), env["name"],
                                 svc["name"], s["host"])
                    with self._lock:
                        changed = self._host_versions.get(cache_key) != version
                        self._host_versions[cache_key] = version
                    if changed:
                        try:
                            dbm.record_host_state_live(
                                self._db_path, env.get("project_id") or 0,
                                env["name"], svc["name"], s["host"], version)
                        except Exception as e:
                            print(f"[monitor] jar 版本写穿失败({s['host']}): {e!r}",
                                  file=sys.stderr)
            agg = jar_probe.aggregate_hosts(rows)
            st = result["services"].setdefault(svc["name"], {"ts": time.time()})
            st.update({"status": agg["status"], "version": agg["version"],
                       "instances": -1, "ts": time.time(), "hosts": rows,
                       "online_hosts": agg["online_hosts"], "unit": unit,
                       "health_port": cfg["port"]})
            online_uptime = next(
                (h["uptime_hours"] for h in rows
                 if h["status"] == "online" and h.get("uptime_hours") is not None),
                None)
            if online_uptime is not None:
                st["uptime_hours"] = online_uptime
```

Nacos 循环（约 1009-1019 行）替换为：

```python
        if online:
            with ThreadPoolExecutor(max_workers=min(8, len(online))) as pool:
                futs = {pool.submit(_probe_nacos_instances, env,
                                    s.get("service_name") or s["name"],
                                    s.get("nacos_groups") or []): s
                        for s in online}
                for fut, s in futs.items():
                    try:
                        inst = fut.result(timeout=20)
                    except Exception:
                        inst = -1
                    if (inst < 0 and (s.get("deploy_mode") or "") == "jar_service"):
                        inst = (result["services"].get(s["name"]) or {}).get(
                            "online_hosts", 0)
                    result["services"][s["name"]]["instances"] = inst
```

健康检查端口（约 1029-1032 行）替换为：

```python
            for svc in online:
                hp = svc.get("health_port") or 0
                if hp <= 0:
                    # jar_service 的健康端口来自 runtime_config.port（聚合时写入视图）
                    hp = (result["services"].get(svc["name"]) or {}).get("health_port", 0) or 0
                hu = (svc.get("health_url") or "").strip()
                if hp <= 0:
                    continue
```

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/test_monitor_jar.py tests/test_monitor_host_jar.py tests/test_monitor_perf.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add webapp/monitor.py tests/test_monitor_jar.py
git commit -m "feat(monitor): aggregate jar process views and write through versions"
```

---

### Task 5: journalctl 纯函数

**Files:**
- Modify: `webapp/log_collector.py:24-100`（`_parse_ts` 附近新增函数）
- Test: `tests/test_log_journal.py`（新建）

**Interfaces:**
- Produces:
  - `_journal_since(cursor: str, now: float = None) -> tuple[str, int]`
  - `_build_journal_cmd(unit: str, since_arg: str, max_lines: int) -> str`
  - `_journal_lines(raw: str, min_us: int = 0) -> list[str]`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_log_journal.py`：

```python
"""journalctl 采集纯函数：游标、命令构造、JSON → 时间戳前缀行"""
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp.log_collector import (_build_journal_cmd, _journal_lines,
                                  _journal_since)


class JournalSinceTest(unittest.TestCase):
    def test_iso_cursor_is_converted_to_epoch(self):
        cursor = "2026-08-18T10:30:01.123457+00:00"
        since, min_us = _journal_since(cursor)
        expected_us = int(datetime.fromisoformat(cursor).timestamp() * 1_000_000)
        self.assertEqual(min_us, expected_us)
        self.assertEqual(since, f"@{expected_us // 1_000_000 - 1}")

    def test_invalid_cursor_falls_back_to_five_minutes(self):
        since, min_us = _journal_since("not-a-time", now=1000)
        self.assertEqual((since, min_us), ("@700", 0))

    def test_empty_cursor_uses_server_side_now(self):
        since, min_us = _journal_since("", now=1000)
        self.assertEqual((since, min_us), ("@700", 0))


class JournalCmdTest(unittest.TestCase):
    def test_command_quotes_unit_and_limits_entries(self):
        cmd = _build_journal_cmd("hqb-auth", "@123", 500)
        self.assertIn("journalctl -u hqb-auth", cmd)
        self.assertIn("--since='@123'", cmd)
        self.assertIn("-o json", cmd)
        self.assertIn("--no-pager", cmd)
        self.assertIn("-n 500", cmd)

    def test_malicious_unit_is_quoted(self):
        cmd = _build_journal_cmd("a;rm -rf /", "@1", 1)
        self.assertIn("'a;rm -rf /'", cmd)


class JournalLinesTest(unittest.TestCase):
    def _entry(self, ts_us, msg):
        return json.dumps({"__REALTIME_TIMESTAMP": str(ts_us), "MESSAGE": msg})

    def test_filters_at_cursor_and_formats(self):
        raw = "\n".join([
            self._entry(1787049001123456, "old"),
            self._entry(1787049002123456, "new"),
        ])
        lines = _journal_lines(raw, min_us=1787049001123457)
        self.assertEqual(lines, ["2026-08-18T10:30:02.123456Z new\n"])

    def test_multiline_message_gets_prefix_per_line(self):
        raw = self._entry(1787049002123456, "line one\nline two")
        lines = _journal_lines(raw)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("Z line one\n"))
        self.assertTrue(lines[1].endswith("Z line two\n"))

    def test_byte_array_and_bad_json(self):
        raw = "\n".join([
            json.dumps({"__REALTIME_TIMESTAMP": 1787049002123456,
                        "MESSAGE": [104, 105]}),
            "{not json",
            json.dumps({"MESSAGE": "no timestamp"}),
            json.dumps({"__REALTIME_TIMESTAMP": 1787049003123456,
                        "MESSAGE": ""}),
        ])
        self.assertEqual(_journal_lines(raw),
                         ["2026-08-18T10:30:02.123456Z hi\n"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_log_journal.py -q`
Expected: FAIL（导入不存在）。

- [ ] **Step 3: 实现**

在 `webapp/log_collector.py` 的 `_build_log_file_path` 之后追加：

```python
_JOURNAL_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ "


def _journal_since(cursor: str, now: float = None) -> tuple:
    """游标 ISO → (journalctl --since 参数, 客户端严格过滤阈值 µs)

    journalctl 不接受带 T/Z 的 ISO 8601，统一用 @epoch；--since 含边界，
    since 取游标秒数 -1，真正的去重由 min_us（严格 >）完成。
    """
    if cursor:
        try:
            dt = datetime.fromisoformat(cursor)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            min_us = int(dt.timestamp() * 1_000_000)
            return f"@{max(0, min_us // 1_000_000 - 1)}", min_us
        except (TypeError, ValueError):
            pass
    base = int(time.time() if now is None else now)
    return f"@{max(0, base - 300)}", 0


def _build_journal_cmd(unit: str, since_arg: str, max_lines: int) -> str:
    """单单元增量拉取；-n 限制条数（多行消息展开后由调用方全量入库）"""
    return (f"journalctl -u {shlex.quote(unit)} "
            f"--since={shlex.quote(since_arg)} -o json --no-pager "
            f"-n {int(max_lines)} 2>/dev/null")


def _journal_lines(raw: str, min_us: int = 0) -> list:
    """journalctl -o json → 带时间戳前缀的日志行（复用 docker -t 解析管线）

    MESSAGE 可能是 str/整数/字节数组；多行消息逐行加同一前缀，保证
    _parse_ts 对每行都能解析出时间。min_us 严格过滤游标及更早条目。
    """
    out = []
    for raw_line in (raw or "").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except (TypeError, ValueError):
            continue
        try:
            ts_us = int(entry.get("__REALTIME_TIMESTAMP") or 0)
        except (TypeError, ValueError):
            ts_us = 0
        if ts_us <= 0 or ts_us <= min_us:
            continue
        msg = entry.get("MESSAGE", "")
        if isinstance(msg, list):
            try:
                msg = bytes(msg).decode("utf-8", "replace")
            except (TypeError, ValueError):
                msg = str(msg)
        elif not isinstance(msg, str):
            msg = str(msg)
        if not msg:
            continue
        prefix = datetime.fromtimestamp(ts_us / 1_000_000,
                                        tz=timezone.utc).strftime(_JOURNAL_TS_FMT)
        for part in msg.splitlines():
            out.append(prefix + part + "\n")
    return out
```

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/test_log_journal.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add webapp/log_collector.py tests/test_log_journal.py
git commit -m "feat(log): add journalctl cursor and JSON line helpers"
```

---

### Task 6: 日志采集器接入 journalctl

**Files:**
- Modify: `webapp/log_collector.py:157-252`（`_collect_round`）
- Modify: `webapp/log_collector.py:243-398`（`_collect_host`）
- Modify: `webapp/log_collector.py:396-437`（`_collect_container_output`）
- Modify: `webapp/routes/log_routes.py:181-200`（`/logs/services`）
- Modify: `webapp/routes/log_routes.py:226-233`（`/logs/tail` 映射）
- Test: `tests/test_log_journal.py`（追加采集集成用例）、`tests/test_log_routes.py`（追加 jar 映射用例）

**Interfaces:**
- Consumes: `_journal_since()`, `_build_journal_cmd()`, `_journal_lines()`
- Produces:
  - `_collect_container_output(..., service_key=None, scan_key=None)`（关键字参数，旧调用不变）
  - `log_entries.service = service_key`，`log_scan_state.container = scan_key`，日志文件目录仍为位置参数 `container`

- [ ] **Step 1: 写失败测试**

在 `tests/test_log_journal.py` 追加：

```python
import os
import shutil
import tempfile
import time
from datetime import timezone
from unittest import mock

from webapp import db as dbm
from webapp.log_collector import LogCollector


class CollectorJournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="log_journal_")
        self.db = os.path.join(self.tmp, "t.db")
        dbm.init_db(self.db)
        self.log_dir = os.path.join(self.tmp, "logs")
        pid = dbm.create_record(self.db, "projects", dict(
            name="p", code="p", description="", git_org="", created_at=0))
        env = dbm.create_record(self.db, "environments", dict(
            project_id=pid, name="prod", is_default=1, registry="{}",
            nacos="{}", mysql_snailjob="{}", ssh='{"user":"root","password":"pw"}',
            build="{}", deploy="{}", monitor_enabled=1))
        dbm.create_record(self.db, "services", dict(
            project_id=pid, name="auth", image_name="", service_name="",
            deploy_mode="jar_service",
            runtime_config={"app_home": "/opt/auth", "port": 8080},
            docker_context="", dockerfile="", build_order="[]",
            nacos_groups="[]", dubbo_services="[]", snailjob_group="",
            health_url="", health_port=0, log_error_keywords="[]",
            clear_log_dir="", git_repo="", enabled=1))
        srv = dbm.create_record(self.db, "servers", dict(
            host="9.9.9.9", ssh_port=22, ssh_user="root", ssh_password="pw",
            ssh_key_file="", nacos_ip="", remark="", enabled=1))
        dbm.bind_server(self.db, env, srv)
        self.ssh_calls = []

        class _FakeMonitor:
            _server_containers = {
                "9.9.9.9": {"containers": [], "ts": time.time()}}

        self.output = "\n".join([
            "@@LOGCUT:deadbeef:journal:auth",
            json.dumps({"__REALTIME_TIMESTAMP": "1787049001123456",
                        "MESSAGE": "Started auth"}),
            json.dumps({"__REALTIME_TIMESTAMP": "1787049002123456",
                        "MESSAGE": "line one\nline two"}),
        ]) + "\n"
        self.collector = LogCollector(self.db, self.log_dir, interval=30,
                                      ssh_func=self._fake_ssh,
                                      monitor=_FakeMonitor())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_ssh(self, host, port, ssh_cfg, cmd, timeout=10):
        self.ssh_calls.append(cmd)
        return self.output

    def test_journal_batch_is_collected_and_indexed_once(self):
        with mock.patch("webapp.log_collector.secrets.token_hex",
                        return_value="deadbeef"):
            self.collector._collect_round()
        self.assertEqual(len(self.ssh_calls), 1)
        cmd = self.ssh_calls[0]
        self.assertIn("journalctl -u auth", cmd)
        self.assertIn("--since='@", cmd)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = os.path.join(self.log_dir, "collect", "prod", "auth",
                            f"{today}.log")
        content = Path(path).read_text(encoding="utf-8")
        self.assertIn("Started auth", content)
        self.assertIn("line one", content)
        conn = dbm.get_conn(self.db)
        try:
            rows = conn.execute(
                "SELECT service, content FROM log_entries ORDER BY id").fetchall()
            st = conn.execute(
                "SELECT container, service, last_ts FROM log_scan_state"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([r["service"] for r in rows], ["auth"] * 3)
        self.assertEqual(len(st), 1)
        self.assertEqual(st[0]["container"], "systemd:auth")
        self.assertEqual(st[0]["service"], "auth")
        self.assertEqual(st[0]["last_ts"], "2026-08-18T10:30:02.123457+00:00")

        # 第二轮相同输出：游标严格过滤 → 不重复入库
        with mock.patch("webapp.log_collector.secrets.token_hex",
                        return_value="deadbeef"):
            self.collector._collect_round()
        conn = dbm.get_conn(self.db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 3)
```

在 `tests/test_log_routes.py` 的 `TestLogRoutes` 追加：

```python
    def test_tail_falls_back_to_service_name_for_jar(self):
        pid = dbm.create_record(self.db, "projects", dict(
            name="jar-p", code="jarp", description="", git_org="",
            created_at=int(time.time())))
        dbm.create_record(self.db, "services", dict(
            project_id=pid, name="jar-auth", image_name="", service_name="",
            deploy_mode="jar_service", runtime_config={"app_home": "/opt/auth"},
            docker_context="", dockerfile="", build_order="[]",
            nacos_groups="[]", dubbo_services="[]", snailjob_group="",
            health_url="", health_port=0, log_error_keywords="[]",
            clear_log_dir="", git_repo="", enabled=1))
        conn = dbm.get_conn(self.db)
        try:
            conn.execute(
                "INSERT INTO log_entries (ts, env, service, container, file_path, "
                "line_offset, line_count, content, keywords) "
                "VALUES (?, 'prod', 'jar-auth', 'jar-auth', '', 0, 1, 'BOOT OK', '[]')",
                (int(time.time()),))
            conn.commit()
        finally:
            conn.close()
        r = self.client.get("/api/logs/tail?service=jar-auth&lines=10",
                            headers=self._auth_headers())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any("BOOT OK" in x["content"] for x in r.get_json()["rows"]))
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_log_journal.py tests/test_log_routes.py -q`
Expected: FAIL（采集器未发 journalctl 命令；tail 查空 `service_name` 返回空）。

- [ ] **Step 3: 实现采集器**

`_collect_round` 的环境查询加 `e.project_id`：

```python
            envs = conn.execute(
                "SELECT e.id, e.name, e.ssh, e.project_id, p.name as project "
                "FROM environments e JOIN projects p ON e.project_id = p.id "
                "WHERE e.monitor_enabled = 1"
            ).fetchall()
```

在方法开头、`jobs = {}` 之前加载 jar 服务：

```python
        jar_by_project = self._load_jar_services()
```

job 初始化与单元挂载：

```python
                if job is None:
                    job = {"host": srv["host"], "port": key[1],
                           "ssh_cfg": _merge_server_ssh(env_ssh, srv),
                           "envs": [], "jar_units": {}}
                    jobs[key] = job
                job["envs"].append((env_name, env_id))
                for info in jar_by_project.get(env_row["project_id"], []):
                    job["jar_units"].setdefault(
                        info["unit"], {"service": info["service"]})
```

新增方法：

```python
    def _load_jar_services(self) -> dict:
        """项目 → [{unit, service}]；单元名口径与部署/监控共用 derive_unit_name"""
        from deploy_strategies.jar_config import derive_unit_name
        conn = dbm.get_conn(self.db_path)
        try:
            rows = conn.execute(
                "SELECT project_id, name, image_name, service_name, runtime_config "
                "FROM services WHERE enabled=1 AND deploy_mode='jar_service'"
            ).fetchall()
        finally:
            conn.close()
        result = {}
        for r in rows:
            unit = derive_unit_name(r["runtime_config"], r["image_name"], r["name"])
            result.setdefault(r["project_id"], []).append(
                {"unit": unit, "service": r["service_name"] or r["name"]})
        return result
```

`_collect_host` 的改动：

```python
        containers = ...
        jar_units = job.get("jar_units") or {}
        if not containers and not jar_units:
            return
```

在容器 `parts` 构建后追加 journal：

```python
        journal_metas = []
        for unit in sorted(jar_units):
            scan_key = f"systemd:{unit}"
            cursors = [st.get(scan_key, {}).get("last_ts", "")
                       for st in states_by_env.values()]
            valid = [c for c in cursors if c]
            last_ts = min(valid) if valid and len(valid) == len(out_envs) else ""
            since_arg, _ = _journal_since(last_ts)
            parts.append("echo " + shlex.quote(_marker(token, f"journal:{unit}")))
            parts.append(_build_journal_cmd(unit, since_arg, self.max_lines))
            journal_metas.append((unit, last_ts))
```

超时与切分：

```python
        timeout = max(30, min(300, 10 + 4 * (len(metas) + len(journal_metas))))
        ...
        chunks = _split_marker_output(
            out, token,
            [c for c, _ in metas] + [f"journal:{u}" for u, _ in journal_metas])
```

容器分发循环之后追加：

```python
        for unit, _last_ts in journal_metas:
            raw = chunks.get(f"journal:{unit}")
            fail_key = f"systemd:{unit}"
            if raw is None:
                with self._lock:
                    for env_name, _ in out_envs:
                        k = (env_name, host, fail_key)
                        self._fail_counts[k] = self._fail_counts.get(k, 0) + 1
                continue
            with self._lock:
                for env_name, _ in out_envs:
                    self._fail_counts.pop((env_name, host, fail_key), None)
            for env_name, env_id in out_envs:
                st = (states_by_env.get(env_name) or {}).get(fail_key, {})
                _since, min_us = _journal_since(st.get("last_ts") or "")
                lines = _journal_lines(raw, min_us)
                if not lines:
                    continue
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                file_offset = (st.get("file_offset", 0)
                               if st.get("file_date") == today else 0)
                self._collect_container_output(
                    env_name, env_id, host, unit, "".join(lines),
                    st.get("last_ts") or "", file_offset,
                    service_key=(jar_units.get(unit) or {}).get("service") or unit,
                    scan_key=fail_key)
```

`_collect_container_output` 签名与内部键：

```python
    def _collect_container_output(self, env_name: str, env_id: int, host: str,
                                  container: str, out: str, last_ts: str,
                                  file_offset: int = 0, *,
                                  service_key: str = None,
                                  scan_key: str = None):
        """处理单容器/单单元采集输出：解析行 → 归档文件 → FTS → 关键词 → 游标

        container: 文件目录与 log_entries.container（journal 传 unit）；
        service_key: log_entries.service（host 采集传 service_name or name）；
        scan_key: log_scan_state.container（journal 传 systemd:<unit>）。
        """
        ...
        service = service_key or container
        scan_key = scan_key or container
        ...
            self._update_scan_state(env_name, host, scan_key, service,
                                    last_line_ts or "", line_offset, today)
```

`log_routes.py`：

`/logs/services` 查询：

```python
            "SELECT DISTINCT e.name AS env, "
            "CASE WHEN s.service_name != '' THEN s.service_name ELSE s.name END AS service "
            "FROM services s "
            "JOIN environments e ON s.project_id = e.project_id "
            "WHERE s.enabled = 1"
```

`/logs/tail` 映射：

```python
        row = conn.execute(
            "SELECT service_name, name FROM services WHERE name = ? LIMIT 1",
            (service,)
        ).fetchone()
        svc_key = (row["service_name"] or row["name"]) if row else service
```

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/test_log_journal.py tests/test_log_routes.py tests/test_log_collector.py tests/test_monitor_perf.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add webapp/log_collector.py webapp/routes/log_routes.py tests/test_log_journal.py tests/test_log_routes.py
git commit -m "feat(log): collect journalctl output through the existing pipeline"
```

---

### Task 7: 每主机版本只读 API

**Files:**
- Modify: `webapp/routes/config_routes.py`（`onboard_service_unit` 之后）
- Test: `tests/test_config_routes_jar.py`

**Interfaces:**
- Produces: `GET /api/projects/<pid>/services/<sid>/hosts?env=<name>` →
  `{"env", "unit", "hosts": [{"host", "unit", "status", "version", "previous_version", "pid", "uptime_hours", "restarts", "cpu", "rss_mb", "updated_at", "live"}]}`

- [ ] **Step 1: 写失败测试**

在 `tests/test_config_routes_jar.py` 的 `JarConfigRoutesTest` 追加：

```python
    def _hosts_ready(self, name, host):
        r = self._create(name=name)
        sid = r.get_json()["id"]
        r = self.c.post(f"/api/projects/{self.pid}/environments",
                        json={"name": "prod"}, headers=self.h)
        assert r.status_code == 201, r.get_data(as_text=True)
        r = self.c.post("/api/servers", json={
            "host": host, "ssh_port": 22, "ssh_user": "root"}, headers=self.h)
        assert r.status_code == 201, r.get_data(as_text=True)
        return sid

    def test_hosts_merges_snapshot_and_live(self):
        sid = self._hosts_ready("auth5", "10.0.0.11")
        dbm.upsert_host_state(self.app.config["DB_PATH"], self.pid, "prod",
                              "auth5", "10.0.0.11", "1.0.0", "0.9.0", 3)
        fake = mock.Mock()
        fake.get_overview.return_value = {"services": {"auth5": {"hosts": [
            {"host": "10.0.0.11", "status": "online", "version": "1.0.1",
             "pid": 42, "uptime_hours": 2.5, "restarts": 1, "cpu": 3.2,
             "rss_mb": 128.0, "unit": "auth5"}]}}}
        with mock.patch("webapp.monitor.get_monitor", return_value=fake):
            r = self.c.get(
                f"/api/projects/{self.pid}/services/{sid}/hosts?env=prod",
                headers=self.h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = r.get_json()
        self.assertEqual(data["env"], "prod")
        self.assertEqual(data["unit"], "auth5")
        row = data["hosts"][0]
        self.assertEqual(row["host"], "10.0.0.11")
        self.assertEqual(row["status"], "online")
        self.assertEqual(row["version"], "1.0.1")
        self.assertEqual(row["previous_version"], "0.9.0")
        self.assertEqual(row["pid"], 42)
        self.assertTrue(row["live"])

    def test_hosts_falls_back_to_snapshot_when_monitor_off(self):
        sid = self._hosts_ready("auth6", "10.0.0.12")
        dbm.upsert_host_state(self.app.config["DB_PATH"], self.pid, "prod",
                              "auth6", "10.0.0.12", "2.0.0", "1.9.0", 4)
        with mock.patch("webapp.monitor.get_monitor", return_value=None):
            r = self.c.get(
                f"/api/projects/{self.pid}/services/{sid}/hosts?env=prod",
                headers=self.h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        row = r.get_json()["hosts"][0]
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["version"], "2.0.0")
        self.assertFalse(row["live"])

    def test_hosts_rejects_non_jar_service(self):
        r = self.c.post(f"/api/projects/{self.pid}/services", json={
            "name": "web7", "image_name": "web7", "service_name": "web7"},
            headers=self.h)
        sid = r.get_json()["id"]
        r = self.c.get(f"/api/projects/{self.pid}/services/{sid}/hosts",
                       headers=self.h)
        self.assertEqual(r.status_code, 400)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_config_routes_jar.py -q`
Expected: FAIL（404，路由不存在）。

- [ ] **Step 3: 实现**

在 `webapp/routes/config_routes.py` 的 `onboard_service_unit` 之后新增：

```python
def _merge_host_row(host: str, unit: str, db_row: dict, live: dict) -> dict:
    """DB 快照 + 实时视图合并：实时优先，DB 补版本/回滚基线"""
    db_row = db_row or {}
    live = live or {}
    return {
        "host": host,
        "unit": live.get("unit") or unit,
        "status": live.get("status") or ("unknown" if db_row else "not_deployed"),
        "version": live.get("version") or db_row.get("current_version") or "",
        "previous_version": db_row.get("previous_version") or "",
        "pid": live.get("pid") or 0,
        "uptime_hours": live.get("uptime_hours"),
        "restarts": live.get("restarts") or 0,
        "cpu": live.get("cpu"),
        "rss_mb": live.get("rss_mb"),
        "updated_at": db_row.get("updated_at") or 0,
        "live": bool(live),
    }


@config_bp.route("/projects/<int:pid>/services/<int:sid>/hosts", methods=["GET"])
@require_auth
def service_hosts(pid, sid):
    """每主机版本/进程视图（只读）：实时监控优先，监控关闭时回退 DB 快照"""
    db_path = current_app.config["DB_PATH"]
    rec = dbm.get_record(db_path, "services", sid)
    if not rec or rec.get("project_id") != pid:
        return _err("服务不存在", 404)
    if (rec.get("deploy_mode") or "container") != "jar_service":
        return _err("该服务不是 jar_service 发布方式")
    env_name = request.args.get("env") or ""
    env_obj = (dbm.get_env_by_name(db_path, pid, env_name) if env_name
               else dbm.get_default_env(db_path, pid))
    if not env_obj:
        return _err("目标环境不存在", 404)
    env_name = env_obj["name"]
    unit = _unit_target(rec)[0]
    db_rows = {r["host"]: r for r in
               dbm.list_host_state(db_path, pid, env_name, rec["name"])}
    live_rows = {}
    try:
        from ..monitor import get_monitor
        monitor = get_monitor()
        if monitor:
            overview = monitor.get_overview(pid, env_name)
            svc_view = (overview.get("services") or {}).get(rec["name"]) or {}
            live_rows = {h["host"]: h
                         for h in (svc_view.get("hosts") or [])}
    except Exception:
        live_rows = {}
    hosts, seen = [], set()
    for srv in dbm.list_env_servers(db_path, env_obj["id"]):
        seen.add(srv["host"])
        hosts.append(_merge_host_row(srv["host"], unit,
                                     db_rows.get(srv["host"]),
                                     live_rows.get(srv["host"])))
    for host, row in live_rows.items():
        if host not in seen:
            hosts.append(_merge_host_row(host, unit, db_rows.get(host), row))
    return _ok({"env": env_name, "unit": unit,
                "hosts": sorted(hosts, key=lambda h: h["host"])})
```

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/test_config_routes_jar.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add webapp/routes/config_routes.py tests/test_config_routes_jar.py
git commit -m "feat(api): expose per-host jar versions and process state"
```

---

### Task 8: 设备页卡片与每主机抽屉

**Files:**
- Create: `webapp/static/js/jar_hosts.js`
- Modify: `webapp/static/js/pages/devices.js`（卡片渲染、抽屉、销毁）
- Modify: `webapp/templates/screen.html:51`（脚本引入与版本号）
- Test: `tests/js/jar_hosts_test.js`（新建）

**Interfaces:**
- Produces: 全局 `jarHostTableHTML(rows: Array) -> string`（纯渲染，自动转义）
- Consumes: `GET /api/projects/<pid>/services/<sid>/hosts?env=`

- [ ] **Step 1: 写失败测试**

新建 `tests/js/jar_hosts_test.js`：

```js
// jar_hosts.js 纯渲染回归：字段展示、空态、快照提示与 XSS 转义
'use strict';
const { jarHostTableHTML } = require('../../webapp/static/js/jar_hosts.js');

const failures = [];
function check(name, cond, detail) {
  if (!cond) failures.push(name + (detail ? ': ' + detail : ''));
}

const html = jarHostTableHTML([{
  host: '10.0.0.1', status: 'online', version: '1.0.1',
  previous_version: '1.0.0', pid: 42, uptime_hours: 2.5, restarts: 1,
  cpu: 3.2, rss_mb: 128, live: true,
}]);
check('status label', html.includes('在线'), html);
check('current version', html.includes('1.0.1'));
check('previous version', html.includes('1.0.0'));
check('pid', html.includes('42'));
check('uptime', html.includes('2.5小时'));
check('cpu', html.includes('3.2%'));
check('memory', html.includes('128 MB'));
check('live tag', html.includes('实时'));

const xss = jarHostTableHTML([{
  host: '<img src=x onerror=alert(1)>', status: 'online', version: '<b>x</b>',
  live: true,
}]);
check('no injected tag', !/<img|<b>/i.test(xss), xss);
check('host escaped', xss.includes('&lt;img'), xss);

const empty = jarHostTableHTML([]);
check('empty state', empty.includes('暂无主机版本数据'), empty);

const snapshot = jarHostTableHTML([{
  host: 'h1', status: 'unknown', version: '2.0.0', live: false,
}]);
check('snapshot tag', snapshot.includes('版本快照'), snapshot);
check('snapshot hint', snapshot.includes('开启实时监控'), snapshot);
check('null cpu placeholder', snapshot.includes('—'));

if (failures.length) {
  console.error('FAIL\n' + failures.join('\n'));
  process.exit(1);
}
console.log('jar hosts render tests passed');
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node tests/js/jar_hosts_test.js`
Expected: FAIL，`Cannot find module .../jar_hosts.js`。

- [ ] **Step 3: 实现 `webapp/static/js/jar_hosts.js`**

```js
// jar_hosts.js — jar_service 每主机版本/进程视图（纯渲染，供设备页抽屉与 Node 用例）
(function(global){
  const STATUS_LABEL = { online: '在线', stopped: '已停止',
                         not_deployed: '未部署', probe_failed: '探测失败',
                         unknown: '未知' };
  const DOT_CLASS = { online: 'ok', stopped: 'bad', probe_failed: 'warn' };

  function esc(v){
    return String(v == null ? '' : v).replace(/[&<>"']/g, c =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  function fmtUptime(h){
    if (h == null || h < 0) return '—';
    if (h < 1) return Math.max(1, Math.round(h * 60)) + '分钟';
    if (h < 48) return h.toFixed(1) + '小时';
    return Math.floor(h / 24) + '天' + Math.floor(h % 24) + '时';
  }
  function fmtNum(v, unit){
    return (v == null || v === '') ? '—' : esc(v) + (unit || '');
  }

  function jarHostTableHTML(rows){
    rows = Array.isArray(rows) ? rows : [];
    if (!rows.length) {
      return '<div class="empty-state"><div class="e-ico">🖥️</div>'
           + '<div>暂无主机版本数据</div></div>';
    }
    const body = rows.map(r => {
      const status = r.status || 'unknown';
      return `<tr>
        <td><span class="dot ${DOT_CLASS[status] || 'gray'}"></span><span class="num">${esc(r.host)}</span></td>
        <td>${esc(STATUS_LABEL[status] || status)}</td>
        <td class="num">${esc(r.version || '—')}</td>
        <td class="num">${esc(r.previous_version || '—')}</td>
        <td class="num">${r.pid ? esc(r.pid) : '—'}</td>
        <td>${fmtUptime(r.uptime_hours)}</td>
        <td class="num">${r.restarts == null ? '—' : esc(r.restarts)}</td>
        <td class="num">${fmtNum(r.cpu, '%')}</td>
        <td class="num">${fmtNum(r.rss_mb, ' MB')}</td>
        <td class="muted">${r.live ? '实时' : '版本快照'}</td>
      </tr>`;
    }).join('');
    const hint = rows.some(r => !r.live)
      ? '<div class="weak" style="margin-top:8px;font-size:12px">'
        + '「版本快照」来自最近一次发布/监控回写；开启实时监控后可看到进程状态。</div>'
      : '';
    return `<div class="tbl-wrap"><table class="tbl"><thead><tr>
      <th>主机</th><th>状态</th><th>当前版本</th><th>上一版本</th>
      <th>PID</th><th>运行时长</th><th>重启</th><th>CPU</th><th>内存</th><th>数据</th>
    </tr></thead><tbody>${body}</tbody></table></div>${hint}`;
  }

  global.jarHostTableHTML = jarHostTableHTML;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { jarHostTableHTML };
  }
})(typeof window !== 'undefined' ? window : globalThis);
```

- [ ] **Step 4: 接入设备页**

`webapp/templates/screen.html` 第 51 行之前插入：

```html
<script src="/static/js/jar_hosts.js?v=1"></script>
```

并把 `devices.js` 的版本号改为 `?v=9`。

`webapp/static/js/pages/devices.js`：

顶部状态区追加：

```js
  let jarHostLive = null;
```

`renderCards` 中 jar 版本与状态：

```js
      const ver = isStatic ? ((lastOk && lastOk.version) || '—')
        : (isJar ? (st.version || (lastOk && lastOk.version) || '—')
                 : (st.version || '—'));
      const jHosts = Array.isArray(st.hosts) ? st.hosts : [];
      const jOnline = jHosts.filter(h => h.status === 'online').length;
```

jar 卡片块（替换原第 117-120 行）：

```js
          ${isJar ? `
          <div><span class="weak">版本</span> <span class="num">${esc(ver)}</span>
               <span class="weak" style="margin-left:10px">状态</span> ${svcLabel(st, run)}</div>
          <div><span class="weak">主机</span> <span class="num">${jHosts.length
            ? `${jOnline}/${jHosts.length} 在线` : '—'}</span>
               ${upTxt ? `<span class="weak" style="margin-left:10px">已运行</span> ${upTxt}` : ''}</div>
          <div><span class="weak">应用根</span> <span class="num ellipsis" style="display:inline-block;max-width:78%;vertical-align:bottom" title="${esc((s.runtime_config || {}).app_home || '')}">${esc((s.runtime_config || {}).app_home || '未配置')}</span></div>
          <div><span class="weak">端口</span> <span class="num">${(s.runtime_config || {}).port || s.health_port || '-'}</span></div>` : isStatic ? `
```

卡片底部按钮（替换原第 139-140 行）：

```js
          ${isJar ? `<button class="btn sm ghost" data-hosts="${s.id}" data-name="${esc(s.name)}" title="查看每主机版本与进程状态">🖥 主机</button>` : ''}
          ${isJar ? '' : ''}
          <button class="svc-log-btn" data-svc="${esc(s.name)}" data-static="${isStatic ? 1 : 0}" title="${isStatic ? '查看发布日志' : (isJar ? '查看进程日志（journalctl）' : '查看实时日志')}">📋 日志</button>
```

绑定事件（在日志按钮绑定之前）：

```js
    wall.querySelectorAll('[data-hosts]').forEach(btn => {
      btn.onclick = (e) => {
        e.stopPropagation();
        openJarHostsDrawer(Number(btn.dataset.hosts), btn.dataset.name);
      };
    });
```

新增抽屉函数（放在 `openServiceLogDrawer` 之前）：

```js
  function stopJarHostPolling(){
    if (jarHostLive){ jarHostLive(); jarHostLive = null; }
  }
  function openJarHostsDrawer(sid, svcName){
    stopJarHostPolling();
    openDrawer({
      title: '每主机版本 · ' + svcName, wide: true,
      onClose: stopJarHostPolling,
      body: '<div id="jarHostBox">加载中...</div>',
    });
    const refresh = () => refreshJarHosts(sid);
    refresh();
    jarHostLive = liveBind('monitor', refresh, 15000);
  }
  async function refreshJarHosts(sid){
    try {
      const env = state.currentEnv || '';
      const data = await api(`/api/projects/${state.currentProject.id}/services/${sid}`
        + `/hosts?env=${encodeURIComponent(env)}`);
      const box = document.getElementById('jarHostBox');
      if (box) box.innerHTML = jarHostTableHTML(data.hosts || []);
    } catch(e) {
      const box = document.getElementById('jarHostBox');
      if (box) box.innerHTML = `<span class="err">加载失败：${esc(e.message || e)}</span>`;
    }
  }
```

`destroy()` 中追加 `stopJarHostPolling();`：

```js
    destroy: function(){ destroyed = true; clearLiveOffs(); lastSig = null; stopSvcLogPolling(); stopJarHostPolling(); },
```

- [ ] **Step 5: 运行 JS 检查**

Run: `node tests/js/jar_hosts_test.js`
Expected: `jar hosts render tests passed`。

Run: `node --check webapp/static/js/pages/devices.js`
Expected: 无输出。

- [ ] **Step 6: 提交**

```bash
git add webapp/static/js/jar_hosts.js webapp/static/js/pages/devices.js webapp/templates/screen.html tests/js/jar_hosts_test.js
git commit -m "feat(ui): show per-host jar versions and process status"
```

---

### Task 9: 文档、质量门禁与全量回归

**Files:**
- Modify: `README.md:143`（实时监控条目）
- Modify: `README.md:292-294`（jar_service 说明）
- Modify: `docs/usage.md:735-740`（前提）、`docs/usage.md:786-787`（故障与观测）

**Interfaces:** 无代码接口。

- [ ] **Step 1: 更新 README**

第 143 行「实时监控」条目末尾追加：

```text
`jar_service` 额外探测 systemd 单元（进程存活、版本、PID、运行时长、重启数、CPU/RSS）并采集 `journalctl` 日志；设备页可查看每主机版本视图。
```

第 292 行 jar 条目末尾追加：

```text
平台周期探测单元状态并从 `.deploy_version` 读取每主机版本；版本变化时写穿 `service_host_state`，监控关闭时页面回退到该快照。
```

- [ ] **Step 2: 更新 usage 文档**

`docs/usage.md` 前提列表追加一条：

```md
- 发布账号需能读取 journal（root 或 `systemd-journal` 组），否则日志采集为空、进程状态仍正常；
```

把第 787 行「设备页对 jar 服务的状态列暂显示…」替换为：

```md
观测（M5）：

- 设备页 jar 卡片展示实时状态（在线/已停止/未部署/探测失败）、当前版本、在线主机数与运行时长；版本以目标机 `.deploy_version` 为准；
- 点「主机」查看每主机版本、上一版本、PID、运行时长、重启数、CPU/内存；监控关闭时显示「版本快照」，来自 `service_host_state`；
- 点「日志」读取采集器入库的 `journalctl` 日志（默认 30s 一轮，按 `systemd:<unit>` 独立游标，关键词告警照常生效）；
- 首次采集窗口为最近 5 分钟，之后按游标增量；日志正文中的多行消息逐行保留并带时间戳。
```

- [ ] **Step 3: 全量测试与质量门禁**

Run: `python -m pytest -q`
Expected: 全部 PASS（ruff/node 缺失时相关步骤 SKIP）。

Run: `python scripts/quality_gate.py`
Expected: 输出各步骤 PASS/SKIP，无 FAIL。

- [ ] **Step 4: 提交**

```bash
git add README.md docs/usage.md
git commit -m "docs: document jar process and journal observability"
```

---

## 验收清单（实现者自检）

- [ ] `python -m pytest -q` 全绿；`python scripts/quality_gate.py` 无 FAIL。
- [ ] 设备页 jar 卡片不再显示「未知」；状态来自监控探测，版本优先取目标机标记。
- [ ] 监控关闭时「主机」抽屉仍能显示 DB 快照（状态 `unknown`、`live=false`），不报错。
- [ ] journal 日志在「日志」抽屉可按服务名查到；`log_entries.service` 为 `service_name or name`。
- [ ] 同一台主机绑定多个环境时，单元探测/日志仍只发 1 条 SSH 命令。
- [ ] 版本未变化时监控轮询不写 `service_host_state`（无写放大）。
- [ ] 所有新增渲染对 `host/version/unit` 做 HTML 转义（XSS 用例通过）。
