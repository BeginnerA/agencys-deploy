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

_STATUS_RANK = {"online": 4, "stopped": 3, "not_deployed": 2,
                "probe_failed": 1, "unknown": 0}


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
