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
TEMPLATES = {"web.xml": "<web-app/>", "catalina.properties": "a=1\n",
             "logging.properties": "h=x\n"}

class FakeRemote:
    def __init__(self, reads=None):
        self.reads = reads or {}
        self.calls = []
    def exec(self, cmd, timeout=60, stdin_data=None):
        self.calls.append(cmd)
        for key, value in self.reads.items():
            if key in cmd:
                return value
        return ""

TEMPLATE_B64 = base64.b64encode(b"<web-app/>").decode()

class InstanceTest(unittest.TestCase):
    def test_preview_is_deterministic_and_has_no_env_values(self):
        server = FakeRemote({"base64 -w0": TEMPLATE_B64})
        a = ti.preview(server, CFG, "orders", 3, "test", "orders-war", "i-123")
        b = ti.preview(server, CFG, "orders", 3, "test", "orders-war", "i-123")
        self.assertEqual(a["digest"], b["digest"])
        self.assertIn("instance/conf/server.xml", a["files"])
        self.assertIn("org.apache.catalina.startup.Bootstrap start", a["unit_content"])
        self.assertNotIn("password", json.dumps(a).lower())

    def test_manifest_roundtrip_and_drift(self):
        manifest = ti.build_manifest(CFG, "orders", 3, "test", "orders-war",
                                     "i-123", "v1", "d" * 64)
        encoded = base64.b64encode(json.dumps(manifest).encode()).decode()
        server = FakeRemote({"base64 -w0": encoded})
        loaded = ti.read_manifest(server, CFG["app_home"])
        self.assertEqual(loaded["instance_id"], "i-123")
        self.assertEqual(loaded["config_digest"], "d" * 64)

    def test_missing_manifest_is_empty(self):
        self.assertEqual(ti.read_manifest(FakeRemote(), CFG["app_home"]), {})

    def test_manifest_files_and_unit_digests_verify(self):
        import hashlib
        files = {"instance/conf/server.xml": "<Server/>"}
        manifest = ti.build_manifest(CFG, "orders", 3, "test", "orders-war",
                                     "i-123", "v1", "d" * 64,
                                     files=files, unit_content="[Service]\n")
        self.assertIn("instance/conf/server.xml", manifest["file_digests"])
        self.assertEqual(len(manifest["unit_digest"]), 64)
        # brief 原键 "unit.service" 与其自身实现拼出的路径
        # /etc/systemd/system/orders-war.service 永不子串匹配（unit 名是
        # orders-war 而非 unit），按 fake 意图改为命中真实单元文件名。
        server = FakeRemote({
            "server.xml": hashlib.sha256(b"<Server/>").hexdigest(),
            "orders-war.service": hashlib.sha256(b"[Service]\n").hexdigest(),
        })
        self.assertEqual(ti.verify_instance(
            server, CFG["app_home"], "orders-war", manifest), [])
        drift = dict(manifest, file_digests={"instance/conf/server.xml": "0" * 64})
        self.assertIn("instance/conf/server.xml", ti.verify_instance(
            server, CFG["app_home"], "orders-war", drift))

    def test_delete_scope_is_only_runtime_cache(self):
        server = FakeRemote({"base64 -w0": TEMPLATE_B64})
        out = ti.preview(server, CFG, "orders", 3, "test", "orders-war", "i-123")
        self.assertEqual(out["deletes"],
                         ["instance/webapps/*", "instance/work/*", "instance/temp/*"])
        self.assertNotIn("logs", " ".join(out["deletes"]))
        self.assertNotIn("releases", " ".join(out["deletes"]))
