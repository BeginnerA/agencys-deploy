# 容器发布迁移场景：compose stop/rm 按项目标签定位容器，手动 docker run 遗留的
# 同名游离容器（无标签）够不着，up -d 因容器名冲突失败。
# update_container/rollback_container 须在 compose rm 后检测并分流清理：
# 无标签 → 游离旧容器直接清理；有标签 → 属于其它 compose 项目，报错交人工。
# 运行：python -m pytest tests/test_container_orphan.py -q
import unittest
from unittest import mock

from deploy import Deployer, get_current_remote_version


class FakeServer:
    """记录 exec 命令序列；replies 按子串匹配返回受控 stdout"""

    def __init__(self, replies=None):
        self.replies = replies or {}
        self.exec_calls = []
        self.host = "10.0.0.1"

    def exec(self, cmd, timeout=30):
        self.exec_calls.append(cmd)
        for key, val in self.replies.items():
            if key in cmd:
                return val
        return ""

    def update_compose_image(self, *args, **kwargs):
        self.exec_calls.append("update_compose_image")


def make_deployer() -> Deployer:
    d = Deployer.__new__(Deployer)
    d.registry_mode = "direct"
    d.compose_path = "/data/docker/docker-compose.yml"
    d.service_name = "fenfa"
    d.full_image = "fenfa/fenfa:1.0.0"
    d.proj_cfg = {}
    d.target_modified = False
    return d


PS_FILTER = "name=^/fenfa$"
ORPHAN_ID = "2641c61996da"


class _FakeChannel:
    def recv_exit_status(self):
        return 0


class _FakeStdout:
    channel = _FakeChannel()

    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data.encode()


class _FakeStderr:
    def read(self):
        return b""


class _FakeClient:
    def __init__(self, captured, stdout):
        self.captured = captured
        self.stdout = stdout

    def set_missing_host_key_policy(self, policy):
        pass

    def exec_command(self, cmd, timeout=30):
        self.captured.append(cmd)
        return (None, self.stdout, _FakeStderr())

    def close(self):
        pass


class CurrentRemoteVersionFilterTest(unittest.TestCase):
    """版本推导查询的容器名过滤必须锚定 /name（docker 容器名含前导斜杠，
    name=^fenfa$ 对 /fenfa 永不命中 → 已部署环境被误判为首次发版）"""

    def test_filter_matches_leading_slash(self):
        captured = []
        client = _FakeClient(captured, _FakeStdout("fenfa/fenfa:1.0.0"))
        with mock.patch("deploy.get_project_config", return_value={"service_name": "fenfa"}), \
                mock.patch("deploy.default_servers_file", return_value="servers.yaml"), \
                mock.patch("deploy.load_yaml", return_value={"servers": [{"ip": "10.0.0.1"}]}), \
                mock.patch("deploy.get_ssh_config", return_value={}), \
                mock.patch("deploy.connect_ssh"), \
                mock.patch("deploy.paramiko.SSHClient", return_value=client):
            self.assertEqual(get_current_remote_version("fenfa", {}, env="prod"), "1.0.0")
        self.assertIn("name=^/fenfa$", captured[0])


class ContainerOrphanTest(unittest.TestCase):
    def _run_update(self, replies):
        d = make_deployer()
        server = FakeServer(replies)
        with mock.patch.object(d, "ensure_image_ready"), mock.patch("deploy.log"):
            d.update_container(server)
        return server.exec_calls

    def test_orphan_cleaned_before_up(self):
        """无标签游离容器：compose rm 后、up -d 前被 docker rm -f 清理"""
        calls = self._run_update({
            PS_FILTER: ORPHAN_ID,
            f"docker inspect {ORPHAN_ID}": "",
        })
        self.assertIn(f"docker rm -f {ORPHAN_ID}", calls)
        rm_pos = calls.index(f"docker rm -f {ORPHAN_ID}")
        rm_compose_pos = calls.index(
            "docker compose -f /data/docker/docker-compose.yml rm -f fenfa")
        up_pos = calls.index(
            "docker compose -f /data/docker/docker-compose.yml up -d fenfa")
        self.assertGreater(rm_pos, rm_compose_pos, "清理必须在 compose rm 之后")
        self.assertLess(rm_pos, up_pos, "清理必须在 up -d 之前，否则容器名冲突")

    def test_orphan_other_project_raises(self):
        """同名容器属于其它 compose 项目：明确报错交人工，不擅自 rm、不继续 up"""
        d = make_deployer()
        server = FakeServer({
            PS_FILTER: ORPHAN_ID,
            f"docker inspect {ORPHAN_ID}": "otherproj",
        })
        with mock.patch.object(d, "ensure_image_ready"), mock.patch("deploy.log"):
            with self.assertRaises(RuntimeError) as cm:
                d.update_container(server)
        msg = str(cm.exception)
        self.assertIn("otherproj", msg)
        self.assertIn(ORPHAN_ID, msg)
        self.assertNotIn(f"docker rm -f {ORPHAN_ID}", server.exec_calls)
        self.assertNotIn(
            "docker compose -f /data/docker/docker-compose.yml up -d fenfa",
            server.exec_calls)

    def test_no_orphan_no_extra_exec(self):
        """无同名容器（正常路径）：仅一次 ps 查询，不执行 inspect / rm"""
        calls = self._run_update({PS_FILTER: ""})
        self.assertNotIn(f"docker rm -f {ORPHAN_ID}", calls)
        self.assertFalse([c for c in calls if c.startswith("docker inspect")])
        ps_calls = [c for c in calls if PS_FILTER in c]
        self.assertEqual(len(ps_calls), 1)

    def test_rollback_also_cleans_orphan(self):
        """回滚链路同样清理游离容器（撞名问题与发布方向无关）"""
        d = make_deployer()
        d.previous_version = "0.9.0"
        d.prev_image = "fenfa/fenfa:0.9.0"
        server = FakeServer({
            PS_FILTER: ORPHAN_ID,
            f"docker inspect {ORPHAN_ID}": "",
        })
        with mock.patch.object(d, "ensure_image_ready"), mock.patch("deploy.log"):
            d.rollback_container(server)
        rm_pos = server.exec_calls.index(f"docker rm -f {ORPHAN_ID}")
        up_pos = server.exec_calls.index(
            "docker compose -f /data/docker/docker-compose.yml up -d fenfa")
        self.assertLess(rm_pos, up_pos)


if __name__ == "__main__":
    unittest.main()
