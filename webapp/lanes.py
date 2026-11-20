"""执行道（lane）：判定两个任务能否同时跑的资源互斥单位

单 worker 串行队列把一次 20 分钟的 Maven 构建放大成全平台停机；无脑并发又会破坏
读-改-写型的共享状态。决定"能不能并行"的不是主机，而是任务实际会碰的共享资源：

    service:/pipeline:  发布目标本身（与提交互斥闸同源，见 db.task_target_keys）
    repo:               构建工作目录（git clone/checkout/pull 的落点）
    mod:                build_order 每个模块目录（mvn clean 会删同一个 target/）
    compose:            宿主机上的 docker-compose.yml（先下载改镜像再上传，并发即丢更新）
    snailjob:           SnailJob 分组（部署前暂停、结束后恢复，并发会互相提前恢复）

键集相交 → 排队串行；不相交 → 并行。互斥判定全部下沉到这里的键，
TaskQueue 只认键不认业务，新增共享资源只需多算一个键。

注意键的两类路径语义：
  - repo/mod 是**平台进程所在机器**的目录，必须归一化（大小写、分隔符、相对锚点），
    否则同一目录的两种写法算出两个键，互斥静默失效；
  - compose 是**目标机上的路径**，原样入键，绝不参与本地路径归一化。
"""
import os
import sys
import logging
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from . import db as dbm

DEPLOY_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = DEPLOY_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from common import repo_local_path  # noqa: E402

DEFAULT_POOL_SIZE = 3
MAX_POOL_SIZE = 16
# 会真正执行构建/发布的任务类型：需要构建道与编排道；restart 只做 docker/systemctl 重启
_BUILDING_TYPES = ("project", "service", "pipeline")

_logger = logging.getLogger(__name__)


def pool_size_from_env() -> int:
    """并行槽位数：PLATFORM_TASK_CONCURRENCY，缺省 3，非法/越界回退安全值

    默认 3 而非更大：一条道里最重的是一段 Maven 构建（CPU/磁盘/私服下载），
    槽位数是构建机的总量闸，不是并行度的上限——并行度由道数决定。
    """
    raw = (os.environ.get("PLATFORM_TASK_CONCURRENCY") or "").strip()
    try:
        n = int(raw)
    except ValueError:
        return DEFAULT_POOL_SIZE
    return max(1, min(n, MAX_POOL_SIZE))


def _norm_dir(path) -> str:
    """本地目录 → 可比对的键片段：锚定发布根、展开 ~、统一分隔符与大小写

    键在平台进程里计算、在别的工作目录里使用，故相对 project_root 一律锚定到
    DEPLOY_ROOT（与 _run_task 给子进程设的 cwd 同一处），不随调用方 CWD 漂移。
    """
    p = Path(os.path.expanduser(str(path)))
    if not p.is_absolute():
        p = DEPLOY_ROOT / p
    return os.path.normcase(os.path.abspath(str(p)))


def _task_env(db_path: str, task: dict):
    """任务的目标环境记录：env 为空按默认环境（与 export_runtime 同口径）"""
    env_name = task.get("env") or ""
    try:
        if env_name:
            return dbm.get_env_by_name(db_path, task["project_id"], env_name)
        return dbm.get_default_env(db_path, task["project_id"])
    except Exception:
        return None


def _workspace_keys(project_root: str, services: list) -> set:
    keys = set()
    if not project_root:
        return keys  # 没配源码根目录就无从判断是否同一份代码，退回目标道
    for svc in services:
        repo = (svc.get("git_repo") or "").strip()
        if repo:
            local = repo_local_path(repo, project_root)
            if local is not None:
                keys.add(f"repo:{_norm_dir(local)}")
        for step in svc.get("build_order") or []:
            if isinstance(step, dict) and (step.get("path") or "").strip():
                keys.add(f"mod:{_norm_dir(Path(project_root) / step['path'])}")
    return keys


def _compose_keys(db_path: str, env_obj, services: list) -> set:
    if not env_obj:
        return set()
    path = (env_obj.get("docker_compose_path") or "").strip()
    if not path:
        path = str((env_obj.get("deploy") or {}).get("docker_compose_path") or "").strip()
    if not path:
        return set()
    if not [s for s in services if (s.get("deploy_mode") or "container") == "container"]:
        return set()  # 进程族/WAR/静态发布都不碰 compose 文件
    try:
        hosts = [row["host"] for row in dbm.list_env_servers(db_path, env_obj["id"])]
    except Exception:
        return set()
    return {f"compose:{host}:{path}" for host in hosts if host}


def task_lanes(db_path: str, task: dict, extra=None) -> set:
    """任务要占的道键集合；任何解析失败都退回目标道，绝不返回空集

    空集等于无约束并行——宁可退化成"同目标互斥"的保守串行，也不能因为
    服务记录缺失/环境改名就让两个任务同时改写同一份共享状态。
    """
    type_, ref_id = task.get("type") or "", task.get("ref_id")
    try:
        keys = set(dbm.task_target_keys(db_path, type_, ref_id))
        if type_ not in _BUILDING_TYPES:
            return keys
        services = [rec for sid in dbm.task_service_ids(db_path, type_, ref_id)
                    if (rec := dbm.get_record(db_path, "services", sid))]
        args = [str(a) for a in (extra or [])] + [str(a) for a in (task.get("extra") or [])]
        skip_build = "--skip-build" in args
        if type_ == "pipeline":
            pl = dbm.get_record(db_path, "pipelines", ref_id)
            skip_build = skip_build or not (pl or {}).get("build_first", 1)
        env_obj = _task_env(db_path, task)
        if not skip_build:
            root = str((env_obj or {}).get("deploy", {}).get("project_root") or "").strip()
            keys |= _workspace_keys(root, services)
            groups = {(s.get("snailjob_group") or "").strip() for s in services}
            keys |= {f"snailjob:{g}" for g in groups if g}
        keys |= _compose_keys(db_path, env_obj, services)
        return keys or set(dbm.task_target_keys(db_path, type_, ref_id))
    except Exception as exc:
        _logger.warning("[lanes] 任务 %s 道计算失败，退回目标道: %r", task.get("id"), exc)
        return {f"{type_}:{ref_id}"}


class QueuedTask:
    """排队中任务的句柄：拿到道之前都可以被 kill_task 取消"""

    def __init__(self, task_id: int, keys: set, fn):
        self.task_id = task_id
        self.keys = keys
        self.fn = fn
        self.queue = None

    def cancel(self) -> bool:
        return bool(self.queue) and self.queue.cancel(self)


class TaskQueue:
    """有界并行 + 同道串行的执行队列

    排队中的任务不占执行槽（只在道不空时才提交给线程池），否则一条繁忙道
    会把池里所有槽位吃光，并行池退化回原来的 FIFO 串行。
    """

    def __init__(self, pool_size: int = None):
        self.size = max(1, int(pool_size if pool_size is not None else pool_size_from_env()))
        self._pool = ThreadPoolExecutor(max_workers=self.size, thread_name_prefix="deploy-task")
        self._lock = threading.Lock()
        self._pending = []
        self._owner = {}    # 道键 -> 占用它的 task_id
        self._running = 0
        self._closed = False

    def submit(self, task_id: int, keys, fn) -> QueuedTask:
        item = QueuedTask(task_id, set(keys) or {f"task:{task_id}"}, fn)
        item.queue = self
        with self._lock:
            self._pending.append(item)
            self._dispatch_locked()
            if item in self._pending:
                # 操作员问"我的任务怎么还没动"时，平台日志要能直接答：等同道的哪个任务
                holders = sorted({self._owner[k] for k in item.keys if k in self._owner})
                _logger.info("[lanes] 任务 #%s 排队等待执行道（当前由 %s 占用）",
                             task_id, "、".join(f"#{h}" for h in holders) or "其他任务")
        return item

    def cancel(self, item: QueuedTask) -> bool:
        """取消仍在排队的任务；已开始执行返回 False（由 kill_task 终止进程树）"""
        with self._lock:
            for i, queued in enumerate(self._pending):
                if queued is item:
                    self._pending.pop(i)
                    return True
        return False

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def busy_count(self) -> int:
        with self._lock:
            return self._running

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=wait)

    # ---- 内部 ----

    def _dispatch_locked(self):
        i = 0
        while not self._closed and self._running < self.size and i < len(self._pending):
            item = self._pending[i]
            held = item.keys & self._owner.keys()
            if held:
                i += 1
                continue
            self._pending.pop(i)
            self._running += 1
            for key in item.keys:
                self._owner[key] = item.task_id
            try:
                self._pool.submit(self._run_item, item)
            except Exception:
                self._running -= 1
                self._release_locked(item)
                raise

    def _release_locked(self, item: QueuedTask):
        for key in item.keys:
            if self._owner.get(key) == item.task_id:
                del self._owner[key]

    def _run_item(self, item: QueuedTask):
        try:
            item.fn()
        except Exception as exc:
            # 执行体自带兜底；走到这里说明兜底也没接住，不能让道静默泄漏
            _logger.error("[lanes] 任务 %s 执行体外抛异常: %r", item.task_id, exc)
        finally:
            with self._lock:
                self._running -= 1
                self._release_locked(item)
                self._dispatch_locked()
