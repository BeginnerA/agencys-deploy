"""任务执行道：判定两个任务能否同时启动

不同发布目标默认并行；只有跨越整个任务生命周期的共享状态才占任务道：

    service:/pipeline:  发布目标本身（与提交互斥闸同源，见 db.task_target_keys）
    snailjob:           SnailJob 分组（部署前暂停、结束后恢复，并发会互相提前恢复）

仓库、构建模块和 Compose 文件由 scripts/resource_locks.py 在实际操作处加锁，
不再阻塞整个任务：Git 写与构建读互斥，同模块构建互斥，同机同 Compose 切换互斥。
"""
import os
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from . import db as dbm

DEFAULT_POOL_SIZE = 3
MAX_POOL_SIZE = 16
# 会真正执行构建/发布的任务类型：需要 SnailJob 道；restart 只做 docker/systemctl 重启
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


def task_lanes(db_path: str, task: dict, extra=None) -> set:
    """任务要占的道键集合；任何解析失败都退回目标道，绝不返回空集

    空集等于无约束并行——宁可退化成"同目标互斥"的保守串行，也不能因为
    服务记录缺失就让两个任务同时改写同一份共享状态。
    """
    type_, ref_id = task.get("type") or "", task.get("ref_id")
    try:
        keys = set(dbm.task_target_keys(db_path, type_, ref_id))
        if type_ not in _BUILDING_TYPES:
            return keys
        services = [rec for sid in dbm.task_service_ids(db_path, type_, ref_id)
                    if (rec := dbm.get_record(db_path, "services", sid))]
        args = [str(a) for a in (extra or [])] + [str(a) for a in (task.get("extra") or [])]
        if "--skip-snailjob" not in args:
            groups = {(s.get("snailjob_group") or "").strip() for s in services}
            keys |= {f"snailjob:{g}" for g in groups if g}
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
