"""平台库快照/恢复的管理端 API 契约

这一层要钉的不是 db_backup 的行为（那些在 tests/test_db_backup.py 里），
而是 HTTP 面：
  - 只有 admin 能拍快照/换库/改保留策略（operator、viewer 一律 403）
  - 一键快照的返回值与列表口径要够前端直接渲染（含 pending、retention）
  - 恢复是「暂存 + 重启生效」，响应必须把 restart_required 说清楚，
    并且在重启前还能取消（否则暂存会在很久以后的一次偶然重启上悄悄换库）
  - 非法快照名/坏校验值不能变成 500：名字来自 URL，是外部输入

安全约束：全程 tempfile 临时目录，绝不读写仓库 data/（那里是真实生产库）。
"""
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from webapp import db_backup
from webapp import db as dbm
from webapp.auth import hash_password
from webapp.app import create_app

ADMIN_PW = "api-admin-pw"


def _retag(db_path: str, name: str, digest: str) -> None:
    """改台账里记的校验值（不改快照文件）：等价于「文件被人动过」"""
    index_path = Path(db_path).resolve().parent / "backups" / "db" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["snapshots"][name]["sha256"] = digest
    index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")


class DbBackupApiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="db_backup_api_"))
        self.app = create_app(data_dir=self.tmp, config_dir=self.tmp / "config",
                              log_dir=self.tmp / "logs")
        self.db = self.app.config["DB_PATH"]
        self.client = self.app.test_client()
        for username, role in (("api_root", "admin"), ("api_ops", "operator"),
                               ("api_view", "viewer")):
            dbm.create_record(self.db, "users", dict(
                username=username, password_hash=hash_password(ADMIN_PW),
                role=role, must_change_password=0, enabled=1,
                created_at=time.time()))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _as(self, username, role_hint=""):
        resp = self.client.post("/api/auth/login",
                                json={"username": username, "password": ADMIN_PW})
        token = resp.get_json()["token"]
        return {"Authorization": f"Bearer {token}"}

    def _admin(self):
        return self._as("api_root")

    def _snapshot(self, label=""):
        resp = self.client.post("/api/db/backups", json={"label": label},
                                headers=self._admin())
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return resp.get_json()

    def _audit_actions(self, action):
        return [r for r in dbm.list_audit_logs(self.db, limit=50)
                if r["action"] == action]

    def _pending_file(self) -> Path:
        return self.tmp / "restore_pending.json"


class AuthGateTest(DbBackupApiCase):
    def test_anonymous_is_401(self):
        for method, path in (("get", "/api/db/backups"),
                             ("get", "/api/db/retention")):
            resp = getattr(self.client, method)(path)
            self.assertEqual(resp.status_code, 401, f"{method.upper()} {path}")
        self.assertEqual(self.client.post("/api/db/backups", json={}).status_code, 401)
        self.assertEqual(self.client.put("/api/db/retention",
                                         json={"keep": 5}).status_code, 401)
        self.assertEqual(self.client.post("/api/db/backups/anything/restore",
                                          json={}).status_code, 401)

    def test_non_admin_is_403(self):
        self._snapshot()
        name = self.client.get("/api/db/backups",
                               headers=self._admin()).get_json()["backups"][0]["name"]
        for username in ("api_ops", "api_view"):
            headers = self._as(username)
            cases = [
                self.client.get("/api/db/backups", headers=headers),
                self.client.post("/api/db/backups", json={}, headers=headers),
                self.client.post(f"/api/db/backups/{name}/restore", json={},
                                 headers=headers),
                self.client.delete("/api/db/restore-pending", headers=headers),
                self.client.get("/api/db/retention", headers=headers),
                self.client.put("/api/db/retention", json={"keep": 5}, headers=headers),
            ]
            for resp in cases:
                self.assertEqual(resp.status_code, 403,
                                 f"{username} 不该碰库维护：{resp.get_json()}")

    def test_disabled_user_is_rejected(self):
        """禁用账号（哪怕手里还有没过期 token）不能拍快照：账号状态每请求回查"""
        uid = dbm.create_record(self.db, "users", dict(
            username="api_off", password_hash=hash_password(ADMIN_PW), role="admin",
            must_change_password=0, enabled=1, created_at=time.time()))
        headers = self._as("api_off")
        self.assertEqual(self.client.get("/api/db/backups",
                                         headers=headers).status_code, 200)
        dbm.update_record(self.db, "users", uid, {"enabled": 0})
        resp = self.client.post("/api/db/backups", json={}, headers=headers)
        self.assertEqual(resp.status_code, 401, "禁用后旧 token 必须失效")
        self.assertEqual(self._snapshot()["kind"], "manual")  # 库仍可正常操作


class SnapshotApiTest(DbBackupApiCase):
    def test_create_returns_the_record_and_lists_it(self):
        rec = self._snapshot(label="上线前")
        for field in ("id", "name", "kind", "label", "size_bytes", "src_bytes",
                      "sha256", "path", "created_at"):
            self.assertIn(field, rec, rec)
        self.assertEqual(rec["kind"], "manual")
        self.assertEqual(rec["label"], "上线前")
        self.assertTrue(Path(rec["path"]).is_file())

        listed = self.client.get("/api/db/backups", headers=self._admin()).get_json()
        self.assertEqual([b["name"] for b in listed["backups"]], [rec["name"]])
        self.assertEqual(listed["pending"], None)
        self.assertEqual(listed["retention"]["keep"], db_backup.DEFAULT_KEEP)

    def test_empty_list_is_not_an_error(self):
        body = self.client.get("/api/db/backups", headers=self._admin()).get_json()
        self.assertEqual(body["backups"], [])
        self.assertIsNone(body["pending"])

    def test_label_is_optional(self):
        self.assertEqual(self._snapshot()["label"], "")

    def test_snapshot_is_audited(self):
        rec = self._snapshot(label="手工")
        hits = self._audit_actions("db.snapshot")
        self.assertEqual(len(hits), 1, hits)
        self.assertEqual(hits[0]["username"], "api_root")
        detail = json.loads(hits[0]["detail"])
        self.assertEqual(detail["name"], rec["name"])
        self.assertEqual(detail["label"], "手工")

    def test_failed_snapshot_is_not_reported_as_success(self):
        """磁盘写不动时必须回错误码并带上原因，不能给前端一个不存在的路径"""
        real = db_backup.snapshot
        db_backup.snapshot = lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full"))
        try:
            resp = self.client.post("/api/db/backups", json={}, headers=self._admin())
        finally:
            db_backup.snapshot = real
        self.assertEqual(resp.status_code, 500)
        self.assertIn("disk full", resp.get_json()["error"],
                      "api.js 把 error 字段当提示文案，原因必须在那儿")


class RestoreApiTest(DbBackupApiCase):
    def test_stage_returns_restart_required_and_shows_pending(self):
        name = self._snapshot()["name"]
        resp = self.client.post(f"/api/db/backups/{name}/restore", json={},
                                headers=self._admin())
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200, body)
        self.assertTrue(body["restart_required"], "运行中不换库是这套设计的底线")
        self.assertEqual(body["name"], name)
        self.assertTrue(self._pending_file().is_file())

        listed = self.client.get("/api/db/backups", headers=self._admin()).get_json()
        self.assertEqual(listed["pending"]["name"], name)
        self.assertTrue(listed["pending"]["restart_required"])
        # 暂存时自动留的 pre-restore 快照也要看得见（前端要能区分种类）
        kinds = [b["kind"] for b in listed["backups"]]
        self.assertIn("pre-restore", kinds)

    def test_stage_is_audited(self):
        name = self._snapshot(label="回滚点")["name"]
        self.client.post(f"/api/db/backups/{name}/restore", json={},
                         headers=self._admin())
        hits = self._audit_actions("db.restore_stage")
        self.assertEqual(len(hits), 1, hits)
        self.assertIn(name, hits[0]["detail"])

    def test_unknown_or_illegal_name_is_400(self):
        """名字来自 URL，是外部输入：没登记过的一律挡回，绝不拼路径去找文件

        路由用 <path:name> 就是为了这个：带斜杠的形式也进到处理函数里由登记表
        裁决，而不是被 URL 路由放过去或者报 500。
        """
        cases = ["platform.999999.20260101-000000.manual.db.gz",  # 格式对但没登记
                 "evil.txt", "sub/platform.000001.manual.db.gz",
                 "..%2Fplatform.db", "..%2F..%2Fdata%2Fplatform.db"]
        for bad in cases:
            resp = self.client.post(f"/api/db/backups/{bad}/restore", json={},
                                    headers=self._admin())
            self.assertEqual(resp.status_code, 400, f"{bad!r} → {resp.get_json()}")
            self.assertIn("快照名不可用", resp.get_json()["error"])
            self.assertFalse(self._pending_file().exists())
        live = self.tmp / "platform.db"
        self.assertTrue(live.is_file(), "拒绝归拒绝，活库不能被动过")
        self.assertEqual(self.client.get("/api/db/backups",
                                         headers=self._admin()).get_json()["backups"], [])

    def test_corrupt_snapshot_is_409_and_leaves_no_pending(self):
        name = self._snapshot()["name"]
        _retag(self.db, name, "0" * 64)  # 台账说该这个哈希，文件不是 → 视同被改动
        resp = self.client.post(f"/api/db/backups/{name}/restore", json={},
                                headers=self._admin())
        self.assertEqual(resp.status_code, 409, resp.get_json())
        self.assertIn("校验", resp.get_json()["error"])
        self.assertFalse(self._pending_file().exists(),
                         "校验不过的快照不能留下待恢复标记")


class CancelPendingTest(DbBackupApiCase):
    def test_cancel_drops_marker_and_staged_payload(self):
        name = self._snapshot()["name"]
        self.client.post(f"/api/db/backups/{name}/restore", json={},
                         headers=self._admin())
        self.assertTrue(self._pending_file().is_file())
        staged = Path(db_backup.pending_restore(self.db)["staged"])
        self.assertTrue(staged.is_file())

        resp = self.client.delete("/api/db/restore-pending", headers=self._admin())
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertFalse(self._pending_file().exists())
        self.assertFalse(staged.exists(), "暂存的整库副本留着白占磁盘且很快过期")
        self.assertIsNone(db_backup.pending_restore(self.db))
        body = self.client.get("/api/db/backups", headers=self._admin()).get_json()
        self.assertIsNone(body["pending"])

    def test_cancel_without_pending_is_ok(self):
        """前端按钮可能和另一次重启撞车：没有待恢复项就是幂等空操作，不是错误"""
        resp = self.client.delete("/api/db/restore-pending", headers=self._admin())
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertFalse(resp.get_json()["cancelled"])

    def test_cancel_keeps_the_snapshots(self):
        """取消的是「重启后换库」这个动作，不是把快照一起删掉"""
        name = self._snapshot()["name"]
        self.client.post(f"/api/db/backups/{name}/restore", json={},
                         headers=self._admin())
        before = [b["name"] for b in
                  self.client.get("/api/db/backups",
                                  headers=self._admin()).get_json()["backups"]]
        self.client.delete("/api/db/restore-pending", headers=self._admin())
        after = [b["name"] for b in
                 self.client.get("/api/db/backups",
                                 headers=self._admin()).get_json()["backups"]]
        self.assertEqual(after, before)
        self.assertIn(name, after)

    def test_cancel_is_audited(self):
        name = self._snapshot()["name"]
        self.client.post(f"/api/db/backups/{name}/restore", json={},
                         headers=self._admin())
        self.client.delete("/api/db/restore-pending", headers=self._admin())
        hits = self._audit_actions("db.restore_cancel")
        self.assertEqual(len(hits), 1, hits)
        self.assertEqual(len(self._audit_actions("db.restore_stage")), 1)


class RetentionApiTest(DbBackupApiCase):
    def test_get_shows_effective_values_bounds_and_defaults(self):
        body = self.client.get("/api/db/retention", headers=self._admin()).get_json()
        self.assertEqual(body["keep"], db_backup.DEFAULT_KEEP)
        self.assertEqual(body["audit_retention_days"], db_backup.DEFAULT_AUDIT_DAYS)
        self.assertEqual(body["bounds"]["keep"], list(db_backup.KEEP_BOUNDS))
        self.assertEqual(body["defaults"]["keep"], db_backup.DEFAULT_KEEP)

    def test_put_persists_and_takes_effect(self):
        resp = self.client.put("/api/db/retention",
                               json={"keep": 5, "audit_retention_days": 90},
                               headers=self._admin())
        self.assertEqual(resp.status_code, 200, resp.get_json())
        got = self.client.get("/api/db/retention", headers=self._admin()).get_json()
        self.assertEqual(got["keep"], 5)
        self.assertEqual(got["audit_retention_days"], 90)
        # 真正被执行侧读到的必须是同一个值，否则设置只是好看
        settings = db_backup.retention_settings(self.db)
        self.assertEqual(settings["keep"], 5)
        self.assertEqual(settings["audit_retention_days"], 90)
        self.assertEqual(len(self._audit_actions("db.retention_update")), 1)

    def test_partial_update_keeps_the_other_value(self):
        self.client.put("/api/db/retention", json={"audit_retention_days": 30},
                        headers=self._admin())
        got = self.client.get("/api/db/retention", headers=self._admin()).get_json()
        self.assertEqual(got["audit_retention_days"], 30)
        self.assertEqual(got["keep"], db_backup.DEFAULT_KEEP)

    def test_out_of_range_is_400_and_changes_nothing(self):
        """0 份等于「拍完就删」，0 天等于「审计全清」——都必须是拒绝而不是夹紧"""
        cases = [({"keep": 0}, "快照保留份数"), ({"keep": 51}, "快照保留份数"),
                 ({"keep": -1}, "快照保留份数"),
                 ({"audit_retention_days": 6}, "审计保留天数"),
                 ({"audit_retention_days": 3651}, "审计保留天数")]
        for payload, expect in cases:
            resp = self.client.put("/api/db/retention", json=payload,
                                   headers=self._admin())
            self.assertEqual(resp.status_code, 400, f"{payload} → {resp.get_json()}")
            # 报错要点名是哪一项，管理员才知道改哪个框
            self.assertIn(expect, resp.get_json()["error"], payload)
        got = self.client.get("/api/db/retention", headers=self._admin()).get_json()
        self.assertEqual(got["keep"], db_backup.DEFAULT_KEEP)
        self.assertEqual(got["audit_retention_days"], db_backup.DEFAULT_AUDIT_DAYS)

    def test_non_numeric_is_400(self):
        for payload in ({"keep": "abc"}, {"keep": None}, {"audit_retention_days": []}):
            resp = self.client.put("/api/db/retention", json=payload,
                                   headers=self._admin())
            self.assertEqual(resp.status_code, 400, f"{payload} → {resp.get_json()}")

    def test_unknown_keys_are_ignored(self):
        resp = self.client.put("/api/db/retention",
                               json={"keep": 4, "keep_forever": True},
                               headers=self._admin())
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(self.client.get("/api/db/retention",
                                         headers=self._admin()).get_json()["keep"], 4)


if __name__ == "__main__":
    unittest.main()
