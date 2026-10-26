"""流水线 stage 执行：每个服务必须使用各自的 servers_file/版本，不能串用最后一个服务配置"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deploy_pipeline


class TestRunStageRouting(unittest.TestCase):

    def _run(self, parallel):
        stage = {
            "name": "stage1", "parallel": parallel,
            "services": [
                {"name": "svc-a", "version": "2.0.0",
                 "servers_file": "servers-a.yaml"},
                {"name": "svc-b", "version": "2.1.0"},
            ],
        }
        calls = {}

        def fake_deploy(name, version, previous, servers_file, *args):
            calls[name] = (version, previous, servers_file)
            return True

        with mock.patch.object(deploy_pipeline, "deploy_one_service",
                               side_effect=fake_deploy):
            ok = deploy_pipeline.run_stage(
                stage, "1.0.0", "0.9.0", "servers-default.yaml",
                {}, True, False, env="prod", canary=False)
        self.assertTrue(ok)
        return calls

    def _assert_routing(self, calls):
        self.assertEqual(calls["svc-a"],
                         ("2.0.0", "0.9.0", "servers-a.yaml"))
        self.assertEqual(calls["svc-b"],
                         ("2.1.0", "0.9.0", "servers-default.yaml"))

    def test_serial_uses_per_service_servers_file(self):
        self._assert_routing(self._run(parallel=False))

    def test_parallel_uses_per_service_servers_file(self):
        self._assert_routing(self._run(parallel=True))


class AutoFillVersionsStrategyTest(unittest.TestCase):
    def test_mixed_pipeline_uses_each_strategy_semantics(self):
        import deploy_pipeline as dp
        pipeline = {"stages": [
            {"services": [{"name": "web-api"}, {"name": "creator-web"}]},
        ]}
        probes = {
            "web-api": mock.Mock(current="1.0.29", next="1.0.30"),
            "creator-web": mock.Mock(current="20261012", next="20261013"),
        }

        def fake_deployer(name, *a, **kw):
            d = mock.Mock()
            d.strategy.query_current_version.return_value = probes[name].current
            d.strategy.next_version.side_effect = lambda cur, n=name: probes[n].next
            return d

        with mock.patch.object(dp, "Deployer", side_effect=fake_deployer), \
                mock.patch.object(dp, "log"):
            dp.auto_fill_versions(pipeline, {}, env="test")

        svcs = {s["name"]: s for s in pipeline["stages"][0]["services"]}
        self.assertEqual(svcs["web-api"]["version"], "1.0.30")
        self.assertEqual(svcs["web-api"]["previous_version"], "1.0.29")
        self.assertEqual(svcs["creator-web"]["version"], "20261013")
        self.assertEqual(svcs["creator-web"]["previous_version"], "20261012")

    def test_explicit_service_version_wins(self):
        import deploy_pipeline as dp
        pipeline = {"stages": [{"services": [
            {"name": "svc", "version": "9.9.9", "previous_version": "9.9.8"}]}]}
        with mock.patch.object(dp, "Deployer") as dep, mock.patch.object(dp, "log"):
            dp.auto_fill_versions(pipeline, {}, env="test")
        dep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
