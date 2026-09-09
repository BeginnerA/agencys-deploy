"""Phase 2 后台调度：周期扫描 → 自动产出决策建议（灰度/失败归因/告警归因）

与 DeployScheduler 同风格：daemon 线程 + 模块级单例；扫描间隔默认 30s；
单轮所有异常静默（记录错误数），不影响后续轮次。规则产出全程写 agent_advice
表，TTL 去重保证不轰炸；LLM 仅在配置可用时润色，缺省降级纯规则。
"""
import threading
import time

from . import advisor as advisor_mod

_DEFAULT_INTERVAL = 30.0
_lock = threading.Lock()
_state = {"thread": None, "stop": threading.Event()}


def start_scheduler(db_path: str, interval: float = _DEFAULT_INTERVAL) -> bool:
    """启动建议调度线程（幂等：已运行则忽略）；返回是否本次新启动"""
    with _lock:
        if _state["thread"] and _state["thread"].is_alive():
            return False
        _state["stop"].clear()
        t = threading.Thread(target=_loop,
                             args=(db_path, max(5.0, float(interval))),
                             name="agent-advisor", daemon=True)
        _state["thread"] = t
        t.start()
        return True


def stop_scheduler() -> None:
    with _lock:
        _state["stop"].set()


def is_running() -> bool:
    return bool(_state["thread"] and _state["thread"].is_alive())


def _loop(db_path: str, interval: float):
    # 启动前先跑一轮，避免部署/告警事件发生后最长等待一个周期
    _tick(db_path)
    while not _state["stop"].wait(interval):
        _tick(db_path)


def _tick(db_path: str) -> dict:
    try:
        return advisor_mod.sweep(db_path, actor={"id": 0, "username": "advisor"})
    except Exception:
        # sweep 内部已逐项兜底；此处防御意外异常导致线程退出
        return {"errors": ["sweep unexpected"]}
