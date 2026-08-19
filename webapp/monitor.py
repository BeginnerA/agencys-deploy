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


def _ssh_exec(host: str, port: int, ssh_cfg: dict, cmd: str, timeout: int = 10) -> str:
    """paramiko 执行远端命令，返回 stdout；失败抛异常"""
    import os
    import pathlib
    import paramiko
    password = ssh_cfg.get("password", "") or None
    key_file = ssh_cfg.get("key_file", "") or None
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = dict(hostname=host, port=int(port or 22), username=ssh_cfg.get("user", "root"),
              timeout=timeout)
    if password:
        kw["password"] = password
    if key_file:
        # paramiko 5.x 加载不存在的 key 文件直接抛异常，会跳过本可用的密码认证；
        # 故 key 文件不存在时跳过密钥，仅用密码（与 scripts/common.py 密钥回退语义一致）
        key_file = str(key_file).replace("~", str(pathlib.Path.home()))
        if os.path.isfile(key_file):
            kw["key_filename"] = key_file
    try:
        client.connect(**kw)
        _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", "ignore")
        err = stderr.read().decode("utf-8", "ignore")
        return out or err
    finally:
        client.close()


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


def _probe_server(host: str, port: int, ssh_cfg: dict, services: list) -> dict:
    """探测单台服务器：SSH 连通性 + 各服务容器镜像；返回 {ssh_ok, containers: {service: image}, error}"""
    try:
        out = _ssh_exec(host, port, ssh_cfg, "docker ps --format '{{.Names}}|{{.Image}}'")
        containers = {}
        for line in out.splitlines():
            if "|" not in line:
                continue
            name, image = line.split("|", 1)
            for s in services:
                if name.strip() == s["service_name"]:
                    containers[s["service_name"]] = image.strip()
        return {"ssh_ok": True, "containers": containers, "error": ""}
    except Exception as e:
        return {"ssh_ok": False, "containers": {}, "error": str(e)[:120]}


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
    返回 {"score": int, "key_ratio": float, "total": int}"""
    if not servers:
        return {"score": 95, "key_ratio": 1.0, "total": 0}
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


class MonitorScheduler:
    """守护线程周期探测；结果缓存 _cache: {env_id: {"servers": [...], "services": {...}, "ts": ...}}"""

    def __init__(self, db_path: str, interval: int = 30):
        self._db_path = db_path
        self._interval = max(10, int(interval))
        self._cache = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="monitor-scheduler")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._cycle()
            except Exception as e:
                # 调度器自愈：单周期异常不退出；打日志保证监控故障可观测
                print(f"[monitor] 探测周期异常: {e!r}", file=sys.stderr)
            self._stop.wait(self._interval)

    def _cycle(self):
        projects = dbm.list_records(self._db_path, "projects")
        for proj in projects:
            for env in dbm.list_environments(self._db_path, proj["id"]):
                if not env.get("monitor_enabled"):
                    continue
                self._probe_env(proj, env)

    def _probe_env(self, proj: dict, env: dict):
        services = dbm.list_services(self._db_path, proj["id"])
        servers = dbm.list_env_servers(self._db_path, env["id"])
        ssh_cfg = env.get("ssh") or {}
        result = {"ts": time.time(), "servers": [], "services": {},
                  "resources": {}, "health_checks": {}}
        health_hosts = {}  # {svc_name: [host, ...]} 用于健康检查
        per_server_metrics = {}  # {host: {cpu, mem, disk, load}} 按服务器存储主机指标
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_probe_server, s["host"], s["ssh_port"],
                                   _merge_server_ssh(ssh_cfg, s), services): s for s in servers}
            for fut, s in futures.items():
                try:
                    r = fut.result(timeout=15)
                except Exception:
                    # 与 Nacos 分支对齐：单服务器超时降级，不中断整轮探测
                    r = {"ssh_ok": False, "containers": {}, "error": "timeout"}
                entry = {"host": s["host"], "ssh_ok": r["ssh_ok"],
                         "containers": len(r["containers"]), "ts": time.time(),
                         "error": r["error"]}
                result["servers"].append(entry)
                if r["ssh_ok"]:
                    # 收集资源使用率（docker stats）
                    try:
                        stats_out = _ssh_exec(s["host"], s["ssh_port"],
                                              _merge_server_ssh(ssh_cfg, s),
                                              "docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemPerc}}'",
                                              timeout=10)
                        res = _parse_docker_resources(stats_out, services)
                        result["resources"].update(res)
                    except Exception:
                        pass
                    # 主机资源指标（CPU/内存/磁盘/负载）—— 按服务器独立存储
                    try:
                        hm_cmd = ("grep -c processor /proc/cpuinfo; "
                                  "cat /proc/loadavg; "
                                  "vmstat 1 2 | tail -1; "
                                  "awk '/MemTotal|MemFree|Buffers|Cached/' /proc/meminfo; "
                                  "df -P / | tail -1")
                        hm_out = _ssh_exec(s["host"], s["ssh_port"],
                                           _merge_server_ssh(ssh_cfg, s), hm_cmd, timeout=10)
                        hm = _parse_host_metrics(hm_out)
                        per_server_metrics[s["host"]] = hm
                    except Exception:
                        pass
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
                            # 记录该服务在此服务器可达，用于后续 HTTP 健康检查
                            health_hosts.setdefault(svc["name"], []).append(s["host"])
        result["host_metrics"] = per_server_metrics
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
        # HTTP 健康检查（在线服务，并发探测）
        if online:
            health_futures = {}
            with ThreadPoolExecutor(max_workers=min(8, len(online))) as pool:
                for svc in online:
                    hp = svc.get("health_port") or 0
                    hu = svc.get("health_url") or ""
                    if hp > 0 and hu:
                        for host in health_hosts.get(svc["name"], [])[:2]:  # 最多探测 2 台
                            health_futures[pool.submit(_probe_health_url, host, hp, hu)] = svc["name"]
                for fut, svc_name in health_futures.items():
                    try:
                        hr = fut.result(timeout=10)
                        if hr["ok"]:
                            result["health_checks"][svc_name] = hr
                    except Exception:
                        pass
        # 安全评分（SSH 密钥配置率）
        result["security"] = _compute_security(servers)
        with self._lock:
            self._cache[env["id"]] = result

    def _aggregate_all_env(self, project_id: int) -> dict:
        """聚合项目下所有环境的探测缓存，用于"全部"视图
        服务器按 host 去重（取最新 ts），服务取最佳状态，指标合并。"""
        envs = dbm.list_environments(self._db_path, project_id)
        if not envs:
            return {"services": {}, "servers": [], "ts": 0, "env": "",
                    "resources": {}, "health_checks": {},
                    "security": {"score": 95, "key_ratio": 1.0, "total": 0},
                    "host_metrics": {}}
        all_servers = {}  # host -> entry (取最新 ts)
        all_services = {}
        all_resources = {}
        all_health = {}
        all_hm = {}
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
            # 服务状态合并：online > probe_failed > not_deployed
            for svc_name, svc_data in data.get("services", {}).items():
                existing = all_services.get(svc_name)
                if existing is None or self._svc_priority(svc_data) > self._svc_priority(existing):
                    all_services[svc_name] = svc_data
            all_resources.update(data.get("resources", {}))
            all_health.update(data.get("health_checks", {}))
            all_hm.update(data.get("host_metrics", {}))
        return {"services": all_services, "servers": list(all_servers.values()),
                "ts": latest_ts, "env": "",
                "resources": all_resources, "health_checks": all_health,
                "security": {"score": 95, "key_ratio": 1.0, "total": len(all_servers)},
                "host_metrics": all_hm}

    @staticmethod
    def _svc_priority(svc_data: dict) -> int:
        """服务状态优先级：online=2, probe_failed=1, not_deployed=0"""
        st = svc_data.get("status", "")
        return 2 if st == "online" else 1 if st == "probe_failed" else 0

    def get_overview(self, project_id: int, env: str = None) -> dict:
        """组装大屏数据：{services: {name: {...}}, servers: [...]}；无缓存时返回空结构
        env 为空或 'all' 时聚合全部环境。"""
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
                    "security": {"score": 95, "key_ratio": 1.0, "total": 0},
                    "host_metrics": {}}
        return {"services": data["services"], "servers": data["servers"],
                "ts": data["ts"], "env": env_obj["name"],
                "resources": data.get("resources", {}),
                "health_checks": data.get("health_checks", {}),
                "security": data.get("security", {"score": 95, "key_ratio": 1.0, "total": 0}),
                "host_metrics": data.get("host_metrics", {})}

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
        """手动触发一次全量探测（同步，最多 20s）"""
        projects = dbm.list_records(self._db_path, "projects")
        for proj in projects:
            for env in dbm.list_environments(self._db_path, proj["id"]):
                if env.get("monitor_enabled"):
                    self._probe_env(proj, env)


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
