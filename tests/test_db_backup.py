"""平台库快照与恢复（item 2）：快照/轮转/恢复暂存/启动应用/审计保留

设计约束（这些用例就是在钉它们）：
  - 快照用 `VACUUM INTO`（只读连接）而不是复制文件：真实 `data/platform.db` 1.21 GB
    里只有 72 MB 在用（freelist 94%），复制既慢又膨胀；VACUUM INTO 顺带回收，
    且只读连接不可能写坏活库，活库有未提交写事务时也能拍。
  - 恢复必须"暂存 + 重启生效"：平台进程、采集线程、deploy 子进程都持着同一个库，
    运行中换文件等于让各方读页缓存打架。
  - 待恢复标记写在库外（data/restore_pending.json）：写进库里的话，恢复动作本身
    会把标记一起恢复回来，下次启动再恢复一次，形成死循环。
  - 换库前把旧文件 rename 进 discarded/：快照之后的写入不留痕迹是不可接受的。

安全约束：全程 tempfile 临时目录，绝不读写仓库 data/（那里是真实 1.2 GB 生产库）。
"""
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from webapp import create_app
from webapp import db as dbm
from webapp import db_backup


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _inflate(snapshot_path: Path) -> Path:
    """快照落盘是 .db.gz，测试里还原成 .unpacked.db 便于直接开库断言"""
    out = snapshot_path.with_suffix(".unpacked.db")
    with gzip.open(snapshot_path, "rb") as src, open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return out


def _bloat(db_path: str, rows: int = 220, keep: int = 6):
    """造一个大库再删掉大部分：auto_vacuum=INCREMENTAL 只把页挂进 freelist
    不还给操作系统，文件因此虚胖——正是要靠 VACUUM INTO 收回来的形态"""
    conn = dbm.get_conn(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS filler(x BLOB)")
        conn.executemany("INSERT INTO filler VALUES (?)",
                         [(b"agencys-deploy" * 400,) for _ in range(rows)])
        conn.commit()
        conn.execute("DELETE FROM filler WHERE rowid > ?", (keep,))
        conn.commit()
    finally:
        conn.close()


def _add_project(db_path: str, name: str) -> int:
    return dbm.create_record(db_path, "projects", dict(
        name=name, code=f"{name.lower()}-{abs(hash(name)) % 10**6}",
        description="", git_org="", created_at=int(time.time())))


def _diverge_schema(db_path: str, column: str = "legacy_col"):
    """把库结构改成本版本 init_db 产物的超集以外的形状：等价于「这次启动的
    _migrate 还要动结构」，用来触发迁移安全网"""
    conn = dbm.get_conn(db_path)
    conn.execute(f"ALTER TABLE projects ADD COLUMN {column} TEXT DEFAULT ''")
    conn.commit()
    conn.close()


def _pragma(db_path: str, statement: str):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(statement).fetchone()[0]
    finally:
        conn.close()


def _columns(db_path: str, table: str = "projects"):
    conn = sqlite3.connect(db_path)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _unset(db_path: str, key: str) -> None:
    """设置项回到「没配过」，让代码走默认值"""
    conn = dbm.get_conn(db_path)
    conn.execute("DELETE FROM platform_settings WHERE key=?", (key,))
    conn.commit()
    conn.close()


def _retag_checksum(db_path: str, name: str, digest: str) -> None:
    """直接编辑台账里的校验值：模拟有人绕过接口改登记"""
    index = Path(db_backup._index_path(db_path))
    data = json.loads(index.read_text(encoding="utf-8"))
    data["snapshots"][name]["sha256"] = digest
    index.write_text(json.dumps(data), encoding="utf-8")


def _setting(db_path: str, key: str, value):
    conn = dbm.get_conn(db_path)
    try:
        conn.execute("INSERT INTO platform_settings(key,value,updated_at) VALUES(?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                     "updated_at=excluded.updated_at",
                     (key, json.dumps(value), int(time.time())))
        conn.commit()
    finally:
        conn.close()


class BackupCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="db_backup_test_"))
        self.db = str(self.tmp / "platform.db")
        dbm.init_db(self.db)
        self.data_dir = self.tmp

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _backup_files(self):
        """备份目录里的所有条目（含半截临时文件），目录还没建视作空"""
        d = self.data_dir / "backups" / "db"
        return sorted(p.name for p in d.glob("*")) if d.is_dir() else []

    def _snapshot_names(self):
        d = self.data_dir / "backups" / "db"
        return sorted(p.name for p in d.glob("*.gz")) if d.is_dir() else []

    def _projects(self):
        conn = dbm.get_conn(self.db)
        try:
            return sorted(r["name"] for r in conn.execute("SELECT name FROM projects"))
        finally:
            conn.close()

    def _seed_audit(self, *ages_days):
        """按天数造审计行（保留期只看 created_at 落在窗口内外）"""
        conn = dbm.get_conn(self.db)
        try:
            now = int(time.time())
            for i, days in enumerate(ages_days):
                conn.execute(
                    "INSERT INTO audit_logs(user_id,username,action,target_type,target_id,"
                    "detail,created_at) VALUES(1,'admin',?,'config',0,'{}',?)",
                    (f"act{i}", now - int(days * 86400)))
            conn.commit()
        finally:
            conn.close()

    def _audit_count(self):
        conn = dbm.get_conn(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        finally:
            conn.close()


class SnapshotTest(BackupCase):
    def test_snapshot_vacuums_and_gzips(self):
        _bloat(self.db)
        live_before = os.path.getsize(self.db)
        rec = db_backup.snapshot(self.db)
        path = Path(rec["path"])
        self.assertTrue(path.exists(), rec)
        self.assertLess(path.stat().st_size, live_before,
                        "快照应当比虚胖的活库小得多（VACUUM INTO 回收 freelist + gzip）")
        inflated = _inflate(path)
        live_freelist = _pragma(self.db, "PRAGMA freelist_count")
        snap = sqlite3.connect(inflated)
        try:
            self.assertEqual(snap.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertLess(snap.execute("PRAGMA freelist_count").fetchone()[0],
                            live_freelist)
        finally:
            snap.close()
        inflated.unlink()

    def test_snapshot_leaves_live_db_untouched_and_writable(self):
        pid = _add_project(self.db, "旺龙")
        db_backup.snapshot(self.db)
        _add_project(self.db, "另一个")
        conn = dbm.get_conn(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 2)
            conn.execute("UPDATE projects SET description='仍然可写' WHERE id=?", (pid,))
            conn.commit()
        finally:
            conn.close()

    def test_snapshot_succeeds_while_a_write_transaction_is_open(self):
        """一键快照不能要求先停平台：活库持有未提交写事务时也要拍得下来"""
        writer = dbm.get_conn(self.db)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO projects(name,code,description,created_at) "
                       "VALUES('事务中','tx','',?)", (int(time.time()),))
        try:
            rec = db_backup.snapshot(self.db)
            self.assertTrue(Path(rec["path"]).exists())
        finally:
            writer.rollback()
            writer.close()

    def test_snapshot_record_carries_checksum_and_sizes(self):
        _bloat(self.db)
        rec = db_backup.snapshot(self.db, kind="manual", label="发布前")
        for key in ("id", "name", "kind", "label", "size_bytes", "src_bytes",
                    "sha256", "path", "created_at"):
            self.assertIn(key, rec, rec)
        self.assertEqual(rec["kind"], "manual")
        self.assertEqual(rec["label"], "发布前")
        self.assertEqual(rec["src_bytes"], os.path.getsize(self.db))
        self.assertEqual(rec["size_bytes"], os.path.getsize(rec["path"]))
        self.assertEqual(rec["sha256"], _sha256(Path(rec["path"])),
                         "校验值必须是最终落盘文件（含 gzip 后）的，否则恢复前无从判损坏")
        listed = db_backup.list_snapshots(self.db)
        self.assertEqual([r["id"] for r in listed], [rec["id"]])

    def test_two_snapshots_in_the_same_second_do_not_collide(self):
        a = db_backup.snapshot(self.db)
        b = db_backup.snapshot(self.db)
        self.assertNotEqual(a["name"], b["name"])
        self.assertEqual(len(self._snapshot_names()), 2)

    def test_failed_snapshot_leaves_no_orphan(self):
        """快照两步都可能挂（磁盘/权限写坏、库被并发变更）。两头都要干净：
        写文件失败不能留下半截 .part，登记失败不能留下没人引用的 .gz。"""
        real_vacuum = db_backup._vacuum_gzip

        def dirty_then_fail(db_path, tmp_out):
            Path(tmp_out).write_bytes(b"half written")
            raise OSError("disk on fire")

        db_backup._vacuum_gzip = dirty_then_fail
        try:
            with self.assertRaises(OSError):
                db_backup.snapshot(self.db)
        finally:
            db_backup._vacuum_gzip = real_vacuum
        self.assertEqual(self._backup_files(), [],
                         f"失败后备份目录里只剩这些：{self._backup_files()}")
        self.assertEqual(db_backup.list_snapshots(self.db), [])

        real_record = db_backup._record
        db_backup._record = lambda *a, **k: (_ for _ in ()).throw(
            sqlite3.OperationalError("db busy"))
        try:
            with self.assertRaises(sqlite3.OperationalError):
                db_backup.snapshot(self.db)
        finally:
            db_backup._record = real_record
        self.assertEqual(self._snapshot_names(), [],
                         "登记失败的快照文件必须回收，否则轮转看不见它会一直吃磁盘")


class RotationTest(BackupCase):
    def _make(self, n):
        """先放开保留数再连拍：快照自带轮转，不然种不出超额快照"""
        _setting(self.db, db_backup.KEEP_KEY, 50)
        recs = []
        for i in range(n):
            _add_project(self.db, f"p{i}")
            recs.append(db_backup.snapshot(self.db, label=f"#{i}"))
        return recs

    def test_prune_keeps_newest_n(self):
        recs = self._make(db_backup.DEFAULT_KEEP + 2)
        _unset(self.db, db_backup.KEEP_KEY)  # 回到没配过 → 走默认保留数
        deleted = db_backup.prune(self.db)
        self.assertEqual(sorted(deleted), sorted(r["name"] for r in recs[:2]), deleted)
        alive = [r["name"] for r in db_backup.list_snapshots(self.db)]
        self.assertEqual(sorted(alive), sorted(r["name"] for r in recs[-db_backup.DEFAULT_KEEP:]),
                         "存活的应当是最新 N 个")
        for name in deleted:
            self.assertFalse((self.data_dir / "backups" / "db" / name).exists())

    def test_keep_comes_from_platform_settings(self):
        self._make(4)
        _setting(self.db, db_backup.KEEP_KEY, 1)
        db_backup.prune(self.db)
        self.assertEqual(len(db_backup.list_snapshots(self.db)), 1)

    def test_prune_never_deletes_the_newest_snapshot(self):
        """keep 被配成 0/负数/非法值时也不能把唯一的回滚点删掉"""
        self._make(2)
        _setting(self.db, db_backup.KEEP_KEY, 0)
        db_backup.prune(self.db)
        self.assertGreaterEqual(len(db_backup.list_snapshots(self.db)), 1)
        _setting(self.db, db_backup.KEEP_KEY, "not-a-number")
        db_backup.prune(self.db)
        self.assertGreaterEqual(len(db_backup.list_snapshots(self.db)), 1)

    def test_snapshot_rotates_by_itself(self):
        """一键快照点十次不能留十份：轮转挂在快照尾部，不靠调用方记得收尾"""
        _setting(self.db, db_backup.KEEP_KEY, 50)
        self._make(4)
        _setting(self.db, db_backup.KEEP_KEY, 2)
        rec = db_backup.snapshot(self.db)
        alive = [r["name"] for r in db_backup.list_snapshots(self.db)]
        self.assertEqual(sorted(alive), sorted(self._snapshot_names()))
        self.assertEqual(len(alive), 2, alive)
        self.assertIn(rec["name"], alive, "最新一份永远不能在轮转里被删掉")

    def test_listing_flags_missing_file(self):
        rec = db_backup.snapshot(self.db)
        os.unlink(rec["path"])
        row = db_backup.list_snapshots(self.db)[0]
        self.assertTrue(row["file_missing"], "文件被人为删过要在列表里显出来，别等到恢复才发现")


class SnapshotFixture(BackupCase):
    """一份快照 + 快照之后又写了一次：恢复对了/错了都能一眼看出来"""

    def setUp(self):
        super().setUp()
        _add_project(self.db, "快照时的项目")
        self.rec = db_backup.snapshot(self.db)
        _add_project(self.db, "快照之后的写入")


class StageRestoreTest(SnapshotFixture):
    def test_only_registered_names_can_be_restored(self):
        for bad in ("../platform.db", "platform.db", "evil.txt", "",
                    self.rec["name"] + "; DROP TABLE projects",
                    "sub/" + self.rec["name"], self.rec["name"].upper()):
            with self.assertRaises(ValueError, msg=bad):
                db_backup.stage_restore(self.db, bad)
        self.assertIsNone(db_backup.pending_restore(self.db),
                          "被拒的恢复不能留下待恢复标记")
        self.assertEqual(self._projects(), ["快照之后的写入", "快照时的项目"],
                         "校验失败不能碰活库")

    def test_checksum_mismatch_is_refused_before_staging(self):
        Path(self.rec["path"]).write_bytes(gzip.compress(b"this is not a sqlite database"))
        with self.assertRaises(db_backup.RestoreError):
            db_backup.stage_restore(self.db, self.rec["name"])
        self.assertIsNone(db_backup.pending_restore(self.db))

    def test_non_database_payload_refused_even_when_checksum_matches(self):
        """台账里的校验值也一起改掉（有人直接编辑 index.json）时，
        integrity_check 是第二道闸"""
        path = Path(self.rec["path"])
        junk = gzip.compress(b"this is not a sqlite database")
        path.write_bytes(junk)
        _retag_checksum(self.db, self.rec["name"], _sha256(path))
        with self.assertRaises(db_backup.RestoreError):
            db_backup.stage_restore(self.db, self.rec["name"])
        self.assertIsNone(db_backup.pending_restore(self.db))

    def test_stage_takes_a_pre_restore_snapshot_then_marks_pending(self):
        res = db_backup.stage_restore(self.db, self.rec["name"])
        self.assertTrue(res["restart_required"], res)
        kinds = [r["kind"] for r in db_backup.list_snapshots(self.db)]
        self.assertIn("pre-restore", kinds, "暂存恢复必须先把当前库拍一份，否则换库无退路")
        pending = db_backup.pending_restore(self.db)
        self.assertEqual(pending["name"], self.rec["name"])
        self.assertTrue((self.data_dir / db_backup.PENDING_FILE).exists())

    def test_pending_marker_lives_outside_the_database(self):
        """标记若在库内，恢复动作会把它一起恢复回来 → 每次启动都再恢复一次"""
        db_backup.stage_restore(self.db, self.rec["name"])
        conn = dbm.get_conn(self.db)
        try:
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn(db_backup.PENDING_FILE.replace(".json", ""), names)
            rows = conn.execute("SELECT key FROM platform_settings").fetchall()
            self.assertNotIn("restore_pending", {r["key"] for r in rows})
        finally:
            conn.close()
        self.assertTrue((self.data_dir / db_backup.PENDING_FILE).is_file())


class ApplyPendingTest(SnapshotFixture):
    def setUp(self):
        super().setUp()
        db_backup.stage_restore(self.db, self.rec["name"])

    def test_apply_swaps_in_snapshot_content(self):
        self.assertIn("快照之后的写入", self._projects())
        res = db_backup.apply_pending_restore(self.db)
        self.assertIsNotNone(res)
        self.assertEqual(self._projects(), ["快照时的项目"])

    def test_apply_clears_wal_and_shm_of_the_discarded_db(self):
        """旧库的 -wal 里可能还压着未合并页；留在原地会被新库当自己的日志续写"""
        wal = Path(self.db + "-wal")
        shm = Path(self.db + "-shm")
        conn = dbm.get_conn(self.db)
        conn.execute("INSERT INTO projects(name,code,description,created_at) "
                     "VALUES('wal里','wal','',?)", (int(time.time()),))
        conn.commit()
        conn.close()
        wal.write_bytes(b"pretend unmerged frames")
        shm.write_bytes(b"pretend shm")
        db_backup.apply_pending_restore(self.db)
        self.assertFalse(wal.exists(), "残留 -wal 会让 SQLite 把旧事务接回新库")
        self.assertFalse(shm.exists())
        self.assertEqual(self._projects(), ["快照时的项目"])

    def test_apply_is_one_shot(self):
        self.assertIsNotNone(db_backup.apply_pending_restore(self.db))
        self.assertIsNone(db_backup.apply_pending_restore(self.db))
        _add_project(self.db, "恢复之后的写入")
        self.assertIsNone(db_backup.apply_pending_restore(self.db))
        self.assertIn("恢复之后的写入", self._projects())

    def test_apply_keeps_a_discarded_copy_of_what_it_replaced(self):
        conn = dbm.get_conn(self.db)
        try:
            conn.execute("INSERT INTO projects(name,code,description,created_at) "
                         "VALUES('换库前的写入','gone','',?)", (int(time.time()),))
            conn.commit()
        finally:
            conn.close()
        db_backup.apply_pending_restore(self.db)
        disc = list((self.data_dir / "backups" / "db" / "discarded").rglob("*.db"))
        self.assertTrue(disc, "被换掉的库必须留一份可手工回滚的原文件")
        c = sqlite3.connect(f"file:{disc[0]}?mode=ro", uri=True)
        try:
            self.assertIn("换库前的写入",
                          [r[0] for r in c.execute("SELECT name FROM projects")])
        finally:
            c.close()

    def test_apply_aborts_on_tampered_payload_and_keeps_live_db(self):
        """待恢复文件在校验不过时绝不能换库：宁可退回原状也不要把损坏文件当真相"""
        before = _sha256(Path(self.db))
        pending = json.loads((self.data_dir / db_backup.PENDING_FILE).read_text(encoding="utf-8"))
        staged = Path(pending["staged"])
        staged.write_bytes(staged.read_bytes()[:16])
        with self.assertRaises(db_backup.RestoreError):
            db_backup.apply_pending_restore(self.db)
        self.assertEqual(_sha256(Path(self.db)), before)
        self.assertIn("快照之后的写入", self._projects())
        self.assertIsNone(db_backup.pending_restore(self.db),
                          "失败的待恢复标记要消费掉，否则每次启动都重试一份坏文件")


class CancelPendingRestoreTest(SnapshotFixture):
    """暂存了但还没重启，管理员反悔了

    没有取消入口的话，一次暂存会在很久以后某次偶然重启时悄悄换库——
    那已经不是我点下的那个动作了（保留策略改完顺手重启，库就回去了）。
    """

    def _stage(self):
        db_backup.stage_restore(self.db, self.rec["name"])
        return Path(db_backup.pending_restore(self.db)["staged"])

    def test_cancel_removes_marker_and_staged_copy(self):
        staged = self._stage()
        self.assertTrue(staged.is_file())
        out = db_backup.cancel_pending_restore(self.db)
        self.assertEqual(out["name"], self.rec["name"])
        self.assertFalse((self.data_dir / db_backup.PENDING_FILE).exists())
        self.assertFalse(staged.exists(), "整库副本留着白占磁盘，而且很快就过期")
        self.assertIsNone(db_backup.pending_restore(self.db))

    def test_apply_after_cancel_restores_nothing(self):
        self._stage()
        db_backup.cancel_pending_restore(self.db)
        self.assertIsNone(db_backup.apply_pending_restore(self.db))
        self.assertIn("快照之后的写入", self._projects(), "取消之后重启不该换库")

    def test_cancel_without_a_marker_is_a_no_op(self):
        """取消按钮可能按在已经重启过的窗口上：没有待恢复项就是空操作，不是错误"""
        self.assertIsNone(db_backup.cancel_pending_restore(self.db))
        self._stage()
        db_backup.cancel_pending_restore(self.db)
        self.assertIsNone(db_backup.cancel_pending_restore(self.db))
        self.assertFalse((self.data_dir / db_backup.PENDING_FILE).exists())

    def test_cancel_keeps_the_snapshots(self):
        """取消的是「重启后换库」这个动作；快照是普通文件，一份都不该少"""
        self._stage()
        before = self._snapshot_names()
        self.assertIn(self.rec["name"], before)
        db_backup.cancel_pending_restore(self.db)
        self.assertEqual(self._snapshot_names(), before)


class StartupWiringTest(SnapshotFixture):
    """恢复必须发生在 init_db 之前：否则旧库先被本版本迁移改过，再被换掉，
    等于把迁移结果丢进垃圾桶，还可能撞上迁移不幂等的分支"""

    def _boot(self):
        return create_app(data_dir=self.data_dir,
                          config_dir=self.tmp / "config",
                          log_dir=self.tmp / "logs")

    def test_pending_restore_is_applied_during_app_boot(self):
        db_backup.stage_restore(self.db, self.rec["name"])
        self.assertIn("快照之后的写入", self._projects())
        self._boot()
        self.assertEqual(self._projects(), ["快照时的项目"])
        self.assertIsNone(db_backup.pending_restore(self.db))

    def test_boot_without_pending_leaves_db_alone(self):
        self.assertTrue(self._boot() is not None)
        self.assertIn("快照之后的写入", self._projects())

    def test_boot_survives_a_failing_restore(self):
        """启动期恢复失败绝不能把平台一起带崩：退回原库继续起，把错误记进日志"""
        db_backup.stage_restore(self.db, self.rec["name"])
        pending = json.loads((self.data_dir / db_backup.PENDING_FILE).read_text(encoding="utf-8"))
        Path(pending["staged"]).write_bytes(b"")
        self.assertTrue(self._boot() is not None)
        self.assertIn("快照之后的写入", self._projects())
        self.assertIsNone(db_backup.pending_restore(self.db))

    def test_boot_records_the_restore_in_audit_logs(self):
        """换库是大事：管理员得能在审计里查到是哪份快照、什么时候生效的"""
        db_backup.stage_restore(self.db, self.rec["name"])
        self._boot()
        hits = [r for r in dbm.list_audit_logs(self.db, limit=20)
                if r["action"] == "db.restore"]
        self.assertTrue(hits, "启动期换库没有留下审计记录")
        self.assertEqual(json.loads(hits[0]["detail"])["name"], self.rec["name"])
        self.assertEqual(hits[0]["username"], "system")

    def test_boot_audits_a_failed_restore_too(self):
        """失败也要留痕：否则事后查不到「某次启动其实尝试过恢复」"""
        db_backup.stage_restore(self.db, self.rec["name"])
        pending = json.loads((self.data_dir / db_backup.PENDING_FILE).read_text(encoding="utf-8"))
        Path(pending["staged"]).write_bytes(b"")
        self._boot()
        hits = [r for r in dbm.list_audit_logs(self.db, limit=20)
                if r["action"] == "db.restore"]
        self.assertTrue(hits)
        self.assertIn("error", json.loads(hits[0]["detail"]))

    def test_boot_without_a_restore_writes_no_audit_noise(self):
        self._boot()
        hits = [r for r in dbm.list_audit_logs(self.db, limit=20)
                if r["action"] == "db.restore"]
        self.assertEqual(hits, [])

    def test_boot_survives_a_failing_safety_snapshot(self):
        """回滚快照自己失败（磁盘满/权限）也不能拦住启动：平台起不来时
        管理员更没法进来处理，日志里会留一条「本次变更没有回滚点」"""
        real = db_backup.autosnapshot_before_migration
        db_backup.autosnapshot_before_migration = lambda _p: (_ for _ in ()).throw(
            OSError("disk full"))
        try:
            self.assertTrue(self._boot() is not None)
        finally:
            db_backup.autosnapshot_before_migration = real
        self.assertIn("快照之后的写入", self._projects())

    def test_pending_restore_consumed_before_init_db(self):
        """换库必须赶在 init_db 之前：否则本版本先迁移旧库、再被换掉，白迁移一场"""
        import webapp
        db_backup.stage_restore(self.db, self.rec["name"])
        seen = {}
        real_init = webapp.init_db

        def spy(db_path):
            seen["pending"] = db_backup.pending_restore(db_path)
            return real_init(db_path)

        webapp.init_db = spy
        try:
            self._boot()
        finally:
            webapp.init_db = real_init
        self.assertIsNone(seen["pending"], "init_db 见到标记还在 = 恢复发生在迁移之后")
        self.assertEqual(self._projects(), ["快照时的项目"])

    def test_migration_safety_snapshot_lands_before_init_db(self):
        """回滚点必须在 init_db（里面是手写 _migrate）之前落盘，晚了就没有回滚意义"""
        import webapp
        _diverge_schema(self.db)
        db_backup.autosnapshot_before_migration(self.db)  # 先消费掉这一档结构
        _diverge_schema(self.db, "second_col")
        seen = {}
        real_init = webapp.init_db

        def spy(db_path):
            seen["kinds"] = [r["kind"] for r in db_backup.list_snapshots(db_path)]
            return real_init(db_path)

        webapp.init_db = spy
        try:
            self._boot()
        finally:
            webapp.init_db = real_init
        self.assertIn("pre-migration", seen["kinds"])


class MigrationSafetyNetTest(BackupCase):
    """`_migrate` 是手写 ALTER 序列，跑坏了没有回滚点。这里钉的判据是：
    拿"本次启动、迁移前的库结构"和"本版本 init_db 会产出的结构"比，
    不一致说明这次启动真要改结构 → 先拍一份 pre-migration。
    同一个结构只拍一次（遗留列会让两者长期不一致，不能每次启动都拍）。"""

    def _fingerprint(self):
        return db_backup.schema_fingerprint(self.db)

    def test_fingerprint_is_stable_and_schema_sensitive(self):
        fp = self._fingerprint()
        self.assertEqual(fp, self._fingerprint())
        _add_project(self.db, "只是数据变化")
        self.assertEqual(fp, self._fingerprint(), "数据变化不该触发回滚快照")
        _diverge_schema(self.db, "smoke")
        self.assertNotEqual(fp, self._fingerprint())

    def test_no_snapshot_when_schema_already_matches_this_build(self):
        """正常启动（结构就是本版本产出的）不应每次都拍——轮转会把有用快照挤掉"""
        self.assertIsNone(db_backup.autosnapshot_before_migration(self.db))
        self.assertEqual(db_backup.list_snapshots(self.db), [])
        self.assertIsNone(db_backup.autosnapshot_before_migration(self.db))

    def test_snapshot_before_a_schema_this_build_will_change(self):
        """活库结构与本版本 init_db 的产物不一致 → 这次启动必然改结构 → 先留回滚点"""
        _diverge_schema(self.db)
        rec = db_backup.autosnapshot_before_migration(self.db)
        self.assertIsNotNone(rec, "结构要变了必须先留回滚点")
        self.assertEqual(rec["kind"], "pre-migration")
        _add_project(self.db, "快照之后的写入")
        inflated = _inflate(Path(rec["path"]))
        self.assertEqual(_columns(inflated, "projects"), _columns(self.db, "projects"))
        snap = sqlite3.connect(inflated)
        try:
            names = [r[0] for r in snap.execute("SELECT name FROM projects")]
            self.assertEqual(snap.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            snap.close()
        self.assertNotIn("快照之后的写入", names, "快照拍的必须是拍的那一刻的状态")
        inflated.unlink()

    def test_one_safety_snapshot_per_schema_state(self):
        _diverge_schema(self.db)
        self.assertIsNotNone(db_backup.autosnapshot_before_migration(self.db))
        self.assertIsNone(db_backup.autosnapshot_before_migration(self.db),
                          "同一结构状态反复启动不能反复拍（遗留差异会长期存在）")
        _diverge_schema(self.db, "another_col")
        self.assertIsNotNone(db_backup.autosnapshot_before_migration(self.db),
                             "结构又变了就得再拍一次")

    def test_missing_db_file_is_not_snapshotted(self):
        ghost = str(self.tmp / "never-created.db")
        self.assertIsNone(db_backup.autosnapshot_before_migration(ghost))


class AuditRetentionTest(BackupCase):
    def test_purge_keeps_the_window(self):
        self._seed_audit(1, 30, 100, 400)
        res = db_backup.purge_audit_logs(self.db, days=180)
        self.assertEqual(res["deleted"], 1, res)
        self.assertEqual(self._audit_count(), 3)

    def test_days_default_and_setting_override(self):
        self._seed_audit(1, 100, 250)
        _setting(self.db, db_backup.AUDIT_DAYS_KEY, 90)
        res = db_backup.purge_audit_logs(self.db)
        self.assertEqual(res["days"], 90)
        self.assertEqual(self._audit_count(), 1)

    def test_invalid_setting_falls_back_to_default(self):
        """坏值既不能变成「不清理」，也不能变成「全清掉」：回到默认窗口"""
        self._seed_audit(1, db_backup.DEFAULT_AUDIT_DAYS + 20)
        _setting(self.db, db_backup.AUDIT_DAYS_KEY, "abc")
        res = db_backup.purge_audit_logs(self.db)
        self.assertEqual(res["days"], db_backup.DEFAULT_AUDIT_DAYS)
        self.assertEqual(res["deleted"], 1, res)
        self.assertEqual(self._audit_count(), 1, "窗口内的要留下")

    def test_purge_is_idempotent_and_reports_no_change(self):
        self._seed_audit(1)
        db_backup.purge_audit_logs(self.db, days=30)
        res = db_backup.purge_audit_logs(self.db, days=30)
        self.assertEqual(res["deleted"], 0)

    def test_housekeeping_prunes_and_purges(self):
        self._seed_audit(1, 400)
        for i in range(db_backup.DEFAULT_KEEP + 1):
            _add_project(self.db, f"p{i}")
            db_backup.snapshot(self.db)
            time.sleep(0.01)
        out = db_backup.housekeeping(self.db)
        self.assertEqual(out["audit_deleted"], 1, out)
        self.assertEqual(len(db_backup.list_snapshots(self.db)), db_backup.DEFAULT_KEEP)


class LayeringTest(BackupCase):
    """快照台账放在哪里：本模块唯一一条「看起来更麻烦但必须这样」的约束"""

    def test_registry_lives_outside_the_snapshotted_database(self):
        """台账若在 platform.db 里：恢复一份旧快照会把台账一起换回旧的，盘上
        更新的文件从此没人认识（UI 看不见、轮转也收不掉）；而且登记是一次普通
        写入，活库被别的写事务占着时一键快照会整笔失败，正好废掉快照的意义。
        所以台账是备份目录里的 index.json，不是第 31 张表。"""
        self.assertNotIn("db_backups", dbm.SCHEMA)
        self.assertNotIn("db_backups", dbm.TABLES)
        rec = db_backup.snapshot(self.db)
        conn = dbm.get_conn(self.db)
        try:
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertNotIn("db_backups", names)
        self.assertTrue((self.data_dir / "backups" / "db" / "index.json").is_file())
        self.assertEqual(rec["path"],
                         str(self.data_dir / "backups" / "db" / rec["name"]),
                         "路径由目录推出：整个 backups/db 拷走或挪盘都还认得")

    def test_index_loss_self_heals_from_the_files(self):
        """index.json 没了不算灾难：盘上的 .gz 仍要能列出、能轮转、能恢复"""
        rec = db_backup.snapshot(self.db, label="带标签")
        (self.data_dir / "backups" / "db" / "index.json").unlink()
        rows = db_backup.list_snapshots(self.db)
        self.assertEqual([r["name"] for r in rows], [rec["name"]])
        self.assertEqual(rows[0]["kind"], "manual")
        self.assertEqual(rows[0]["size_bytes"], rec["size_bytes"])
        self.assertEqual(rows[0]["label"], "", "标签只活在台账里，扫描不出来")
        self.assertEqual(rows[0]["sha256"], "")
        db_backup.stage_restore(self.db, rec["name"])
        self.assertEqual(db_backup.list_snapshots(self.db)[0]["sha256"], rec["sha256"],
                         "缺校验值就现场补算并写回台账，下次才有得比")

    def test_a_corrupt_index_does_not_break_the_write_path(self):
        rec = db_backup.snapshot(self.db)
        (self.data_dir / "backups" / "db" / "index.json").write_text(
            "{ 坏掉的 json", encoding="utf-8")
        self.assertEqual([r["name"] for r in db_backup.list_snapshots(self.db)],
                         [rec["name"]])
        again = db_backup.snapshot(self.db)
        self.assertEqual({r["name"] for r in db_backup.list_snapshots(self.db)},
                         {rec["name"], again["name"]})

    def test_audit_purge_only_touches_audit_logs(self):
        """清理窗口不能顺手把别的数据带走"""
        pid = _add_project(self.db, "别删我")
        db_backup.purge_audit_logs(self.db, days=7)
        conn = dbm.get_conn(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT name FROM projects WHERE id=?", (pid,)).fetchone()["name"], "别删我")
        finally:
            conn.close()


class CollectorHousekeepingTest(BackupCase):
    """周期维护有没有挂在采集循环上

    keep-N 与审计保留写在本模块里不等于会执行：没有小时级入口，快照只增不减，
    audit_logs 也永远长下去。这里钉的是「谁在什么时候调用 housekeeping」。

    直接跑 `_run_loop`（不经 start()）：首轮必定进入小时分支（上次维护时间从 0
    起算），测试因此不必等 3600 秒。轮次节拍用 `_collect_round` 计数（它是循环里
    每轮都跑的那一步；归档/FTS/库维护都只在小时分支里，不能当轮次钟）。
    """

    def _collector(self, on_round=None):
        from webapp.log_collector import LogCollector
        c = LogCollector(self.db, str(self.data_dir / "logs"), interval=1)
        c._archive_and_cleanup = lambda: None
        c._fts_maintenance = lambda: None
        if on_round:
            real_round = c._collect_round
            c._collect_round = lambda: (on_round(), real_round())[1]
        return c

    def _run_until(self, collector, until, timeout=20):
        """在后台线程里跑采集循环，直到 until() 为真或超时，然后收线程"""
        thread = threading.Thread(target=collector._run_loop, daemon=True)
        thread.start()
        try:
            deadline = time.time() + timeout
            while time.time() < deadline and not until():
                time.sleep(0.05)
        finally:
            collector.stop()
            thread.join(timeout=10)
        return thread

    def test_hourly_branch_rotates_and_purges(self):
        """轮转与清理必须真的由采集循环驱动跑起来（端到端，不打桩 housekeeping 本体）

        保留数刻意在快照拍完之后再调小：快照自己就会按当时的 keep 轮转，
        先调小等于在 setup 里就把活儿干完了，测不出接线。
        """
        _setting(self.db, db_backup.KEEP_KEY, 50)
        for i in range(3):
            _add_project(self.db, f"p{i}")
            db_backup.snapshot(self.db)
            time.sleep(0.01)
        self.assertEqual(len(self._snapshot_names()), 3)
        _setting(self.db, db_backup.KEEP_KEY, 2)
        self._seed_audit(1, 400)
        calls = []
        real = db_backup.housekeeping
        db_backup.housekeeping = lambda p: (calls.append(p), real(p))[1]
        try:
            self._run_until(self._collector(), lambda: calls)
        finally:
            db_backup.housekeeping = real
        self.assertEqual(calls, [self.db], "维护必须作用在采集器自己那个库上")
        self.assertEqual(len(self._snapshot_names()), 2,
                         "改了保留数要在一轮维护后见效，不必等下次手动快照")
        self.assertEqual(self._audit_count(), 1, "窗口外的审计行应被收掉")

    def test_housekeeping_runs_once_per_hour_not_every_round(self):
        """维护里有 VACUUM INTO 与 DELETE，逐轮（30 秒）跑等于给活库加 constant 负载"""
        calls = []
        rounds = []
        real = db_backup.housekeeping
        db_backup.housekeeping = lambda p: (calls.append(p), real(p))[1]
        try:
            collector = self._collector(on_round=lambda: rounds.append(len(rounds)))
            self._run_until(collector, lambda: len(rounds) >= 2)
        finally:
            db_backup.housekeeping = real
        self.assertGreaterEqual(len(rounds), 2, "没跑到第二轮，节奏断言无意义")
        self.assertEqual(len(calls), 1, f"小时级门控失效：{len(calls)} 轮各跑了一次")

    def test_failing_housekeeping_never_stops_collecting(self):
        """备份盘写不动（磁盘满/权限）只是库长快一点，不该把采集线程一起带走"""
        calls = []
        rounds = []

        def boom(p):
            calls.append(p)
            raise OSError("备份盘不可写")

        real = db_backup.housekeeping
        db_backup.housekeeping = boom
        try:
            collector = self._collector(on_round=lambda: rounds.append(len(rounds)))
            self._run_until(collector, lambda: len(rounds) >= 2)
        finally:
            db_backup.housekeeping = real
        self.assertGreaterEqual(len(rounds), 2, "维护异常后采集循环必须继续跑")
        self.assertEqual(len(calls), 1, "失败也按小时节奏重试，不逐轮刷异常")


if __name__ == "__main__":
    unittest.main()
