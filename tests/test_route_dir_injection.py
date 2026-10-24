"""路由与 advisor 的配置/数据目录解耦（P2-6）

背景：多处把目录写死在模块常量上，`create_app(config_dir=…)` 注入后仍读仓库真实目录：
  - `config_routes.CONFIG_DIR` → `/api/config/import` 永远导入 `config/`
  - `config_routes` 导出目标 `DEPLOY_ROOT/data/export/config` → 写进真实 `data/`
  - `deploy_enhance_routes.CONFIG_DIR` → webhook token 永远读 `config/deploy.yaml`
  - `agent/advisor.DATA_DIR` → 灰度决策文件扫描永远看真实 `data/`

本文件锁定：以上落点一律取 `app.config["CONFIG_DIR"] / ["DATA_DIR"]`（路由内）或
`db_path` 所在目录（advisor，后台线程无 app 上下文），注入即生效。

安全约束：临时目录内构造 YAML/决策文件，绝不读写仓库 `config/`、`data/`。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import webapp as webapp_pkg
from webapp import db as dbm


def _minimal_projects_yaml() -> str:
    """一份可被 importer 接受的最小 projects.yaml（只 1 个服务）"""
    return (
        "projects:\n"
        "  iso-svc:\n"
        "    git_org: org\n"
        "    description: 注入测试服务\n"
    )


class ConfigRouteDirInjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="route_dir_"))
        cls.data_dir = cls.tmp / "data"
        cls.config_dir = cls.tmp / "config"
        cls.config_dir.mkdir(parents=True, exist_ok=True)
        # webhook token 只存在于注入目录，仓库 config/ 无此值
        (cls.config_dir / "deploy.yaml").write_text(
            "deploy:\n  webhook_token: injected-token-42\n", encoding="utf-8")
        (cls.config_dir / "projects.yaml").write_text(
            _minimal_projects_yaml(), encoding="utf-8")
        cls.app = webapp_pkg.create_app(data_dir=cls.data_dir,
                                        config_dir=cls.config_dir)
        cls.c = cls.app.test_client()
        token = cls._login(cls.c, "admin", "admin")
        # 首启 admin 带 must_change_password，除改密/取自身外所有接口 403
        r = cls.c.post("/api/auth/password",
                       json={"old_password": "admin", "new_password": "iso1234"},
                       headers=cls._h(token))
        assert r.status_code == 200, r.get_data(as_text=True)
        cls.token = cls._login(cls.c, "admin", "iso1234")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def _login(client, username, password):
        r = client.post("/api/auth/login",
                        json={"username": username, "password": password})
        assert r.status_code == 200, r.get_data(as_text=True)
        return r.get_json()["token"]

    @staticmethod
    def _h(token):
        return {"Authorization": f"Bearer {token}"}

    def test_webhook_token_reads_injected_config_dir(self):
        from webapp.routes.deploy_enhance_routes import _get_webhook_token
        with self.app.test_request_context("/"):
            self.assertEqual(_get_webhook_token(), "injected-token-42")

    def test_config_import_reads_injected_config_dir(self):
        """导入落点 = app.config['CONFIG_DIR']：只有注入目录里的服务会被导入

        断言用「服务名 iso-svc 存在」而非项目计数增长：首启引导已从同一注入目录
        导过一次，再导入是幂等的（复用已有项目），计数不会变。
        """
        db_path = self.app.config["DB_PATH"]
        r = self.c.post("/api/config/import", headers=self._h(self.token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        results = r.get_json()["results"]["projects.yaml"]
        self.assertTrue(results["ok"], msg=results["message"])
        pid = dbm.list_records(db_path, "projects")[0]["id"]
        self.assertIsNotNone(dbm.get_service_by_name(db_path, pid, "iso-svc"),
                             "注入目录的 projects.yaml 未被读取")

    def test_config_export_writes_into_injected_data_dir(self):
        """导出落点 = app.config['DATA_DIR']：不得写进仓库 data/

        export_all 以 stub 拦截：本用例只验证「传给导出器的目标目录」，
        既不让真实凭据 YAML 落到临时盘，也保证「修复前」的失败仅体现在断言上
        ——若不 stub，未修复时它会真的覆盖仓库 data/export/config/（派生产物）。
        """
        with mock.patch("webapp.exporter.export_all", return_value=["projects.yaml"]) as spy:
            r = self.c.get("/api/config/export", headers=self._h(self.token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        target = Path(spy.call_args[0][1]).resolve()
        self.assertTrue(target.is_relative_to(self.data_dir.resolve()),
                        f"导出目录越界：{target}")
        self.assertEqual(Path(r.get_json()["dir"]).resolve(), target)


class AdvisorCanaryDirTest(unittest.TestCase):
    """灰度决策文件扫描按 db_path 所在目录，而非模块 DATA_DIR"""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="advisor_dir_"))
        self.db_path = str(self.tmp / "platform.db")
        dbm.init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_decision(self, name="canary_decision_7.json", state="waiting"):
        (self.tmp / name).write_text(json.dumps(
            {"task_id": 7, "state": state}), encoding="utf-8")

    def test_finds_waiting_file_next_to_db(self):
        from agent.advisor import canary_waiting_files
        self._write_decision()
        out = canary_waiting_files(self.db_path)
        self.assertEqual([tid for tid, _ in out], [7])

    def test_ignores_non_waiting_and_foreign_dirs(self):
        from agent.advisor import canary_waiting_files
        self._write_decision(state="decided")
        self.assertEqual(canary_waiting_files(self.db_path), [])
        # 另一实例的数据目录（本目录的子目录）不得被扫入：glob 不递归、按目录切片
        other = self.tmp / "other"
        other.mkdir()
        (other / "canary_decision_9.json").write_text(
            json.dumps({"task_id": 9, "state": "waiting"}), encoding="utf-8")
        self.assertEqual(canary_waiting_files(self.db_path), [])
        # 反向同理：以 other 为实例目录时，本目录的 decided 文件也不该混入
        self.assertEqual([tid for tid, _ in canary_waiting_files(str(other / "platform.db"))],
                         [9])


class CanaryPathEnvTest(unittest.TestCase):
    """发布子进程侧的决策文件目录：平台注入 PLATFORM_DATA_DIR 后不得写仓库 data/"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="canary_env_"))
        self._orig = os.environ.pop("PLATFORM_DATA_DIR", None)

    def tearDown(self):
        if self._orig is None:
            os.environ.pop("PLATFORM_DATA_DIR", None)
        else:
            os.environ["PLATFORM_DATA_DIR"] = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_env_var_relocates_decision_file(self):
        os.environ["PLATFORM_DATA_DIR"] = str(self.tmp)
        from deploy import canary_decision_path
        self.assertEqual(canary_decision_path(5).resolve(),
                         (self.tmp / "canary_decision_5.json").resolve())

    def test_explicit_dir_beats_env(self):
        other = self.tmp / "other"
        os.environ["PLATFORM_DATA_DIR"] = str(self.tmp)
        from deploy import canary_decision_path
        self.assertEqual(canary_decision_path(6, data_dir=other).resolve(),
                         (other / "canary_decision_6.json").resolve())


class CanaryReadRouteDirInjectionTest(unittest.TestCase):
    """灰度决策的读侧也必须按实例数据目录定位

    写侧（POST）与 advisor 扫描都已改成 db 同目录，唯独 GET 读侧漏了参数：
    注入实例的 waiting 文件写在 <data-dir>/，GET 却去仓库 data/ 找 → 大屏灰度
    弹窗永远显示"无等待中的决策"，人工确认按钮点不动，发布卡到超时。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="canary_read_"))
        self.data_dir = self.tmp / "data"
        self.config_dir = self.tmp / "config"
        self.config_dir.mkdir(parents=True)
        self.app = webapp_pkg.create_app(data_dir=self.data_dir,
                                         config_dir=self.config_dir)
        self.c = self.app.test_client()
        token = self._login("admin", "admin")
        r = self.c.post("/api/auth/password",
                        json={"old_password": "admin", "new_password": "iso1234"},
                        headers=self._h(token))
        assert r.status_code == 200, r.get_data(as_text=True)
        self.token = self._login("admin", "iso1234")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, username, password):
        r = self.c.post("/api/auth/login",
                        json={"username": username, "password": password})
        assert r.status_code == 200, r.get_data(as_text=True)
        return r.get_json()["token"]

    def _h(self, token):
        return {"Authorization": f"Bearer {token}"}

    def _write_decision(self, task_id=77, state="waiting"):
        from deploy import canary_decision_path, data_dir_for_db
        path = canary_decision_path(task_id,
                                    data_dir_for_db(self.app.config["DB_PATH"]))
        path.write_text(json.dumps({"task_id": task_id, "action": "continue",
                                    "state": state}), encoding="utf-8")
        return path

    def test_get_returns_waiting_decision_in_injected_data_dir(self):
        self._write_decision()
        r = self.c.get("/api/deploy/canary-decisions/77",
                       headers=self._h(self.token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json().get("state"), "waiting")

    def test_other_instance_dir_is_not_read(self):
        """反向验证落点唯一：另一实例目录（用 PLATFORM_DATA_DIR 模拟）里的
        waiting 文件不得被本实例的 GET 读到"""
        foreign = self.tmp / "other-instance"
        foreign.mkdir()
        from deploy import data_dir_for_db
        (foreign / "canary_decision_78.json").write_text(
            json.dumps({"task_id": 78, "state": "waiting"}), encoding="utf-8")
        self._write_decision(task_id=78, state="waiting")
        saved = os.environ.get("PLATFORM_DATA_DIR")
        os.environ["PLATFORM_DATA_DIR"] = str(foreign)
        try:
            # 本实例目录里的 78 是 waiting；把环境变量指向另一实例后，
            # 若 GET 仍按环境变量取目录，则会读到 foreign 的同名文件——
            # 断言的是「读到的内容来自本实例」：删掉本实例文件后必须变 none
            (data_dir_for_db(self.app.config["DB_PATH"])
             / "canary_decision_78.json").unlink()
            r = self.c.get("/api/deploy/canary-decisions/78",
                           headers=self._h(self.token))
            self.assertNotEqual(r.get_json().get("state"), "waiting",
                                "GET 按 PLATFORM_DATA_DIR 取目录，读到了别的实例")
        finally:
            if saved is None:
                os.environ.pop("PLATFORM_DATA_DIR", None)
            else:
                os.environ["PLATFORM_DATA_DIR"] = saved


class LogContextRouteDirInjectionTest(unittest.TestCase):
    """/logs/context 的路径遍历防护以 app.config['LOG_DIR'] 为准"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="log_ctx_"))
        self.data_dir = self.tmp / "data"
        self.log_dir = self.tmp / "logs"
        self.config_dir = self.tmp / "config"
        self.config_dir.mkdir(parents=True)
        self.app = webapp_pkg.create_app(data_dir=self.data_dir,
                                         config_dir=self.config_dir,
                                         log_dir=self.log_dir)
        self.c = self.app.test_client()
        token = self._login("admin", "admin")
        r = self.c.post("/api/auth/password",
                        json={"old_password": "admin", "new_password": "iso1234"},
                        headers=self._h(token))
        assert r.status_code == 200, r.get_data(as_text=True)
        self.token = self._login("admin", "iso1234")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, username, password):
        r = self.c.post("/api/auth/login",
                        json={"username": username, "password": password})
        assert r.status_code == 200, r.get_data(as_text=True)
        return r.get_json()["token"]

    def _h(self, token):
        return {"Authorization": f"Bearer {token}"}

    def _entry(self, file_path, offset=0):
        return dbm.create_record(self.app.config["DB_PATH"], "log_entries", dict(
            ts=int(time.time()), env="prod", service="iso-svc",
            container="iso-svc", file_path=str(file_path), line_offset=offset,
            line_count=3, content="target line", keywords="[]"))

    def test_reads_file_under_injected_log_dir(self):
        collect = self.log_dir / "collect"
        collect.mkdir(parents=True, exist_ok=True)
        path = collect / "iso-svc.log"
        path.write_bytes(b"l1\nl2\nl3\n")
        entry_id = self._entry(path, offset=3)
        r = self.c.get(f"/api/logs/context?id={entry_id}&lines=1",
                       headers=self._h(self.token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("l2", "".join(r.get_json()["lines"]))

    def test_still_rejects_path_outside_log_dir(self):
        """防护本身不能被拆掉：目录外的路径依然 403"""
        outside = self.tmp / "secret.log"
        outside.write_text("top secret\n", encoding="utf-8")
        entry_id = self._entry(outside, offset=1)
        r = self.c.get(f"/api/logs/context?id={entry_id}",
                       headers=self._h(self.token))
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
