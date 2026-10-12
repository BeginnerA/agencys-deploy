#!/usr/bin/env python3
"""智能运维平台单服务发布主控脚本

示例：
    python deploy.py --project xxx --version 1.0.29 --previous-version 1.0.28
"""
import os
import sys
import time
import re
import json
import argparse
import tempfile
import subprocess
import threading
from pathlib import Path
import yaml
import paramiko

# 把 scripts 目录加入模块搜索路径
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

from common import load_config, get_project_config, get_ssh_config, get_registry_auth, get_registry_mode, connect_ssh, merge_server_ssh, log, fail, load_yaml, default_servers_file, as_number
from build import build_project
from nacos_manager import NacosClient
from snailjob_manager import backup_jobs, set_job_status, restore_jobs, clear_summary
from health_check import wait_for_healthy
from notify import send_notify
from deploy_strategies import create_strategy


# ---------- 灰度 Web 决策：deploy.py 子进程 ↔ Web 决策 API 的文件协议 ----------

def canary_decision_path(task_id: int) -> Path:
    """灰度决策文件：Web 子进程写请求、Web 决策 API 写结果，均落盘于 data/ 目录"""
    return Path(__file__).resolve().parent / "data" / f"canary_decision_{int(task_id)}.json"


def read_canary_decision(task_id: int) -> dict:
    """读取灰度决策文件；文件不存在或损坏返回 {"state": "none"}"""
    try:
        with open(canary_decision_path(task_id), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"state": "none"}
    except (OSError, ValueError):
        return {"state": "none"}


def wait_canary_decision(task_id: int, timeout_minutes: int = 30,
                         poll_interval: float = 5) -> str:
    """Web 子进程无终端场景：轮询决策文件等待人工决策

    返回 'continue' | 'stop' | 'rollback'；超时（默认 30 分钟）按 stop 处理，
    避免任务无限挂起。
    """
    deadline = time.time() + max(1, int(timeout_minutes)) * 60
    while True:
        state = read_canary_decision(task_id).get("state")
        if state in ("continue", "stop", "rollback"):
            return state
        if time.time() >= deadline:
            log(f"[canary] 等待 Web 决策超时（{timeout_minutes} 分钟），默认停止发布")
            return "stop"
        time.sleep(max(0.2, float(poll_interval)))


def get_current_remote_version(project: str, config: dict, env: str = None) -> str:
    """SSH 查询首台服务器上容器当前镜像 tag；容器不存在返回 None（首次发版场景）"""
    proj = get_project_config(project)
    service_name = proj["service_name"]
    servers_file = default_servers_file(env=env)
    servers = load_yaml(Path(servers_file)).get("servers", [])
    if not servers:
        raise RuntimeError("服务器清单为空，无法查询当前版本")
    first = servers[0]
    ssh_cfg = merge_server_ssh(get_ssh_config(config), first)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        connect_ssh(client, first["ip"], ssh_cfg)
        cmd = "docker ps --filter 'name=^{}$' --format '{{{{.Image}}}}'".format(service_name)
        stdin, stdout, stderr = client.exec_command(cmd, timeout=30)
        image = stdout.read().decode("utf-8", errors="ignore").strip()
        rc = stdout.channel.recv_exit_status()
        if rc != 0:
            raise RuntimeError(f"[{first['ip']}] 查询容器镜像失败: {stderr.read().decode('utf-8', errors='ignore')}")
    finally:
        client.close()
    if not image:
        log(f"[{first['ip']}] 未发现运行中的容器 {service_name}（首次发版场景）")
        return None
    m = re.search(r":(\d+\.\d+\.\d+)$", image)
    if not m:
        raise RuntimeError(f"无法从镜像名解析版本号: {image}")
    return m.group(1)


def bump_patch_version(version: str) -> str:
    """版本号 patch+1：1.0.29 → 1.0.30"""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", version)
    if not m:
        raise ValueError(f"版本号格式非法（应为 x.y.z）: {version}")
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{major}.{minor}.{patch + 1}"


def local_image_id(image: str) -> str:
    """查询本地镜像 ID（sha256:...）；镜像不存在返回 None"""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


def tar_image_id(tar_path: Path) -> str:
    """读取 docker save 导出的 tar/tar.gz 包内镜像 ID（config 文件名）；无法解析返回 None

    "r:*" 模式自动识别 gzip 压缩，普通 tar 与 tar.gz 均可读取。
    """
    try:
        import json
        import tarfile
        with tarfile.open(str(tar_path), "r:*") as tf:
            manifest = json.loads(tf.extractfile("manifest.json").read())
        cfg = manifest[0]["Config"]  # 形如 <sha256 十六进制>.json
        if cfg.endswith(".json"):
            cfg = cfg[:-5]
        return cfg if cfg.startswith("sha256:") else f"sha256:{cfg}"
    except Exception:
        return None


def _resolve_config_source(args) -> str:
    """auto 模式：DB 中存在该项目（任一服务匹配）→ db，否则 yaml"""
    if args.config_source != "auto":
        return args.config_source
    db_path = Path(__file__).resolve().parent / "data" / "platform.db"
    if not db_path.exists():
        return "yaml"
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT s.project_id FROM services s JOIN projects p ON p.id=s.project_id "
                "WHERE s.name=? LIMIT 1", (args.project,)).fetchone()
        finally:
            conn.close()
        return "db" if row else "yaml"
    except Exception:
        return "yaml"


def _switch_to_db_config(args) -> None:
    """db 模式：导出运行时 YAML 到 data/runtime/ 并覆盖 common 配置目录"""
    from webapp.exporter import export_runtime
    db_path = Path(__file__).resolve().parent / "data" / "platform.db"
    if not db_path.exists():
        fail("data/platform.db 不存在，请先通过 Web Dashboard 初始化配置（或使用 yaml 模式）")
    import sqlite3
    if args.project_id is None:
        try:
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT project_id FROM services WHERE name=? LIMIT 1",
                    (args.project,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as e:
            fail(f"data/platform.db 不可用（{e}），请先通过 Web Dashboard 初始化配置（或使用 yaml 模式）")
        if not row:
            fail(f"db 模式未找到服务 {args.project}，请先检查 Dashboard 配置")
        args.project_id = row[0]
    else:
        # 显式 --project-id：先校验项目存在，避免导出空配置后误导性报错
        try:
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT 1 FROM projects WHERE id=? LIMIT 1", (args.project_id,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as e:
            fail(f"data/platform.db 不可用（{e}），请先通过 Web Dashboard 初始化配置（或使用 yaml 模式）")
        if not row:
            fail(f"db 模式未找到项目 ID {args.project_id}，请检查 Dashboard 配置")
    tmp = Path(__file__).resolve().parent / "data" / "runtime" / f"p{args.project_id}" / (args.env or "prod")
    try:
        export_runtime(str(db_path), args.project_id, args.env or "", tmp)
    except ValueError as e:
        fail(f"{e}（可使用 yaml 模式或先配置环境）")
    except sqlite3.Error as e:
        fail(f"data/platform.db 读取失败（{e}），库文件可能损坏或未初始化（可使用 yaml 模式）")
    # 模块级变量覆盖：必须用 `import common`（与顶部 from common import ... 是同一模块实例）；
    # scripts/common 目录无 __init__.py，`import scripts.common` 会是第二个模块实例，覆盖无效
    import common as common_mod
    common_mod.CONFIG_DIR_OVERRIDE = tmp


# 分片并行上传的文件大小下限：小文件单流更快，不值得多开 channel
_SFTP_PARALLEL_MIN_SIZE = 64 * 1024 * 1024


class _ThrottledProgress:
    """上传进度聚合与节流日志：线程安全，每 10% 或每 15 秒至多输出一行。

    单流上传用 set(t) 直接设置累计值（SFTP put 回调语义）；
    分片并行上传各线程调用 add(n) 累加本通道写入量。
    """

    def __init__(self, host: str, label: str, total: int, start_time: float = None):
        self.host = host
        self.label = label
        self.total = total
        self.start = start_time or time.time()
        self._lock = threading.Lock()
        self._transferred = 0
        self._last_pct = 0
        self._last_log = self.start

    def set(self, transferred: int):
        with self._lock:
            self._transferred = transferred
            self._maybe_log()

    def add(self, n: int):
        with self._lock:
            self._transferred += n
            self._maybe_log()

    def _maybe_log(self):
        now = time.time()
        pct = int(self._transferred * 100 / self.total) if self.total else 0
        # 每 10% 或每 15 秒输出一次进度（与历史日志格式保持一致）
        if pct >= self._last_pct + 10 or now - self._last_log >= 15:
            elapsed = now - self.start
            speed = self._transferred / elapsed / (1024 * 1024) if elapsed > 0 else 0
            remaining_mb = (self.total - self._transferred) / (1024 * 1024)
            eta = remaining_mb / speed if speed > 0 else 0
            log(f"[{self.host}] 上传进度 {self.label}: {pct}% "
                f"({self._transferred // (1024 * 1024)}/{self.total // (1024 * 1024)} MB) "
                f"速度 {speed:.1f} MB/s 预计剩余 {eta:.0f}s")
            self._last_log = now
            self._last_pct = pct


class RemoteServer:
    def __init__(self, host: str, ssh_cfg: dict):
        self.host = host
        self.ssh_cfg = ssh_cfg
        self._client = None
        self._sftp = None

    def client(self) -> paramiko.SSHClient:
        if self._client is None:
            self._client = paramiko.SSHClient()
            self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            connect_ssh(self._client, self.host, self.ssh_cfg)
        return self._client

    def sftp(self) -> paramiko.SFTPClient:
        if self._sftp is None:
            # 调大本端 channel 窗口（默认 2MB）：SFTP 下载（日志拉取、compose 同步）
            # 在高延迟链路受窗口限制明显；上传方向流量控制由服务端窗口决定，
            # 本端调优无害，大文件上传提速由 sftp_put_with_progress 分片并行承担
            self.client().get_transport().default_window_size = paramiko.common.MAX_WINDOW_SIZE
            self._sftp = self.client().open_sftp()
            # SFTP 通道设置超时，避免大文件传输时无限挂起（transport 层超时）
            self._sftp.get_channel().settimeout(300)
        return self._sftp

    def sftp_put_with_progress(self, local_path: str, remote_path: str, label: str = "",
                               streams: int = 1):
        """通过 SFTP 上传文件并输出进度日志，避免大文件传输时看起来像卡住。

        label: 日志中显示的文件描述（如镜像包名），为空时使用文件名。
        streams: 并行上传通道数（>1 且文件不小于 _SFTP_PARALLEL_MIN_SIZE 时启用
            分片并行：多个 SFTP channel 各写目标文件一段 offset 区间，突破单
            channel 吞吐上限，高延迟/高带宽链路提速明显；1 = 传统单流上传）。
        """
        file_size = os.path.getsize(local_path)
        if not label:
            label = Path(local_path).name
        size_mb = file_size / (1024 * 1024)
        parallel = streams > 1 and file_size >= _SFTP_PARALLEL_MIN_SIZE
        log(f"[{self.host}] 开始上传 {label}（{size_mb:.1f} MB）-> {remote_path}"
            + (f"（{min(int(streams), 8)} 通道并行）" if parallel else ""))
        start_time = time.time()
        progress = _ThrottledProgress(self.host, label, file_size, start_time)
        if parallel:
            self._sftp_put_parallel(local_path, remote_path, file_size, streams, progress)
        else:
            self.sftp().put(local_path, remote_path,
                            callback=lambda sent, _total: progress.set(sent))
        elapsed = time.time() - start_time
        log(f"[{self.host}] 上传完成 {label}，耗时 {elapsed:.1f}s")

    def _sftp_put_parallel(self, local_path: str, remote_path: str, file_size: int,
                           streams: int, progress: _ThrottledProgress):
        """分片并行上传：先写 .part 临时文件，全部完成后校验大小再改名落正式名。

        每个通道用独立 SFTP channel/file handle 写各自的 offset 区间
        （SFTP 写请求自带 offset，多 handle 并发写同一文件互不覆盖）；
        任一通道失败则清理 .part 并抛异常，由调用方按原有失败语义处理
        （预传输记录失败、滚动发布阶段自动重试）。
        """
        part = remote_path + ".part"
        streams = max(1, min(int(streams), 8))  # 上限 8：OpenSSH MaxSessions 默认 10，给 exec 留余量
        # 先经 sftp() 触发 transport 窗口调优，并清理上次失败可能残留的 .part
        try:
            self.sftp().remove(part)
        except IOError:
            pass
        # 分片边界：前 n-1 片等长，末片收尾
        base = file_size // streams
        ranges = []
        offset = 0
        for i in range(streams):
            end = file_size if i == streams - 1 else offset + base
            ranges.append((offset, end))
            offset = end
        errors = []

        def _upload_range(idx: int):
            seg_start, seg_end = ranges[idx]
            handle = None
            try:
                # 首通道 'wb' 创建并截断 .part；其余通道 'r+b' 不截断，各写各的区间
                handle = self.client().open_sftp().open(part, "wb" if idx == 0 else "r+b")
                handle.set_pipelined(True)  # 写请求流水线化，不逐个等响应
                handle.seek(seg_start)
                with open(local_path, "rb") as f:
                    f.seek(seg_start)
                    remaining = seg_end - seg_start
                    while remaining > 0:
                        chunk = f.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise RuntimeError(f"本地文件 {local_path} 读取提前结束")
                        handle.write(chunk)
                        remaining -= len(chunk)
                        progress.add(len(chunk))
            except Exception as exc:  # 各通道异常汇聚到主线程统一抛出
                errors.append(exc)
            finally:
                if handle is not None:
                    try:
                        # pipelined 模式下服务端写错误在 close 回收响应时暴露
                        handle.close()
                    except Exception as exc:
                        errors.append(exc)

        threads = [threading.Thread(target=_upload_range, args=(i,),
                                    name=f"sftp-put-{self.host}-{i}") for i in range(streams)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            self._remove_quietly(part)
            raise RuntimeError(f"[{self.host}] 并行上传 {Path(part).name} 失败: {errors[0]}")
        remote_size = self.sftp().stat(part).st_size
        if remote_size != file_size:
            self._remove_quietly(part)
            raise RuntimeError(
                f"[{self.host}] 并行上传校验失败: 远端 {remote_size} 字节 != 本地 {file_size} 字节")
        # 落正式名：先清理同版本残留（同 tag 重传场景），目标必不存在，rename 原子生效
        self._remove_quietly(remote_path)
        self.sftp().rename(part, remote_path)

    def _remove_quietly(self, remote_path: str):
        """删除远端文件；不存在或失败均忽略（清理场景不应产生新错误）"""
        try:
            self.sftp().remove(remote_path)
        except Exception:
            pass

    def exec(self, cmd: str, timeout: int = 60, stdin_data: str = None) -> str:
        """远程执行命令；stdin_data 非空时写入子进程 stdin（密码等敏感数据不落命令行）"""
        log(f"[{self.host}] {cmd}")
        stdin, stdout, stderr = self.client().exec_command(cmd, timeout=timeout)
        if stdin_data:
            stdin.write(stdin_data)
            stdin.flush()
        out = stdout.read().decode("utf-8", errors="ignore")
        err = stderr.read().decode("utf-8", errors="ignore")
        rc = stdout.channel.recv_exit_status()
        if rc != 0:
            # 抛异常而非直接退出：让 deploy_server 的异常处理有机会回滚容器/恢复 Nacos/SnailJob
            raise RuntimeError(f"[{self.host}] 命令执行失败 (rc={rc}): {cmd}\nstdout:\n{out}\nstderr:\n{err}")
        return out

    def update_compose_image(self, compose_path: str, service_name: str, image: str):
        """通过 SFTP 下载 docker-compose.yml，修改镜像，再上传"""
        sftp = self.sftp()
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".yml", delete=False) as f:
            local_path = f.name
        try:
            sftp.get(compose_path, local_path)
            with open(local_path, "r", encoding="utf-8") as f:
                doc = yaml.safe_load(f)
            if service_name not in doc.get("services", {}):
                # 抛异常而非直接退出：让 deploy_server 有机会回滚容器/恢复 Nacos/SnailJob
                raise RuntimeError(f"[{self.host}] docker-compose.yml 中未找到服务: {service_name}")
            old_image = doc["services"][service_name].get("image")
            doc["services"][service_name]["image"] = image
            with open(local_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(doc, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
            sftp.put(local_path, compose_path)
            log(f"[{self.host}] {compose_path} 镜像已从 {old_image} 更新为 {image}")
        finally:
            Path(local_path).unlink(missing_ok=True)

    def try_exec(self, cmd: str, timeout: int = 60) -> bool:
        """执行命令并返回是否成功（不抛异常，用于存在性检查/可失败的可选操作）"""
        try:
            self.exec(cmd, timeout=timeout)
            return True
        except Exception:
            return False

    def has_image(self, image: str) -> bool:
        """检查服务器本地是否已存在该镜像"""
        return self.try_exec(f"docker image inspect {image} >/dev/null 2>&1", timeout=15)

    def image_id(self, image: str) -> str:
        """查询远端镜像 ID（sha256:...）；镜像不存在返回 None"""
        try:
            out = self.exec(f"docker image inspect {image} --format '{{{{.Id}}}}'", timeout=15)
            return out.strip() or None
        except Exception:
            return None

    def close(self):
        if self._sftp:
            self._sftp.close()
        if self._client:
            self._client.close()


class Deployer:
    _save_lock = threading.Lock()  # 串行化本地 docker save，避免并行发布时重复导出同一镜像
    # 直传模式传输优化默认值（类级：__init__ 按 deploy 配置覆盖；测试可绕过 __init__ 构造）
    upload_streams = 4        # 镜像包并行上传通道数（1 = 传统单流上传）
    image_compress_level = 6  # docker save 导出 gzip 压缩级别（1-9）
    def __init__(self, project: str, version: str, previous_version: str = None, config: dict = None, env: str = None, canary: bool = False):
        self.project = project
        self.version = version
        self.previous_version = previous_version
        self.config = config
        self.env = env
        self.canary = canary
        self.proj_cfg = get_project_config(project)
        self.ssh_cfg = get_ssh_config(config)
        self.nacos = NacosClient(env=env)
        self.compose_path = config["deploy"]["docker_compose_path"]
        self.registry_mode = get_registry_mode(config)
        self.registry_host = config["registry"]["host"]
        self.registry_namespace = config["registry"]["namespace"]
        self.registry_auth = get_registry_auth(config)
        self.image_name = self.proj_cfg.get("image_name") or project
        self.service_name = self.proj_cfg.get("service_name") or project
        # dist/传输优化字段：容器直传与静态包发布均需，统一设置（1 = 传统单流上传）
        deploy_cfg = config["deploy"]
        self.dist_dir = Path(deploy_cfg.get("dist_dir", "dist"))
        if not self.dist_dir.is_absolute():
            self.dist_dir = Path(__file__).resolve().parent / self.dist_dir
        self.remote_tmp_dir = deploy_cfg.get("remote_tmp_dir", "/tmp/platform-deploy")
        # 并行上传通道数（1-8）与镜像包压缩级别（1-9）；0/空串等非法值回退类默认
        cfg_streams = as_number(deploy_cfg.get("upload_streams"), self.upload_streams)
        self.upload_streams = max(1, min(int(cfg_streams) or self.upload_streams, 8))
        cfg_level = as_number(deploy_cfg.get("image_compress_level"), self.image_compress_level)
        self.image_compress_level = max(1, min(int(cfg_level) or self.image_compress_level, 9))
        if self.registry_mode == "direct":
            # 直传模式：镜像名 = {namespace}/{image_name}:{version}（与 IDEA 直推格式一致）
            # namespace 留空则不带前缀；镜像只存在于本地与目标服务器
            ns = self.registry_namespace
            self.image_prefix = f"{ns}/" if ns else ""
            self.full_image = f"{self.image_prefix}{self.image_name}:{self.version}"
            # 首次发版无回滚版本（previous_version 未指定）时 prev_image 置空，失败则跳过容器回滚
            self.prev_image = self._make_image(self.previous_version) if self.previous_version else None
        else:
            self.full_image = f"{self.registry_host}/{self.registry_namespace}/{self.image_name}:{self.version}"
            # 首次发版无回滚版本（previous_version 未指定）时 prev_image 置空，失败则跳过容器回滚
            self.prev_image = self._make_image(self.previous_version) if self.previous_version else None
        self._docker_logged_in = set()  # 已登录的远程主机，避免重复登录
        self._no_git_tag = False
        self._skip_git_push = False
        # 发布策略：按项目配置 deploy_mode 路由（缺省 container，行为与既有完全一致）
        self._strategy = create_strategy(self.proj_cfg.get("deploy_mode", "container"), self)

    @property
    def strategy(self):
        """发布策略实例（懒兑底：测试绕过 __init__ 构造时首次访问自动回退容器策略）

        容器策略是纯适配层，全部方法委托回 Deployer 既有同名方法，
        既有测试的 patch 点（Deployer 方法、deploy.wait_for_healthy）不受影响。
        """
        if getattr(self, "_strategy", None) is None:
            mode = getattr(self, "proj_cfg", {}) or {}
            self._strategy = create_strategy(mode.get("deploy_mode", "container"), self)
        return self._strategy

    def _make_image(self, version: str) -> str:
        """按当前 registry 模式拼装镜像全名（direct 直传模式不带仓库前缀）"""
        if self.registry_mode == "direct":
            return f"{self.image_prefix}{self.image_name}:{version}"
        return f"{self.registry_host}/{self.registry_namespace}/{self.image_name}:{version}"

    def service_names(self) -> list:
        names = [self.service_name]
        names.extend(self.proj_cfg.get("dubbo_services", []))
        return names

    def nacos_groups(self) -> list:
        """Nacos 分组列表：优先用项目级 nacos_groups，回退到全局配置的单个 group

        全局未配置 Nacos（nacos.url 为空，环境未接入 Nacos）时返回空列表：
        nacos_offline/nacos_online 遍历零次，发布流程自动跳过全部 Nacos 操作。
        """
        if not self.nacos.configured:
            return []
        groups = self.proj_cfg.get("nacos_groups")
        if groups:
            return groups
        return [self.nacos.group]

    def _canary_prompt(self, first_host: str) -> str:
        """金丝雀发布交互确认：首台发布成功后暂停，等待人工决策

        Returns: 'continue' | 'stop' | 'rollback'
        """
        log(f"\n{'=' * 60}")
        log(f"金丝雀模式：首台 {first_host} 已发布成功")
        log(f"请验证服务是否正常，然后选择操作：")
        log(f"  [y] 继续发布剩余服务器")
        log(f"  [n] 停止发布（首台保持新版本）")
        if self.previous_version:
            log(f"  [r] 回滚首台到 {self.previous_version}")
        log(f"{'=' * 60}")
        # Web 子进程无终端：写决策请求文件并轮询等待 Web 端人工决策
        task_id = os.environ.get("PLATFORM_TASK_ID")
        if not sys.stdin.isatty() and task_id:
            try:
                return self._canary_file_decision(int(task_id), first_host)
            except ValueError:
                log(f"[canary] 非法 PLATFORM_TASK_ID: {task_id!r}，默认停止发布")
                return "stop"
        while True:
            try:
                choice = input(">>> 请选择 (y/n{}): ".format("/r" if self.previous_version else "")).strip().lower()
            except (EOFError, KeyboardInterrupt):
                log("\n输入中断，默认停止发布")
                return "stop"
            if choice in ("y", "yes"):
                return "continue"
            elif choice in ("n", "no", ""):
                return "stop"
            elif choice in ("r", "rollback") and self.previous_version:
                return "rollback"
            else:
                log("无效输入，请重新选择")

    def _canary_file_decision(self, task_id: int, first_host: str) -> str:
        """Web 子进程场景的灰度决策：写决策请求文件 → 轮询等待 Web 端结果 → 清理文件

        Web 端通过 POST /api/deploy/canary-decisions/<task_id> 写入 continue/stop/rollback；
        超时默认 30 分钟（CANARY_DECISION_TIMEOUT 环境变量可覆盖），超时按停止处理。
        """
        path = canary_decision_path(task_id)
        timeout = int(os.environ.get("CANARY_DECISION_TIMEOUT", "30"))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({
                    "task_id": task_id,
                    "state": "waiting",
                    "host": first_host,
                    "version": self.version,
                    "previous_version": self.previous_version,
                    "updated_at": int(time.time()),
                }, f, ensure_ascii=False)
            log(f"[canary] 已写入决策请求（task #{task_id}），等待 Web 端确认（超时 {timeout} 分钟）")
            decision = wait_canary_decision(task_id, timeout_minutes=timeout)
            log(f"[canary] Web 端决策: {decision}")
            return decision
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def nacos_offline(self, host: str):
        for svc in self.service_names():
            for grp in self.nacos_groups():
                self.nacos.offline_ip(svc, host, group=grp)
        for svc in self.service_names():
            for grp in self.nacos_groups():
                self.nacos.wait_for_absence(svc, host, timeout=60, group=grp)

    def nacos_online(self, host: str, weight: float = 1.0):
        """上线 Nacos 实例并等待健康；若 online_ip 返回 False（无快照且 list 不可见），跳过该服务/分组的等待"""
        restored_any = False
        for svc in self.service_names():
            for grp in self.nacos_groups():
                ok = self.nacos.online_ip(svc, host, weight, group=grp)
                if ok:
                    restored_any = True
        if not restored_any:
            return  # 所有服务/分组均未找到实例，无需等待健康检查
        for svc in self.service_names():
            for grp in self.nacos_groups():
                self.nacos.wait_for_healthy(svc, host, timeout=120, group=grp)

    def update_container(self, server: RemoteServer):
        # 确保新镜像可用（直传模式: save/scp/load；仓库模式: docker pull）
        if self.registry_mode == "direct":
            self.ensure_image_ready(server)
        else:
            server.exec(f"docker pull {self.full_image}", timeout=120)
        # 镜像就绪后容器已可替换，标记为已修改
        self.container_modified = True
        # 停止并删除旧容器
        server.exec(f"docker compose -f {self.compose_path} stop {self.service_name}", timeout=60)
        server.exec(f"docker compose -f {self.compose_path} rm -f {self.service_name}", timeout=30)
        # 清空宿主机旧应用日志（可选，配置 clear_log_dir 时生效；失败仅告警）
        self.clear_project_logs(server)
        # 修改 docker-compose.yml 并启动
        server.update_compose_image(self.compose_path, self.service_name, self.full_image)
        server.exec(f"docker compose -f {self.compose_path} up -d {self.service_name}", timeout=120)

    def clear_project_logs(self, server: RemoteServer):
        """启动新容器前清空宿主机旧应用日志（可配置 clear_log_dir；省略/留空则不清空）

        仅删除文件、保留目录结构；目录不存在时跳过；清空失败仅告警，不阻断发布。
        """
        log_dir = (self.proj_cfg.get("clear_log_dir") or "").strip()
        if not log_dir:
            log(f"[{server.host}] 未配置 clear_log_dir，跳过旧日志清空")
            return
        cmd = f"if [ -d {log_dir} ]; then find {log_dir} -type f -delete; fi"
        if server.try_exec(cmd, timeout=60):
            log(f"[{server.host}] 旧应用日志已清空: {log_dir}")
        else:
            log(f"[{server.host}] 警告: 清空旧应用日志失败（不影响发布）: {log_dir}")

    def rollback_container(self, server: RemoteServer):
        if not self.previous_version:
            log(f"[{server.host}] 首次发版未指定 --previous-version，无回滚版本，跳过容器回滚")
            return
        log(f"[{server.host}] 开始回滚到 {self.prev_image}")
        if self.registry_mode == "direct":
            self.ensure_image_ready(server, version=self.previous_version)
        else:
            server.exec(f"docker pull {self.prev_image}", timeout=120)
        server.exec(f"docker compose -f {self.compose_path} stop {self.service_name}", timeout=60)
        server.exec(f"docker compose -f {self.compose_path} rm -f {self.service_name}", timeout=30)
        server.update_compose_image(self.compose_path, self.service_name, self.prev_image)
        server.exec(f"docker compose -f {self.compose_path} up -d {self.service_name}", timeout=120)

    def ensure_docker_login(self, server: RemoteServer):
        """检查远程服务器 Docker 登录状态，未登录则自动登录（直传模式无需登录）"""
        if self.registry_mode == "direct":
            return
        if server.host in self._docker_logged_in:
            return
        host = self.registry_host
        # 检查 ~/.docker/config.json 中是否已有该仓库的认证信息
        check_cmd = (
            f"test -f ~/.docker/config.json && "
            f"python3 -c \"import json,sys; d=json.load(open(sys.argv[1])); "
            f"sys.exit(0 if any('{host}' in k for k in d.get('auths',{{}})) else 1)\" "
            f"~/.docker/config.json 2>/dev/null"
        )
        try:
            server.exec(check_cmd, timeout=10)
            log(f"[{server.host}] Docker 仓库 {host} 已登录")
            self._docker_logged_in.add(server.host)
            return
        except Exception:
            pass
        # 未登录或检查失败 → 执行 docker login
        username = self.registry_auth["username"]
        password = self.registry_auth["password"]
        if not username:
            log(f"[{server.host}] 未配置镜像仓库账号，跳过 docker login")
            return
        log(f"[{server.host}] Docker 登录 {host} (用户: {username})")
        # 密码经 exec 的 stdin 通道传递（--password-stdin），shell 命令行/进程参数
        # 中均不出现密码（echo 拼接仍会短暂暴露于 ps，禁止使用）
        server.exec(
            f"docker login {host} -u {username} --password-stdin",
            timeout=30,
            stdin_data=password + "\n",
        )
        self._docker_logged_in.add(server.host)
        log(f"[{server.host}] Docker 登录成功")

    def _ensure_local_tar(self, version: str = None) -> tuple:
        """确保本地镜像包已导出（gzip 压缩），返回 (local_tar, local_id, image)

        复用规则：本地 tar 与当前本地镜像 digest 一致才复用；本地缺少镜像
        （跳过构建场景）但已有可用 tar 时也复用。
        """
        version = version or self.version
        image = f"{self.image_prefix}{self.image_name}:{version}"
        local_id = local_image_id(image)
        tar_name = f"{self.image_name}_{version}.tar.gz"
        local_tar = self.dist_dir / tar_name
        if local_tar.exists() and (local_id is None or tar_image_id(local_tar) == local_id):
            log(f"复用本地镜像包（与本地镜像一致）: {local_tar}")
        else:
            if local_id is None:
                raise RuntimeError(f"本地缺少镜像 {image}，且本地镜像包不可用")
            log(f"本地导出镜像 {image} -> {local_tar}（gzip 压缩）")
            self.dist_dir.mkdir(parents=True, exist_ok=True)
            with self._save_lock:
                # 并行发布/预传输时其他线程可能已导出同一版本，校验内容一致才复用
                if local_tar.exists() and tar_image_id(local_tar) == local_id:
                    log(f"镜像包已被其他任务导出，复用: {local_tar}")
                else:
                    # docker save 流式输出并 gzip 压缩：传输量约减 1/3，
                    # 避免未压缩中间文件；远端 docker load 自动识别 gzip
                    import gzip
                    proc = subprocess.Popen(
                        ["docker", "save", image],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
                    try:
                        with gzip.open(str(local_tar), "wb", compresslevel=self.image_compress_level) as fout:
                            while True:
                                chunk = proc.stdout.read(1024 * 1024)
                                if not chunk:
                                    break
                                fout.write(chunk)
                    finally:
                        proc.stdout.close()
                    err = proc.stderr.read().decode("utf-8", errors="ignore")
                    proc.stderr.close()
                    rc = proc.wait()
                    if rc != 0:
                        local_tar.unlink(missing_ok=True)  # 清理残留的半成品包
                        raise RuntimeError(
                            f"本地 docker save {image} 失败（本地可能没有该版本镜像）: "
                            f"{err.strip()}"
                        )
        return local_tar, local_id, image

    def _transfer_image_to_server(self, server: RemoteServer, version: str = None):
        """单台服务器镜像就绪：远端一致则跳过，否则上传镜像包并 docker load 校验

        滚动发布（ensure_image_ready）与并行预传输（prefetch_images）共用；
        幂等：远端已与本地一致时直接返回。
        """
        version = version or self.version
        image = f"{self.image_prefix}{self.image_name}:{version}"
        local_id = local_image_id(image)
        remote_id = server.image_id(image)
        if remote_id:
            if local_id is None:
                log(f"[{server.host}] 本地无镜像 {image}（跳过构建场景），远端已存在，跳过传输")
                return
            if remote_id == local_id:
                log(f"[{server.host}] 远端镜像与本地一致（{local_id[:12]}），跳过传输: {image}")
                return
            log(f"[{server.host}] 远端 {image} 与本地不一致（{remote_id[:12]} != {local_id[:12]}），重新传输")
        local_tar, local_id, image = self._ensure_local_tar(version)
        tar_name = f"{self.image_name}_{version}.tar.gz"
        log(f"[{server.host}] 上传镜像包 {tar_name} 到 {self.remote_tmp_dir} ...")
        server.exec(f"mkdir -p {self.remote_tmp_dir}", timeout=15)
        server.sftp_put_with_progress(str(local_tar), f"{self.remote_tmp_dir}/{tar_name}",
                                      label=tar_name, streams=self.upload_streams)
        log(f"[{server.host}] docker load {tar_name} ...")
        server.exec(f"docker load -i {self.remote_tmp_dir}/{tar_name}", timeout=600)
        loaded_id = server.image_id(image)
        if local_id:
            if loaded_id != local_id:
                raise RuntimeError(f"[{server.host}] docker load 后镜像 {image} 与本地不一致（{loaded_id} != {local_id}）")
        elif not loaded_id:
            raise RuntimeError(f"[{server.host}] docker load 后仍未找到镜像 {image}")
        log(f"[{server.host}] 镜像就绪: {image}")

    def ensure_image_ready(self, server: RemoteServer, version: str = None):
        """直传模式：确保目标服务器存在与本地一致的指定版本镜像（save → 上传 → load）

        以镜像 ID（digest）为一致性依据：服务器已有同 tag 但内容不同（如同版本
        重复发布新代码）时仍会重新传输；本地与远端一致则跳过。本地缺少该镜像且
        远端已有（跳过构建场景）时维持现状不传输；都缺失时抛异常。
        """
        self._transfer_image_to_server(server, version)

    def prefetch_images(self, servers: list) -> list:
        """直传模式：并行预传输镜像包到所有服务器（上传 + docker load 并行）

        本地导出只做一次（_ensure_local_tar 内 _save_lock 保证），各服务器使用
        独立 SSH 连接并行传输，传输耗时从 N 倍降为 1 倍。滚动发布阶段会对失败
        服务器自动重试（_transfer_image_to_server 幂等），因此预传输失败只记录、
        不中断流程。仓库模式（push）镜像已在构建阶段推送，直接返回空列表。
        """
        if self.registry_mode != "direct":
            return []
        log(f">>> 并行预传输镜像到 {len(servers)} 台服务器（直传模式，上传+load 并行）")

        def _prefetch_one(srv: dict) -> tuple:
            host = srv["ip"]
            server = RemoteServer(host, merge_server_ssh(self.ssh_cfg, srv))
            try:
                self._transfer_image_to_server(server)
                log(f"[{host}] 镜像预传输完成")
                return host, True
            except Exception as e:
                log(f"[{host}] 镜像预传输失败: {e}")
                return host, False
            finally:
                server.close()

        from concurrent.futures import ThreadPoolExecutor
        workers = max(1, min(len(servers), 8))  # 上限 8，避免并发过多挤占带宽
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_prefetch_one, servers))
        failures = [host for host, ok in results if not ok]
        log(f"镜像预传输完成：成功 {len(servers) - len(failures)} 台" +
            (f"，失败 {len(failures)} 台: {failures}（滚动阶段自动重试）" if failures else "，全部就绪"))
        return failures

    def wait_health(self, host: str, server_entry: dict = None):
        """健康验证薄封装：供策略层调用；内部仍走模块级 wait_for_healthy，
        保持既有测试 mock.patch("deploy.wait_for_healthy") 的 patch 点不变。"""
        wait_for_healthy(host, self.project, ssh_override=server_entry, env=self.env)

    def _nacos_ip(self, server_entry: dict) -> str:
        """Nacos 操作使用的 IP：优先用 nacos_ip（内网），回退到 ip（外网）"""
        if server_entry and server_entry.get("nacos_ip"):
            return server_entry["nacos_ip"]
        return server_entry["ip"] if server_entry else ""

    def deploy_server(self, host: str, server_entry: dict = None, is_canary: bool = False) -> bool:
        log(f"\n========== 开始发布 {host} ==========")
        ssh_cfg = merge_server_ssh(self.ssh_cfg, server_entry) if server_entry else self.ssh_cfg
        server = RemoteServer(host, ssh_cfg)
        nacos_ip = self._nacos_ip(server_entry)
        if self.nacos_groups() and nacos_ip != host:
            log(f"Nacos 使用内网 IP: {nacos_ip}（SSH 外网 IP: {host}）")
        self.container_modified = False  # 跟踪目标状态是否已被修改（容器：docker pull 成功后置 True）
        try:
            self.strategy.check(server)
            self.strategy.prepare(server)
            if self.strategy.supports_nacos:
                self.nacos_offline(nacos_ip)
            # 传输阶段幂等（预传输成功时秒级跳过），滚动阶段自动重试预传输失败的机器
            self.strategy.transfer(server)
            self.strategy.switch(server)
            self.strategy.verify(server, server_entry)
            if self.strategy.supports_nacos:
                self.nacos_online(nacos_ip, weight=1.0)
            wait_sec = as_number(self.config["deploy"].get("canary_wait"), 0)
            if is_canary and wait_sec > 0:
                log(f"首台灰度观察 {wait_sec} 秒，请观察业务指标...")
                time.sleep(wait_sec)
            log(f"========== {host} 发布成功 ==========\n")
            return True
        except Exception as e:
            log(f"[{host}] 发布异常: {e}")
            if self.container_modified and self.previous_version:
                # 容器已被修改（旧容器已停止/删除），且有回滚版本，回滚到旧版本
                try:
                    self.strategy.rollback(server)
                except Exception as re:
                    log(f"[{host}] 容器回滚也失败: {re}")
            elif self.container_modified and not self.previous_version:
                # 首次发版：无回滚版本，容器已替换为新版本但启动失败，只能保留现状等待人工处理
                log(f"[{host}] 首次发版无回滚版本（--previous-version 未指定），容器保持新版本，请人工排查")
            else:
                # 容器未被修改（如镜像传输失败），旧容器仍在运行，无需回滚容器
                log(f"[{host}] 容器未被修改（旧容器仍在运行），跳过容器回滚")
            # 无论容器是否回滚，都必须恢复 Nacos 上线（无 Nacos 能力的发布方式自动跳过）
            try:
                if self.strategy.supports_nacos:
                    self.nacos_online(nacos_ip, weight=1.0)
            except Exception as ne:
                log(f"[{host}] Nacos 恢复上线失败: {ne}")
            if self.container_modified and self.previous_version:
                status = "已回滚"
            elif self.container_modified:
                status = "无回滚版本，容器保持新版本"
            else:
                status = "旧容器仍在运行"
            log(f"========== {host} 发布失败（{status}）==========\n")
            return False
        finally:
            server.close()

    def run(self, servers: list, skip_build: bool = False, skip_snailjob: bool = False, clear_summary_flag: bool = False):
        # 普通发布未指定回滚版本时，自动查询当前线上版本作为失败回滚目标（策略层按发布方式实现）；
        # 首次发版（查不到线上版本）或查询失败时维持现状：previous_version 保持空，失败则跳过回滚
        if not self.previous_version:
            try:
                current = self.strategy.query_current_version()
            except Exception as e:
                log(f"自动查询回滚版本失败（{e}），本次发布失败时将跳过容器回滚")
                current = None
            if current:
                self.previous_version = current
                self.prev_image = self._make_image(current)
                log(f"自动回滚兜底：未指定回滚版本，当前线上版本 {current} 将作为失败回滚目标")
            else:
                log("未指定回滚版本且未发现运行中容器（首次发版），发布失败时将跳过容器回滚")
        # prev_image 仅供容器策略回滚使用；无 previous_version 保持空跳过回滚
        send_notify(self.config, "start", project=self.project, version=self.version,
                    previous_version=self.previous_version, env=self.env)
        start_ts = time.time()
        if not skip_build:
            log(">>> 步骤 1/5: " + ("前端构建并打包静态 zip" if self.strategy.name != "container" else "Maven 构建并推送镜像"))
            build_project(self.project, self.version, env=self.env)
        else:
            log(">>> 步骤 1/5: 跳过构建")

        group = self.proj_cfg.get("snailjob_group", "")
        backup = None
        if group and not skip_snailjob:
            log(f">>> 步骤 2/5: 暂停 SnailJob group={group}")
            backup = backup_jobs(group, Path("backups"), env=self.env)
            set_job_status(group, 0, env=self.env)
        else:
            log(">>> 步骤 2/5: 无需暂停 SnailJob")

        if self.strategy.supports_prefetch:
            if self.strategy.name != "container":
                log(">>> 步骤 3/6: 并行预传输制品（各服务器并行）")
            elif self.registry_mode == "direct":
                log(">>> 步骤 3/6: 并行预传输镜像（直传模式，上传+load 并行）")
            else:
                log(">>> 步骤 3/6: 并行预传输镜像（仓库模式，构建时已推送，跳过）")
            prefetch_failures = self.strategy.prefetch(servers)
        else:
            log(">>> 步骤 3/6: 当前发布方式无预传输阶段，跳过")
            prefetch_failures = []
        if prefetch_failures:
            log(f"预传输失败 {len(prefetch_failures)} 台（滚动发布时将自动重试）: {prefetch_failures}")
        log(">>> 步骤 4/6: 滚动发布" + ("（金丝雀模式）" if self.canary else ""))
        success_all = True  # 全部服务器成功的汇总标志，任一台失败或中断即置 False
        # canary batch: read from DB
        canary_batches = None
        if self.canary:
            try:
                from webapp.deploy_scheduler import compute_canary_batches
                import sqlite3, json as _json
                _db = Path(__file__).resolve().parent / "data" / "platform.db"
                _conn = sqlite3.connect(str(_db))
                _conn.row_factory = sqlite3.Row
                try:
                    _proj = _conn.execute("SELECT id FROM projects WHERE code=?", (self.project,)).fetchone()
                    if _proj:
                        _cfg = _conn.execute("SELECT steps FROM canary_configs WHERE project_id=? AND is_default=1 LIMIT 1", (_proj["id"],)).fetchone()
                        if _cfg:
                            canary_batches = compute_canary_batches(_json.loads(_cfg["steps"]), len(servers))
                finally:
                    _conn.close()
            except Exception as e:
                log(f"[canary] read config failed, fallback: {e}")
        try:
            if canary_batches:
                log(f">>> canary batches: {len(canary_batches)}")
                for bi, batch in enumerate(canary_batches):
                    log(f'--- batch {batch["batch"]}/{len(canary_batches)}: {batch["server_count"]} servers ---')
                    batch_ok = True
                    for si in batch["server_indices"]:
                        srv = servers[si]
                        host = srv["ip"]
                        ok = self.deploy_server(host, srv, is_canary=(bi==0 and si==batch["server_indices"][0]))
                        if not ok:
                            success_all = False
                            batch_ok = False
                            break
                    if not batch_ok:
                        break
                    if batch["observe_minutes"] > 0 and bi < len(canary_batches) - 1:
                        if batch["auto_advance"]:
                            time.sleep(batch["observe_minutes"] * 60)
                        else:
                            dec = self._canary_prompt(servers[batch["server_indices"][0]]["ip"])
                            if dec != "continue":
                                success_all = False
                                break
            else:
                try:
                    for idx, srv in enumerate(servers):
                        host = srv["ip"]
                        is_canary = (idx == 0)
                        ok = self.deploy_server(host, srv, is_canary=is_canary)
                        if not ok:
                            success_all = False
                            log("首台或中间服务器失败，停止继续滚动，请人工排查。")
                            break
                        # 金丝雀模式：首台成功后暂停，等待人工确认
                        if self.canary and is_canary and idx < len(servers) - 1:
                            decision = self._canary_prompt(host)
                            if decision == "rollback":
                                log(f">>> 回滚首台 {host} 到 {self.previous_version}")
                                rollback_server = RemoteServer(host, merge_server_ssh(self.ssh_cfg, srv))
                                try:
                                    nacos_ip = self._nacos_ip(srv)
                                    if self.strategy.supports_nacos:
                                        self.nacos_offline(nacos_ip)
                                    self.strategy.rollback(rollback_server)
                                    if self.strategy.supports_nacos:
                                        self.nacos_online(nacos_ip, weight=1.0)
                                    log(f"首台 {host} 已回滚")
                                except Exception as re:
                                    log(f"首台回滚失败: {re}")
                                    try:
                                        if self.strategy.supports_nacos:
                                            self.nacos_online(nacos_ip, weight=1.0)
                                    except Exception:
                                        pass
                                finally:
                                    rollback_server.close()
                                success_all = False
                                break
                            elif decision == "stop":
                                log("已停止发布剩余服务器，首台保持新版本")
                                break
                            # continue → 继续发布剩余服务器
                except Exception as e:
                    log(f"部署异常: {e}")
                    success_all = False
        finally:
            # 无论发布成功/失败/异常，都必须恢复 SnailJob，避免任务组长期停留在暂停状态
            if group and not skip_snailjob:
                log(f">>> 步骤 5/6: 恢复 SnailJob group={group}")
                # 按备份快照精确恢复：只恢复备份前运行的任务，备份前暂停的保持暂停
                restore_jobs(group, backup, env=self.env)
            else:
                log(">>> 步骤 5/6: 无需恢复 SnailJob")

        if clear_summary_flag:
            log(">>> 步骤 6/6: 清零 sj_job_summary 计数器")
            clear_summary(env=self.env)
        else:
            log(">>> 步骤 6/6: 不清零计数器")

        if success_all:
            log("全部服务器发布完成")
            send_notify(self.config, "success", project=self.project, version=self.version,
                        previous_version=self.previous_version, env=self.env,
                        elapsed=f"{time.time() - start_ts:.0f}s")
            if not self._no_git_tag and self.proj_cfg.get("git_tag_enabled", True):
                from git_tag import tag_project
                # git_tag_suffix 显式配置优先；未配置时默认用环境名做后缀隔离，
                # 避免多环境同版本发布互相覆盖 tag 指向（如 prod 已打 v1.0.30，
                # test 再打会强制移动该 tag 到 test HEAD，prod 历史丢失）
                suffix = self.config["deploy"].get("git_tag_suffix") or self.env or ""
                tag_project(self.proj_cfg, self.version,
                            self.config["deploy"]["project_root"],
                            push=not self._skip_git_push,
                            suffix=suffix)
        else:
            log("发布中断，已回滚失败服务器")
            send_notify(self.config, "failed", project=self.project, version=self.version,
                        previous_version=self.previous_version, env=self.env,
                        elapsed=f"{time.time() - start_ts:.0f}s", detail="发布中断，已回滚失败服务器")
        return success_all

    def rollback_all(self, servers: list, skip_snailjob: bool = False):
        """全量回滚：将所有服务器从当前版本回滚到 previous_version

        流程与 run() 类似，但跳过构建，各服务器并行执行：Nacos 下线 → 回滚容器 → 健康检查 → Nacos 上线。
        某台失败不阻断后续服务器（尽量多回滚），最终汇总结果。
        """
        if not self.previous_version:
            log("错误：全量回滚必须指定 --previous-version（目标回滚版本）")
            return False

        log(f">>> 全量回滚模式：{self.project} {self.version} -> {self.previous_version}")
        log(f">>> 目标服务器: {[s['ip'] for s in servers]}")

        send_notify(self.config, "rollback", project=self.project, version=self.version,
                    previous_version=self.previous_version, env=self.env)
        start_ts = time.time()

        group = self.proj_cfg.get("snailjob_group", "")
        backup = None
        if group and not skip_snailjob:
            log(f">>> 步骤 1/3: 暂停 SnailJob group={group}")
            backup = backup_jobs(group, Path("backups"), env=self.env)
            set_job_status(group, 0, env=self.env)
        else:
            log(">>> 步骤 1/3: 无需暂停 SnailJob")

        log(">>> 步骤 2/3: 并行回滚所有服务器（各服务器独立 SSH 连接，传输+容器回滚并行）")

        def _rollback_one(srv: dict) -> tuple:
            """单台回滚：Nacos 下线 → 回滚容器 → 健康检查 → Nacos 上线，返回 (host, ok)"""
            host = srv["ip"]
            log(f"\n========== 开始回滚 {host} ==========")
            ssh_cfg = merge_server_ssh(self.ssh_cfg, srv)
            server = RemoteServer(host, ssh_cfg)
            nacos_ip = self._nacos_ip(srv)
            snapshotted = False
            container_rolled_back = False
            try:
                if self.strategy.supports_nacos:
                    self.nacos_offline(nacos_ip)
                    # 记录回滚前 Nacos 是否确实有该服务的实例（用于后续判断是否需要等待 Nacos 上线）
                    snapshotted = any(
                        (svc, grp, nacos_ip) in self.nacos._snapshotted_keys
                        for svc in self.service_names()
                        for grp in self.nacos_groups()
                    )
                self.strategy.rollback(server)
                container_rolled_back = True  # 回滚执行成功即标记
                self.strategy.verify(server, srv)
                # 容器回滚成功 + 健康检查通过
                if snapshotted:
                    # 回滚前服务在 Nacos 有注册，需要恢复上线并等待健康
                    try:
                        self.nacos_online(nacos_ip, weight=1.0)
                    except Exception as ne:
                        # 容器已回滚成功，仅 Nacos 上线失败，记为警告而非失败
                        log(f"[{host}] 警告: 容器回滚成功，但 Nacos 上线异常: {ne}")
                elif self.strategy.supports_nacos:
                    # 回滚前服务就未在 Nacos 注册，尝试上线但不等待健康
                    try:
                        self.nacos_online(nacos_ip, weight=1.0)
                    except Exception:
                        pass
                log(f"========== {host} 回滚成功 ==========")
                return host, True
            except Exception as e:
                log(f"[{host}] 回滚异常: {e}")
                if container_rolled_back:
                    # 容器已成功回滚到目标版本，仅 Nacos 等后续步骤失败
                    log(f"[{host}] 警告: 容器已回滚到 {self.previous_version}，但后续步骤异常: {e}")
                    if snapshotted:
                        try:
                            self.nacos_online(nacos_ip, weight=1.0)
                        except Exception:
                            pass
                    log(f"========== {host} 回滚成功（容器已回滚，Nacos 待恢复） ==========")
                    return host, True
                # 容器未成功回滚
                if snapshotted:
                    try:
                        self.nacos_online(nacos_ip, weight=1.0)
                    except Exception:
                        pass
                log(f"========== {host} 回滚失败 ==========")
                return host, False
            finally:
                server.close()

        rollback_results = {}  # {host: bool}
        try:
            # 本地旧版本镜像包只导出一次（_ensure_local_tar 内 _save_lock 保证），
            # 各服务器并行回滚，耗时从 N 倍降为 1 倍（受最慢服务器瓶颈）
            from concurrent.futures import ThreadPoolExecutor
            workers = max(1, min(len(servers), 8))  # 上限 8，与并行预传输一致
            with ThreadPoolExecutor(max_workers=workers) as pool:
                rollback_results = dict(pool.map(_rollback_one, servers))
        finally:
            if group and not skip_snailjob:
                log(f">>> 步骤 3/3: 恢复 SnailJob group={group}")
                restore_jobs(group, backup, env=self.env)
            else:
                log(">>> 步骤 3/3: 无需恢复 SnailJob")

        # 汇总结果
        success_count = sum(1 for v in rollback_results.values() if v)
        fail_count = len(rollback_results) - success_count
        log(f"\n回滚结果：成功 {success_count} 台，失败 {fail_count} 台")
        for host, ok in rollback_results.items():
            log(f"  {host}: {'成功' if ok else '失败'}")
        send_notify(self.config, "success" if fail_count == 0 else "failed",
                    project=self.project, version=self.previous_version,
                    previous_version=self.previous_version, env=self.env,
                    elapsed=f"{time.time() - start_ts:.0f}s",
                    detail=f"全量回滚：成功 {success_count} 台，失败 {fail_count} 台")
        return fail_count == 0

    def push_image_only(self, servers: list):
        """仅推送镜像到服务器，不执行发布（不 Nacos 下线、不更新容器）

        直传模式：并行 docker save → 上传 → docker load；仓库模式（push）的
        镜像已在构建阶段推送到 registry，无需额外操作。
        """
        if self.registry_mode != "direct":
            log(f"当前为仓库模式（{self.registry_mode}），构建阶段已将镜像推送到 registry，无需额外推送")
            return True

        log(f">>> 推送镜像模式：{self.project} {self.version}")
        log(f">>> 目标服务器: {[s['ip'] for s in servers]}")

        failures = self.prefetch_images(servers)
        success_count = len(servers) - len(failures)
        log(f"\n推送结果：成功 {success_count} 台，失败 {len(failures)} 台")
        return len(failures) == 0


def main():
    parser = argparse.ArgumentParser(description="智能运维平台单服务发布")
    parser.add_argument("--project", required=True, help="项目名称，例如 xxx")
    parser.add_argument("--version", default=None, help="新版本号，例如 1.0.29；使用 --auto-version 时可省略")
    parser.add_argument("--previous-version", default=None, help="回滚用旧版本号，例如 1.0.28；首次发版可不传，失败时跳过容器回滚")
    parser.add_argument("--env", help="目标环境，例如 prod/test，会读取 deploy.{env}.yaml 和 servers.{env}.yaml")
    parser.add_argument("--servers-file", default=None, help="服务器清单文件（覆盖 --env 的默认选择）")
    parser.add_argument("--skip-build", action="store_true", help="跳过 Maven 构建和镜像推送")
    parser.add_argument("--skip-snailjob", action="store_true", help="跳过 SnailJob 暂停/恢复")
    parser.add_argument("--clear-summary", action="store_true", help="发布结束后清零 sj_job_summary")
    parser.add_argument("--auto-version", action="store_true", help="自动查询服务器当前版本作为 previous-version，新版本=当前版本 patch+1（与显式 --version/--previous-version 互斥）")
    parser.add_argument("--no-git-tag", action="store_true", help="发布成功后不打 Git Tag")
    parser.add_argument("--skip-git-push", action="store_true", help="发布成功后打 Git Tag 但不推送远端")
    parser.add_argument("--canary", action="store_true", help="金丝雀模式：首台发布后暂停等待人工确认，再决定是否继续发布剩余服务器")
    parser.add_argument("--config-source", choices=["auto", "yaml", "db"], default="auto",
                        help="配置来源：auto=DB 存在该项目则用 db 否则 yaml（默认）；yaml=旧 config/ 文件；db=SQLite（需 --project-id）")
    parser.add_argument("--project-id", type=int, default=None,
                        help="--config-source db 时的项目 ID（数字）；--project 传该项目内的服务名")
    parser.add_argument("--rollback", action="store_true", help="全量回滚模式：将所有服务器从当前版本回滚到 --previous-version，跳过构建")
    parser.add_argument("--build-only", action="store_true", help="仅本地构建（Maven + Docker），不推送镜像到服务器、不发布")
    parser.add_argument("--push-only", action="store_true", help="构建并推送镜像到服务器，但不发布（不 Nacos 下线、不更新容器）")
    parser.add_argument("--force-window", action="store_true", help="强制跳过发布窗口检查（由 Web 端窗口确认后自动传入，deploy.py 本身不做窗口校验）")
    args = parser.parse_args()

    source = _resolve_config_source(args)
    if source == "db":
        _switch_to_db_config(args)

    if args.rollback and args.build_only:
        parser.error("--rollback 和 --build-only 不能同时使用")
    if args.rollback and args.push_only:
        parser.error("--rollback 和 --push-only 不能同时使用")
    if args.build_only and args.push_only:
        parser.error("--build-only 和 --push-only 不能同时使用")
    if args.rollback and not args.previous_version:
        parser.error("--rollback 必须指定 --previous-version（目标回滚版本）")
    if not args.version and not args.auto_version:
        parser.error("--version 为必填项（或使用 --auto-version 自动推导）")

    if args.env:
        os.environ["PLATFORM_DEPLOY_ENV"] = args.env

    config = load_config(env=args.env)

    if args.auto_version and (args.rollback or args.build_only or args.push_only):
        parser.error("--auto-version 只能用于普通发布模式，不能与 --rollback/--build-only/--push-only 同时使用")
    if args.auto_version and (args.version or args.previous_version):
        parser.error("--auto-version 与显式 --version/--previous-version 互斥")
    if args.auto_version:
        # 版本推导下沉策略层：容器=semver patch+1，静态=日期号（YYYYMMDD[-N]）
        probe = Deployer(args.project, None, None, config, env=args.env)
        current = probe.strategy.query_current_version()
        if current is None:
            parser.error("服务器上未发现运行中容器/已发布站点（首次发版），请手动指定 --version 和 --previous-version")
        args.previous_version = current
        args.version = probe.strategy.next_version(current)
        log(f"自动版本: 当前线上 {current} → 新版本 {args.version}")
    if args.build_only:
        log(f">>> 仅构建模式：{args.project} {args.version}")
        build_project(args.project, args.version, env=args.env)
        log("构建完成")
        sys.exit(0)

    # --push-only: 构建 + 推送镜像到服务器
    if args.push_only:
        log(f">>> 构建并推送模式：{args.project} {args.version}")
        build_project(args.project, args.version, env=args.env)
        servers_file = args.servers_file or default_servers_file(env=args.env)
        servers = load_yaml(Path(servers_file)).get("servers", [])
        if not servers:
            fail("服务器清单为空")
        deployer = Deployer(args.project, args.version, args.previous_version, config, env=args.env)
        success = deployer.push_image_only(servers)
        if success:
            log("镜像推送完成")
        else:
            log("部分服务器镜像推送失败")
        sys.exit(0 if success else 1)

    servers_file = args.servers_file or default_servers_file(env=args.env)
    servers = load_yaml(Path(servers_file)).get("servers", [])
    if not servers:
        fail("服务器清单为空")

    # 启动前确认环境信息，防止误操作
    server_ips = [s["ip"] for s in servers]
    log(f"目标环境: {args.env or '默认/生产'}")
    log(f"服务器清单: {Path(servers_file).name} => {server_ips}")
    nacos_conf = config.get("nacos") or {}
    log(f"Nacos 地址: {nacos_conf.get('url') or '未配置（跳过 Nacos 操作）'}，"
        f"命名空间: {nacos_conf.get('namespace') or 'public'}")

    deployer = Deployer(args.project, args.version, args.previous_version, config, env=args.env, canary=args.canary)
    deployer._no_git_tag = args.no_git_tag
    deployer._skip_git_push = args.skip_git_push
    if args.rollback:
        success = deployer.rollback_all(servers, skip_snailjob=args.skip_snailjob)
    else:
        success = deployer.run(servers, skip_build=args.skip_build, skip_snailjob=args.skip_snailjob, clear_summary_flag=args.clear_summary)
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
