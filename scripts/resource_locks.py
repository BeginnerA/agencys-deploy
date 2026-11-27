import ctypes
import hashlib
import os
import posixpath
import re
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent


def _lock_file(key, data_dir):
    if not str(key).strip():
        raise ValueError("resource lock key must not be empty")
    configured = data_dir or os.environ.get("PLATFORM_DATA_DIR")
    directory = Path(configured) if configured else ROOT / "data" / "locks"
    if configured and not data_dir:
        directory = directory / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
    return directory / f"{digest}.lock"


@contextmanager
def resource_lock(key, data_dir=None, shared=False):
    """跨进程共享/独占锁；锁文件常驻，避免删除时破坏 inode 一致性。"""
    lock_file = _lock_file(key, data_dir)
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        if os.name == "nt":
            _lock_windows(fd, shared)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        try:
            if locked:
                if os.name == "nt":
                    _unlock_windows(fd)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class _Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", ctypes.c_ulong),
                ("OffsetHigh", ctypes.c_ulong),
                ("hEvent", ctypes.c_void_p)]


def _lock_windows(fd, shared):
    import msvcrt
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    lock = kernel32.LockFileEx
    lock.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                     ctypes.c_ulong, ctypes.c_ulong, ctypes.POINTER(_Overlapped)]
    lock.restype = ctypes.c_int
    overlapped = _Overlapped()
    if not lock(msvcrt.get_osfhandle(fd), 0 if shared else 2, 0, 1, 0,
                ctypes.byref(overlapped)):
        raise ctypes.WinError(ctypes.get_last_error())


def _unlock_windows(fd):
    import msvcrt
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    unlock = kernel32.UnlockFileEx
    unlock.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                       ctypes.c_ulong, ctypes.POINTER(_Overlapped)]
    unlock.restype = ctypes.c_int
    overlapped = _Overlapped()
    if not unlock(msvcrt.get_osfhandle(fd), 0, 1, 0, ctypes.byref(overlapped)):
        raise ctypes.WinError(ctypes.get_last_error())


def _host_path(path):
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = ROOT / p
    return os.path.normcase(str(p.resolve()))


def repo_lock_key(project_root, repo):
    repo = str(repo).strip().rstrip("/\\")
    is_remote = "://" in repo or bool(re.match(r"^[^/\\]+@[^:]+:.+", repo))
    if is_remote:
        repo = Path(urlsplit(repo).path).name
        if repo.endswith(".git"):
            repo = repo[:-4]
    return f"repo:{_host_path(Path(project_root) / repo)}"


def module_lock_key(project_root, module_path):
    return f"module:{_host_path(Path(project_root) / module_path)}"


def module_lock_plan(project_root, module_path) -> dict:
    """返回模块锁键及共享模式；子模块共享祖先守卫，父模块独占自身守卫。"""
    root = Path(_host_path(project_root))
    path = Path(_host_path(Path(project_root) / module_path))
    plan = {f"module:{path}": False}
    try:
        path.relative_to(root)
    except ValueError:
        plan[f"module:{root}"] = True
        return plan
    for ancestor in path.parents:
        if ancestor == root:
            plan[f"module:{ancestor}"] = True
            break
        if root in ancestor.parents:
            plan[f"module:{ancestor}"] = True
    return plan


@contextmanager
def module_lock(project_root, module_path, data_dir=None):
    with ExitStack() as locks:
        for key, shared in sorted(module_lock_plan(project_root, module_path).items()):
            locks.enter_context(resource_lock(key, data_dir=data_dir, shared=shared))
        yield


def artifact_lock_key(project_name, proj):
    mode = (proj or {}).get("deploy_mode") or "container"
    if mode == "static_archive":
        identity = (proj.get("zip_prefix") or project_name).strip()
    else:
        identity = (proj.get("image_name") or project_name).strip()
    return f"artifact:{mode}:{identity}"


def snailjob_lock_key(group):
    return f"snailjob:{str(group).strip()}"


def compose_lock_key(host, compose_path):
    host = str(host).strip().lower().rstrip(".")
    path = posixpath.normpath(str(compose_path).strip())
    if path.startswith("//"):
        path = "/" + path.lstrip("/")
    return f"compose:{host}:{path}"
