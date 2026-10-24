"""后台任务执行器：单 worker 串行；--config-source db --project-id 调用 deploy.py"""
import os
import re
import signal
import sys
import threading
import time
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, Future

from . import db as dbm
from .db import update_task

DEPLOY_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_LOG_DIR = DEPLOY_ROOT / "data" / "logs"
# 生效日志目录（生产单实例：root/data/logs）。由 set_log_dir() 在 create_app 时写入，
# 调用点也可显式传 log_dir（来自 app.config["LOG_DIR"]）——见 _log_root()。
# 用模块全局而非私有变量：既有测试直接 patch 本名，patch/restore 语义保持不变。
LOG_DIR = _DEFAULT_LOG_DIR

_executor = ThreadPoolExecutor(max_workers=1)
# 排队期 task_id -> Future（占位，支持队列内取消）；运行期被 Popen 覆盖
_task_registry = {}


def set_log_dir(path) -> None:
    """配置任务日志目录（应用工厂在 create_app 时调用）；传 None 回到默认值。

    后台线程（发布调度器 / Agent 执行器）没有 app 上下文，只能经模块配置取目录；
    有 app 上下文的调用点应显式传 log_dir，避免同进程多实例共用一份全局配置。
    """
    global LOG_DIR
    if path is None:
        LOG_DIR = _DEFAULT_LOG_DIR
        return
    LOG_DIR = Path(path)


def _log_root(log_dir=None) -> Path:
    """生效日志目录：显式参数优先，其次模块配置 LOG_DIR"""
    return Path(log_dir) if log_dir is not None else Path(LOG_DIR)


class _RestartHandle:
    """restart 任务运行期句柄：协作取消（线程内每台服务器之间检查 cancel 事件）"""
    def __init__(self):
        self.cancel = threading.Event()


def build_cli(task: dict, extra_args: list = None) -> list:
    """根据任务记录组装 CLI 参数（db 模式）；restart 类型不产 CLI（线程内执行）"""
    if task["type"] == "restart":
        return []
    script = "deploy_pipeline.py" if task["type"] == "pipeline" else "deploy.py"
    cmd = [sys.executable, str(DEPLOY_ROOT / script),
           "--config-source", "db",
           "--project-id", str(task["project_id"])]
    if task["type"] == "pipeline":
        cmd += ["--pipeline", task["name"]]
    else:
        cmd += ["--project", task["name"]]
    if task["env"]:
        cmd += ["--env", task["env"]]
    if task["version"]:
        cmd += ["--version", task["version"]]
    if task["previous_version"]:
        cmd += ["--previous-version", task["previous_version"]]
    cmd += extra_args or []
    return cmd


def _backfill_auto_versions(db_path: str, task_id: int, log_file: str):
    """自动版本号场景：从日志解析子进程实际解析的版本号并回写到任务记录。

    deploy.py / deploy_pipeline.py 在 --auto-version 模式下会输出如下日志：
      自动版本: 当前线上 1.0.29 → 新版本 1.0.30
      [svc-name] 自动版本: 当前线上 1.0.29 → 新版本 1.0.30
    取第一条匹配作为任务级版本号（单服务即唯一；流水线取首个服务代表）。
    """
    try:
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except OSError:
        return
    m = re.search(
        r"(?:^|\n)(?:\[[\w-]+\]\s*)?自动版本:\s*当前线上\s+(\S+)\s*→\s*新版本\s+(\S+)",
        content
    )
    if m:
        previous_version, version = m.group(1), m.group(2)
        update_task(db_path, task_id, version=version, previous_version=previous_version)


def backfill_existing_auto_versions(db_path: str):
    """一次性回填历史自动版本任务：扫描 extra 含 --auto-version 且 version 为空的记录，
    从对应日志文件解析实际版本号并回写。幂等，已回填的任务不会重复处理。"""
    from .db import get_conn
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, log_file FROM tasks "
            "WHERE extra LIKE '%--auto-version%' "
            "AND (version IS NULL OR version = '')"
        ).fetchall()
    finally:
        conn.close()
    count = 0
    for row in rows:
        log_file = row["log_file"]
        if not log_file:
            continue
        _backfill_auto_versions(db_path, row["id"], log_file)
        count += 1
    if count:
        import logging
        logging.getLogger(__name__).info(
            "[platform] 历史自动版本任务回填完成，处理 %d 条", count)


def submit_task(db_path: str, task: dict, extra_args: list = None, log_dir=None):
    """排队执行任务；log_dir 省略时取模块配置（由 create_app 写入）"""
    fut = _executor.submit(_run_task, db_path, task, extra_args or [], log_dir)
    # 登记 Future 占位：kill_task 在排队窗口期（Popen 之前）也能取消。
    # setdefault 防微秒级竞态：若 _run_task 已抢先注册 Popen，占位不得覆盖它
    _task_registry.setdefault(task["id"], fut)


def _save_deploy_snapshot(db_path, task_id: int, log_file: str):
    """部署成功后写环境快照（deployment_snapshots）：快照对比的数据生产点，
    覆盖 Web/Agent/调度/webhook 全通道（此前仅测试写入，对比功能空转）。

    image_versions 口径：优先解析子进程日志的自动版本行（流水线每服务一行
    `[name] 自动版本: ... 新版本 X`，单服务无前缀行归属任务 name）；固定版本
    模式单服务任务记 {任务 name: version}，流水线展开 stages 全部服务同版本。
    restart 任务无版本变更不落快照。失败静默（快照绝不阻断任务终态回写）。
    """
    try:
        task = dbm.get_task(db_path, task_id)  # 重取：auto_version 回填后的最新记录
        if not task or task.get("type") == "restart":
            return
        version = task.get("version") or ""
        image_versions = {}
        content = ""
        try:
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except OSError:
            pass
        if content:
            # 流水线自动版本：每服务一行带 [name] 前缀
            for m in re.finditer(
                    r"(?:^|\n)\[([\w.-]+)\]\s*自动版本:\s*当前线上\s+\S+\s*→\s*新版本\s+(\S+)",
                    content):
                image_versions[m.group(1)] = m.group(2)
            # 单服务自动版本：行首无前缀，归属任务 name
            m = re.search(
                r"(?:^|\n)自动版本:\s*当前线上\s+\S+\s*→\s*新版本\s+(\S+)", content)
            if m and task["name"] not in image_versions:
                image_versions[task["name"]] = m.group(1)
        if not image_versions and version:
            if task["type"] == "pipeline":
                pl = dbm.get_record(db_path, "pipelines", task.get("ref_id") or 0)
                for stage in (pl.get("stages") if pl else None) or []:
                    for svc in (stage.get("services") or []):
                        sid = svc.get("service_id")
                        rec = dbm.get_record(db_path, "services", sid) if sid else None
                        if rec:
                            image_versions[rec["name"]] = version
            else:
                image_versions[task["name"]] = version
        dbm.create_record(db_path, "deployment_snapshots", dict(
            project_id=task["project_id"], env=task.get("env") or "",
            task_id=task_id, version=version,
            image_versions=image_versions, deployed_at=int(time.time())))
    except Exception:
        pass


def _run_task(db_path, task, extra_args, log_dir=None):
    # 落点目录：显式参数（提交方 app.config）优先，否则模块配置
    log_root = _log_root(log_dir)
    log_root.mkdir(parents=True, exist_ok=True)
    log_file = log_root / f"task_{task['id']}.log"
    try:
        update_task(db_path, task["id"], status="running", log_file=str(log_file),
                    started_at=time.time())  # 启动时刻才计 started_at（排除排队时长）
        # 执行参数以持久化到任务记录的 extra 为准（submit 传入值与其一致），
        # 保证重新运行时恢复的参数与原始执行一致
        extra = extra_args if extra_args else (task.get("extra") or [])
        if task["type"] == "restart":
            # 重启任务：线程内直接执行（不产 CLI/子进程），日志由 _run_restart 写入；
            # 注册 _RestartHandle 供 kill_task 协作取消（覆盖排队期 Future 占位）
            handle = _RestartHandle()
            _task_registry[task["id"]] = handle
            try:
                status = _run_restart(db_path, task, str(log_file), handle)
            finally:
                _task_registry.pop(task["id"], None)
            update_task(db_path, task["id"], status=status, finished_at=time.time())
            return
        cmd = build_cli(task, extra)
        env_map = dict(os.environ)
        # 注入任务 ID：deploy.py 灰度决策（非终端子进程）依赖它定位决策文件
        env_map["PLATFORM_TASK_ID"] = str(task["id"])
        # 注入数据目录（= 实例 DB 所在目录）：子进程的灰度决策文件必须与
        # Web 端扫描的目录一致，否则多实例/隔离目录下会一直等不到人工决策
        env_map["PLATFORM_DATA_DIR"] = str(Path(db_path).resolve().parent)
        with open(log_file, "w", encoding="utf-8") as f:
            # 日志开头记录实际命令行，便于核对参数（如 --skip-build）是否生效
            f.write("[platform] 执行命令: " + " ".join(cmd) + "\n")
            proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT,
                                    env=env_map, cwd=str(DEPLOY_ROOT),
                                    start_new_session=True,
                                    creationflags=getattr(
                                        subprocess, "CREATE_NO_WINDOW", 0))
        _task_registry[task["id"]] = proc  # 覆盖排队期 Future 占位
        rc = proc.wait()
        _task_registry.pop(task["id"], None)
        # 自动版本号场景：子进程已解析实际版本并写入日志，回写到任务记录
        if "--auto-version" in extra:
            _backfill_auto_versions(db_path, task["id"], str(log_file))
        update_task(db_path, task["id"], status="success" if rc == 0 else "failed",
                    finished_at=time.time())
        if rc == 0:
            _save_deploy_snapshot(db_path, task["id"], str(log_file))
    except Exception as exc:
        # 兜底：线程池异常无人取回，不处理会永久卡 pending/running 且无日志
        _task_registry.pop(task["id"], None)
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"\n[platform] 任务执行异常: {exc!r}\n")
            update_task(db_path, task["id"], status="failed",
                        finished_at=time.time())
        except Exception:
            pass


def tail_log(log_file: str, offset: int = 0, max_chars: int = 8000, tail: bool = False,
             log_dir=None) -> tuple:
    """返回 (文本, 新 offset, 是否截断)；offset 为已读字符数；log_file 必须位于生效日志目录（防御纵深）

    tail=True 且 offset=0 且日志较大时，直接返回尾部 max_chars 字符，
    避免前端打开运行中任务时从头逐块追赶导致长时间卡顿。
    已结束任务应传 tail=False 以返回完整日志。
    log_dir 省略时取模块配置（create_app 写入），有 app 上下文的调用点应显式传入。
    """
    if not log_file:
        return "", 0, False
    try:
        resolved = Path(log_file).resolve()
    except OSError:
        return "", 0, False
    if not resolved.is_relative_to(_log_root(log_dir).resolve()):
        return "", 0, False
    if not resolved.exists():
        return "", 0, False
    file_size = resolved.stat().st_size
    with open(resolved, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
        if tail and offset == 0 and file_size > max_chars:
            # 运行中任务首次打开：返回尾部窗口并标记截断（跳过可能不完整的行首）
            text = content[-max_chars:]
            nl = text.find('\n')
            if nl != -1:
                text = text[nl + 1:]
            # offset 必须返回完整文件字符数（已读至末尾的位置）：前端以 offset 为
            # 已读游标继续切片。若只返回 len(text)（尾部窗口长度），下次从字符偏移
            # len(text) 处切片会与已显示内容错位 → 重复/乱序；返回字节数（原实现）
            # 遇中文（字节>字符）时切片返回空 → 日志丢失
            return text, len(content), True
    text = content[offset:offset + max_chars]
    return text, offset + len(text), False


def kill_task(task_id: int, db_path: str = None) -> bool:
    """终止任务：排队中取消 Future；运行中 restart 置协作取消标志（由 worker 落库终态）；其余类型终止进程树（Windows taskkill /T；POSIX killpg）。
    db_path 传入时，排队取消会同步落库 failed（避免大屏出现永久 pending 幽灵任务）"""
    entry = _task_registry.get(task_id)
    if entry is None:
        return False
    if isinstance(entry, _RestartHandle):
        # 协作取消：仅置标志，终态（failed）由 worker 线程观察到 cancel 后落库
        entry.cancel.set()
        return True
    if isinstance(entry, Future):
        if entry.cancel():
            # 取消成功：任务不会执行，占位需手动清除（_run_task 不再有机会 pop）
            _task_registry.pop(task_id, None)
            if db_path:
                update_task(db_path, task_id, status="failed",
                            finished_at=time.time())
            return True
        # cancel 失败：_run_task 已启动但 Popen 注册可能尚未完成，短暂等待重试
        for _ in range(50):
            time.sleep(0.05)
            entry = _task_registry.get(task_id)
            if entry is not None and not isinstance(entry, Future):
                break
        else:
            # 任务已在执行且注册窗口已过但 registry 无 Popen（异常退出被 pop）
            return False
        if isinstance(entry, _RestartHandle):
            entry.cancel.set()
            return True
    proc = entry
    if proc.poll() is not None:
        return False
    try:
        if os.name == "nt":
            # /T 进程树终止：deploy.py 的孙进程（mvn/docker/ssh）随主进程一并结束
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    return True

# restart 健康检查轮询参数（测试可 patch 缩短，避免真实等待 60s）
_RESTART_HEALTH_TIMEOUT = 60.0
_RESTART_HEALTH_INTERVAL = 5.0


def _run_restart(db_path: str, task: dict, log_file: str,
                 handle: "_RestartHandle") -> str:
    """执行服务重启：并发对每台服务器 docker restart + 健康检查。

    返回 "success" / "failed"。日志逐行写入 log_file（每台一行 + 汇总行）。
    复用 monitor 的 SSH 连接池（同进程），凭据合并语义与监控探测一致。
    """
    import shlex
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from . import db as dbm
    from . import monitor as monitor_mod

    rec = dbm.get_record(db_path, "services", task["ref_id"])
    env_obj = dbm.get_env_by_name(db_path, task["project_id"], task["env"])
    f = open(log_file, "w", encoding="utf-8")
    try:
        if not rec:
            f.write("[platform] 服务记录不存在，重启中止\n")
            return "failed"
        svc_name = rec.get("service_name") or ""
        if not svc_name:
            f.write(f"[platform] 服务「{rec['name']}」未配置容器名（service_name），重启中止\n")
            return "failed"
        if not env_obj:
            f.write(f"[platform] 环境「{task['env']}」不存在，重启中止\n")
            return "failed"
        servers = dbm.list_env_servers(db_path, env_obj["id"])
        if not servers:
            f.write("[platform] 环境未绑定服务器，重启中止\n")
            return "failed"
        f.write(f"[platform] 重启服务「{rec['name']}」（容器名 {svc_name}），"
                f"环境「{task['env']}」，共 {len(servers)} 台服务器\n")
        f.flush()
        env_ssh = env_obj.get("ssh") or {}
        health_port = int(rec.get("health_port") or 0)
        health_url = rec.get("health_url") or "/"
        results = []  # (host, status, msg)，status ∈ ok/skipped/failed/cancelled

        def restart_one(server: dict) -> tuple:
            host = server["host"]
            port = int(server.get("ssh_port") or 22)
            ssh_cfg = monitor_mod._merge_server_ssh(env_ssh, server)
            if handle.cancel.is_set():
                return host, "cancelled", "任务已终止，跳过"
            try:
                # 容器存在性精确匹配（grep -F 字面量、-x 整行，避免 hqb-auth 命中
                # hqb-auth-bak）；docker ps -a 同时列出停止的容器，容器存在
                # （无论运行/停止）即命中 → docker restart 将其拉起，日志「重启成功」
                # 与行为一致；docker restart 成功时 stdout 为容器名，失败时
                # stderr 返回错误文本（_ssh_exec_multi 返回 out or err）
                outs = monitor_mod._ssh_exec_multi(host, port, ssh_cfg, [
                    "docker ps -a --format '{{.Names}}' | grep -Fx " + shlex.quote(svc_name),
                    "docker restart " + shlex.quote(svc_name),
                ], timeout=60)
            except Exception as exc:
                return host, "failed", f"SSH/执行失败: {exc}"
            if not outs[0].strip():
                return host, "skipped", "容器不存在，跳过"
            if outs[1].strip() != svc_name:
                return host, "failed", f"docker restart 失败: {outs[1].strip() or '无输出'}"
            if handle.cancel.is_set():
                return host, "cancelled", "任务已终止，跳过"
            if health_port <= 0:
                return host, "ok", "重启成功"
            # 健康检查：轮询 health_port+health_url 最多 60s；超时仅警告（应用启动慢属常见）
            t0 = time.time()
            while time.time() - t0 < _RESTART_HEALTH_TIMEOUT:
                if handle.cancel.is_set():
                    return host, "cancelled", "任务已终止，跳过"
                r = monitor_mod._probe_health_url(host, health_port, health_url)
                if r["ok"]:
                    return host, "ok", f"重启成功（健康检查 {r['ms']}ms）"
                time.sleep(_RESTART_HEALTH_INTERVAL)
            return host, "ok", "重启成功（健康检查超时（应用启动慢？），仍视为成功）"

        with ThreadPoolExecutor(max_workers=min(8, len(servers))) as pool:
            futures = [pool.submit(restart_one, s) for s in servers]
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as exc:
                    results.append(("?", "failed", f"执行异常: {exc}"))
        # 按服务器绑定顺序输出日志（并发结果乱序，先排回 order_no 顺序）
        order = {s["host"]: i for i, s in enumerate(servers)}
        results.sort(key=lambda r: order.get(r[0], len(servers)))
        for host, status, msg in results:
            f.write(f"[{host}] {msg}\n")
            f.flush()
        ok_count = sum(1 for _, st, _ in results if st == "ok")
        fail_count = sum(1 for _, st, _ in results if st == "failed")
        skip_count = sum(1 for _, st, _ in results if st == "skipped")
        if handle.cancel.is_set():
            f.write("[platform] 任务已被终止\n")
            return "failed"
        if fail_count:
            f.write(f"[platform] 汇总：{ok_count} 台成功，{fail_count} 台失败，"
                    f"{skip_count} 台跳过 → 任务失败\n")
            return "failed"
        if ok_count == 0:
            f.write(f"[platform] 汇总：全部 {skip_count} 台未找到容器 → 任务失败\n")
            return "failed"
        f.write(f"[platform] 汇总：{ok_count} 台成功，{skip_count} 台跳过 → 任务成功\n")
        return "success"
    finally:
        f.flush()
        f.close()
