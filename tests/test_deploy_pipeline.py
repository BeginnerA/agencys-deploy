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


if __name__ == "__main__":
    unittest.main()
