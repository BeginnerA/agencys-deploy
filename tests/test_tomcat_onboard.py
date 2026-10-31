"""Task 7：接入与配置事务（受锁、可恢复）。

计划文档 docs/superpowers/plans/2026-10-30-war-tomcat-deploy.md Task 7 的验收用例，
外加接入/事务红线（先读后写、令牌只走 stdin、路径护栏、失败释放锁）的加固用例。

与计划文本的最小偏差（均有注释或下文说明）：
1. maintenance_stop 两个用例的假远端对归属清单读取回放可解码清单：计划里的
   统一 "inactive" 回包是 Task 4 修订前（exec 逐条跑 systemctl）的 fake 形状；
   修订后 update_manifest 先读后写且缺失即拒绝（本任务既定契约），
   "inactive" 会解出乱码被当成"清单不存在"，用例就测不到停止本身了。
2. apply_config 摘要时序断言用 rindex 而非 index：备份 cp 命令同样包含
   .smartops-instance.json 路径，index() 会命中 reload 之前的备份而非其后的写入。
全部走假远端，不做真实 SSH/远端操作。
"""
import base64
import json
import unittest
from deploy_strategies import tomcat_instance as ti
from deploy_strategies.tomcat_config import parse_war_config

CFG = parse_war_config({
    "app_home": "/opt/apps/orders", "tomcat_home": "/opt/tomcat",
    "java_home": "/usr/lib/jvm/java-17", "run_user": "appuser",
    "unit_name": "orders-war", "port": 18080, "context_path": "/orders",
    "readiness": {"path": "/health"},
})
PLAN = {
    "digest": "a" * 64, "unit": "orders-war", "unit_content": "[Service]\n",
    "files": {"instance/conf/server.xml": "<Server/>"},
    "manifest": {"schema_version": 1, "instance_id": "i1",
                 "app_home": CFG["app_home"], "unit": "orders-war",
                 "config_digest": "a" * 64, "maintenance":
                 {"active": False, "traffic_snapshot": None}},
}
# update_manifest 先读后写：维护/清理用例的假远端需要一份可解码、归属匹配的清单
MANIFEST_I1_B64 = base64.b64encode(json.dumps(
    {"schema_version": 1, "instance_id": "i1"}).encode()).decode()


class FakeRemote:
    def __init__(self):
        self.calls = []
        self.stdis = []

    def exec(self, cmd, timeout=60, stdin_data=None):
        self.calls.append(cmd)
        self.stdis.append(stdin_data)
        return ""


def _manifest_reader(server, manifest_b64, default):
    """给假远端挂上"清单读取回 manifest_b64、其余回 default"的 exec。"""
    def fake(cmd, timeout=60, stdin_data=None):
        server.calls.append(cmd)
        server.stdis.append(stdin_data)
        if "base64 -w0" in cmd and "smartops-instance" in cmd:
            return manifest_b64
        return default
    server.exec = fake


class OnboardTest(unittest.TestCase):
    def test_stale_preview_rejected_before_write(self):
        server = FakeRemote()
        with self.assertRaises(ti.StalePreview):
            ti.onboard(server, PLAN, 1, "test", "i1", "b" * 64)
        self.assertEqual(server.calls, [])

    def test_onboard_refuses_existing_other_owner(self):
        class Existing(FakeRemote):
            def exec(self, cmd, timeout=60, stdin_data=None):
                self.calls.append(cmd)
                if "base64 -w0" in cmd:
                    return base64.b64encode(json.dumps(
                        {"instance_id": "other"}).encode("utf-8")).decode("ascii")
                return ""

        server = Existing()
        with self.assertRaises(RuntimeError):
            ti.onboard(server, PLAN, 1, "test", "i1", "a" * 64)
        self.assertNotIn("owner.json", "\n".join(server.calls))

    def test_onboard_writes_files_unit_and_never_starts(self):
        server = FakeRemote()
        out = ti.onboard(server, PLAN, 1, "test", "i1", "a" * 64)
        joined = "\n".join(server.calls)
        self.assertIn("owner.json", joined)
        self.assertIn("instance/conf/server.xml", joined)
        self.assertIn(".smartops-instance.json", joined)
        self.assertIn("daemon-reload", joined)
        self.assertIn("enable", joined)
        self.assertNotIn("systemctl start", joined)
        self.assertEqual(out["status"], "ready")

    def test_maintenance_flag_is_written_before_stop(self):
        server = FakeRemote()
        # 偏差 1：清单读取回放可解码清单，其余命令保持计划原文的 "inactive" 回包
        _manifest_reader(server, MANIFEST_I1_B64, "inactive")
        snap = ti.maintenance_stop(server, CFG["app_home"], "orders-war", "i1",
                                   "t", {"orders|G1": [{"port": 8080,
                                                        "enabled": True,
                                                        "weight": 10}]})
        self.assertTrue(snap["active"])
        self.assertEqual(snap["traffic_snapshot"]["orders|G1"][0]["weight"], 10)
        joined = "\n".join(server.calls)
        self.assertIn(".smartops-maintenance.json", joined)
        write_at = joined.index(".smartops-maintenance.json")
        stop_at = joined.index("systemctl stop")
        self.assertLess(write_at, stop_at, "维护标志必须先于停止落盘")

    def test_maintenance_stop_failure_keeps_flag(self):
        server = FakeRemote()
        def failing(cmd, timeout=60, stdin_data=None):
            server.calls.append(cmd)
            if "systemctl stop" in cmd:
                raise RuntimeError("stop failed")
            if "base64 -w0" in cmd and "smartops-instance" in cmd:
                return MANIFEST_I1_B64  # 偏差 1：让用例真正走到停止失败这一步
            return "inactive"
        server.exec = failing
        with self.assertRaises(RuntimeError):
            ti.maintenance_stop(server, CFG["app_home"], "orders-war", "i1",
                                "t", {})
        self.assertIn(".smartops-maintenance.json", "\n".join(server.calls))

    def test_clear_maintenance_clears_manifest_and_file(self):
        manifest = {"schema_version": 1, "instance_id": "i1",
                    "maintenance": {"active": True, "traffic_snapshot": {}}}
        encoded = base64.b64encode(json.dumps(manifest).encode()).decode()
        server = FakeRemote()
        server.exec = lambda cmd, timeout=60, stdin_data=None: (
            server.calls.append(cmd) or
            (encoded if "smartops-instance.json" in cmd else "inactive"))
        ti.clear_maintenance(server, CFG["app_home"], "i1", "t")
        joined = "\n".join(server.calls)
        self.assertIn("rm -f", joined)
        self.assertIn(".smartops-instance.json", joined)

    def test_apply_config_updates_digest_after_reload(self):
        manifest = {"schema_version": 1, "instance_id": "i1",
                    "config_digest": "a" * 64,
                    "maintenance": {"active": True, "traffic_snapshot": {}}}
        encoded = base64.b64encode(json.dumps(manifest).encode()).decode()
        server = FakeRemote()
        server.exec = lambda cmd, timeout=60, stdin_data=None: (
            server.calls.append(cmd) or
            (encoded if "base64 -w0" in cmd and "smartops-instance" in cmd
             else ""))
        ti.apply_config(server, CFG["app_home"], "orders-war", "i1", "t",
                        {"instance/conf/server.xml": "new"}, "[Service]\n",
                        "b" * 64, manifest_changes={"config_digest": "b" * 64})
        joined = "\n".join(server.calls)
        self.assertIn("daemon-reload", joined)
        self.assertIn(".smartops-instance.json", joined)
        # 清单写入必须晚于 daemon-reload，避免 reload 失败时留下新摘要。
        # 偏差 2：备份 cp 同样含清单路径，index() 会命中 reload 之前的备份，
        # 故用 rindex() 定位最后一次出现（即 reload 之后的清单写入）。
        self.assertGreater(joined.rindex(".smartops-instance.json"),
                           joined.index("daemon-reload"))

    def test_apply_config_restores_backup_on_reload_failure(self):
        server = FakeRemote()
        def failing(cmd, timeout=60, stdin_data=None):
            server.calls.append(cmd)
            if "daemon-reload" in cmd:
                raise RuntimeError("reload failed")
            return ""
        server.exec = failing
        with self.assertRaises(RuntimeError):
            ti.apply_config(server, CFG["app_home"], "orders-war", "i1", "t",
                            {"instance/conf/server.xml": "new"}, "[Service]\n",
                            "b" * 64)
        joined = "\n".join(server.calls)
        self.assertIn(".config-backup", joined)
        self.assertIn("cp -a", joined)
        self.assertIn("rm -rf", joined)


class TransactionHardeningTest(unittest.TestCase):
    """加固：接口清单中未直接验收的行为边界，沿用 Task 4 测试的先例。"""

    def test_read_maintenance_missing_or_garbage_returns_empty(self):
        server = FakeRemote()
        self.assertEqual(ti.read_maintenance(server, CFG["app_home"]), {})
        garbage = base64.b64encode(b"\xff\xfe not json").decode()
        server.exec = lambda cmd, timeout=60, stdin_data=None: (
            server.calls.append(cmd) or garbage)
        self.assertEqual(ti.read_maintenance(server, CFG["app_home"]), {})
        self.assertIn(".smartops-maintenance.json", "\n".join(server.calls))

    def test_update_manifest_refuses_missing_or_foreign(self):
        server = FakeRemote()  # 清单缺失
        with self.assertRaises(RuntimeError):
            ti.update_manifest(server, CFG["app_home"], "i1", "tok",
                               status="ready")
        foreign = base64.b64encode(json.dumps(
            {"instance_id": "other"}).encode()).decode()
        server.exec = lambda cmd, timeout=60, stdin_data=None: (
            server.calls.append(cmd) or foreign)
        with self.assertRaises(RuntimeError):
            ti.update_manifest(server, CFG["app_home"], "i1", "tok",
                               status="ready")
        self.assertNotIn("cat >", "\n".join(server.calls))  # 两次都未发生写入

    def test_apply_config_rejects_paths_outside_instance_conf(self):
        for rel in ("instance/conf/../evil.xml", "webapps/escape.xml"):
            with self.subTest(rel=rel):
                server = FakeRemote()
                with self.assertRaises(ValueError):
                    ti.apply_config(server, CFG["app_home"], "orders-war",
                                    "i1", "tok", {rel: "x"}, "[Service]\n",
                                    "c" * 64)
                joined = "\n".join(server.calls)
                self.assertIn(".config-backup", joined)
                self.assertIn("rm -rf", joined)  # 失败走了恢复路径，实例保持停止
                self.assertNotIn("cat >", joined)  # 未写入任何受管配置

    def test_apply_config_backup_failure_never_clears_live_conf(self):
        # 备份 cp 自身失败：不存在可还原的备份，还原链的 rm -rf 会先毁掉
        # 现网唯一 conf 再被 cp 失败打断；必须直接重抛原始异常，远端不动。
        server = FakeRemote()
        def failing_backup(cmd, timeout=60, stdin_data=None):
            server.calls.append(cmd)
            if "mkdir -p" in cmd and ".config-backup" in cmd:
                raise RuntimeError("backup failed")
            return ""
        server.exec = failing_backup
        with self.assertRaisesRegex(RuntimeError, "backup failed"):
            ti.apply_config(server, CFG["app_home"], "orders-war", "i1", "t",
                            {"instance/conf/server.xml": "new"}, "[Service]\n",
                            "b" * 64)
        joined = "\n".join(server.calls)
        self.assertNotIn("rm -rf", joined)
        self.assertNotIn("daemon-reload", joined)

    def test_transaction_commands_never_carry_token(self):
        token = "s3cret-token-abcdef0123"
        server = FakeRemote()
        _manifest_reader(server, MANIFEST_I1_B64, "inactive")
        ti.maintenance_stop(server, CFG["app_home"], "orders-war", "i1",
                            token, {})
        ti.apply_config(server, CFG["app_home"], "orders-war", "i1", token,
                        {"instance/conf/server.xml": "new"}, "[Service]\n",
                        "d" * 64, manifest_changes={"config_digest": "d" * 64})
        ti.clear_maintenance(server, CFG["app_home"], "i1", token)
        joined = "\n".join(server.calls)
        self.assertNotIn(token, joined)  # 令牌绝不进命令文本（exec 会记录整条命令）
        for data in server.stdis:
            if data is not None:
                self.assertTrue(data.startswith(token + "\n"),
                                "guarded 系列调用的 stdin 首行必须是令牌")

    def test_onboard_failure_releases_lock_and_propagates(self):
        server = FakeRemote()
        def failing(cmd, timeout=60, stdin_data=None):
            server.calls.append(cmd)
            if "daemon-reload" in cmd:
                raise RuntimeError("reload failed")
            return ""
        server.exec = failing
        with self.assertRaises(RuntimeError):
            ti.onboard(server, PLAN, 1, "test", "i1", "a" * 64)
        joined = "\n".join(server.calls)
        # 失败也必须释放占用：last-result 落盘（cat >）后移除 owner/phase
        self.assertIn("last-result.json", joined)
        self.assertIn("rm -f", joined)
