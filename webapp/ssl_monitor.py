"""SSL 自动探测守护线程：周期检测 custom_monitors 中 type=ssl 的域名证书"""
import json
import sys
import threading
import time

from . import db as dbm
from .ssl_probe import probe_ssl

# 连续探测失败次数阈值，超过后触发告警
PROBE_FAIL_THRESHOLD = 3


class SSLMonitor:
    """SSL 证书自动探测守护线程

    每 interval 秒遍历 custom_monitors 表中 type=ssl 且 enabled=1 的记录，
    调用 probe_ssl() 更新证书到期信息。独立数据库连接（与 monitor 相同模式）。
    """

    def __init__(self, db_path: str, interval: int = 3600):
        self._db_path = db_path
        self._interval = max(60, int(interval))
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="ssl-monitor")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._run_once()
            except Exception as e:
                print(f"[ssl-monitor] 探测周期异常: {e!r}", file=sys.stderr)
            self._stop.wait(self._interval)

    def _run_once(self):
        """一轮探测：遍历所有启用的 SSL 监控项"""
        rows = dbm.list_records(self._db_path, "custom_monitors",
                                "type='ssl' AND enabled=1")
        for row in rows:
            try:
                self._probe_one(row)
            except Exception as e:
                print(f"[ssl-monitor] 探测 {row['target']} 异常: {e!r}",
                      file=sys.stderr)

    def _probe_one(self, row: dict):
        """探测单个域名并更新记录"""
        domain = row["target"]
        port = row.get("port") or 443
        result = probe_ssl(domain, port, timeout=10)
        now = int(time.time())
        if result["error"]:
            # 探测失败：累加失败计数，保留旧 days_left
            fail_count = (row.get("probe_fail_count") or 0) + 1
            dbm.update_record(self._db_path, "custom_monitors", row["id"], dict(
                last_checked=now,
                error=result["error"],
                probe_fail_count=fail_count,
                updated_at=now))
        else:
            # 探测成功：更新所有字段（含增强信息）
            detail = json.dumps({
                "cert_subject": result["cert_subject"],
                "cert_issuer": result["cert_issuer"],
                "key_type": result.get("key_type", ""),
                "key_bits": result.get("key_bits", 0),
                "tls_version": result.get("tls_version", ""),
            }, ensure_ascii=False)
            san_domains = result.get("san_domains") or []
            fingerprint = result.get("fingerprint_sha256") or ""
            # 检测证书变更（指纹变化）
            old_fp = row.get("cert_fingerprint") or ""
            if old_fp and fingerprint and old_fp != fingerprint:
                print(f"[ssl-monitor] 证书已更换: {domain} "
                      f"(旧指纹: {old_fp[:17]}... → 新指纹: {fingerprint[:17]}...)",
                      file=sys.stderr)
            dbm.update_record(self._db_path, "custom_monitors", row["id"], dict(
                last_checked=now,
                expiry_at=result["cert_expiry"],
                days_left=result["days_left"],
                detail=detail,
                error="",
                probe_fail_count=0,
                cert_fingerprint=fingerprint,
                san_domains=san_domains,
                updated_at=now))

    def probe_one_now(self, monitor_id: int) -> dict:
        """手动触发单个监控项的探测（供 API 调用）"""
        row = dbm.get_record(self._db_path, "custom_monitors", monitor_id)
        if not row or row.get("type") != "ssl":
            return {"error": "监控项不存在或非 SSL 类型"}
        self._probe_one(row)
        return dbm.get_record(self._db_path, "custom_monitors", monitor_id)

    def get_probe_failures(self) -> list:
        """返回连续探测失败超过阈值的监控项列表（供告警引擎调用）"""
        rows = dbm.list_records(self._db_path, "custom_monitors",
                                f"type='ssl' AND enabled=1 AND probe_fail_count >= {PROBE_FAIL_THRESHOLD}")
        return rows


_ssl_monitor = None


def get_ssl_monitor() -> "SSLMonitor":
    global _ssl_monitor
    return _ssl_monitor


def init_ssl_monitor(app) -> None:
    """启动 SSL 监控线程"""
    global _ssl_monitor
    if _ssl_monitor is None:
        interval = app.config.get("SSL_CHECK_INTERVAL", 3600)
        _ssl_monitor = SSLMonitor(app.config["DB_PATH"], interval=interval)
    _ssl_monitor.start()
