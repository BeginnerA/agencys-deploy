"""实时探测调度器：周期 SSH 容器状态 + Nacos 实例健康，内存缓存供大屏轮询"""
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from . import db as dbm

# Nacos accessToken 缓存：{base_url: (token, expire_ts)}，避免每服务每轮重复登录
_NACOS_TOKEN_CACHE = {}


class _SSHConnectionPool:
    """SSH 连接池：复用 paramiko 客户端，避免每次调用都进行 TCP 握手 + SSH 认证

    连接按 (host, port, username) 缓存，同一服务器的多次调用复用同一连接。
    每次取用时检查连接活性，死亡连接自动重建。
    线程安全：per-host 锁保证同一服务器不会并发创建多个连接。
    """

    def __init__(self, max_age=300, cleanup_interval=60):
        self._clients = {}      # {(host, port, user): {"client": SSHClient, "ts": float}}
        self._locks = {}        # {(host, port, user): Lock}
        self._pool_lock = threading.Lock()  # 保护 _locks 字典本身
        self._max_age = max_age  # 连接最大存活秒数（超过后强制重建，防止僵死）
        self._cleanup_interval = cleanup_interval
        self._last_cleanup = time.time()

    def get_client(self, host, port, ssh_cfg, timeout=10):
        """获取或创建到指定服务器的 SSH 客户端（复用已有连接）"""
        import os
        import pathlib
        import paramiko

        user = ssh_cfg.get("user", "root")
        key = (host, int(port or 22), user)

        # 确保 per-host 锁存在
        with self._pool_lock:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
        host_lock = self._locks[key]

        with host_lock:
            entry = self._clients.get(key)
            if entry:
                age = time.time() - entry["ts"]
                client = entry["client"]
                transport = client.get_transport()
                # 复用条件：连接活跃 + 未超时
                if transport and transport.is_active() and age < self._max_age:
                    entry["ts"] = time.time()  # 刷新使用时间
                    return client
                # 连接已死或过期 → 清理
                try:
                    client.close()
                except Exception:
                    pass
                del self._clients[key]

            # 创建新连接
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            kw = dict(hostname=host, port=int(port or 22),
                      username=user, timeout=timeout)
            password = ssh_cfg.get("password", "") or None
            key_file = ssh_cfg.get("key_file", "") or None
            if password:
                kw["password"] = password
            if key_file:
                key_file = str(key_file).replace("~", str(pathlib.Path.home()))
                if os.path.isfile(key_file):
                    kw["key_filename"] = key_file
            client.connect(**kw)
            self._clients[key] = {"client": client, "ts": time.time()}

            # 定期清理过期/死亡连接（避免内存泄漏）
            now = time.time()
            if now - self._last_cleanup > self._cleanup_interval:
                self._do_cleanup()
                self._last_cleanup = now

            return client

    def _do_cleanup(self):
        """清理所有过期或死亡连接（在调用者持有的锁保护下执行）"""
        stale_keys = []
        for k, entry in list(self._clients.items()):
            transport = entry["client"].get_transport()
            if not transport or not transport.is_active() or \
               time.time() - entry["ts"] > self._max_age:
                stale_keys.append(k)
        for k in stale_keys:
            entry = self._clients.pop(k, None)
            if entry:
                try:
                    entry["client"].close()
                except Exception:
                    pass

    def close_all(self):
        """关闭所有缓存连接（应用关闭时调用）"""
        with self._pool_lock:
            for entry in self._clients.values():
                try:
                    entry["client"].close()
                except Exception:
                    pass
            self._clients.clear()


_ssh_pool = _SSHConnectionPool()


def _ssh_exec_multi(host: str, port: int, ssh_cfg: dict, cmds: list, timeout: int = 10) -> list:
    """复用 SSH 连接顺序执行多条命令，返回各命令 stdout 列表；失败抛异常

    通过 _ssh_pool 复用 paramiko 客户端，同一服务器的多次调用共享连接，
    省去重复的 TCP 握手 + SSH 认证开销（探测热路径的主要开销）。
    """
    client = _ssh_pool.get_client(host, port, ssh_cfg, timeout)
    outs = []
    for cmd in cmds:
        _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", "ignore")
        err = stderr.read().decode("utf-8", "ignore")
        outs.append(out or err)
    return outs


def _ssh_exec(host: str, port: int, ssh_cfg: dict, cmd: str, timeout: int = 10) -> str:
    """paramiko 执行远端命令，返回 stdout；失败抛异常"""
    return _ssh_exec_multi(host, port, ssh_cfg, [cmd], timeout)[0]


def _merge_server_ssh(env_ssh: dict, s: dict) -> dict:
    """合并服务器级 ssh 凭据：服务器级字段优先，缺省回退环境级。
    与连接测试/部署（scripts/common.py merge_server_ssh）语义一致，
    避免环境级凭据过期时探测全部失败。"""
    ssh = dict(env_ssh or {})
    if s.get("ssh_user"):
        ssh["user"] = s["ssh_user"]
    if s.get("ssh_password"):
        ssh["password"] = s["ssh_password"]
    if s.get("ssh_key_file"):
        ssh["key_file"] = s["ssh_key_file"]
    return ssh


def _probe_server_full(host: str, port: int, ssh_cfg: dict, services: list,
                       timeout: int = 10, health_hosts: dict = None,
                       skip_extended: bool = False) -> dict:
    """单连接探测整台服务器：容器镜像 + docker stats 资源 + 主机指标 + 扩展指标

    返回 {ssh_ok, containers, containers_stopped, container_list, resources, host_metrics, extended, error}。
    第一轮：4 条命令（ps/ps -a/stats/主机指标），1 次 SSH 连接；
    第二轮：扩展指标（容器详情/swap/网络/diskIO/TCP/docker_df/SSL），1 次 SSH 连接。
    共 2 次连接，避免逐命令独立认证（探测热路径的主要开销）。
    skip_extended=True 时跳过第二轮（节流，降低 SSH 频率）。
    """
    result = {"ssh_ok": False, "containers": {}, "container_list": [],
              "containers_stopped": {}, "resources": {}, "host_metrics": {},
              "extended": {}, "error": ""}
    try:
        # 第一轮：基础指标（ps + ps -a + stats + 主机指标）
        # ps -a 用于识别容器存在但未运行（Exited）的服务：docker ps 只看运行中容器，
        # 否则已停止的服务会被误判为 not_deployed（从未部署）
        ps_out, ps_a_out, stats_out, hm_out = _ssh_exec_multi(
            host, port, ssh_cfg,
            ["docker ps --format '{{.Names}}|{{.Image}}'",
             "docker ps -a --format '{{.Names}}|{{.Status}}|{{.Image}}'",
             "docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemPerc}}'",
             "grep -c processor /proc/cpuinfo; "
             "cat /proc/loadavg; "
             "vmstat 1 2 | tail -1; "
             "awk '/MemTotal|MemFree|Buffers|Cached/' /proc/meminfo; "
             "df -P / | tail -1"],
            timeout=timeout)
        containers = {}
        container_names = []
        for line in ps_out.splitlines():
            if "|" not in line:
                continue
            name, image = line.split("|", 1)
            name = name.strip()
            container_names.append(name)
            for s in services:
                if name == s["service_name"]:
                    containers[s["service_name"]] = image.strip()
                    break
        # 已停止容器：docker ps -a 中存在、状态非 Up（Exited/Created/Restarting），
        # 仅登记与配置服务同名的容器（基础设施容器不关心停止与否）
        containers_stopped = {}
        for line in ps_a_out.splitlines():
            parts = line.split("|")
            if len(parts) < 3:
                continue
            name, status = parts[0].strip(), parts[1].strip()
            if not name or status.startswith("Up"):
                continue
            for s in services:
                if name == s["service_name"]:
                    containers_stopped[s["service_name"]] = parts[2].strip()
                    break
        result["containers"] = containers
        result["containers_stopped"] = containers_stopped
        result["container_list"] = container_names
        result["resources"] = _parse_docker_resources(stats_out, services)
        result["host_metrics"] = _parse_host_metrics(hm_out)
        result["ssh_ok"] = True
        # 第二轮：扩展指标（容器详情/swap/网络/diskIO/TCP/docker_df/SSL）
        # skip_extended 时节流跳过，降低 SSH 频率（每 N 轮才采集一次）
        if not skip_extended:
            try:
                ext_cmds = _build_extended_cmds(services, health_hosts or {})
                (cs_out, sw_out, net_out,
                 dio_out, tcp_out, ddf_out, ssl_out) = _ssh_exec_multi(
                    host, port, ssh_cfg, ext_cmds, timeout=25)
                result["extended"] = {
                    "container_status": _parse_container_status(cs_out, services),
                    "swap": _parse_swap(sw_out),
                    **_parse_network(net_out),
                    **_parse_diskio(dio_out),
                    "tcp": _parse_tcp(tcp_out),
                    "docker_df": _parse_docker_df(ddf_out),
                    "ssl_days": _parse_ssl_days(ssl_out),
                }
            except Exception as ext_err:
                import sys
                print(f"[monitor] 扩展指标采集失败({host}): {ext_err!r}",
                      file=sys.stderr)  # 扩展指标失败不影响基础探测
    except Exception as e:
        result["error"] = str(e)[:120]
    return result


def _probe_nacos_instances(env_cfg: dict, svc_name: str, groups: list) -> int:
    """查询 Nacos 实例健康数（某服务在全部分组下的健康实例总和）

    适配计划缺口：scripts/nacos_manager.py 的 NacosClient 构造签名是
    NacosClient(env)（内部 load_config 读 YAML），与计划假设的
    NacosClient(url=..., namespace=...) 不兼容；且 DB 模式下 YAML 可能过期。
    故直接以 env_cfg（DB 环境配置）调用 Nacos 1.x Open API（等价实现）。
    """
    try:
        nacos = env_cfg.get("nacos") or {}
        base = nacos.get("url", "")
        if not base:
            return -1
        params = {"namespaceId": nacos.get("namespace", ""),
                  "groupName": nacos.get("group", "DEFAULT_GROUP")}
        # Nacos 1.x 认证（与 scripts/nacos_manager.py 的 _ensure_token 等价）；
        # token 缓存至过期前 60s，避免每服务每轮重复登录（登录超时 10s）
        username = nacos.get("username", "")
        if username:
            token, expire = _NACOS_TOKEN_CACHE.get(base, (None, 0))
            if not token or time.time() >= expire - 60:
                r = requests.post(f"{base}/nacos/v1/auth/users/login",
                                  data={"username": username,
                                        "password": nacos.get("password", "")},
                                  timeout=10)
                r.raise_for_status()
                data = r.json()
                token = data.get("accessToken")
                expire = time.time() + data.get("tokenTtl", 18000)
                _NACOS_TOKEN_CACHE[base] = (token, expire)
            params["accessToken"] = token
        groups = groups or [params["groupName"]]
        total = 0
        for g in groups:
            p = dict(params, groupName=g, serviceName=svc_name, healthyOnly="false")
            resp = requests.get(f"{base}/nacos/v1/ns/instance/list", params=p, timeout=10)
            resp.raise_for_status()
            hosts = resp.json().get("hosts", [])
            total += sum(1 for h in hosts if h.get("healthy"))
        return total
    except Exception:
        return -1  # 查询失败


def _parse_docker_resources(out: str, services: list) -> dict:
    """解析 docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemPerc}}' 输出
    返回 {service_name: {"cpu": float, "mem": float}}"""
    resources = {}
    for line in out.splitlines():
        if "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        name = parts[0].strip()
        for s in services:
            if name == s["service_name"]:
                try:
                    cpu = float(parts[1].strip().rstrip("%"))
                    mem = float(parts[2].strip().rstrip("%"))
                    resources[name] = {"cpu": cpu, "mem": mem}
                except (ValueError, IndexError):
                    pass
                break
    return resources


def _probe_health_url(host: str, port: int, url: str, timeout: int = 5) -> dict:
    """HTTP 健康检查，返回 {"ok": bool, "ms": int}"""
    if not url.startswith("/"):
        url = "/" + url
    full_url = f"http://{host}:{port}{url}"
    try:
        t0 = time.time()
        r = requests.get(full_url, timeout=timeout, allow_redirects=False)
        ms = int((time.time() - t0) * 1000)
        return {"ok": r.status_code == 200, "ms": ms}
    except Exception:
        return {"ok": False, "ms": -1}


def _compute_security(servers: list) -> dict:
    """计算 SSH 安全配置评分
    返回 {"score": int|None, "key_ratio": float|None, "total": int}；
    total=0 时 score/key_ratio 为 None（无数据，前端显示占位而非误导性满分）"""
    if not servers:
        return {"score": None, "key_ratio": None, "total": 0}
    key_count = sum(1 for s in servers if s.get("ssh_key_file"))
    pwd_only = sum(1 for s in servers if not s.get("ssh_key_file") and s.get("ssh_password"))
    no_auth = sum(1 for s in servers if not s.get("ssh_key_file") and not s.get("ssh_password"))
    total = len(servers)
    key_ratio = key_count / total if total else 0
    # 评分：密钥认证 1.0 + 仅密码 0.4 + 无认证 0.0
    score = int((key_ratio * 1.0 + (pwd_only / total if total else 0) * 0.4) * 100)
    return {"score": min(100, score), "key_ratio": key_ratio, "total": total}


def _parse_host_metrics(out: str) -> dict:
    """解析主机指标命令输出
    命令: vmstat 1 2 | tail -1; awk '/MemTotal|MemFree|Buffers|Cached/' /proc/meminfo; df -P / | tail -1; cat /proc/loadavg /proc/cpuinfo | grep -c processor
    返回 {cpu, mem, disk, load} 百分比（0-100），失败字段为 None"""
    metrics = {"cpu": None, "mem": None, "disk": None, "load": None}
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    # vmstat 最后一行: r b swpd free buff cache si so bi bo in cs us sy id wa st
    for i in range(len(lines) - 1, -1, -1):
        parts = lines[i].split()
        if len(parts) >= 15:
            try:
                int(parts[0])  # r 列应为数字
                idle = float(parts[14])
                metrics["cpu"] = round(max(0, min(100, 100 - idle)), 1)
                break
            except (ValueError, IndexError):
                continue
    # /proc/meminfo
    mi = {}
    for l in lines:
        for k in ("MemTotal", "MemFree", "Buffers", "Cached"):
            if l.startswith(k + ":"):
                try:
                    mi[k] = int(l.split()[1])
                except (ValueError, IndexError):
                    pass
    if mi.get("MemTotal"):
        free = mi.get("MemFree", 0) + mi.get("Buffers", 0) + mi.get("Cached", 0)
        metrics["mem"] = round((mi["MemTotal"] - free) / mi["MemTotal"] * 100, 1)
    # df -P /
    for l in lines:
        parts = l.split()
        if "/" in parts and l.startswith("/dev/"):
            for p in parts:
                if p.endswith("%"):
                    try:
                        metrics["disk"] = float(p.rstrip("%"))
                    except ValueError:
                        pass
                    break
            break
    # load average（1min）/ CPU 核数 → 百分比
    # 注意：先输出 CPU 核数，再输出 loadavg，避免 grep -c 结果拼到 loadavg 行末尾
    ncpu = 1
    for l in lines:
        if l.isdigit():
            ncpu = max(1, int(l))
            break
    for l in lines:
        parts = l.split()
        if len(parts) >= 3 and "." in parts[0]:
            try:
                load1 = float(parts[0])
                metrics["load"] = round(load1 / ncpu * 100, 1)
                break
            except (ValueError, IndexError):
                pass
    return metrics


# ---------- 扩展指标解析函数 ----------

def _build_extended_cmds(services: list, health_hosts: dict) -> list:
    """构造第二轮 SSH 命令列表：容器详情 + swap + 网络 + diskIO + TCP + docker_df + SSL

    返回 7 条命令的列表，直接传给 _ssh_exec_multi 作为 cmds 参数，
    避免字符串拼接/拆分导致的 shell 语法破坏（for 循环内分号等）。
    """
    # 1. 容器重启次数 + 运行时长（从 docker ps Status 解析）
    # 采集所有容器（含未注册为服务的基础设施容器如 RocketMQ 等），
    # 避免服务器上有容器但 container_status 为空导致运行时长永远为空。
    c1 = "docker ps -a --format '{{.Names}}|{{.Status}}'"
    # 2. Swap（/proc/meminfo）
    c2 = "grep -E 'SwapTotal|SwapFree' /proc/meminfo 2>/dev/null || true"
    # 3. 网络流量（两次采样间隔 1s，计算每秒字节数）
    c3 = "head -5 /proc/net/dev; sleep 1; head -5 /proc/net/dev"
    # 4. 磁盘 IO（两次采样 /proc/diskstats，间隔 1s）
    c4 = ("grep -E 'sd[a-z]+|vd[a-z]+|xvd[a-z]+' /proc/diskstats 2>/dev/null; "
          "sleep 1; "
          "grep -E 'sd[a-z]+|vd[a-z]+|xvd[a-z]+' /proc/diskstats 2>/dev/null")
    # 5. TCP 连接状态分布（输出带标签的计数，便于解析）
    c5 = ("echo ESTABLISHED:$(ss -tan state established 2>/dev/null | tail -n +2 | wc -l); "
          "echo TIME_WAIT:$(ss -tan state time-wait 2>/dev/null | tail -n +2 | wc -l); "
          "echo CLOSE_WAIT:$(ss -tan state close-wait 2>/dev/null | tail -n +2 | wc -l)")
    # 6. Docker 磁盘占用
    c6 = "docker system df 2>/dev/null || true"
    # 7. SSL 证书到期（自动检测 HTTPS 端口：先试 443，否则从 openresty/nginx 容器找）
    ssl_hosts = list({h for hosts in (health_hosts or {}).values() for h in hosts[:1]})[:3]
    if ssl_hosts:
        targets = " ".join(ssl_hosts)
        c7 = ("for h in " + targets + "; do "
              "port=443; "
              # 先测 443 是否可达
              "if ! echo | timeout 2 bash -c \"cat < /dev/tcp/$h/443\" 2>/dev/null; then "
              # 443 不通，从 openresty/nginx 容器找 443 映射
              "eport=$(docker ps --format '{{.Names}}|{{.Ports}}' 2>/dev/null "
              "| grep -iE 'openresty|nginx' | head -1 "
              "| grep -oE '[0-9]+->443/tcp' | cut -d'>' -f1); "
              "if [ -n \"$eport\" ]; then port=$eport; fi; "
              "fi; "
              "echo \"$h:$port|$(echo | openssl s_client -connect $h:$port "
              "-servername $h 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null)\"; "
              "done || true")
    else:
        c7 = "true"
    return [c1, c2, c3, c4, c5, c6, c7]


def _parse_container_status(out: str, services: list) -> dict:
    """解析容器重启次数和运行时长
    返回 {container_name: {"restart_count": int, "uptime_hours": float}}
    包含服务器上所有容器（含未注册为服务的基础设施容器）。"""
    result = {}
    for line in out.splitlines():
        if "|" not in line:
            continue
        name = line.split("|", 1)[0].strip()
        status = line.split("|", 1)[1]
        if not name:
            continue
        info = {"restart_count": 0, "uptime_hours": -1}
        # Restart count: "Up X hours (healthy)" → 0; "Exited" → 1+
        status_lower = status.lower().strip()
        if "up" in status_lower:
            # 尝试从 "Up X (restarted N times)" 解析（Docker 20.10+）
            m = re.search(r'restarted?\s+(\d+)', status_lower)
            if m:
                info["restart_count"] = int(m.group(1))
            # 解析 uptime（status_lower 已转小写，故用 'up' 而非 'Up'）
            # Docker 输出格式：Up X years/months/weeks/days/hours/minutes
            m2 = re.search(
                r'up\s+'
                r'(?:(\d+)\s+years?)?[\s,]*'
                r'(?:(\d+)\s+months?)?[\s,]*'
                r'(?:(\d+)\s+weeks?)?[\s,]*'
                r'(?:(\d+)\s+days?)?[\s,]*'
                r'(?:(\d+)\s+hours?)?[\s,]*'
                r'(?:(\d+)\s+mins?)?',
                status_lower)
            if m2:
                years = int(m2.group(1) or 0)
                months = int(m2.group(2) or 0)
                weeks = int(m2.group(3) or 0)
                days = int(m2.group(4) or 0)
                hours = int(m2.group(5) or 0)
                mins = int(m2.group(6) or 0)
                info["uptime_hours"] = round(
                    years * 8760 + months * 720 + weeks * 168 +
                    days * 24 + hours + mins / 60, 1)
        else:
            info["restart_count"] = 1  # 容器不在运行状态
            info["uptime_hours"] = -1  # 已停止容器不参与运行时长计算（-1 被前端过滤）
        result[name] = info
    return result


def _parse_swap(out: str) -> float:
    """解析 Swap 使用率百分比（0-100），无 swap 或失败返回 None"""
    total = free = None
    for line in out.splitlines():
        if line.startswith("SwapTotal:"):
            try:
                total = int(line.split()[1])
            except (ValueError, IndexError):
                pass
        elif line.startswith("SwapFree:"):
            try:
                free = int(line.split()[1])
            except (ValueError, IndexError):
                pass
    if total and total > 0 and free is not None:
        return round((total - free) / total * 100, 1)
    return None


def _parse_network(out: str) -> dict:
    """解析网络流量（两次 /proc/net/dev 采样差值 / 1s 间隔）
    返回 {"net_rx": bytes/s, "net_tx": bytes/s}，取所有非 lo 接口总和"""
    result = {"net_rx": None, "net_tx": None}
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    # /proc/net/dev 格式: 2 行 header + N 行数据
    # 命令: head -5 /proc/net/dev; sleep 1; head -5 /proc/net/dev
    # 输出 10 行（5+5），前 5 行是第一次采样，后 5 行是第二次采样
    # 提取所有含 ':' 的数据行（跳过 header），按顺序每 N 个接口为一组
    iface_data = []  # [(iface, rx, tx), ...]
    for line in lines:
        if ":" not in line:
            continue
        if line.startswith("Inter") or line.startswith("face"):
            continue
        parts = line.split(":", 1)
        iface = parts[0].strip()
        if iface == "lo":
            continue
        vals = parts[1].split()
        if len(vals) >= 10:
            try:
                rx = int(vals[0])
                tx = int(vals[8])
                iface_data.append((iface, rx, tx))
            except (ValueError, IndexError):
                pass
    # 按接口数量拆分两组（前 N 个接口 = 第一次采样，后 N 个 = 第二次）
    if len(iface_data) >= 2:
        half = len(iface_data) // 2
        b1 = {iface: (rx, tx) for iface, rx, tx in iface_data[:half]}
        b2 = {iface: (rx, tx) for iface, rx, tx in iface_data[half:]}
        common = set(b1) & set(b2)
        if common:
            rx_total = sum(b2[iface][0] - b1[iface][0] for iface in common)
            tx_total = sum(b2[iface][1] - b1[iface][1] for iface in common)
            result["net_rx"] = max(0, rx_total)  # bytes/s (1s interval)
            result["net_tx"] = max(0, tx_total)
    return result


def _parse_diskio(out: str) -> dict:
    """解析磁盘 IO（两次 /proc/diskstats 采样差值 / 1s 间隔）
    返回 {"disk_read": sectors/s, "disk_write": sectors/s}"""
    result = {"disk_read": None, "disk_write": None}
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    # /proc/diskstats: major minor name reads_merged reads sectors ... writes_merged writes sectors ...
    def parse_diskstats(lines_list):
        stats = {}
        for line in lines_list:
            parts = line.split()
            if len(parts) < 14:
                continue
            name = parts[2]
            # 只取整盘设备（排除分区如 sda1）
            if not re.match(r'^(sd[a-z]+|vd[a-z]+|xvd[a-z]+)$', name):
                continue
            try:
                read_sectors = int(parts[5])
                write_sectors = int(parts[9])
                stats[name] = (read_sectors, write_sectors)
            except (ValueError, IndexError):
                pass
        return stats
    # 命令: grep ... /proc/diskstats; sleep 1; grep ... /proc/diskstats
    # 输出为两段（各 N 行设备数据），按行数对半分
    # 更健壮：找第一段最后一个设备行与第二段第一个设备行的分界点
    all_stats = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 14 and re.match(r'^(sd[a-z]+|vd[a-z]+|xvd[a-z]+)$', parts[2]):
            all_stats.append(line)
    if len(all_stats) >= 2:
        half = len(all_stats) // 2
        s1 = parse_diskstats(all_stats[:half])
        s2 = parse_diskstats(all_stats[half:])
        common = set(s1) & set(s2)
        if common:
            dr = sum(s2[d][0] - s1[d][0] for d in common)
            dw = sum(s2[d][1] - s1[d][1] for d in common)
            result["disk_read"] = max(0, dr)
            result["disk_write"] = max(0, dw)
    return result


def _parse_tcp(out: str) -> dict:
    """解析 TCP 连接状态分布
    命令输出格式：ESTABLISHED:N\nTIME_WAIT:N\nCLOSE_WAIT:N"""
    counts = {"ESTABLISHED": 0, "TIME_WAIT": 0, "CLOSE_WAIT": 0}
    for line in out.splitlines():
        line = line.strip()
        if ":" in line:
            state, _, count_str = line.partition(":")
            state = state.strip().upper()
            if state in counts:
                try:
                    counts[state] = int(count_str.strip())
                except ValueError:
                    pass
    return counts


def _parse_docker_df(out: str) -> dict:
    """解析 docker system df 输出
    返回 {"images": bytes, "containers": bytes, "volumes": bytes}"""
    result = {"images": 0, "containers": 0, "volumes": 0}
    type_map = {"IMAGES": "images", "CONTAINERS": "containers", "VOLUMES": "volumes"}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        row_type = parts[0].upper()
        if row_type not in type_map:
            continue
        key = type_map[row_type]
        size_str = parts[3].upper()
        try:
            if "GB" in size_str:
                result[key] = int(float(size_str.replace("GB", "")) * 1024 ** 3)
            elif "MB" in size_str:
                result[key] = int(float(size_str.replace("MB", "")) * 1024 ** 2)
            elif "KB" in size_str:
                result[key] = int(float(size_str.replace("KB", "")) * 1024)
            elif "B" in size_str:
                result[key] = int(float(size_str.replace("B", "")))
        except (ValueError, IndexError):
            pass
    return result


def _parse_ssl_days(out: str) -> float:
    """解析 SSL 证书到期天数（取最小值），失败返回 None"""
    import datetime
    min_days = None
    for line in out.splitlines():
        if "notAfter=" not in line:
            continue
        try:
            date_str = line.split("notAfter=", 1)[1].strip()
            exp = datetime.datetime.strptime(date_str, "%b %d %H:%M:%S %Y %Z")
            days = (exp - datetime.datetime.utcnow()).days
            if min_days is None or days < min_days:
                min_days = days
        except (ValueError, IndexError):
            pass
    return float(min_days) if min_days is not None else None


class MonitorScheduler:
    """守护线程周期探测；结果缓存 _cache: {env_id: {"servers": [...], "services": {...}, "ts": ...}}"""

    def __init__(self, db_path: str, interval: int = 30):
        self._db_path = db_path
        self._interval = max(10, int(interval))
        self._cache = {}
        self._server_containers = {}  # {host: [container_name, ...]} 供日志采集复用
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._round = 0  # 探测轮次计数器（扩展指标节流用）

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="monitor-scheduler")
        self._thread.start()

    def stop(self):
        self._stop.set()
        _ssh_pool.close_all()  # 关闭所有缓存 SSH 连接

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._cycle()
            except Exception as e:
                # 调度器自愈：单周期异常不退出；打日志保证监控故障可观测
                print(f"[monitor] 探测周期异常: {e!r}", file=sys.stderr)
            self._stop.wait(self._interval)

    def _cycle(self):
        self._round += 1
        projects = dbm.list_records(self._db_path, "projects")
        tasks = []
        for proj in projects:
            for env in dbm.list_environments(self._db_path, proj["id"]):
                if not env.get("monitor_enabled"):
                    continue
                tasks.append((proj, env))
        self._probe_envs(tasks)

    def _probe_envs(self, env_tasks: list):
        """环境间并发探测（全局池限流）：整轮时长收敛到单环境水平而非环境之和

        每个环境内部保留自己的 8 并发 SSH 池（限流），
        全局并发上限 = 环境池 4 × 环境内 8 = 32 连接，避免环境增多时连接风暴。
        """
        if not env_tasks:
            return
        with ThreadPoolExecutor(max_workers=min(4, len(env_tasks))) as pool:
            futures = [pool.submit(self._probe_env, proj, env)
                       for proj, env in env_tasks]
            for fut in futures:
                try:
                    # 单环境探测含 3 个顺序阶段（服务器/nacos/健康检查），
                    # 各阶段内部已有单任务超时兜底，外层仅防意外卡死
                    fut.result(timeout=120)
                except Exception as e:
                    print(f"[monitor] 环境探测异常: {e!r}", file=sys.stderr)

    def _probe_env(self, proj: dict, env: dict):
        services = dbm.list_services(self._db_path, proj["id"], enabled_only=True)
        servers = dbm.list_env_servers(self._db_path, env["id"])
        ssh_cfg = env.get("ssh") or {}
        result = {"ts": time.time(), "servers": [], "services": {},
                  "resources": {}, "health_checks": {}}
        health_hosts = {}  # {svc_name: [host, ...]} 用于健康检查
        per_server_metrics = {}  # {host: {cpu, mem, disk, load}} 按服务器存储主机指标
        per_server_extended = {}  # {host: {swap, net_rx, ...}} 扩展指标
        # 扩展指标节流时先从缓存回填（供后续服务循环读取 container_status/uptime_hours）
        # 非 skip 轮次会被本轮新鲜数据覆盖；skip 轮次保证前端持续有运行时长数据
        prev_ext_cache = self._cache.get(env["id"])
        if prev_ext_cache:
            per_server_extended = dict(prev_ext_cache.get("extended_metrics") or {})
        # 扩展指标节流：每 3 轮采集一次（@60s 间隔 ≈ 3 分钟一次），
        # 大幅减少 SSH 连接数（跳过时每台服务器每轮仅 1 次连接而非 2 次）
        skip_extended = (self._round % 3) != 0
        # 每台服务器整体一个任务（单连接内串行 ps/stats/主机指标，任务间全并发）：
        # 避免旧实现中 stats/指标在调度线程串行执行，整轮时长随服务器数线性增长
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_probe_server_full, s["host"], s["ssh_port"],
                                   _merge_server_ssh(ssh_cfg, s), services,
                                   10, health_hosts, skip_extended): s for s in servers}
            for fut, s in futures.items():
                try:
                    r = fut.result(timeout=30 if skip_extended else 50)
                except Exception:
                    # 与 Nacos 分支对齐：单服务器超时降级，不中断整轮探测
                    r = {"ssh_ok": False, "containers": {}, "resources": {},
                         "host_metrics": {}, "extended": {}, "error": "timeout"}
                entry = {"host": s["host"], "ssh_ok": r["ssh_ok"],
                         "containers": len(r["containers"]), "ts": time.time(),
                         "error": r["error"], "remark": s.get("remark", "")}
                result["servers"].append(entry)
                if r["ssh_ok"]:
                    # 资源使用率 + 主机指标（docker stats / vmstat 等，单连接内已取回）
                    result["resources"].update(r.get("resources") or {})
                    per_server_metrics[s["host"]] = r.get("host_metrics") or {}
                    ext = r.get("extended") or {}
                    if ext:
                        per_server_extended[s["host"]] = ext
                    # 缓存容器列表供日志采集复用（省去 log_collector 独立 docker ps SSH）
                    if r.get("container_list"):
                        self._server_containers[s["host"]] = {
                            "containers": r["container_list"],
                            "ts": time.time()
                        }
                    for svc in services:
                        image = r["containers"].get(svc["service_name"])
                        st = result["services"].setdefault(svc["name"],
                                                           {"status": "not_deployed",
                                                            "version": "", "instances": -1,
                                                            "ts": time.time()})
                        if image:
                            m = re.search(r":(\d+\.\d+\.\d+)$", image)
                            st["version"] = m.group(1) if m else image.rsplit("/", 1)[-1]
                            st["status"] = "online"
                            # 服务运行时长（从容器状态中按 service_name 匹配）
                            ext_cs = (per_server_extended.get(s["host"]) or {}).get("container_status") or {}
                            c_info = ext_cs.get(svc["service_name"])
                            if c_info and c_info.get("uptime_hours", -1) >= 0:
                                st["uptime_hours"] = c_info["uptime_hours"]
                            # 记录该服务在此服务器可达，用于后续 HTTP 健康检查
                            health_hosts.setdefault(svc["name"], []).append(s["host"])
                        else:
                            # 容器存在但未运行（docker ps -a 命中 Exited/Created）→ stopped，
                            # 区分「部署过但已停止」与「从未部署」；仅当尚无在线实例时标记，
                            # 避免多服务器场景下停止实例覆盖在线状态
                            s_img = (r.get("containers_stopped") or {}).get(svc["service_name"])
                            if s_img and st.get("status") in (None, "not_deployed"):
                                m = re.search(r":(\d+\.\d+\.\d+)$", s_img)
                                st["version"] = m.group(1) if m else s_img.rsplit("/", 1)[-1]
                                st["status"] = "stopped"
        result["host_metrics"] = per_server_metrics
        # 扩展指标节流时保留上次采集结果：skip_extended 轮次不采集扩展指标，
        # 如果不从上一轮缓存回填，前端会看到数据"闪现几秒就消失"（缓存被空 dict 覆盖）
        if not per_server_extended and not skip_extended:
            pass  # 本轮采集了但全部为空（可能全部失败），正常写入空
        elif not per_server_extended:
            # 本轮跳过了采集 → 从上次缓存回填，保证前端持续有数据
            prev_cache = self._cache.get(env["id"])
            if prev_cache:
                per_server_extended = dict(prev_cache.get("extended_metrics") or {})
        result["extended_metrics"] = per_server_extended
        # SSH 全部失败时服务状态标 probe_failed（区分"确实未部署"与"探测不到"，
        # 避免把运行中的服务误报为未部署）
        if result["servers"] and not any(e["ssh_ok"] for e in result["servers"]):
            for svc in services:
                result["services"].setdefault(svc["name"],
                                               {"status": "probe_failed",
                                                "version": "", "instances": -1,
                                                "ts": time.time()})
        # Nacos 实例数（与 SSH 同池并发：N 服务 × 10s 串行会阻塞调度循环与 refresh）
        online = [s for s in services
                  if result["services"].get(s["name"], {}).get("status") == "online"]
        if online:
            with ThreadPoolExecutor(max_workers=min(8, len(online))) as pool:
                futs = {pool.submit(_probe_nacos_instances, env, s["service_name"],
                                    s.get("nacos_groups") or []): s for s in online}
                for fut, s in futs.items():
                    try:
                        result["services"][s["name"]]["instances"] = fut.result(timeout=20)
                    except Exception:
                        result["services"][s["name"]]["instances"] = -1
        # HTTP 健康检查（在线服务，并发探测）+ 响应时间持久化
        if online:
            health_futures = {}
            with ThreadPoolExecutor(max_workers=min(8, len(online))) as pool:
                for svc in online:
                    hp = svc.get("health_port") or 0
                    hu = svc.get("health_url") or "/"
                    if hp > 0:
                        for host in health_hosts.get(svc["name"], [])[:2]:  # 最多探测 2 台
                            health_futures[pool.submit(_probe_health_url, host, hp, hu)] = (
                                svc["name"], host)
                for fut, (svc_name, host) in health_futures.items():
                    try:
                        hr = fut.result(timeout=10)
                        if hr["ok"]:
                            result["health_checks"][svc_name] = hr
                        # 持久化响应时间（无论成功失败都记录）
                        try:
                            dbm.save_response_time(
                                self._db_path, env["id"], svc_name, host,
                                hr["ms"] if hr["ms"] > 0 else -1, hr["ok"])
                        except Exception:
                            pass
                    except Exception:
                        pass
        # 持久化扩展指标快照
        for host, ext in per_server_extended.items():
            try:
                dbm.save_monitor_snapshot(self._db_path, env["id"], host, ext)
            except Exception:
                pass
        # 安全评分（SSH 密钥配置率）
        result["security"] = _compute_security(servers)
        with self._lock:
            self._cache[env["id"]] = result

    @staticmethod
    def _merge_extended(existing: dict, new_data: dict) -> dict:
        """合并同一服务器跨环境的扩展指标：container_status 取并集（不同环境部署不同服务），
        其余指标（swap/net/tcp/...）取非空值覆盖。

        解决 bug：同一台服务器属于多个环境时，后处理的环境若未部署匹配容器，
        空 container_status 会覆盖前面环境已匹配到的容器数据（导致运行时长永远为空）。
        """
        if not existing:
            return dict(new_data)
        merged = dict(existing)
        # container_status 按服务名合并（不同环境可能部署不同服务）
        old_cs = merged.get("container_status") or {}
        new_cs = (new_data.get("container_status") or {})
        if new_cs:
            merged_cs = dict(old_cs)
            merged_cs.update(new_cs)
            merged["container_status"] = merged_cs
        # 其余指标：非空则覆盖
        for k, v in new_data.items():
            if k == "container_status":
                continue
            if v is not None and v != {} and v != []:
                merged[k] = v
        return merged

    def _aggregate_all_env(self, project_id: int) -> dict:
        """聚合项目下所有环境的探测缓存，用于"全部"视图
        服务器按 host 去重（取最新 ts），服务取最佳状态，指标合并。"""
        envs = dbm.list_environments(self._db_path, project_id)
        if not envs:
            return {"services": {}, "servers": [], "ts": 0, "env": "",
                    "resources": {}, "health_checks": {},
                    "security": {"score": None, "key_ratio": None, "total": 0},
                    "host_metrics": {}, "extended_metrics": {}}
        all_servers = {}  # host -> entry (取最新 ts)
        all_services = {}
        all_resources = {}
        all_health = {}
        all_hm = {}
        all_ext = {}
        latest_ts = 0
        for env_obj in envs:
            with self._lock:
                data = self._cache.get(env_obj["id"])
            if not data:
                continue
            latest_ts = max(latest_ts, data.get("ts", 0))
            # 服务器按 host 去重，保留最新
            for sv in data.get("servers", []):
                h = sv["host"]
                if h not in all_servers or sv.get("ts", 0) > all_servers[h].get("ts", 0):
                    all_servers[h] = sv
            # 服务状态合并：online > probe_failed > stopped > not_deployed
            for svc_name, svc_data in data.get("services", {}).items():
                existing = all_services.get(svc_name)
                if existing is None or self._svc_priority(svc_data) > self._svc_priority(existing):
                    all_services[svc_name] = svc_data
            all_resources.update(data.get("resources", {}))
            all_health.update(data.get("health_checks", {}))
            all_hm.update(data.get("host_metrics", {}))
            # 扩展指标跨环境合并（container_status 取并集，避免空数据覆盖）
            for host, ext in (data.get("extended_metrics") or {}).items():
                all_ext[host] = self._merge_extended(all_ext.get(host), ext)
        return {"services": all_services, "servers": list(all_servers.values()),
                "ts": latest_ts, "env": "",
                "resources": all_resources, "health_checks": all_health,
                "security": {"score": None, "key_ratio": None, "total": len(all_servers)},
                "host_metrics": all_hm, "extended_metrics": all_ext}

    @staticmethod
    def _svc_priority(svc_data: dict) -> int:
        """服务状态优先级：online=3 > probe_failed=2 > stopped=1 > not_deployed=0
        stopped（容器存在但停止）信息量高于 not_deployed（从未部署）。"""
        st = svc_data.get("status", "")
        return (3 if st == "online" else 2 if st == "probe_failed"
                else 1 if st == "stopped" else 0)

    def _aggregate_all_projects(self) -> dict:
        """聚合所有项目所有环境的探测缓存，用于监控中心全局视图（无项目/环境筛选）"""
        with self._lock:
            cache_copy = {k: dict(v) for k, v in self._cache.items()}
        if not cache_copy:
            return {"services": {}, "servers": [], "ts": 0, "env": "",
                    "resources": {}, "health_checks": {},
                    "security": {"score": None, "key_ratio": None, "total": 0},
                    "host_metrics": {}, "extended_metrics": {}}
        all_servers = {}  # host -> entry (取最新 ts)
        all_services = {}
        all_resources = {}
        all_health = {}
        all_hm = {}
        all_ext = {}
        latest_ts = 0
        for data in cache_copy.values():
            latest_ts = max(latest_ts, data.get("ts", 0))
            for sv in data.get("servers", []):
                h = sv["host"]
                if h not in all_servers or sv.get("ts", 0) > all_servers[h].get("ts", 0):
                    all_servers[h] = sv
            for svc_name, svc_data in data.get("services", {}).items():
                existing = all_services.get(svc_name)
                if existing is None or self._svc_priority(svc_data) > self._svc_priority(existing):
                    all_services[svc_name] = svc_data
            all_resources.update(data.get("resources", {}))
            all_health.update(data.get("health_checks", {}))
            all_hm.update(data.get("host_metrics", {}))
            # 扩展指标跨环境合并（container_status 取并集，避免空数据覆盖）
            for host, ext in (data.get("extended_metrics") or {}).items():
                all_ext[host] = self._merge_extended(all_ext.get(host), ext)
        return {"services": all_services, "servers": list(all_servers.values()),
                "ts": latest_ts, "env": "",
                "resources": all_resources, "health_checks": all_health,
                "security": {"score": None, "key_ratio": None, "total": len(all_servers)},
                "host_metrics": all_hm, "extended_metrics": all_ext}

    def get_overview(self, project_id: int = None, env: str = None) -> dict:
        """组装大屏数据：{services: {name: {...}}, servers: [...]}；无缓存时返回空结构
        project_id 为空时聚合所有项目所有环境；
        env 为空或 'all' 时聚合项目下全部环境。"""
        if not project_id:
            return self._aggregate_all_projects()
        envs = dbm.list_environments(self._db_path, project_id)
        if not envs:
            return {"services": {}, "servers": [], "ts": 0}
        # "全部"视图：聚合所有环境
        if not env or env == "all":
            return self._aggregate_all_env(project_id)
        env_obj = next((e for e in envs if e["name"] == env), None)
        if env_obj is None:
            env_obj = next((e for e in envs if e["is_default"]), envs[0])
        with self._lock:
            data = self._cache.get(env_obj["id"])
        if not data:
            return {"services": {}, "servers": [], "ts": 0, "env": env_obj["name"],
                    "resources": {}, "health_checks": {},
                    "security": {"score": None, "key_ratio": None, "total": 0},
                    "host_metrics": {}, "extended_metrics": {}}
        return {"services": data["services"], "servers": data["servers"],
                "ts": data["ts"], "env": env_obj["name"],
                "resources": data.get("resources", {}),
                "health_checks": data.get("health_checks", {}),
                "security": data.get("security", {"score": None, "key_ratio": None, "total": 0}),
                "host_metrics": data.get("host_metrics", {}),
                "extended_metrics": data.get("extended_metrics", {})}

    def get_servers_status(self) -> dict:
        """聚合各环境探测缓存：host → {ok, ts, error}（供服务器列表连接状态展示）"""
        with self._lock:
            cache = {k: dict(v) for k, v in self._cache.items()}
        status = {}
        for data in cache.values():
            for s in data.get("servers", []):
                status[s["host"]] = {"ok": bool(s.get("ssh_ok")),
                                     "ts": s.get("ts"),
                                     "error": s.get("error", "")}
        return status

    def refresh(self):
        """手动触发一次全量探测（同步，环境间并发）"""
        projects = dbm.list_records(self._db_path, "projects")
        tasks = []
        for proj in projects:
            for env in dbm.list_environments(self._db_path, proj["id"]):
                if env.get("monitor_enabled"):
                    tasks.append((proj, env))
        self._probe_envs(tasks)


_monitor = None


def get_monitor() -> "MonitorScheduler":
    global _monitor
    return _monitor


def init_monitor(app) -> None:
    global _monitor
    if _monitor is None:
        _monitor = MonitorScheduler(app.config["DB_PATH"],
                                    app.config.get("MONITOR_INTERVAL", 30))
    _monitor.start()
