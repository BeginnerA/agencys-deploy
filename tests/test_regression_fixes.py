"""全量修复回归测试：覆盖 #1-#17 的关键行为（含 verify 阶段发现的 tail 偏移语义补充）"""
import datetime
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from webapp import db as dbm

from common import get_nacos_auth, get_mysql_auth, get_ssh_config, as_number
from deploy import Deployer
from git_tag import tag_project
import health_check as hc
from health_check import check_http
from nacos_manager import NacosClient


class TestTailLogOffset(unittest.TestCase):
    """#1 tail_log 偏移语义：offset 为字符游标；tail 分支返回完整文件字符数
    （已读至末尾的位置），前端增量追尾不重复、不丢失（含中文日志）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log_dir = os.path.join(self.tmp, "logs")
        os.makedirs(self.log_dir, exist_ok=True)
        self.patcher = mock.patch("webapp.task_manager.LOG_DIR",
                                  Path(self.log_dir))
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, text):
        path = Path(self.log_dir) / "task_1.log"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_tail_offset_is_full_char_count(self):
        """tail 分支 offset == 完整文件字符数（非尾部窗口长度、非字节数）"""
        from webapp.task_manager import tail_log
        content = "中文日志行-" * 2000  # 单行 12000 字符，字节数远大于字符数
        path = self._write(content)
        text, offset, truncated = tail_log(path, 0, max_chars=200, tail=True)
        self.assertTrue(truncated)
        self.assertLessEqual(len(text), 200)
        self.assertEqual(offset, len(content))
        # 用该 offset 继续拉取 → 无新内容（尾部窗口不会重复返回）
        text2, offset2, _ = tail_log(path, offset, max_chars=8000, tail=True)
        self.assertEqual(text2, "")
        self.assertEqual(offset2, offset)

    def test_incremental_tail_gets_only_new_lines(self):
        """追尾：追加日志后从返回 offset 拉取只得到新增部分"""
        from webapp.task_manager import tail_log
        content = "line-%04d 中文日志\n" * 3000
        path = self._write(content)
        text1, offset1, truncated = tail_log(path, 0, max_chars=100, tail=True)
        self.assertTrue(truncated)
        with open(path, "a", encoding="utf-8") as f:
            f.write("NEW-SEGMENT\n")
        text2, offset2, _ = tail_log(path, offset1, max_chars=8000, tail=True)
        self.assertEqual(text2, "NEW-SEGMENT\n")
        self.assertEqual(offset2, offset1 + len("NEW-SEGMENT\n"))
        self.assertEqual(text2.count("NEW-SEGMENT"), 1)
        self.assertNotIn("line-2999", text2)  # 旧内容不再返回

    def test_full_consume_no_dup_no_loss(self):
        """模拟前端完整消费：tail 首拉 + 循环 offset 拉取 → 拼接结果
        与完整内容的后缀逐字符一致（无重复、无乱序）"""
        from webapp.task_manager import tail_log
        content = "seq-%04d 中文日志内容\n" * 1500
        path = self._write(content)
        text1, offset, truncated = tail_log(path, 0, max_chars=300, tail=True)
        self.assertTrue(truncated)
        self.assertTrue(content.endswith(text1))  # 尾部窗口是完整内容的后缀
        parts = [text1]
        while True:
            t, offset, _ = tail_log(path, offset, max_chars=8000, tail=False)
            if not t:
                break
            parts.append(t)
        full = "".join(parts)
        self.assertTrue(content.endswith(full))  # 连续后缀：无重复无丢失

    def test_tail_small_file_falls_back_to_full(self):
        from webapp.task_manager import tail_log
        path = self._write("small log\n")
        text, offset, truncated = tail_log(path, 0, max_chars=8000, tail=True)
        self.assertFalse(truncated)
        self.assertEqual(text, "small log\n")
        self.assertEqual(offset, len("small log\n"))


class TestNacosOnlineIpSnapshot(unittest.TestCase):
    """#3 online_ip 快照时序：先全部 set_instance 成功再移除快照；
    任一实例恢复失败时快照保留（下次上线仍可重试）"""

    def _client(self):
        c = object.__new__(NacosClient)  # 绕过 __init__（不加载真实配置）
        c.base = "http://nacos:8848"
        c.namespace = "public"
        c.group = "DEFAULT_GROUP"
        c.username = ""
        c.password = ""
        c._token = None
        c._token_expire = 0
        c._snapshots = {}
        c._snapshotted_keys = set()
        return c

    def test_snapshot_restored_then_removed(self):
        c = self._client()
        key = ("svc", "DEFAULT_GROUP", "10.0.0.1")
        c._snapshots[key] = [{"port": 8080, "cluster": "DEFAULT",
                              "ephemeral": True}]
        c._snapshotted_keys.add(key)
        c.set_instance = mock.Mock()
        c.list_instances = mock.Mock(return_value=[])
        ok = c.online_ip("svc", "10.0.0.1")
        self.assertTrue(ok)
        c.set_instance.assert_called_once()
        # 全部恢复成功 → 快照移除
        self.assertNotIn(key, c._snapshots)
        self.assertNotIn(key, c._snapshotted_keys)

    def test_snapshot_kept_on_total_failure(self):
        c = self._client()
        key = ("svc", "DEFAULT_GROUP", "10.0.0.1")
        c._snapshots[key] = [{"port": 8080, "cluster": "DEFAULT",
                              "ephemeral": True}]
        c._snapshotted_keys.add(key)
        c.set_instance = mock.Mock(side_effect=RuntimeError("nacos unreachable"))
        c.list_instances = mock.Mock(return_value=[])
        with self.assertRaises(RuntimeError):
            c.online_ip("svc", "10.0.0.1")
        # 恢复失败 → 快照保留
        self.assertIn(key, c._snapshots)
        self.assertIn(key, c._snapshotted_keys)

    def test_snapshot_kept_on_partial_failure(self):
        """多实例快照：第二个实例恢复失败 → 快照整体保留"""
        c = self._client()
        key = ("svc", "DEFAULT_GROUP", "10.0.0.1")
        c._snapshots[key] = [
            {"port": 8080, "cluster": "DEFAULT", "ephemeral": True},
            {"port": 8081, "cluster": "DEFAULT", "ephemeral": True},
        ]
        c._snapshotted_keys.add(key)
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("second instance failed")
        c.set_instance = mock.Mock(side_effect=flaky)
        c.list_instances = mock.Mock(return_value=[])
        with self.assertRaises(RuntimeError):
            c.online_ip("svc", "10.0.0.1")
        self.assertIn(key, c._snapshots)
        self.assertIn(key, c._snapshotted_keys)

    def test_no_snapshot_list_fallback(self):
        """无快照：list 兜底纠正新注册实例的 enabled/weight"""
        c = self._client()
        c.set_instance = mock.Mock()
        c.list_instances = mock.Mock(return_value=[
            {"ip": "10.0.0.1", "port": 8080, "clusterName": "DEFAULT",
             "ephemeral": True, "enabled": False, "weight": 0},
        ])
        ok = c.online_ip("svc", "10.0.0.1")
        self.assertTrue(ok)
        c.set_instance.assert_called_once()

    def test_nothing_found_returns_false(self):
        """无快照且 list 不可见 → False（调用方不应等待健康检查）"""
        c = self._client()
        c.set_instance = mock.Mock()
        c.list_instances = mock.Mock(return_value=[
            {"ip": "10.0.0.2", "port": 8080, "clusterName": "DEFAULT",
             "ephemeral": True, "enabled": True, "weight": 1.0},
        ])
        ok = c.online_ip("svc", "10.0.0.1")
        self.assertFalse(ok)
        c.set_instance.assert_not_called()


class TestSchedulerLock(unittest.TestCase):
    """#6 SQLite 租约锁：互斥 + TTL 过期失效 + owner 校验防误删"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mutual_exclusion(self):
        from webapp.deploy_scheduler import (
            release_scheduler_lock, try_acquire_scheduler_lock,
        )
        self.assertTrue(try_acquire_scheduler_lock(self.db))
        self.assertFalse(try_acquire_scheduler_lock(self.db))  # 已持有
        release_scheduler_lock(self.db)
        self.assertTrue(try_acquire_scheduler_lock(self.db))  # 释放后可再获取

    def test_expired_lock_reacquirable(self):
        from webapp.deploy_scheduler import try_acquire_scheduler_lock
        self.assertTrue(try_acquire_scheduler_lock(self.db, ttl=1))
        time.sleep(1.1)
        self.assertTrue(try_acquire_scheduler_lock(self.db))  # 过期自动失效

    def test_release_owner_check(self):
        from webapp.deploy_scheduler import (
            release_scheduler_lock, try_acquire_scheduler_lock,
        )
        self.assertTrue(try_acquire_scheduler_lock(self.db))
        conn = dbm.get_conn(self.db)
        conn.execute(
            "UPDATE scheduler_locks SET owner='other-instance' "
            "WHERE name='deploy_scheduler'")
        conn.commit()
        conn.close()
        release_scheduler_lock(self.db)  # owner 不匹配：不得删除他人锁
        conn = dbm.get_conn(self.db)
        try:
            row = conn.execute(
                "SELECT owner FROM scheduler_locks WHERE name='deploy_scheduler'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["owner"], "other-instance")


class TestSslProbeKeyParse(unittest.TestCase):
    """#8 用 cryptography 真实解析证书公钥（原按证书长度猜类型会把
    ECDSA P-384 误判为 RSA 2048）"""

    def _self_signed_der(self, pubkey, sign_key):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.x509.oid import NameOID
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "regression")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(pubkey)
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=1))
                .sign(sign_key, hashes.SHA256()))
        return cert.public_bytes(serialization.Encoding.DER)

    def test_rsa_2048(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from webapp.ssl_probe import _parse_pubkey_from_der
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        der = self._self_signed_der(key.public_key(), key)
        self.assertEqual(_parse_pubkey_from_der(der), ("RSA", 2048))

    def test_ecdsa_p256(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        from webapp.ssl_probe import _parse_pubkey_from_der
        key = ec.generate_private_key(ec.SECP256R1())
        der = self._self_signed_der(key.public_key(), key)
        self.assertEqual(_parse_pubkey_from_der(der), ("ECDSA", 256))

    def test_invalid_der_returns_empty(self):
        from webapp.ssl_probe import _parse_pubkey_from_der
        self.assertEqual(_parse_pubkey_from_der(b"garbage"), ("", 0))
        self.assertEqual(_parse_pubkey_from_der(b""), ("", 0))


class TestCreateTaskStartedAt(unittest.TestCase):
    """#9 create_task 不写 started_at：由 task_manager 启动时落库，
    排队时长不计入执行耗时"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_started_at_null_until_run(self):
        dbm.create_record(self.db, "projects", dict(
            name="p", code="p", description="", git_org="", created_at=1))
        tid = dbm.create_task(self.db, 1, "project", 1, "app", "prod",
                              "1.0.0", "", 1)
        task = dbm.get_task(self.db, tid)
        self.assertEqual(task["status"], "pending")
        self.assertIsNone(task["started_at"])
        # 启动后由 task_manager 落库
        dbm.update_task(self.db, tid, status="running", started_at=12345)
        task = dbm.get_task(self.db, tid)
        self.assertEqual(task["started_at"], 12345)


class TestListRecordsOrderWhitelist(unittest.TestCase):
    """#10 list_records order 白名单：非法排序回退 id，防 SQL 注入"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_server(self, host):
        return dbm.create_record(self.db, "servers", dict(
            host=host, ssh_port=22, ssh_user="", ssh_password="",
            nacos_ip="", remark="", enabled=1))

    def test_legal_order_applied(self):
        self._make_server("10.0.0.2")
        self._make_server("10.0.0.1")
        rows = dbm.list_records(self.db, "servers", order="host DESC")
        self.assertEqual([r["host"] for r in rows],
                         ["10.0.0.2", "10.0.0.1"])

    def test_injection_order_falls_back_to_id(self):
        self._make_server("10.0.0.2")
        self._make_server("10.0.0.1")
        rows = dbm.list_records(self.db, "servers",
                                order="host; DROP TABLE servers")
        self.assertEqual(len(rows), 2)  # 不报错、未注入
        self.assertEqual([r["host"] for r in rows],
                         ["10.0.0.2", "10.0.0.1"])  # 按 id 排序

    def test_weird_order_falls_back(self):
        self._make_server("10.0.0.1")
        rows = dbm.list_records(self.db, "servers", order="id DESC;--")
        self.assertEqual(len(rows), 1)


class TestMonitorSecurityScore(unittest.TestCase):
    """#12 security 阈值：无数据时 score=None（前端占位），有数据按公式计算"""

    def test_empty_list_none_score(self):
        from webapp.monitor import _compute_security
        result = _compute_security([])
        self.assertEqual(result["total"], 0)
        self.assertIsNone(result["score"])
        self.assertIsNone(result["key_ratio"])

    def test_mixed_servers_score(self):
        from webapp.monitor import _compute_security
        servers = [
            {"ssh_key_file": "/root/.ssh/id_rsa", "ssh_password": ""},
            {"ssh_key_file": "", "ssh_password": "pw"},
            {"ssh_key_file": "", "ssh_password": ""},
        ]
        result = _compute_security(servers)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["score"], 46)  # int((1/3*1.0 + 1/3*0.4)*100)


class TestHealthCheckHttp(unittest.TestCase):
    """#13 check_http 死代码修复：空 body / UP / ok 均视为健康"""

    def _fake_resp(self, status, text):
        return mock.Mock(status_code=status, text=text)

    def test_up_body(self):
        with mock.patch("health_check.requests.get",
                        return_value=self._fake_resp(200, '{"status":"UP"}')):
            self.assertTrue(check_http("http://h/health"))

    def test_empty_body_healthy(self):
        with mock.patch("health_check.requests.get",
                        return_value=self._fake_resp(200, "")):
            self.assertTrue(check_http("http://h/health"))

    def test_ok_lowercase(self):
        with mock.patch("health_check.requests.get",
                        return_value=self._fake_resp(200, "ok")):
            self.assertTrue(check_http("http://h/health"))

    def test_other_status_false(self):
        with mock.patch("health_check.requests.get",
                        return_value=self._fake_resp(503, "DOWN")):
            self.assertFalse(check_http("http://h/health"))

    def test_exception_false(self):
        with mock.patch("health_check.requests.get",
                        side_effect=Exception("timeout")):
            self.assertFalse(check_http("http://h/health"))

    def test_static_mode_200_non_up_body_healthy(self):
        """静态站点语义：200 但 body 无 UP/ok（如 index.html）须判健康（发布/监控同口径）"""
        html = "<!doctype html><html><head><title>管理端</title></head><body></body></html>"
        with mock.patch("health_check.requests.get",
                        return_value=self._fake_resp(200, html)):
            # 默认 Actuator 语义：无 UP/ok 的非空 body → False（旧行为保持）
            self.assertFalse(check_http("https://site.example.com/"))
            # require_health_body=False：200 即 True（修复静态发布误报失败）
            self.assertTrue(check_http("https://site.example.com/",
                                        require_health_body=False))

    def test_static_mode_non_200_still_false(self):
        """静态模式下非 200 依旧判不健康（不误放宽状态码）"""
        with mock.patch("health_check.requests.get",
                        return_value=self._fake_resp(503, "<html>down</html>")):
            self.assertFalse(check_http("https://site.example.com/",
                                        require_health_body=False))


class TestGitTagSuffix(unittest.TestCase):
    """#14 git tag 环境隔离：suffix 非空 → v{version}-{suffix}；留空 → v{version}"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, ".git"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_run(self):
        def fake_run(cmd, **kwargs):
            if cmd[3] == "rev-parse":  # ["git", "-C", path, "rev-parse", ref]
                if cmd[4] == "HEAD":
                    return mock.Mock(stdout="abc123\n")
                raise subprocess.CalledProcessError(1, cmd)
            return mock.Mock(stdout="")
        return mock.patch("git_tag.subprocess.run", side_effect=fake_run)

    def test_with_suffix(self):
        with self._fake_run() as run:
            ok = tag_project({"git_repo": "repo", "image_name": "app"},
                             "1.0.0", self.tmp, push=False, suffix="test")
        self.assertTrue(ok)
        tag_cmds = [c.args[0] for c in run.call_args_list if c.args[0][3] == "tag"]
        self.assertEqual(tag_cmds,
                         [["git", "-C", self.repo, "tag", "v1.0.0-test"]])

    def test_without_suffix(self):
        with self._fake_run() as run:
            ok = tag_project({"git_repo": "repo", "image_name": "app"},
                             "1.0.0", self.tmp, push=False, suffix="")
        self.assertTrue(ok)
        tag_cmds = [c.args[0] for c in run.call_args_list if c.args[0][3] == "tag"]
        self.assertEqual(tag_cmds,
                         [["git", "-C", self.repo, "tag", "v1.0.0"]])


class TestUpdateScheduleCronValidation(unittest.TestCase):
    """#15 update_schedule cron 校验 + next_run_at 重算（非法表达式会致计划
    静默停摆，更新时必须校验并立即生效）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db_path)
        from webapp import create_app
        # 目录靠 create_app 注入：裸调用会让工厂在默认落点上跑一遍迁移/引导
        self.app = create_app(data_dir=self.tmp)
        self.app.config["DB_PATH"] = self.db_path
        self.app.config["TESTING"] = True
        with self.app.app_context():
            self.proj_id = dbm.create_record(self.db_path, "projects", dict(
                name="p1", code="p1", description="", git_org="",
                created_at=int(time.time())))
            from werkzeug.security import generate_password_hash
            dbm.create_record(self.db_path, "users", dict(
                username="admin", password_hash=generate_password_hash("test123"),
                role="admin", must_change_password=0, enabled=1,
                created_at=int(time.time())))
        self.client = self.app.test_client()
        resp = self.client.post("/api/auth/login",
                                json={"username": "admin", "password": "test123"})
        self.token = resp.get_json()["token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.sid = dbm.create_record(self.db_path, "deploy_schedules", dict(
            project_id=self.proj_id, type="cron", name="sched",
            ref_type="project", ref_id=self.proj_id, env="prod",
            version="1.0.0", previous_version="", extra="[]",
            schedule_at=0, cron_expr="0 2 * * *", enabled=1,
            last_run_at=0, next_run_at=0, created_by=1,
            created_at=int(time.time())))

    def test_invalid_cron_rejected(self):
        resp = self.client.put(f"/api/deploy/schedules/{self.sid}",
                               headers=self.headers,
                               json={"cron_expr": "not a cron"})
        self.assertEqual(resp.status_code, 400)

    def test_empty_cron_rejected(self):
        resp = self.client.put(f"/api/deploy/schedules/{self.sid}",
                               headers=self.headers, json={"cron_expr": ""})
        self.assertEqual(resp.status_code, 400)

    def test_valid_cron_recomputes_next_run(self):
        resp = self.client.put(f"/api/deploy/schedules/{self.sid}",
                               headers=self.headers,
                               json={"cron_expr": "*/5 * * * *"})
        self.assertEqual(resp.status_code, 200)
        rec = dbm.get_record(self.db_path, "deploy_schedules", self.sid)
        self.assertEqual(rec["cron_expr"], "*/5 * * * *")
        self.assertGreater(rec["next_run_at"], 0)


class TestNoNacosEnvironment(unittest.TestCase):
    """#16 环境未接入 Nacos（nacos.url 为空）时发布跳过全部 Nacos 操作：
    空 url 不再拼出无 scheme 的 Invalid URL 中断发布（无 Nacos 服务发布回归）"""

    def test_get_nacos_auth_empty_url(self):
        """表单保存过的全空 nacos 段：url 为空、group 回退默认值"""
        auth = get_nacos_auth({"nacos": {"url": "", "namespace": "", "group": "",
                                         "username": "", "password": ""}})
        self.assertEqual(auth["url"], "")
        self.assertEqual(auth["group"], "DEFAULT_GROUP")

    def test_get_nacos_auth_missing_section(self):
        """历史配置完全缺失 nacos 段：不再 KeyError"""
        auth = get_nacos_auth({})
        self.assertEqual(auth["url"], "")
        self.assertEqual(auth["group"], "DEFAULT_GROUP")

    def test_client_guard_when_unconfigured(self):
        """url 为空时实例操作抛可诊断错误（而非 requests 的 Invalid URL）"""
        c = object.__new__(NacosClient)
        c.base = ""
        with self.assertRaises(RuntimeError) as ctx:
            c.list_instances("im-platform")
        self.assertIn("未配置", str(ctx.exception))

    def _deployer(self, configured: bool, groups: list) -> Deployer:
        d = Deployer.__new__(Deployer)  # 绕过 __init__（不加载真实配置）
        d.nacos = mock.Mock()
        d.nacos.configured = configured
        d.nacos.group = "DEFAULT_GROUP"
        d.proj_cfg = {"nacos_groups": groups}
        return d

    def test_groups_empty_when_unconfigured(self):
        """未接入 Nacos：即使服务级配了分组也返回空列表（跳过全部 Nacos 操作）"""
        d = self._deployer(configured=False, groups=["DEFAULT_GROUP"])
        self.assertEqual(d.nacos_groups(), [])

    def test_groups_service_level_priority(self):
        """已接入 Nacos：服务级分组优先；缺失时回退全局 group"""
        d = self._deployer(configured=True, groups=["DUBBO_GROUP"])
        self.assertEqual(d.nacos_groups(), ["DUBBO_GROUP"])
        d.proj_cfg = {}
        self.assertEqual(d.nacos_groups(), ["DEFAULT_GROUP"])

    def test_deploy_server_succeeds_without_nacos(self):
        """端到端：未接入 Nacos 的服务发布成功且零 Nacos 实例调用（原 bug 场景）"""
        d = Deployer.__new__(Deployer)
        d.project = "im-platform"
        d.version = "1.0.0"
        d.previous_version = None
        d.env = "test"
        d.ssh_cfg = {}
        d.config = {"deploy": {"canary_wait": 0}}
        d.service_name = "im-platform"
        d.proj_cfg = {"nacos_groups": [], "dubbo_services": [],
                      "service_name": "im-platform"}
        d.container_modified = False
        d.nacos = mock.Mock()
        d.nacos.configured = False
        d.nacos.group = ""
        server = mock.Mock()
        with mock.patch("deploy.RemoteServer", return_value=server), \
                mock.patch.object(d, "ensure_docker_login"), \
                mock.patch.object(d, "update_container"), \
                mock.patch("deploy.wait_for_healthy"), \
                mock.patch("deploy.log"):
            ok = d.deploy_server("47.95.172.17", None)
        self.assertTrue(ok)
        server.close.assert_called_once()
        d.nacos.offline_ip.assert_not_called()
        d.nacos.online_ip.assert_not_called()


class TestEmptyNumberConfig(unittest.TestCase):
    """#17 Dashboard 清空数字字段会存为空串（health_timeout: '' 等），
    空串被原样取出参与算术，曾抛 unsupported operand type(s) for +: 'float' and 'str'
    使已成功上线的发布被误判失败并触发回滚流程（无 Nacos 服务的重发布回归）"""

    def test_as_number_semantics(self):
        """空串/None/非法值回退默认；数字字符串可解析；整数结果保持 int"""
        self.assertEqual(as_number("", 120), 120)
        self.assertEqual(as_number(None, 5), 5)
        self.assertEqual(as_number("abc", 5), 5)
        self.assertEqual(as_number("120", 5), 120)
        self.assertEqual(as_number(120.0, 5), 120)
        self.assertEqual(as_number(2.5, 5), 2.5)

    def _wait(self, deploy_cfg: dict, is_running=None):
        """在空串/非法数字配置下执行 wait_for_healthy，返回 (结果, sleep_mock, log_mock)"""
        cfg = {"deploy": deploy_cfg}
        proj = {"service_name": "im-platform", "health_url": "", "log_error_keywords": []}
        sleep_mock = mock.Mock()
        with mock.patch.object(hc, "load_config", return_value=cfg), \
                mock.patch.object(hc, "get_project_config", return_value=proj), \
                mock.patch.object(hc, "get_ssh_config", return_value={}), \
                mock.patch.object(hc, "check_container_running", side_effect=is_running or [False, True]), \
                mock.patch.object(hc, "check_container_logs", return_value=True), \
                mock.patch.object(hc.time, "sleep", sleep_mock), \
                mock.patch.object(hc, "log") as log_mock:
            ok = hc.wait_for_healthy("47.95.172.17", "im-platform")
        return ok, sleep_mock, log_mock

    def test_wait_for_healthy_empty_numeric_config(self):
        """health_timeout/health_interval 为空串：回退 120/5，不再 float+str 崩溃（原 bug 场景）"""
        ok, sleep_mock, log_mock = self._wait({"health_timeout": "", "health_interval": ""})
        self.assertTrue(ok)
        sleep_mock.assert_called_once_with(5)  # interval 回退 5
        self.assertTrue(any("超时 120 秒" in str(c) for c in log_mock.call_args_list))

    def test_wait_for_healthy_zero_timeout_falls_back(self):
        """health_timeout=0 视为无效：回退 120（否则 deadline 立即过期必判超时）"""
        ok, _, log_mock = self._wait({"health_timeout": 0, "health_interval": 0}, is_running=[True])
        self.assertTrue(ok)
        self.assertTrue(any("超时 120 秒" in str(c) for c in log_mock.call_args_list))

    def _deployer_for_canary(self, canary_wait):
        d = Deployer.__new__(Deployer)  # 绕过 __init__（不加载真实配置）
        d.project = "im-platform"
        d.version = "1.0.0"
        d.previous_version = None
        d.env = "test"
        d.ssh_cfg = {}
        d.config = {"deploy": {"canary_wait": canary_wait}}
        d.service_name = "im-platform"
        d.proj_cfg = {"nacos_groups": [], "service_name": "im-platform"}
        d.container_modified = False
        d.nacos = mock.Mock()
        d.nacos.configured = False
        d.nacos.group = ""
        return d

    def _deploy_canary(self, canary_wait, sleep_mock):
        d = self._deployer_for_canary(canary_wait)
        server = mock.Mock()
        with mock.patch("deploy.RemoteServer", return_value=server), \
                mock.patch.object(d, "ensure_docker_login"), \
                mock.patch.object(d, "update_container"), \
                mock.patch("deploy.wait_for_healthy"), \
                mock.patch("deploy.time.sleep", sleep_mock), \
                mock.patch("deploy.log"):
            return d.deploy_server("47.95.172.17", None, is_canary=True)

    def test_canary_empty_wait_not_crash(self):
        """canary_wait 为空串：灰度发布不再 ''.0 > 0 的 TypeError 崩溃"""
        ok = self._deploy_canary("", mock.Mock())
        self.assertTrue(ok)

    def test_canary_string_wait_sleeps(self):
        """canary_wait 为数字字符串：解析后灰度观察等待"""
        sleep_mock = mock.Mock()
        ok = self._deploy_canary("60", sleep_mock)
        self.assertTrue(ok)
        sleep_mock.assert_called_once_with(60)

    def test_ssh_port_empty_falls_back(self):
        """ssh.port 为空串：回退 22（paramiko 收到空串会连接失败）"""
        ssh = get_ssh_config({"ssh": {"user": "root", "port": "",
                                        "key_file": "~/.ssh/id_rsa_deploy", "password": ""}})
        self.assertEqual(ssh["port"], 22)
        ssh2 = get_ssh_config({"ssh": {"user": "root", "port": "2222",
                                         "key_file": "~/.ssh/id_rsa_deploy", "password": ""}})
        self.assertEqual(ssh2["port"], 2222)

    def test_mysql_port_empty_falls_back(self):
        """mysql_snailjob.port 为空串：回退 3306（pymysql 收到空串会连接失败）"""
        auth = get_mysql_auth({"mysql_snailjob": {"host": "10.0.0.1", "port": "",
                                                   "database": "snailjob", "username": "", "password": ""}})
        self.assertEqual(auth["port"], 3306)


if __name__ == "__main__":
    unittest.main()
