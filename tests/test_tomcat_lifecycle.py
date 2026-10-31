"""Task 8：严格生命周期与逐主机结果。

计划文档 docs/superpowers/plans/2026-10-30-war-tomcat-deploy.md Task 8 的验收用例。
全部走替身（FakeLifecycle / EmptyServer），不做真实 SSH/网络；编排层只验证
阶段推进、结果映射与 fail-closed 语义，真实远端动作由各底层模块自己的用例覆盖。

与计划文本的最小偏差（均为让计划自身用例可执行的最小修复，见注释）：
1. make() 的部署器替身里 ``_nacos_ip`` 由 ``lambda entry: ...`` 改为
   ``lambda *args, **kwargs: ...``：类字典中的 lambda 会按方法绑定，
   ``d._nacos_ip(server_entry)`` 会按 ``Deployer._nacos_ip(self, server_entry)``
   的实参形状传入两个位置参数，单参 lambda 必然 TypeError
   （真实签名见 deploy.py ``_nacos_ip(self, server_entry)``）。
2. make() 增加 ``lc._release_exists = lambda server, version: True`` 桩：
   计划的 test_health_fail_then_recover 期望恢复成功（recovery_status ==
   "succeeded"），但替身 server 是 ``object()``，真实 ``_release_exists``
   的 exec 必然异常返回 False，用例会卡在 "blocked"；恢复源缺失场景由
   test_explicit_rollback_missing_source_blocks_before_stop 覆盖为 False。
"""
import unittest

from deploy_strategies.tomcat_lifecycle import HostResult, WarTomcatLifecycle


class FakeLifecycle(WarTomcatLifecycle):
    def __init__(self, probe_failures=0, stop_ok=True):
        self.probe_failures = probe_failures
        self.stop_ok = stop_ok
        self.calls = []
        self.manifest = {"instance_id": "i1", "maintenance": {"active": False}}
        self.current = "1.0.0"
    def _preflight(self, server, **kwargs):
        self.calls.append("preflight")
    def _snapshot(self, server):
        self.calls.append("snapshot")
        return {"version": self.current, "sha": "a" * 64, "healthy": True}
    def _stage(self, server, version, sha, **kw):
        self.calls.append("stage")
        return "created"
    def _stop(self, server, instance_id, token):
        self.calls.append("stop")
        if not self.stop_ok:
            raise RuntimeError("stop failed")
    def _activate(self, server, instance_id, token, version):
        self.calls.append("activate")
        self.current = version
    def _clean(self, server, instance_id, token):
        self.calls.append("clean")
    def _start(self, server, instance_id, token):
        self.calls.append("start")
    def _probe(self, server):
        self.calls.append("probe")
        if self.probe_failures:
            self.probe_failures -= 1
            raise RuntimeError("health failed")
        return {"ok": True, "status": 200}
    def _traffic(self, server, action, **kw):
        self.calls.append(f"traffic:{action}")
        return {"ok": True}
    def _acquire(self, server, instance_id):
        self.calls.append("acquire")
        return "token"
    def _release(self, server, instance_id, token, result):
        self.calls.append("release")


class LifecycleTest(unittest.TestCase):
    def make(self, **kw):
        d = type("D", (), {"version": "2.0.0", "previous_version": "",
                           "nacos": type("N", (), {"configured": True})(),
                           # 偏差 1：兼容 Deployer._nacos_ip(self, server_entry) 的绑定调用
                           "_nacos_ip": lambda *args, **kwargs: "10.0.0.1"})()
        lc = FakeLifecycle(**kw)
        lc.d = d
        lc.cfg = {"readiness": {"path": "/health", "timeout": 1, "interval": 1,
                                "expected_status": 200, "body_contains": ""},
                  "context_path": "", "app_home": "/opt/app", "port": 8080}
        lc.service = "orders"
        lc.unit = "orders-war"
        lc.instance_id = lambda server: "i1"
        lc._read_maintenance = lambda server: (lc.manifest.get("maintenance") or {})
        lc._clear_maintenance = lambda server, iid, token: lc.calls.append(
            "clear_maintenance")
        # 偏差 2：替身 server 无 exec，恢复源存在性默认桩为存在
        lc._release_exists = lambda server, version: True
        return lc

    def test_success_releases_with_published(self):
        lc = self.make()
        out = lc.deploy(object())
        self.assertTrue(out.ok)
        self.assertEqual(out.operation_status, "published")
        self.assertEqual(out.recovery_status, "not_needed")
        self.assertEqual(lc.calls[-1], "release")

    def test_health_fail_then_recover(self):
        lc = self.make(probe_failures=1)
        out = lc.deploy(object())
        self.assertFalse(out.ok)
        self.assertEqual(out.recovery_status, "succeeded")
        self.assertEqual(out.current_version, "1.0.0")
        self.assertIn("release", lc.calls)

    def test_first_deploy_failure_stops_and_reports_unavailable(self):
        lc = self.make(probe_failures=1)
        lc.current = ""
        lc._snapshot = lambda server: {"version": "", "sha": "", "healthy": False}
        out = lc.deploy(object())
        self.assertFalse(out.ok)
        self.assertEqual(out.recovery_status, "unavailable")
        self.assertEqual(out.process_state, "stopped")

    def test_stop_failure_is_not_success(self):
        lc = self.make(stop_ok=False)
        out = lc.deploy(object())
        self.assertFalse(out.ok)
        self.assertNotEqual(out.operation_status, "published")

    def test_restart_requires_maintenance_release(self):
        lc = self.make()
        snap = {"orders|G1": [{"port": 8080, "enabled": True, "weight": 7}]}
        lc.manifest["maintenance"] = {"active": True, "traffic_snapshot": snap}
        lc._read_manifest = lambda server: lc.manifest
        with self.assertRaises(RuntimeError):
            lc.restart(object(), release_maintenance=False)
        seen = []
        lc.d.restore_traffic = lambda ip, snapshot: seen.append(snapshot)
        out = lc.restart(object(), release_maintenance=True)
        self.assertTrue(out.ok)
        self.assertEqual(seen, [snap])
        self.assertIn("clear_maintenance", lc.calls)

    def test_restart_clear_failure_is_not_success(self):
        lc = self.make()
        lc.manifest["maintenance"] = {"active": True, "traffic_snapshot": {}}
        lc._read_manifest = lambda server: lc.manifest
        def fail(*args, **kwargs):
            raise RuntimeError("clear failed")
        lc._clear_maintenance = fail
        out = lc.restart(object(), release_maintenance=True)
        self.assertFalse(out.ok)
        self.assertEqual(out.process_state, "running")
        self.assertEqual(out.app_health, "healthy")
        self.assertIn("维护标志清除失败", out.error)

    def test_explicit_rollback_missing_source_blocks_before_stop(self):
        lc = self.make()
        lc._release_exists = lambda server, version: False
        out = lc.rollback(object(), "1.0.0", explicit=True)
        self.assertEqual(out.recovery_status, "blocked")
        self.assertNotIn("stop", lc.calls)

    def test_preflight_rejects_missing_manifest(self):
        class EmptyServer:
            def exec(self, cmd, timeout=60, stdin_data=None):
                return ""
        lc = WarTomcatLifecycle(
            type("D", (), {"version": "2.0.0", "previous_version": ""})(),
            {"app_home": "/opt/app", "port": 8080, "context_path": "",
             "run_user": "u", "readiness": {"path": "/h"}},
            "orders", "orders-war", instance_id=lambda server: "i1")
        with self.assertRaises(RuntimeError):
            lc._preflight(EmptyServer())


class FinishHeldTest(unittest.TestCase):
    """_finish 释放占用失败的直测：终态结论不被记账失败推翻，占用如实记 held。"""

    def make(self, ok=True, error=""):
        lc = WarTomcatLifecycle(None, {"app_home": "/opt/app"}, "orders",
                                "orders-war")
        def broken_release(server, instance_id, token, result):
            raise RuntimeError("owner busy")
        lc._release = broken_release
        return lc, HostResult(ok, "published" if ok else "failed", "done",
                              "2.0.0", "1.0.0", error=error)

    def test_release_failure_marks_held_and_preserves_verdict(self):
        lc, result = self.make()
        out = lc._finish(object(), "i1", "tok", result)
        self.assertTrue(out.ok)
        self.assertEqual(out.operation_status, "published")
        self.assertEqual(out.lock_state, "held")
        self.assertIn("释放占用失败", out.error)
        self.assertIn("owner busy", out.error)

    def test_release_failure_appends_to_existing_error(self):
        lc, result = self.make(ok=False, error="健康检查未通过: 503")
        out = lc._finish(object(), "i1", "tok", result)
        self.assertFalse(out.ok)
        self.assertEqual(out.lock_state, "held")
        self.assertIn("健康检查未通过: 503", out.error)
        self.assertIn("释放占用失败", out.error)


if __name__ == "__main__":
    unittest.main()
