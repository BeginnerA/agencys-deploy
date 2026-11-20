"""平台库（SQLite）快照与恢复

为什么要有这个模块：`_migrate` 是手写 ALTER 序列，跑坏了没有任何回滚点；
`platform.db` 也没有任何备份入口。这里补齐三件事：一键快照 + keep-N 轮转、
按天清理 audit_logs、以及给结构迁移自动留一份 pre-migration 回滚点。

四条不可让步的设计：

1. 快照用只读连接的 `VACUUM INTO`，不是复制文件。
   实测生产库 1.21 GB 里只有 72 MB 在用（freelist 94%，auto_vacuum=INCREMENTAL
   只挂页不还盘），复制既慢又把空洞一起搬走；VACUUM INTO 顺手压实。
   只读连接还带来两个性质：快照动作不可能写坏活库；活库持有未提交写事务时
   也拍得下来（拍到的是一致快照，不含未提交内容）——一键快照不必先停平台。

2. 恢复分两步：`stage_restore` 只校验 + 暂存 + 写标记，真正换库发生在下次
   启动的 `apply_pending_restore`。平台进程、采集线程、deploy 子进程都开着
   同一个库文件，运行中换文件等于让各方与页缓存打架。

3. 待恢复标记写在库外（`data/restore_pending.json`）。写进库里的话，换库动作
   会把标记一起换回来，下次启动再换一次 —— 自锁循环。

4. 换库前把旧文件（主库 + -wal + -shm）整体 rename 进
   `backups/db/discarded/<时间戳>/`，只留最新一组。三件套必须同目录：分开的
   -wal 既回不去也读不出。快照之后的写入否则不留任何痕迹。

5. 快照台账是备份目录里的 `index.json`，不是 `platform.db` 里的一张表。
   写用例时才发现放库里有两个真问题：恢复一份旧快照会把台账一起换回旧的
   （盘上更新的文件从此没人认识——看不见也轮转不掉）；而登记是一次普通写入，
   活库被别的写事务占着时一键快照会整笔失败，正好废掉第 1 条的意义。
   台账因此当缓存看：读不动就按空处理，list_snapshots() 拿目录里的文件自愈，
   手工拷回来的快照也能列出并补算校验值。

命名 `platform.{id:06d}.{时间戳}.{种类}.db.gz`：id 来自台账自增号，天然不撞名
（`VACUUM INTO` 拒绝覆盖已存在文件，同秒两次点击会让其中一次莫名失败），
且同长度可按字典序排。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import tempfile
import time
from pathlib import Path

from . import db as dbm

log = logging.getLogger("platform.db_backup")

# 保留策略默认值（管理端可改，见 KEEP_KEY / AUDIT_DAYS_KEY）
DEFAULT_KEEP = 3
DEFAULT_AUDIT_DAYS = 180
KEEP_BOUNDS = (1, 50)
AUDIT_DAYS_BOUNDS = (7, 3650)

KEEP_KEY = "db_backup_keep"
AUDIT_DAYS_KEY = "db_audit_retention_days"
_INDEX_VERSION = 1

# 台账里记「已为哪个结构状态拍过迁移回滚快照」的字段名（库外，理由见模块 docstring）
PRE_MIGRATION_FIELD = "pre_migration_schema"

PENDING_FILE = "restore_pending.json"
STAGED_FILE = "restore_staging.db"
KINDS = ("manual", "pre-migration", "pre-restore")

# 启动期换库的审计署名（没有请求上下文，不能挂到某个管理员身上）
SYSTEM_USER = "system"

# 本版本 init_db 会产出的结构指纹（只跟代码有关，进程内算一次）
_TARGET_FINGERPRINT = None


class RestoreError(RuntimeError):
    """快照不可信（缺文件 / 校验不符 / 结构损坏），拒绝据此恢复"""


# ---------- 路径与设置 ----------

def _data_dir(db_path: str) -> Path:
    """实例数据目录（与库文件同级）。唯一真源是 deploy.data_dir_for_db，
    deploy 会拉起 paramiko，按仓库惯例在函数内延迟导入。"""
    from deploy import data_dir_for_db
    return Path(data_dir_for_db(db_path))


def _backup_dir(db_path: str) -> Path:
    return _data_dir(db_path) / "backups" / "db"


def _get_setting(db_path: str, key: str):
    conn = dbm.get_conn(db_path)
    try:
        row = conn.execute("SELECT value FROM platform_settings WHERE key=?",
                           (key,)).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def _set_setting(db_path: str, key: str, value) -> None:
    conn = dbm.get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO platform_settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            "updated_at=excluded.updated_at",
            (key, json.dumps(value), int(time.time())))
        conn.commit()
    finally:
        conn.close()


def _int_setting(db_path: str, key: str, default: int, bounds: tuple) -> int:
    """读不到 / 值不成整数 / 越界都退回默认，绝不让一个坏配置卡住维护动作"""
    raw = _get_setting(db_path, key)
    if raw is None:
        return default
    try:
        val = int(json.loads(raw))
    except (ValueError, TypeError):
        log.warning("[db-backup] 设置 %s=%r 不是整数，按默认 %s 处理", key, raw, default)
        return default
    lo, hi = bounds
    if not lo <= val <= hi:
        log.warning("[db-backup] 设置 %s=%s 超出 %s~%s，按默认 %s 处理",
                    key, val, lo, hi, default)
        return default
    return val


def retention_settings(db_path: str) -> dict:
    """管理端展示用的当前保留策略"""
    return {
        "keep": _int_setting(db_path, KEEP_KEY, DEFAULT_KEEP, KEEP_BOUNDS),
        "audit_retention_days": _int_setting(db_path, AUDIT_DAYS_KEY,
                                             DEFAULT_AUDIT_DAYS, AUDIT_DAYS_BOUNDS),
        "defaults": {"keep": DEFAULT_KEEP, "audit_retention_days": DEFAULT_AUDIT_DAYS},
        "bounds": {"keep": list(KEEP_BOUNDS), "audit_retention_days": list(AUDIT_DAYS_BOUNDS)},
    }


def set_retention(db_path: str, values: dict) -> dict:
    """写入保留策略（`values` 的键就是 platform_settings 的键），返回生效后的策略

    越界与非整数抛 ValueError，文案自带范围：边界只有 KEEP_BOUNDS /
    AUDIT_DAYS_BOUNDS 一处定义，管理端不要再抄一份判据。
    """
    spec = {KEEP_KEY: ("快照保留份数", KEEP_BOUNDS),
            AUDIT_DAYS_KEY: ("审计保留天数", AUDIT_DAYS_BOUNDS)}
    for key, value in values.items():
        label, (low, high) = spec[key]
        # bool 是 int 的子类，但 true/false 当份数用一定是调用方出错
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{label}必须是 {low}~{high} 的整数")
        if not low <= value <= high:
            raise ValueError(f"{label}需在 {low}~{high} 之间")
    for key, value in values.items():
        _set_setting(db_path, key, value)
    return retention_settings(db_path)


# ---------- 快照 ----------

# 台账只有一个 writer 语义：进程内用锁串起来，跨进程靠原子 replace + 扫描自愈
# （_index_read / _index_write 自己不加锁，调用方按需持 _index_lock，
#   避免 snapshot → prune 这种嵌套路径上自锁）
_index_lock = threading.Lock()
_NAME_RE = re.compile(r"^platform\.(\d{6})\.(\d{8}-\d{6})\.([a-z-]+)\.db\.gz$")


def _index_path(db_path: str) -> Path:
    return _backup_dir(db_path) / "index.json"


def _empty_index() -> dict:
    return {"version": _INDEX_VERSION, "next_id": 1, "snapshots": {}}


def _index_read(db_path: str) -> dict:
    """读台账。缺文件 / 读不动 / 形状不对都不算错：盘上的 .gz 才是事实，
    台账只是缓存，缺什么补什么"""
    try:
        raw = json.loads(_index_path(db_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_index()
    except (OSError, ValueError):
        log.exception("[db-backup] 台账读不动，按空台账处理")
        return _empty_index()
    if not isinstance(raw, dict) or not isinstance(raw.get("snapshots"), dict):
        log.warning("[db-backup] 台账形状不对（%s），按空台账处理", _index_path(db_path))
        return _empty_index()
    raw.setdefault("next_id", 1)
    return raw


def _index_write(db_path: str, index: dict) -> None:
    path = _index_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.part")
    payload = dict(index)
    payload["version"] = _INDEX_VERSION
    payload.setdefault("next_id", 1)
    payload.setdefault("snapshots", {})
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)


def _salvage(path: Path) -> dict:
    """台账里没有这个文件（index.json 丢了、或手工拷回来的快照）：能从文件名和
    stat 复原多少复原多少，校验值留空等下次用到时补"""
    m = _NAME_RE.match(path.name)
    kind = m.group(3) if m else "manual"
    ident = int(m.group(1)) if m else 0
    try:
        stat = path.stat()
        created = (int(time.mktime(time.strptime(m.group(2), "%Y%m%d-%H%M%S")))
                   if m else 0) or int(stat.st_mtime)
    except OSError:
        return {"id": ident, "kind": kind, "label": "", "size_bytes": 0,
                "src_bytes": 0, "sha256": "", "created_at": int(time.time())}
    return {"id": ident, "kind": kind, "label": "", "size_bytes": stat.st_size,
            "src_bytes": 0, "sha256": "", "created_at": created}


def _entry(db_path: str, name: str, meta: dict, exists: bool) -> dict:
    """台账里的一条 + 现场状态 → 对外一行（path 永远由目录推出，不落盘）"""
    return {
        "id": int(meta.get("id") or 0),
        "kind": meta.get("kind", "manual"),
        "label": meta.get("label", ""),
        "size_bytes": int(meta.get("size_bytes") or 0),
        "src_bytes": int(meta.get("src_bytes") or 0),
        "sha256": meta.get("sha256", ""),
        "created_at": int(meta.get("created_at") or 0),
        "name": name,
        "path": str(_backup_dir(db_path) / name),
        "file_missing": not exists,
    }


def _now_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _vacuum_gzip(db_path: str, tmp_out: Path) -> tuple:
    """只读连接 VACUUM INTO → gzip 落盘；返回 (压缩后字节数, .gz 的 sha256)

    校验值算的是最终落盘文件（含 gzip），恢复时才能一眼判损坏。
    """
    raw = Path(str(tmp_out) + ".raw")
    _unlink(raw)
    try:
        # VACUUM INTO 拒绝覆盖已存在文件，所以每次都得是新路径
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("VACUUM INTO ?", (raw.as_posix(),))
        finally:
            conn.close()
        with open(raw, "rb") as fin, gzip.open(tmp_out, "wb") as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
        return Path(tmp_out).stat().st_size, _sha256_file(tmp_out)
    finally:
        # VACUUM 中途失败也会留下半截 .raw，不能让它悬在备份目录里
        _unlink(raw)


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _next_id(db_path: str) -> int:
    """台账里的自增号：只用来给文件名去重，不代表数据库里的任何东西"""
    with _index_lock:
        index = _index_read(db_path)
        return int(index.get("next_id") or 1)


def _record(db_path: str, name: str, kind: str, label: str,
            size_bytes: int, src_bytes: int, sha256: str, ident: int) -> dict:
    """把一份刚落盘的快照登记进台账（库外 index.json，理由见模块 docstring）"""
    meta = {
        "id": ident,
        "kind": kind,
        "label": label,
        "size_bytes": size_bytes,
        "src_bytes": src_bytes,
        "sha256": sha256,
        "created_at": int(time.time()),
    }
    with _index_lock:
        index = _index_read(db_path)
        snapshots = index["snapshots"]
        snapshots[name] = meta
        index["next_id"] = max(int(index.get("next_id") or 1), ident + 1)
        _index_write(db_path, index)
    return _entry(db_path, name, meta, exists=True)


def snapshot(db_path: str, kind: str = "manual", label: str = "") -> dict:
    """拍一份紧凑快照并登记，返回登记行；失败不留下半截文件、也不留孤儿登记"""
    if kind not in KINDS:
        raise ValueError(f"未知快照种类: {kind}")
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"数据库不存在: {db_path}")
    bdir = _backup_dir(db_path)
    bdir.mkdir(parents=True, exist_ok=True)
    src_bytes = os.path.getsize(db_path)
    ts = _now_stamp()
    ident = _next_id(db_path)
    dest = tmp = None
    for _ in range(5):
        # 并发点击可能抢到同一个 id：VACUUM INTO 不覆盖，换号重试而不是报错
        stem = f"platform.{ident:06d}.{ts}.{kind}"
        dest, tmp = bdir / f"{stem}.db.gz", bdir / f"{stem}.part"
        if not dest.exists():
            name = dest.name
            break
        ident += 1
    else:
        raise RuntimeError("快照命名连续冲突，请检查 data/backups/db 是否残留半截文件")
    try:
        size_bytes, sha256 = _vacuum_gzip(db_path, tmp)
        os.replace(tmp, dest)
    except Exception:
        _unlink(tmp)
        raise
    try:
        rec = _record(db_path, name, kind, label, size_bytes, src_bytes, sha256, ident)
    except Exception:
        # 没人引用的 .gz 轮转看不见，会一直吃磁盘
        _unlink(dest)
        raise
    try:
        prune(db_path)
    except Exception as e:
        log.warning("[db-backup] 快照已生成但轮转失败 %s: %s", name, e)
    return rec


def list_snapshots(db_path: str) -> list:
    """盘上文件 ∪ 台账条目，按新→旧

    两个方向都要兜住：台账丢了（index.json 被删/写坏）文件还得看得见、还得能
    轮转掉；文件被人 rm 了台账还在，就标 file_missing 显出来，别等恢复才发现。
    """
    bdir = _backup_dir(db_path)
    if not bdir.is_dir():
        return []
    known = _index_read(db_path)["snapshots"]
    entries = {}
    for path in bdir.glob("*.db.gz"):
        meta = known.get(path.name)
        if meta is None:
            meta = _salvage(path)
        entries[path.name] = _entry(db_path, path.name, meta,
                                    exists=True)
    for name, meta in known.items():
        entries.setdefault(name, _entry(db_path, name, meta,
                                        exists=(bdir / name).is_file()))
    return sorted(entries.values(),
                  key=lambda e: (e["created_at"], e["id"]), reverse=True)


def prune(db_path: str, keep: int = None) -> list:
    """只留最新 N 份（N 至少 1：唯一的回滚点不能被任何配置删掉），返回删掉的文件名"""
    if keep is None:
        keep = _int_setting(db_path, KEEP_KEY, DEFAULT_KEEP, KEEP_BOUNDS)
    else:
        keep = max(KEEP_BOUNDS[0], min(KEEP_BOUNDS[1], int(keep)))
    names = []
    with _index_lock:
        index = _index_read(db_path)
        for row in list_snapshots(db_path)[keep:]:
            try:
                os.unlink(_backup_dir(db_path) / row["name"])
            except FileNotFoundError:
                pass
            except OSError as e:
                # 删不掉（占用/权限）就连台账一起留着，别让记录和文件脱钩
                log.warning("[db-backup] 轮转删文件失败 %s: %s", row["name"], e)
                continue
            index["snapshots"].pop(row["name"], None)
            names.append(row["name"])
        if names:
            _index_write(db_path, index)
    return names


# ---------- 恢复 ----------

def _remember_checksum(db_path: str, entry: dict) -> dict:
    """手工拷回来的快照没有校验值：现场补算并写回台账，下次就有得比了"""
    digest = _sha256_file(Path(entry["path"]))
    with _index_lock:
        index = _index_read(db_path)
        meta = index["snapshots"].setdefault(
            entry["name"], {k: entry[k] for k in
                            ("id", "kind", "label", "size_bytes", "src_bytes",
                             "created_at")})
        meta["sha256"] = digest
        _index_write(db_path, index)
    out = dict(entry)
    out["sha256"] = digest
    return out


def _lookup(db_path: str, name: str) -> dict:
    """恢复只认台账/盘上逐字相等的快照名：路径穿越、改名、拼出来的名字都进不来

    名单来自 list_snapshots（只含备份目录里的 *.db.gz 与台账键），
    所以传进来的字符串永远没有被当作路径拼出去的机会。
    """
    if not isinstance(name, str) or not name:
        raise ValueError("快照名不能为空")
    if os.path.basename(name) != name:
        raise ValueError(f"非法快照名: {name}")
    for entry in list_snapshots(db_path):
        if entry["name"] != name:
            continue
        if entry["file_missing"]:
            raise RestoreError(f"快照文件不存在: {name}")
        if not entry["sha256"]:
            entry = _remember_checksum(db_path, entry)
        return entry
    raise ValueError(f"未登记的快照名: {name}")


def _inflate(src: Path, dst: Path) -> None:
    with gzip.open(src, "rb") as fin, open(dst, "wb") as fout:
        shutil.copyfileobj(fin, fout, 1 << 20)


def _check_database(path: Path) -> None:
    """恢复前的第二道闸：校验值可能被连登记表一起改掉，结构坏了照样不能上机"""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    if not result or result[0] != "ok":
        raise RestoreError(f"快照结构校验未通过: {result}")


def stage_restore(db_path: str, name: str) -> dict:
    """校验 + 暂存 + 写待恢复标记；真正生效在下一次启动

    先给当前库拍一份 pre-restore：换库之后想反悔，至少还回得去。
    """
    row = _lookup(db_path, name)
    src = Path(row["path"])
    if not src.is_file():
        raise RestoreError(f"快照文件不存在: {name}")
    actual = _sha256_file(src)
    if actual != row["sha256"]:
        raise RestoreError(f"快照校验值不符（文件可能已被改动）: {name}")
    ddir = _data_dir(db_path)
    staged = ddir / STAGED_FILE
    _unlink(staged)
    try:
        _inflate(src, staged)
        _check_database(staged)
    except RestoreError:
        _unlink(staged)
        raise
    except (OSError, EOFError, sqlite3.Error) as e:
        # gzip 截断/解压失败都归到这里：不能拿半截库去换活库
        _unlink(staged)
        raise RestoreError(f"快照解包失败: {name}（{e}）") from e
    snapshot(db_path, kind="pre-restore", label=f"恢复 {name} 前自动保留")
    payload = {
        "name": name,
        "kind": row["kind"],
        "staged": str(staged),
        "sha256": _sha256_file(staged),
        "staged_at": int(time.time()),
        "requested_src_bytes": row["src_bytes"],
    }
    pending = ddir / PENDING_FILE
    tmp = pending.with_suffix(".json.part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, pending)
    log.warning("[db-backup] 已暂存恢复源 %s，重启平台后生效", name)
    return {"restart_required": True, "name": name, "staged": str(staged)}


def pending_restore(db_path: str):
    """当前待恢复标记；没有（或读不动）就是 None"""
    pending = _data_dir(db_path) / PENDING_FILE
    try:
        return json.loads(pending.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.exception("[db-backup] 待恢复标记读不动: %s", pending)
        return None


def cancel_pending_restore(db_path: str):
    """撤回一次「重启后换库」：删标记与暂存副本；快照一份不动

    没有这个出口，一次暂存会在很久以后某次偶然重启（改完配置顺手重启）悄悄把库
    换掉，那已经不是管理员点下的那个动作了。标记先删：两步之间断电也不会留下
    「有标记但副本已没」的半截状态。没有待恢复项时返回 None——取消按在已重启过
    的窗口上是常态，不是错误。
    """
    pending = pending_restore(db_path)
    if not pending:
        return None
    _unlink(_data_dir(db_path) / PENDING_FILE)
    staged = Path(pending.get("staged", ""))
    if staged.is_file():
        _unlink(staged)
    log.warning("[db-backup] 已取消待恢复 %s（重启后不会再换库）", pending.get("name"))
    return pending


def _discard(db_path: str) -> list:
    """把即将被替换的活库整体挪走（含 -wal/-shm），返回 [(原路径, 挪走后的路径)]

    rename 而不是 copy：换库是不可回退的破坏性动作，至少留下字节级原文件；
    活库正被另一进程持有时，Windows 上的 rename 会直接失败——这正是我们要的
    失败方式（宁可换库失败，也不要写坏一个正在被读的库）。
    一次换库的三件套放进同一个时间戳目录：主库和它的 -wal 分开留存毫无意义，
    按目录清理也不可能把一组拆散。
    """
    live = Path(db_path)
    root = _backup_dir(db_path) / "discarded"
    bundle = root / _now_stamp()
    bundle.mkdir(parents=True, exist_ok=True)
    moved = []
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(live) + suffix)
        if not src.exists():
            continue
        dst = bundle / (live.name + suffix)
        os.replace(src, dst)
        moved.append((src, dst))
    _prune_discarded(root)
    return moved


def _prune_discarded(root: Path, keep: int = 1) -> None:
    """换库现场只留最新 keep 组（目录名是时间戳，倒序排）"""
    bundles = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)
    for victim in bundles[keep:]:
        shutil.rmtree(victim, ignore_errors=True)


def _restore_discarded(moved: list) -> None:
    """换库最后一步失败时把原库放回原位：不能出现「旧的挪走了、新的没落地」"""
    for src, dst in reversed(moved):
        try:
            os.replace(dst, src)
        except OSError:
            log.error("[db-backup] 原库回位失败，请手工移回 %s → %s", dst, src)


def apply_pending_restore(db_path: str):
    """启动期消费待恢复标记：换库。没有标记返回 None

    调用方（create_app）必须跑在 init_db 之前，并且要把异常吞掉——
    恢复失败不该把整个平台带崩。
    """
    pending = pending_restore(db_path)
    if not pending:
        return None
    live = Path(db_path)
    # 先消费标记：坏文件不能每次启动都重试一遍
    _unlink(_data_dir(db_path) / PENDING_FILE)
    staged = Path(pending.get("staged", ""))
    try:
        if not staged.is_file():
            raise RestoreError(f"暂存的恢复源已不在: {staged}")
        if _sha256_file(staged) != pending.get("sha256"):
            raise RestoreError("暂存的恢复源校验不符，拒绝换库")
        _check_database(staged)
    except RestoreError:
        _unlink(staged)
        raise
    except (OSError, EOFError, sqlite3.Error) as e:
        _unlink(staged)
        raise RestoreError(f"恢复源不可读: {e}") from e
    # 换库：先整体挪走原库（含 -wal/-shm），再把恢复源放上去。
    # 这一步不删任何 wal/shm —— 失败时原库连同它的 wal 会被完整放回原位。
    moved = _discard(db_path)
    try:
        os.replace(staged, live)
    except OSError as e:
        _restore_discarded(moved)
        _unlink(staged)
        raise RestoreError(f"换库失败（原库已回位）: {e}") from e
    log.warning("[db-backup] 已按快照 %s 换库（原库挪到 %s）",
                pending.get("name"), [dst.name for _, dst in moved])
    return {"name": pending.get("name"),
            "discarded": [str(dst) for _, dst in moved]}


# ---------- 结构迁移安全网 ----------

def schema_fingerprint(db_path: str) -> str:
    """库结构的哈希（只看 sqlite_master 里的 DDL，不看数据、不看行序）"""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT type, name, COALESCE(sql,'') FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
    finally:
        conn.close()
    blob = "\n".join(f"{t}|{n}|{s}" for t, n, s in rows)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _target_fingerprint() -> str:
    """本版本 init_db 会产出的结构：拿临时目录建一个空库量一遍，进程内缓存。
    这样判据不需要有人记得维护一个版本号常量（`_migrate` 恰恰缺这个纪律）。"""
    global _TARGET_FINGERPRINT
    if _TARGET_FINGERPRINT is None:
        probe_dir = tempfile.mkdtemp(prefix="db_schema_probe_")
        probe = os.path.join(probe_dir, "schema_probe.db")
        try:
            dbm.init_db(probe)
            _TARGET_FINGERPRINT = schema_fingerprint(probe)
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)
    return _TARGET_FINGERPRINT


def autosnapshot_before_migration(db_path: str):
    """结构即将被本版本改动时先拍一份回滚点；不需要则返回 None

    判据：迁移前的库结构 ≠ 本版本产物 → 这次 init_db 里的 `_migrate` 必然动它。
    同一个结构状态只拍一次：存量库可能因为历史遗留列与产物长期不一致，
    每次启动都拍会把有用快照挤出轮转，也白占磁盘。
    """
    if not os.path.isfile(db_path):
        return None  # 首启空库：没有要保护的东西
    pre = schema_fingerprint(db_path)
    if pre == _target_fingerprint():
        return None
    # 状态记在台账里而不是 platform_settings：启动钩子跑在 init_db 之前，
    # 那时对库做任何写操作都可能撞上别的进程持有的写锁
    if _index_read(db_path).get(PRE_MIGRATION_FIELD) == pre:
        return None
    rec = snapshot(db_path, kind="pre-migration",
                   label=f"结构指纹 {pre} 迁移前自动保留")
    with _index_lock:
        index = _index_read(db_path)
        index[PRE_MIGRATION_FIELD] = pre
        _index_write(db_path, index)
    log.warning("[db-backup] 检测到结构将由本版本变更（%s），已留回滚点 %s", pre, rec["name"])
    return rec


# ---------- 审计日志保留 ----------

def purge_audit_logs(db_path: str, days: int = None) -> dict:
    """按天清理 audit_logs：快照表记的是文件，审计表是会无限长的行"""
    if days is None:
        days = _int_setting(db_path, AUDIT_DAYS_KEY, DEFAULT_AUDIT_DAYS,
                            AUDIT_DAYS_BOUNDS)
    else:
        days = max(AUDIT_DAYS_BOUNDS[0], min(AUDIT_DAYS_BOUNDS[1], int(days)))
    cutoff = int(time.time()) - days * 86400
    conn = dbm.get_conn(db_path)
    try:
        cur = conn.execute("DELETE FROM audit_logs WHERE created_at < ?", (cutoff,))
        conn.commit()
        deleted = cur.rowcount
    finally:
        conn.close()
    if deleted:
        log.info("[db-backup] 审计日志保留 %s 天，清理 %s 条", days, deleted)
    return {"days": days, "deleted": deleted, "cutoff": cutoff}


# ---------- 周期维护 ----------

def housekeeping(db_path: str) -> dict:
    """每小时维护入口：快照轮转 + 审计按天清理

    快照本身在生成时就轮转过了，这里再跑一次是为了让管理员改了保留数之后
    下一小时就见效，不必等到下次手动快照。
    """
    pruned = prune(db_path)
    audit = purge_audit_logs(db_path)
    return {"pruned": pruned, "audit_deleted": audit["deleted"],
            "audit_days": audit["days"]}


# ---------- 启动钩子 ----------

def run_boot_hooks(db_path: str):
    """create_app 在 init_db 之前调用；返回需要在 init_db 之后补记的审计条目

    两个动作都吞异常：快照子系统自己坏了（磁盘满、权限、库被另一进程占着）
    不该把整个平台带崩 —— 平台起不来时管理员更需要能打开界面看日志。
    换库失败尤其要安静：原库已经在原地了，按现状启动就是正确答案。
    """
    entry = None
    pending = pending_restore(db_path)
    if pending:
        name = pending.get("name", "")
        try:
            applied = apply_pending_restore(db_path)
            entry = {"username": SYSTEM_USER, "action": "db.restore",
                     "detail": {"name": name, "discarded": applied["discarded"]}}
        except Exception as e:
            log.exception("[db-backup] 待恢复 %s 应用失败，按现有数据库继续启动", name)
            entry = {"username": SYSTEM_USER, "action": "db.restore",
                     "detail": {"name": name, "error": str(e)}}
    try:
        autosnapshot_before_migration(db_path)
    except Exception:
        log.exception("[db-backup] 迁移前回滚快照失败：本次结构变更没有回滚点，请尽快手工快照")
    return entry
