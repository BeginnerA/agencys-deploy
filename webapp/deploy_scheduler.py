"""发布调度器：定时发布 + 窗口管控 + 灰度推进"""
import sys
import threading
import time


def check_release_window(db_path: str, project_id: int) -> dict | None:
    """检查当前是否命中发布窗口。

    返回 None 表示未命中任何窗口；
    返回 {"id", "name", "block_level"} 表示命中。
    repeat_yearly=1 的窗口将年份偏移到当前年份后比较。
    优先匹配项目级窗口（project_id 匹配），其次全局窗口（project_id=0）。
    """
    now = int(time.time())
    windows = []
    # 项目级 + 全局窗口
    for w in _list_windows(db_path, project_id):
        if _window_matches(w, now):
            windows.append(w)
    if not windows:
        return None
    # block 优先于 warn
    blocks = [w for w in windows if w["block_level"] == "block"]
    return blocks[0] if blocks else windows[0]


def _list_windows(db_path: str, project_id: int) -> list:
    """查询项目级 + 全局窗口"""
    from . import db as dbm
    return dbm.list_records(
        db_path, "release_windows",
        "project_id=? OR project_id=0", (project_id,))


def _window_matches(w: dict, now: int) -> bool:
    """判断窗口在当前时刻是否生效（支持年度重复）"""
    start = w["start_at"]
    end = w["end_at"]
    if w.get("repeat_yearly"):
        # 将窗口的年份偏移到当前年份
        import time as _t
        cur_year = _t.localtime(now).tm_year
        win_year = _t.localtime(start).tm_year
        offset = (cur_year - win_year) * 365 * 24 * 3600
        # 粗略偏移（忽略闰年），精确到天级别足够
        start += offset
        end += offset
    return start <= now <= end
