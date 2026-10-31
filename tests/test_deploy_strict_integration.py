"""Task 9：Deployer 严格主机生命周期接入（deploy_server / rollback_all / 首台回滚）。

计划文档 docs/superpowers/plans/2026-10-30-war-tomcat-deploy.md Task 9 的验收用例；
策略替身为显式声明 strict_host_lifecycle 的 mock.Mock，RemoteServer 打桩，
不做真实 SSH/网络。旧七种模式的替身未声明该能力时必须仍走原分支
（test_deploy_result_semantics / test_node_python_service 依赖此语义）。
"""
import unittest
from pathlib import Path
from unittest import mock

import deploy as deploy_mod
from deploy import Deployer
from deploy_strategies.tomcat_lifecycle import HostResult

RUNTIME = {
    "app_home": "/opt/apps/orders", "tomcat_home": "/opt/tomcat",
    "java_home": "/usr/lib/jvm/java-17", "run_user": "appuser",
    "unit_name": "orders-war", "port": 18080, "context_path": "/orders",
    "readiness": {"path": "/health"},
}


def make_deployer():
    d = Deployer.__new__(Deployer)
    d.project = "orders"
    d.project_id = 3
    d.version = "2.0.0"
    d.previous_version = "1.0.0"
    d.env = "test"
    d.dist_dir = Path("/tmp/dist")
    d.ssh_cfg = {}
    d.config = {"deploy": {"canary_wait": 0}}
    d.proj_cfg = {"deploy_mode": "war_tomcat", "image_name": "orders",
                  "runtime_config": RUNTIME}
    d._strategy = None
    d.target_modified = False
    d._record_host_state = mock.Mock()
    d._nacos_ip = lambda entry: (entry or {}).get("ip", "")
    d.nacos = mock.Mock(configured=False)
    return d


class StrictIntegrationTest(unittest.TestCase):
    def make(self, results):
        d = make_deployer()
        strategy = mock.Mock()
        strategy.strict_host_lifecycle = True
        strategy.deploy_host.side_effect = lambda server, entry=None: results[server.host]
        strategy.rollback_host.side_effect = (
            lambda server, target, entry=None: results[server.host])
        d._strategy = strategy
        return d, strategy

    def test_deploy_server_uses_strict_result_without_old_nacos(self):
        d, strategy = self.make({"a": HostResult(True, "published", "done",
                                                 "2.0.0", "1.0.0", "running",
                                                 "healthy")})
        server = mock.Mock(host="a")
        with mock.patch.object(deploy_mod, "RemoteServer", return_value=server):
            self.assertTrue(d.deploy_server("a", {"ip": "a"}))
        strategy.deploy_host.assert_called_once()
        d._record_host_state.assert_called_once_with("a", "2.0.0", "1.0.0")
        self.assertFalse(d.nacos.online_ip.called)

    def test_deploy_server_failure_returns_false_and_keeps_recovery_status(self):
        d, strategy = self.make({"a": HostResult(False, "failed", "recovered",
                                                 "1.0.0", "1.0.0", "running",
                                                 "healthy", "succeeded")})
        with mock.patch.object(deploy_mod, "RemoteServer", return_value=mock.Mock(host="a")):
            self.assertFalse(d.deploy_server("a", {"ip": "a"}))
        d._record_host_state.assert_not_called()

    def test_deploy_server_lock_busy_maps_to_per_host_failure(self):
        """carry-forward：acquire 抛 LockBusy 时折算为本主机失败，不向上炸批"""
        d, strategy = self.make({})
        strategy.deploy_host.side_effect = RuntimeError("实例已被其它逻辑操作占用")
        with mock.patch.object(deploy_mod, "RemoteServer", return_value=mock.Mock(host="a")):
            self.assertFalse(d.deploy_server("a", {"ip": "a"}))

    def test_rollback_all_strict_stops_after_first_failure(self):
        d, strategy = self.make({
            "a": HostResult(True, "rolled_back", "done", "1.0.0", "2.0.0"),
            "b": HostResult(False, "failed", "rollback", "2.0.0", "1.0.0"),
            "c": HostResult(True, "rolled_back", "done", "1.0.0", "2.0.0"),
        })
        servers = [{"ip": "a"}, {"ip": "b"}, {"ip": "c"}]
        with mock.patch.object(deploy_mod, "RemoteServer",
                               side_effect=lambda host, cfg: mock.Mock(host=host)):
            out = d._rollback_all_strict(servers)
        self.assertEqual(out, {"a": True, "b": False})
        self.assertEqual(strategy.rollback_host.call_count, 2)

    def test_first_host_rollback_uses_recorded_baseline(self):
        d, strategy = self.make({"a": HostResult(True, "rolled_back", "done")})
        with mock.patch.object(deploy_mod, "RemoteServer",
                               return_value=mock.Mock(host="a")):
            self.assertTrue(d._rollback_first_host_strict("a", {"ip": "a"}, "1.2.3"))
        strategy.rollback_host.assert_called_once()
        self.assertEqual(strategy.rollback_host.call_args.args[1], "1.2.3")

    def test_first_host_rollback_without_baseline_refuses(self):
        """基线缺失（如发布停在停机前）：拒绝首台回滚，不盲回 previous_version"""
        d, strategy = self.make({"a": HostResult(True, "rolled_back", "done")})
        with mock.patch.object(deploy_mod, "RemoteServer") as rs:
            self.assertFalse(d._rollback_first_host_strict("a", {"ip": "a"}, ""))
        rs.assert_not_called()
        strategy.rollback_host.assert_not_called()


if __name__ == "__main__":
    unittest.main()
