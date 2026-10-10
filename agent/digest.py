"""周期巡检日报：聚合平台健康面 → 纯文本日报 → 现成通知渠道推送

设计原则：
    1. 数据全部来自平台既有事实源（监控缓存/告警表/任务表/审批单/定时计划），
       不做额外探测，单次聚合毫秒级，可挂在建议调度线程里每轮顺带检查；
    2. 发送复用 alert_channels 六渠道，走 send_custom（不写 alert_events，
       日报不是告警）；渠道未配置时结果里带原因，配置页可直接排查；
    3. 多实例互斥复用 scheduler_locks 租约锁（独立锁名 agent_digest），
       cron 到点判断用 48h 补发窗口：服务重启跨日不漏发，停机超 2 天不补发；
    4. 每次实际发送落一条 agent_digest_logs 历史（成功失败都记），
       供配置页追溯与失败排查。
"""
import json
import sys
import time

from webapp import db as dbm

# 配置存 platform_settings 的 key（value=JSON）
_DIGEST_KEY = "agent_digest"
# 默认配置：关闭状态，每天 09:00，不发任何渠道
_DEFAULT_CFG = {"enabled": 0, "cron": "0 9 * * *", "channels": [],
                "title": "巡检日报", "last_sent_at": 0, "last_result": ""}
# cron 到点补发窗口（秒）：覆盖正常跨日与每周一次的场景，过长则补发失去时效意义
_DUE_WINDOW = 48 * 3600
# 租约锁名（与发布调度锁分开，互不阻塞）
_DIGEST_LOCK = "agent_digest"


# ---------- 配置 ----------

def get_digest_config(db_path: str) -> dict:
    rows = dbm.list_records(db_path, "platform_settings", "key=?",
                            (_DIGEST_KEY,))
    cfg = dict(_DEFAULT_CFG)
    if rows and rows[0]["value"]:
        try:
            saved = json.loads(rows[0]["value"])
            if isinstance(saved, dict):
                cfg.update(saved)
        except ValueError:
            pass
    return cfg


def save_digest_config(db_path: str, enabled: int = None, cron: str = None,
                       channels: list = None, title: str = None) -> dict:
    """更新日报配置（未传字段保留原值）；返回更新后的完整配置"""
    from .tools import _validate_cron
    cfg = get_digest_config(db_path)
    if cron is not None:
        _validate_cron(cron)
        cfg["cron"] = cron
    if channels is not None:
        from webapp.alert_channels import CHANNELS
        bad = [c for c in channels if c not in CHANNELS]
        if bad:
            raise ValueError(f"未知渠道: {','.join(bad)}"
                             f"（可用: {','.join(CHANNELS)}）")
        cfg["channels"] = list(channels)
    if title is not None and title.strip():
        cfg["title"] = title.strip()[:40]
    if enabled is not None:
        cfg["enabled"] = 1 if enabled else 0
    _write_back(db_path, cfg)
    return cfg


def _write_back(db_path: str, cfg: dict) -> None:
    """配置持久化（存在则更新，否则创建；与 save_llm_settings 同一模式）"""
    now = int(time.time())
    rows = dbm.list_records(db_path, "platform_settings", "key=?",
                            (_DIGEST_KEY,))
    if rows:
        dbm.update_record(db_path, "platform_settings", rows[0]["id"],
                          dict(value=json.dumps(cfg, ensure_ascii=False),
                               updated_at=now))
    else:
        dbm.create_record(db_path, "platform_settings", dict(
            key=_DIGEST_KEY, value=json.dumps(cfg, ensure_ascii=False),
            updated_at=now))


# ---------- 数据聚合 ----------

def collect_snapshot(db_path: str, now: int = None) -> dict:
    """聚合巡检数据；任何单点失败降级为该节 unavailable，不阻断整份日报"""
    now = int(now or time.time())
    return {
        "ts": now,
        "health": _health(db_path),
        "alerts": _alerts(db_path),
        "deploys": _deploys(db_path, now),
        "approvals": _approvals(db_path),
        "schedules": _schedules(db_path, now),
    }


def _health(db_path: str) -> dict:
    """监控缓存中的服务健康统计（监控未启动时返回 unavailable）"""
    try:
        from webapp.monitor import get_monitor
        mon = get_monitor()
        ov = (mon.get_overview() or {}) if mon else {}
        services = ov.get("services") or {}
        online = [n for n, s in services.items() if s.get("status") == "online"]
        bad = {n: s.get("status") for n, s in services.items()
               if s.get("status") != "online"}
        return {"total": len(services), "online": len(online),
                "abnormal": bad, "ok": True}
    except Exception:
        return {"ok": False}


def _alerts(db_path: str) -> dict:
    """未解决告警统计 + 最近 5 条（P0/P1 优先）"""
    conn = dbm.get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, severity, subject, status, target_info, first_seen_at "
            "FROM alerts WHERE status != 'resolved' "
            "ORDER BY CASE severity WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 "
            "ELSE 2 END, id DESC LIMIT 50").fetchall()
        items = [dict(r) for r in rows]
        by_sev = {}
        for a in items:
            by_sev[a["severity"]] = by_sev.get(a["severity"], 0) + 1
        return {"total": len(items), "by_severity": by_sev,
                "top": items[:5], "ok": True}
    except Exception:
        return {"ok": False}
    finally:
        conn.close()


def _deploys(db_path: str, now: int) -> dict:
    """近 24h 发布任务成败统计 + 失败清单"""
    since = now - 24 * 3600
    conn = dbm.get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, name, env, version, status, finished_at FROM tasks "
            "WHERE finished_at >= ? AND type IN ('project','pipeline') "
            "ORDER BY finished_at DESC LIMIT 200", (since,)).fetchall()
        tasks = [dict(r) for r in rows]
        ok_n = sum(1 for t in tasks if t["status"] == "success")
        fails = [t for t in tasks if t["status"] == "failed"]
        return {"total": len(tasks), "success": ok_n, "failed": len(fails),
                "fail_list": fails[:5], "ok": True}
    except Exception:
        return {"ok": False}
    finally:
        conn.close()


def _approvals(db_path: str) -> dict:
    n = len(dbm.list_records(db_path, "agent_runs",
                             "status='awaiting_approval'", (), "id"))
    return {"pending": n, "ok": True}


def _schedules(db_path: str, now: int) -> dict:
    rows = dbm.list_records(db_path, "deploy_schedules",
                            "enabled=1 AND next_run_at > 0", (), "next_run_at")
    upcoming = [{"name": r["name"], "next_run_at": r["next_run_at"]}
                for r in rows if r["next_run_at"] <= now + 24 * 3600][:5]
    return {"enabled": len(rows), "upcoming": upcoming, "ok": True}


# ---------- 文本渲染 ----------

def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, OSError, ValueError):
        return "-"


def render_text(snap: dict, title: str = "巡检日报") -> str:
    """快照 → 纯文本日报（各渠道通吃；单节失败降级为'数据不可用'）"""
    day = _fmt_ts(snap.get("ts")).split(" ")[0]
    lines = [f"📋 {title} {day} {_fmt_ts(snap.get('ts')).split(' ')[1]}"]

    h = snap.get("health") or {}
    if h.get("ok"):
        if h.get("total"):
            lines.append(f"—— 服务健康：在线 {h['online']}/{h['total']}")
            for n, st in (h.get("abnormal") or {}).items():
                lines.append(f"  ⚠️ {n}: {st}")
        else:
            lines.append("—— 服务健康：暂无监控数据")
    else:
        lines.append("—— 服务健康：数据不可用（监控未启动）")

    a = snap.get("alerts") or {}
    if a.get("ok"):
        if a.get("total"):
            sev = " ".join(f"{k}×{v}" for k, v in
                           sorted((a.get("by_severity") or {}).items()))
            lines.append(f"—— 告警：未解决 {a['total']} 条（{sev}）")
            for t in a.get("top") or []:
                ti = t.get("target_info") or {}
                svc = ti.get("service", "") if isinstance(ti, dict) else ""
                lines.append(f"  #{t['id']} [{t['severity']}] {t['subject']}"
                             + (f"（{svc}，{_fmt_ts(t.get('first_seen_at'))}）"
                                if svc else ""))
        else:
            lines.append("—— 告警：无未解决告警 ✅")
    else:
        lines.append("—— 告警：数据不可用")

    d = snap.get("deploys") or {}
    if d.get("ok"):
        lines.append(f"—— 发布（近24h）：成功 {d.get('success', 0)}"
                     f" / 失败 {d.get('failed', 0)}"
                     f" / 共 {d.get('total', 0)} 次")
        for t in d.get("fail_list") or []:
            lines.append(f"  ❌ {t['name']} {t.get('env') or ''}"
                         f" {t.get('version') or ''}"
                         f" @ {_fmt_ts(t.get('finished_at'))}")
    else:
        lines.append("—— 发布：数据不可用")

    ap = snap.get("approvals") or {}
    sc = snap.get("schedules") or {}
    if ap.get("ok") and sc.get("ok"):
        lines.append(f"—— 审批与计划：待审审批单 {ap.get('pending', 0)} 张；"
                     f"启用中定时计划 {sc.get('enabled', 0)} 个")
        for u in sc.get("upcoming") or []:
            lines.append(f"  🕐 {u['name']} → {_fmt_ts(u['next_run_at'])}")
    return "\n".join(lines)


# ---------- 到点判断与发送 ----------

def _due_ts(cron_expr: str, now: int) -> int:
    """最近一个应发时刻（在 [now-48h, now] 窗口内向后找第一个触发点）"""
    from croniter import croniter
    from datetime import datetime
    base = datetime.fromtimestamp(now - _DUE_WINDOW)
    nxt = croniter(cron_expr, base).get_next(datetime)
    return int(nxt.timestamp())


def _log_send(db_path: str, results: list, summary: str) -> None:
    """发送结果落历史表；写失败仅告警不阻断（历史缺失不影响发送主流程）"""
    try:
        dbm.create_record(db_path, "agent_digest_logs", dict(
            sent_at=int(time.time()),
            ok_count=sum(1 for _, ok, _ in results if ok),
            total_count=len(results),
            channels=",".join(c for c, _, _ in results),
            detail="; ".join(f"{c}={'ok' if ok else d}" for c, ok, d in results),
            summary=summary))
    except Exception as e:
        print(f"[agent-digest] 发送历史写入失败: {e!r}", file=sys.stderr)


def build_and_send(db_path: str) -> dict:
    """立即聚合并发送一次日报（手动触发/测试用）；返回发送结果与文本"""
    from webapp.alert_channels import send_custom
    cfg = get_digest_config(db_path)
    if not cfg.get("channels"):
        raise ValueError("日报未配置通知渠道，请先在配置中选择渠道")
    snap = collect_snapshot(db_path)
    text = render_text(snap, cfg.get("title") or "巡检日报")
    results = send_custom(db_path, text, cfg["channels"],
                          subject=cfg.get("title") or "巡检日报")
    ok_n = sum(1 for _, ok, _ in results if ok)
    cfg["last_sent_at"] = int(time.time())
    cfg["last_result"] = (
        f"{ok_n}/{len(results)} 渠道成功: "
        + "; ".join(f"{c}={'ok' if ok else detail}" for c, ok, detail in results))
    _write_back(db_path, cfg)
    _log_send(db_path, results, cfg["last_result"])
    return {"text": text, "results": results,
            "summary": cfg["last_result"]}


def check_and_send(db_path: str) -> dict:
    """调度轮询入口：enabled 且 cron 到点未发 → 发送（多实例租约锁互斥）

    每轮开销为一次 setting 读取 + 一次 cron 计算，可安全高频调用。
    """
    cfg = get_digest_config(db_path)
    if not cfg.get("enabled") or not cfg.get("channels"):
        return {"sent": False, "reason": "disabled"}
    now = int(time.time())
    try:
        due = _due_ts(cfg["cron"], now)
    except (ImportError, ValueError) as e:
        return {"sent": False, "reason": f"cron 非法: {e}"}
    if now < due or (cfg.get("last_sent_at") or 0) >= due:
        return {"sent": False, "reason": "not due"}
    from webapp.deploy_scheduler import (try_acquire_scheduler_lock,
                                         release_scheduler_lock)
    if not try_acquire_scheduler_lock(db_path, lock_name=_DIGEST_LOCK):
        return {"sent": False, "reason": "locked"}
    try:
        # 双检：拿锁期间其他实例可能刚发过
        cfg = get_digest_config(db_path)
        if (cfg.get("last_sent_at") or 0) >= due:
            return {"sent": False, "reason": "sent by peer"}
        res = build_and_send(db_path)
        return {"sent": True, "summary": res["summary"]}
    finally:
        release_scheduler_lock(db_path, lock_name=_DIGEST_LOCK)
