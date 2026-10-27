"""实时探测调度器：周期 SSH 容器状态 + Nacos 实例健康，内存缓存供大屏轮询"""
import json
import re
import shlex
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from . import db as dbm
from . import events
from .jar_probe import build_probe_cmd, parse_probe_output

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


def _probe_health_via_ssh(host: str, ssh_port: int, ssh_cfg: dict,
                          checks: list, timeout: int = 15) -> list:
    """通过 SSH 在目标服务器本地探测服务健康（对 127.0.0.1:port）。

    现实项目里微服务业务端口通常只在宿主机 localhost / 内网暴露，云安全组不对
    公网放行，从监控机直连必然超时（历史 response_time_history 全为 -1/0 即此因）。
    改为复用 _probe_server_full 已建立的 SSH 连接，在服务器本地一次性批量探测该主机
    上的全部在线服务端口，规避外部网络不可达问题。

    checks: [(svc_name, health_port, health_url), ...]
    返回 [(svc_name, {"ok": bool, "ms": int, "method": str}), ...]。
    分层（均在 localhost 执行）：有 health_url → curl 严格（须 200）；
    无 health_url → curl 宽松（任意 HTTP 响应码即存活）→ /dev/tcp 端口连通兜底。
    各检查后台并行执行（&）+ wait，总耗时≈单个最慢检查而非累加。
    """
    if not checks:
        return []
    bodies = []
    for name, hp, hu in checks:
        qn = shlex.quote(str(name))
        hp = int(hp)
        if hu:
            url = hu.replace("{ip}", "127.0.0.1")
            target = shlex.quote(f"http://127.0.0.1:{hp}{url}")
            bodies.append(
                f"N={qn}; T=$(date +%s%3N); C=$(curl -s -o /dev/null -m 3 -w '%{{http_code}}' {target} 2>/dev/null); "
                f"E=$(date +%s%3N); echo \"$N|http_strict|$((E-T))|$C\""
            )
        else:
            target = shlex.quote(f"http://127.0.0.1:{hp}/")
            bodies.append(
                f"N={qn}; T=$(date +%s%3N); C=$(curl -s -o /dev/null -m 3 -w '%{{http_code}}' {target} 2>/dev/null); "
                f"E=$(date +%s%3N); "
                f"if [ -n \"$C\" ] && [ \"$C\" != 000 ]; then echo \"$N|http_loose|$((E-T))|$C\"; "
                f"else T2=$(date +%s%3N); timeout 2 bash -c 'exec 3<>/dev/tcp/127.0.0.1/{hp}' 2>/dev/null "
                f"&& E2=$(date +%s%3N) && echo \"$N|tcp|$((E2-T2))|1\" || echo \"$N|down|-1|0\"; fi"
            )
    cmd = " ".join(f"( {b} ) &" for b in bodies) + " wait"
    try:
        out = _ssh_exec(host, ssh_port, ssh_cfg, cmd, timeout=timeout)
    except Exception:
        return []
    results = []
    for line in out.splitlines():
        line = line.strip()
        if line.count("|") < 3:
            continue
        name, method, ms_s, extra = line.split("|", 3)
        try:
            ms = int(float(ms_s))
        except ValueError:
            ms = -1
        if method == "http_strict":
            ok = (extra.strip() == "200")
        elif method in ("http_loose", "tcp"):
            ok = True
        else:  # down
            ok = False
        if ok and ms < 1:
            ms = 1  # localhost 亚毫秒连接按 1ms 计，保证前端有值可显
        results.append((name, {"ok": ok, "ms": ms if ok else -1, "method": method}))
    return results


def _probe_server_full(host: str, port: int, ssh_cfg: dict, service_names: set,
                       timeout: int = 10, skip_extended: bool = False,
                       units: dict = None) -> dict:
    """单连接探测整台服务器：容器 + 主机指标 + 扩展指标 + jar/systemd 单元

    units: {unit: {"app_home": ...}}，非空时在第一批命令末尾追加单元探测，
    结果写入 result["jar_probe"]（uptime/clk_tck/units）。

    返回 {ssh_ok, containers, containers_stopped, container_list, container_status,
         resources, host_metrics, extended, jar_probe, error}。
    第一轮：2 条命令（ps -a 一次覆盖全部容器状态 + 仅服务容器的 stats/主机指标），
    含 jar 单元时追加第 3 条（systemctl/版本/进程 stat），1 次 SSH 连接；
    第二轮：扩展指标 4 条命令，1 次 SSH 连接。
    service_names 为需采集资源指标的容器名集合（跨环境合并后的并集）。
    container_status 每轮从第一轮 ps -a 输出构建（节流轮也保持运行时长新鲜）。
    skip_extended=True 时跳过第二轮（节流，降低 SSH 频率）。
    """
    result = {"ssh_ok": False, "containers": {}, "container_list": [],
              "containers_stopped": {}, "container_status": {}, "resources": {},
              "host_metrics": {}, "extended": {}, "jar_probe": {}, "error": ""}
    try:
        # 第一轮：基础指标。ps -a 一次覆盖运行中/已停止（原 ps + ps -a 两次
        # dockerd 调用合并为一次，并用 shell 变量复用输出避免重复调用 dockerd）；
        # docker stats 仅对「运行中且属于配置服务」的容器采样（原为全容器采样，
        # 容器多时 dockerd CPU 采样是服务器端主要负载），@@STATS@@ 分段
        stats_fmt = ("docker stats --no-stream "
                     "--format '{{.Name}}|{{.CPUPerc}}|{{.MemPerc}}'")
        if service_names:
            grep_args = " ".join("-e " + shlex.quote(n) for n in sorted(service_names))
            filter_stats = ("N=$(echo \"$PS\" | awk -F'|' '$2 ~ /^Up/ {print $1}' "
                            "| grep -xF " + grep_args + "); "
                            "[ -n \"$N\" ] && " + stats_fmt + " $N")
        else:
            filter_stats = "true"
        base_cmd = ("PS=$(docker ps -a --format '{{.Names}}|{{.Status}}|{{.Image}}'); "
                    "echo \"$PS\"; echo '@@STATS@@'; " + filter_stats)
        # 主机指标：/proc/stat 累计值（CPU 使用率由调度器跨轮差分计算，取代
        # vmstat 1 2 的 1s 阻塞采样——被监控服务器上少一次 sleep 1 采样窗口）
        host_cmd = ("head -1 /proc/stat; "
                    "cat /proc/loadavg; "
                    "grep -c processor /proc/cpuinfo; "
                    "awk '/MemTotal|MemFree|Buffers|Cached/' /proc/meminfo; "
                    "df -P / | tail -1")
        unit_cmd, unit_token = (build_probe_cmd(units) if units else (None, None))
        cmds = [base_cmd, host_cmd] + ([unit_cmd] if unit_cmd else [])
        outs = _ssh_exec_multi(host, port, ssh_cfg, cmds, timeout=timeout)
        base_out, hm_out = outs[0], outs[1]
        if unit_cmd:
            result["jar_probe"] = parse_probe_output(
                outs[2] if len(outs) > 2 else "", unit_token, list(units))
        ps_part, _, stats_out = base_out.partition("@@STATS@@")
        containers = {}
        container_names = []
        containers_stopped = {}
        for line in ps_part.splitlines():
            parts = line.split("|")
            if len(parts) < 3:
                continue
            name, status, image = parts[0].strip(), parts[1].strip(), parts[2].strip()
            if not name:
                continue
            if status.startswith("Up"):
                container_names.append(name)  # 运行中容器（供日志采集复用）
                if name in service_names:
                    containers[name] = image
            elif name in service_names:
                # 已停止容器（Exited/Created/Restarting）：仅登记配置服务同名容器
                # （基础设施容器不关心停止与否），区分「部署过已停止」与「从未部署」
                containers_stopped[name] = image
        result["containers"] = containers
        result["containers_stopped"] = containers_stopped
        result["container_list"] = container_names
        result["container_status"] = _parse_container_status(ps_part)
        result["resources"] = _parse_docker_resources(stats_out, service_names)
        result["host_metrics"] = _parse_host_metrics(hm_out)
        result["ssh_ok"] = True
        # 第二轮：扩展指标（swap/网络/diskIO/TCP/docker_df）
        # skip_extended 时节流跳过，降低 SSH 频率（每 N 轮才采集一次）
        if not skip_extended:
            try:
                ext_cmds = _build_extended_cmds()
                sw_out, netdio_out, tcp_out, ddf_out = _ssh_exec_multi(
                    host, port, ssh_cfg, ext_cmds, timeout=25)
                result["extended"] = {
                    "swap": _parse_swap(sw_out),
                    **_parse_network(netdio_out),
                    **_parse_diskio(netdio_out),
                    "tcp": _parse_tcp(tcp_out),
                    "docker_df": _parse_docker_df(ddf_out),
                }
            except Exception as ext_err:
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


def _parse_docker_resources(out: str, service_names: set) -> dict:
    """解析 docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemPerc}}' 输出
    （仅保留 service_names 集合中的服务容器）
    返回 {service_name: {"cpu": float, "mem": float}}"""
    resources = {}
    for line in out.splitlines():
        if "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        name = parts[0].strip()
        if name not in service_names:
            continue
        try:
            cpu = float(parts[1].strip().rstrip("%"))
            mem = float(parts[2].strip().rstrip("%"))
            resources[name] = {"cpu": cpu, "mem": mem}
        except (ValueError, IndexError):
            pass
    return resources


_http_local = threading.local()


def _http_session() -> requests.Session:
    """每线程复用一个 requests.Session：健康检查连接池复用（避免每次新建 TCP），
    连接错误重试 1 次（服务重启后 keep-alive 旧连接可能已失效，防止误判失败）"""
    session = getattr(_http_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_maxsize=16, max_retries=1)
        session.mount("http://", adapter)
        _http_local.session = session
    return session


def _probe_tcp_port(host: str, port: int, timeout: int = 3) -> dict:
    """TCP 端口连通性探测：仅判断端口是否在接受连接，不要求任何业务接口。
    适用于未提供健康检查端点的服务，返回 {"ok": bool, "ms": int, "method": "tcp"}"""
    import socket
    try:
        t0 = time.time()
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        ms = int((time.time() - t0) * 1000)
        return {"ok": True, "ms": ms, "method": "tcp"}
    except (socket.timeout, ConnectionRefusedError, OSError):
        return {"ok": False, "ms": -1, "method": "tcp"}


def _probe_health_url(host: str, port: int, url: str, timeout: int = 5,
                     strict: bool = True) -> dict:
    """HTTP 健康检查，返回 {"ok": bool, "ms": int, "method": str}

    strict=True：专用健康端点模式，要求 200（配了 health_url 时）；
    strict=False：宽松存活模式，未配专用接口时探测根路径，只要服务器
                 返回任意 HTTP 状态码（含 404/401/5xx）即说明应用层在响应，
                 视为存活；连接失败/超时才判不健康。"""
    if not url.startswith("/"):
        url = "/" + url
    full_url = f"http://{host}:{port}{url}"
    method = "http_strict" if strict else "http_loose"
    try:
        t0 = time.time()
        r = _http_session().get(full_url, timeout=timeout, allow_redirects=False)
        ms = int((time.time() - t0) * 1000)
        ok = (r.status_code == 200) if strict else True  # 拿到响应即存活
        return {"ok": ok, "ms": ms, "method": method}
    except Exception:
        return {"ok": False, "ms": -1, "method": method}


def _probe_health_adaptive(host: str, port: int, timeout: int = 5) -> dict:
    """自适应健康检查（服务未配专用 health_url 时使用）：
    先尝试宽松 HTTP（任意响应即存活）；HTTP 失败（端口非 HTTP 协议或被
    拦截）再降级 TCP 连通性探测。任一方式成功即视为健康。"""
    loose = _probe_health_url(host, port, "/", timeout, strict=False)
    if loose["ok"]:
        return loose
    # HTTP 不可达（非 HTTP 服务 / 拒绝连接）：降级 TCP 端口探测
    return _probe_tcp_port(host, port, timeout=3)


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

    命令: head -1 /proc/stat; cat /proc/loadavg; grep -c processor /proc/cpuinfo;
    awk '/MemTotal|MemFree|Buffers|Cached/' /proc/meminfo; df -P / | tail -1
    返回 {cpu, mem, disk, load, cpu_raw}：
    cpu 由 cpu_raw 跨轮差分计算（调度器 _apply_cpu_delta），解析阶段为 None；
    cpu_raw={"busy": 累计非空闲, "total": 累计总量}（/proc/stat 前 8 列）。
    失败字段为 None。
    """
    metrics = {"cpu": None, "mem": None, "disk": None, "load": None,
               "cpu_raw": None}
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    # /proc/stat 首行: cpu user nice system idle iowait irq softirq steal...
    for l in lines:
        parts = l.split()
        if parts and parts[0] == "cpu" and len(parts) >= 5:
            try:
                vals = [int(v) for v in parts[1:9]]
                idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                metrics["cpu_raw"] = {"busy": sum(vals) - idle, "total": sum(vals)}
            except ValueError:
                pass
            break
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
    # CPU 核数（grep -c 输出纯数字行）
    ncpu = 1
    for l in lines:
        if l.isdigit():
            ncpu = max(1, int(l))
            break
    # load average（1min）/ CPU 核数 → 百分比
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

def _build_extended_cmds() -> list:
    """构造第二轮 SSH 命令列表：swap + 网络/磁盘IO + TCP + docker_df

    返回 4 条命令的列表，直接传给 _ssh_exec_multi 作为 cmds 参数，
    避免字符串拼接/拆分导致的 shell 语法破坏（for 循环内分号等）。
    相比旧实现 7 条命令的优化：
    - 网络与磁盘 IO 合并一条命令共享一次 sleep 1（原为两次独立 sleep，
      采样窗口串行阻塞 2s）；
    - TCP 分布 1 个 ss + awk 聚合取代 3 个 ss 进程（原每状态起一个 ss）；
    - 删除 SSL 探测（health_hosts 恒为空 → 死代码；证书过期检查已由平台侧
      ssl_monitor 每小时执行，不在被监控服务器上跑 openssl）。
    """
    # 1. Swap（/proc/meminfo）
    c1 = "grep -E 'SwapTotal|SwapFree' /proc/meminfo 2>/dev/null || true"
    # 2. 网络流量 + 磁盘 IO：两次采样共享一次 sleep 1
    #    （_parse_network / _parse_diskio 从同一输出中各取所需行，互不干扰）
    c2 = ("head -5 /proc/net/dev; "
          "grep -E 'sd[a-z]+|vd[a-z]+|xvd[a-z]+' /proc/diskstats 2>/dev/null; "
          "sleep 1; "
          "head -5 /proc/net/dev; "
          "grep -E 'sd[a-z]+|vd[a-z]+|xvd[a-z]+' /proc/diskstats 2>/dev/null")
    # 3. TCP 连接状态分布（单次 ss + awk 聚合，输出带标签计数便于解析）
    c3 = ("ss -tan 2>/dev/null | awk 'NR>1 {c[$1]++} "
          "END {printf \"ESTABLISHED:%d\\nTIME_WAIT:%d\\nCLOSE_WAIT:%d\\n\", "
          "c[\"ESTAB\"]+c[\"ESTABLISHED\"], c[\"TIME-WAIT\"], c[\"CLOSE-WAIT\"]}'")
    # 4. Docker 磁盘占用
    c4 = "docker system df 2>/dev/null || true"
    return [c1, c2, c3, c4]


def _parse_container_status(out: str, services: list = None) -> dict:
    """解析容器重启次数和运行时长（docker ps -a --format '{{.Names}}|{{.Status}}[|{{.Image}}]'）
    返回 {container_name: {"restart_count": int, "uptime_hours": float}}
    包含服务器上所有容器（含未注册为服务的基础设施容器）。
    services 参数保留兼容（解析不区分服务）。"""
    result = {}
    for line in out.splitlines():
        if "|" not in line:
            continue
        parts = line.split("|")
        name = parts[0].strip()
        status = parts[1] if len(parts) > 1 else ""
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
        self._prev_cpu = {}  # {host: /proc/stat 累计值} 跨轮差分算 CPU 使用率
        self._jar_prev = {}      # {(host, unit): {pid, cpu_ticks, ts}} CPU 差分基准
        self._host_versions = {}  # {(project_id, env, service, host): version} 写穿去重

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
        # 采集完成即推一轮信号（事件只带计数不带业务数据，前端自行重取 overview）
        events.publish("monitor", {"round": self._round, "envs": len(tasks)})

    def _probe_envs(self, env_tasks: list):
        """两阶段探测：跨环境主机去重（主机只探一次）→ 环境级结果组装并发

        同一主机可被多个环境绑定（共用测试机）：旧实现每环境各探测一遍全套
        命令（绑定 5 个环境的主机 → 每轮 5 次 SSH + 5 份 docker 命令）；
        现按 (host, port) 去重为主机作业（服务名取各环境并集），主机阶段
        16 并发；环境阶段 4 并发（Nacos/健康检查等平台侧动作，不重复 SSH）。
        """
        if not env_tasks:
            return
        from deploy_strategies.jar_config import (derive_unit_name,
                                                  parse_runtime_config)
        env_ctxs = []
        jar_by_project = {}  # {project_id: [{"unit", "app_home"}, ...]}
        for proj, env in env_tasks:
            services = dbm.list_services(self._db_path, proj["id"], enabled_only=True)
            servers = dbm.list_env_servers(self._db_path, env["id"])
            env_ctxs.append({"proj": proj, "env": env, "services": services,
                             "servers": servers})
            for svc in services:
                if (svc.get("deploy_mode") or "container") != "jar_service":
                    continue
                cfg = parse_runtime_config(svc.get("runtime_config"))
                unit = derive_unit_name(cfg, svc.get("image_name"), svc["name"])
                jar_by_project.setdefault(proj["id"], []).append(
                    {"unit": unit, "app_home": cfg["app_home"]})
        host_jobs = {}  # (host, port) -> {"host","port","ssh_cfg","service_names","jar_units"}
        for ctx in env_ctxs:
            env_ssh = ctx["env"].get("ssh") or {}
            svc_names = [svc["service_name"] for svc in ctx["services"]
                         if svc.get("service_name")
                         and (svc.get("deploy_mode") or "container") == "container"]
            for s in ctx["servers"]:
                key = (s["host"], int(s.get("ssh_port") or 22))
                job = host_jobs.get(key)
                if job is None:
                    job = {"host": s["host"], "port": key[1],
                           "ssh_cfg": _merge_server_ssh(env_ssh, s),
                           "service_names": set(), "jar_units": {}}
                    host_jobs[key] = job
                job["service_names"].update(svc_names)
                for info in jar_by_project.get(ctx["proj"]["id"], []):
                    job["jar_units"].setdefault(
                        info["unit"], {"app_home": info["app_home"]})
        skip_extended = (self._round % 3) != 0
        host_results = self._probe_hosts(host_jobs, skip_extended)
        with ThreadPoolExecutor(max_workers=min(4, len(env_ctxs))) as pool:
            futures = [pool.submit(self._probe_env, ctx, host_results)
                       for ctx in env_ctxs]
            for fut in futures:
                try:
                    # 环境阶段含 Nacos/健康检查（各自内部已有超时兜底），
                    # 外层仅防意外卡死
                    fut.result(timeout=120)
                except Exception as e:
                    print(f"[monitor] 环境探测异常: {e!r}", file=sys.stderr)

    def _probe_hosts(self, host_jobs: dict, skip_extended: bool) -> dict:
        """并发探测去重后的主机集合（单主机单连接），返回 {(host, port): 探测结果}。

        旧实现在每个环境的内部池里探测同一主机，主机负载随绑定环境数倍增；
        去重后一台主机整轮只执行一次 _probe_server_full。
        """
        results = {}
        if not host_jobs:
            return results
        with ThreadPoolExecutor(max_workers=min(16, len(host_jobs))) as pool:
            futures = {pool.submit(_probe_server_full, job["host"], job["port"],
                                   job["ssh_cfg"], job["service_names"],
                                   10, skip_extended,
                                   job.get("jar_units") or None): key
                       for key, job in host_jobs.items()}
            for fut, key in futures.items():
                try:
                    r = fut.result(timeout=30 if skip_extended else 50)
                except Exception:
                    # 与 Nacos 分支对齐：单主机超时降级，不中断整轮探测
                    r = {"ssh_ok": False, "containers": {}, "containers_stopped": {},
                         "container_list": [], "container_status": {},
                         "resources": {}, "host_metrics": {}, "extended": {},
                         "jar_probe": {}, "error": "timeout"}
                if r.get("ssh_ok"):
                    # CPU 使用率：/proc/stat 跨轮差分（取代 vmstat sleep 采样）
                    self._apply_cpu_delta(key[0], r.get("host_metrics") or {})
                    # 缓存容器列表供日志采集复用（省去 log_collector 独立 docker ps）
                    if r.get("container_list"):
                        self._server_containers[key[0]] = {
                            "containers": r["container_list"], "ts": time.time()}
                results[key] = r
        return results

    def _apply_cpu_delta(self, host: str, hm: dict):
        """用 /proc/stat 两轮累计值差分计算 CPU 使用率（取代 vmstat 1s 阻塞采样）

        首次探测无基准（cpu 保持 None，前端显示占位）；计数回绕（主机重启）
        时丢弃基准。cpu_raw 属内部状态，计算后从指标 dict 中移除。
        """
        raw = hm.pop("cpu_raw", None)
        if not raw:
            return
        prev = self._prev_cpu.get(host)
        self._prev_cpu[host] = raw
        if not prev:
            return
        d_total = raw["total"] - prev["total"]
        d_busy = raw["busy"] - prev["busy"]
        if d_total > 0 and d_busy >= 0:
            hm["cpu"] = round(min(100.0, max(0.0, d_busy / d_total * 100)), 1)

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

    def _probe_env(self, ctx: dict, host_results: dict):
        """环境级组装：把去重后的主机探测结果切片为本环境视图并叠加平台侧检查

        ctx: {"proj","env","services","servers"}（_probe_envs 构建）；
        host_results: {(host, port): _probe_server_full 结果}（整轮共享，不重复 SSH）。
        """
        env = ctx["env"]
        services = ctx["services"]
        servers = ctx["servers"]
        result = {"ts": time.time(), "servers": [], "services": {},
                  "resources": {}, "health_checks": {}}
        health_hosts = {}  # {svc_name: [host, ...]} 用于健康检查
        per_server_metrics = {}  # {host: {cpu, mem, disk, load}} 按服务器存储主机指标
        # 扩展指标：先从上一轮缓存回填（节流轮/采集失败轮保证前端持续有数据）；
        # 每轮做 per-host 浅拷贝，避免跨轮对象别名被就地修改
        with self._lock:
            prev_cache = self._cache.get(env["id"])
        per_server_extended = {}
        if prev_cache:
            for h, ext in (prev_cache.get("extended_metrics") or {}).items():
                per_server_extended[h] = dict(ext)
        fresh_hosts = set()  # 本轮扩展指标为新采集的 host（仅这些落库，避免写放大）
        svc_name_set = {svc["service_name"] for svc in services
                        if svc.get("service_name")}
        for s in servers:
            key = (s["host"], int(s.get("ssh_port") or 22))
            r = host_results.get(key)
            if r is None:
                r = {"ssh_ok": False, "containers": {}, "containers_stopped": {},
                     "resources": {}, "host_metrics": {}, "extended": {},
                     "jar_probe": {}, "container_status": {}, "error": "timeout"}
            # 服务资源/状态按本环境服务集合从主机结果中切片（主机结果为跨环境并集）
            matched = sum(1 for n in (r.get("containers") or {}) if n in svc_name_set)
            result["servers"].append(
                {"host": s["host"], "ssh_ok": r["ssh_ok"], "containers": matched,
                 "ts": time.time(), "error": r.get("error", ""),
                 "remark": s.get("remark", "")})
            if not r["ssh_ok"]:
                continue
            for name, val in (r.get("resources") or {}).items():
                if name in svc_name_set:
                    result["resources"][name] = val
            per_server_metrics[s["host"]] = r.get("host_metrics") or {}
            ext = r.get("extended") or {}
            if ext:
                per_server_extended[s["host"]] = ext
                fresh_hosts.add(s["host"])
            # 第一轮 ps -a 的 container_status 每轮新鲜（节流轮也持续更新运行时长/
            # 重启数）；快照仅 fresh_hosts 落库，避免写放大（旧实现 3 轮均回填写库）
            fresh_cs = r.get("container_status") or {}
            cur_ext = per_server_extended.get(s["host"])
            if fresh_cs:
                if cur_ext:
                    merged_cs = dict(cur_ext.get("container_status") or {})
                    merged_cs.update(fresh_cs)
                    cur_ext["container_status"] = merged_cs
                else:
                    cur_ext = {"container_status": fresh_cs}
                    per_server_extended[s["host"]] = cur_ext
            ext_cs = (cur_ext or {}).get("container_status") or {}
            for svc in services:
                if (svc.get("deploy_mode") or "container") in ("static_archive",
                                                               "jar_service"):
                    continue  # 静态托管/纯 Java 进程没有容器，走各自的探测分支
                image = (r.get("containers") or {}).get(svc["service_name"])
                st = result["services"].setdefault(svc["name"],
                                                   {"status": "not_deployed",
                                                    "version": "", "instances": -1,
                                                    "ts": time.time()})
                if image:
                    m = re.search(r":(\d+\.\d+\.\d+)$", image)
                    st["version"] = m.group(1) if m else image.rsplit("/", 1)[-1]
                    st["status"] = "online"
                    # 服务运行时长（从容器状态中按 service_name 匹配）
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
        result["extended_metrics"] = per_server_extended
        jar_services = [svc for svc in services
                        if (svc.get("deploy_mode") or "container") == "jar_service"]
        if jar_services:
            self._merge_jar_services(env, jar_services, servers, host_results,
                                     result, health_hosts)
        # 静态托管服务：无容器/Nacos 可查，配了 health_url 则 HTTP 探活（200 即在线，
        # 静态首页无 UP 体，不能复用 check_http 的 Actuator 语义）；未配置则 unknown
        for svc in services:
            if (svc.get("deploy_mode") or "container") != "static_archive":
                continue
            url = (svc.get("health_url") or "").strip()
            st = {"version": "", "instances": -1, "ts": time.time()}
            if url:
                try:
                    try:
                        target = url.format(ip="")
                    except (KeyError, IndexError, ValueError):
                        target = url  # 含非 {ip} 花括号占位：按原 URL 探测
                    resp = _http_session().get(target, timeout=8)
                    st["status"] = "online" if resp.status_code == 200 else "stopped"
                except Exception:
                    st["status"] = "stopped"
            else:
                st["status"] = "unknown"
            result["services"][svc["name"]] = st
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
        # 健康检查（在线服务）：经 SSH 在服务器本地探测 127.0.0.1:health_port。
        # 微服务业务端口不对公网放行（云安全组只挡外部），从监控机直连必失败，
        # 故复用 _probe_server_full 已建立的 SSH 连接在宿主机 localhost 探测。
        # 分层：有 health_url→curl 严格(须 200)；无→curl 宽松(任意响应码即存活)
        # →/dev/tcp 端口连通兜底。每台主机一次批量 SSH（内部并行），多主机并发。
        if online:
            env_ssh = env.get("ssh") or {}
            server_by_host = {s["host"]: s for s in servers}
            host_checks = {}  # {host: [(svc_name, hp, hu), ...]}
            for svc in online:
                hp = svc.get("health_port") or 0
                if hp <= 0:
                    # jar_service 的健康端口来自 runtime_config.port（聚合时写入视图）
                    hp = (result["services"].get(svc["name"]) or {}).get("health_port", 0) or 0
                hu = (svc.get("health_url") or "").strip()
                if hp <= 0:
                    continue
                for host in health_hosts.get(svc["name"], []):
                    host_checks.setdefault(host, []).append((svc["name"], hp, hu))
            if host_checks:
                def _run_host_checks(host, checks):
                    s = server_by_host.get(host)
                    if not s:
                        return host, []
                    ssh_cfg = _merge_server_ssh(env_ssh, s)
                    sport = int(s.get("ssh_port") or 22)
                    return host, _probe_health_via_ssh(host, sport, ssh_cfg, checks)
                with ThreadPoolExecutor(max_workers=min(8, len(host_checks))) as pool:
                    futs = [pool.submit(_run_host_checks, h, cs)
                            for h, cs in host_checks.items()]
                    for fut in futs:
                        try:
                            host, hr_list = fut.result(timeout=30)
                        except Exception:
                            continue
                        for svc_name, hr in hr_list:
                            # 持久化响应时间（无论成功失败都记录）
                            try:
                                dbm.save_response_time(
                                    self._db_path, env["id"], svc_name, host,
                                    hr["ms"] if hr["ms"] > 0 else -1, hr["ok"])
                            except Exception:
                                pass
                            if hr["ok"] and svc_name not in result["health_checks"]:
                                result["health_checks"][svc_name] = {**hr, "host": host}
        # 持久化扩展指标快照：仅本轮新采集的 host（节流轮与采集失败轮不落库，
        # 快照写放大 3× → 1×）
        for host in fresh_hosts:
            ext = per_server_extended.get(host)
            if not ext:
                continue
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
        events.publish("monitor", {"round": self._round, "envs": len(tasks),
                                   "manual": True})


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
