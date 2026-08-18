"""SnailJob 定时任务管理：备份、暂停、恢复、清零巡检计数器"""
import sys
import json
import argparse
import datetime
from pathlib import Path
import pymysql
from common import load_config, get_mysql_auth, log, fail


def connect(env: str = None):
    auth = get_mysql_auth(load_config(env=env))
    try:
        return pymysql.connect(
            host=auth["host"],
            port=auth["port"],
            database=auth["database"],
            user=auth["username"],
            password=auth["password"],
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )
    except Exception as e:
        fail(f"连接 MySQL 失败: {e}")


def _format_sql_value(v):
    """将 Python 值格式化为合法 SQL 字面量"""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    # str / datetime / Decimal / dict 等统一转字符串后转义
    s = str(v)
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def backup_jobs(group_name: str, backup_dir: Path, env: str = None) -> dict:
    """备份指定 group_name 的 sj_job 记录，返回 {sql_file, snapshot_file, snapshot}

    snapshot 为 {job_id: job_status} 原始状态快照：job_status=0 表示备份前就处于暂停，
    恢复时不得改变这些任务的状态。快照持久化为同名 .json 文件，供 CLI 分进程场景使用。
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = backup_dir / f"sj_job_{group_name}_{ts}.sql"
    snapshot_file = backup_dir / f"sj_job_{group_name}_{ts}.json"
    conn = connect(env=env)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM sj_job WHERE group_name = %s", (group_name,))
            rows = cur.fetchall()
        snapshot = {str(r["id"]): int(r["job_status"]) for r in rows}
        # 快照始终写入（即使 0 条也生成空文件，保证 resume 有据可查）
        snapshot_file.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        if not rows:
            log(f"group_name={group_name} 没有需要备份的任务，快照已生成 {snapshot_file}")
            return {"sql_file": file_path, "snapshot_file": snapshot_file, "snapshot": snapshot}
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(f"-- SnailJob backup for group_name={group_name} at {ts}\n")
            f.write("SET NAMES utf8mb4;\n")
            for r in rows:
                cols = ", ".join(f"`{k}`" for k in r.keys())
                vals = ", ".join(_format_sql_value(v) for v in r.values())
                f.write(f"REPLACE INTO sj_job ({cols}) VALUES ({vals});\n")
        log(f"已备份 {len(rows)} 条 sj_job 记录到 {file_path}")
        return {"sql_file": file_path, "snapshot_file": snapshot_file, "snapshot": snapshot}
    finally:
        conn.close()


def set_job_status(group_name: str, status: int, env: str = None):
    if status not in (0, 1):
        fail("status 只能是 0 或 1")
    conn = connect(env=env)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sj_job SET job_status = %s WHERE group_name = %s AND job_status != %s",
                (status, group_name, status),
            )
            conn.commit()
            log(f"sj_job group_name={group_name} job_status 已设置为 {status}，影响 {cur.rowcount} 条")
    finally:
        conn.close()


def restore_jobs(group_name: str, backup, env: str = None):
    """按备份快照恢复 job_status：只把备份时为运行状态（1）、当前为 0 的任务恢复为 1

    backup 可以是 backup_jobs() 的返回值（进程内场景），也可以是快照 .json 文件路径（CLI 场景）。
    备份前本就暂停（快照 job_status=0）的任务保持不变，不强行恢复；发布期间新增的任务也不干预。
    """
    if backup is None:
        fail(f"没有 {group_name} 的备份快照，无法精确恢复（请人工处理）")
    if isinstance(backup, (str, Path)):
        backup = json.loads(Path(backup).read_text(encoding="utf-8"))
    elif isinstance(backup, dict) and "snapshot" in backup:
        backup = backup["snapshot"]
    resume_ids = [str(i) for i, s in backup.items() if int(s) == 1]
    if not resume_ids:
        log(f"快照中无备份时为运行状态的任务，无需恢复")
        return
    conn = connect(env=env)
    try:
        with conn.cursor() as cur:
            placeholders = ", ".join(["%s"] * len(resume_ids))
            cur.execute(
                f"UPDATE sj_job SET job_status = 1 WHERE group_name = %s AND id IN ({placeholders}) AND job_status != 1",
                (group_name, *resume_ids),
            )
            conn.commit()
            log(f"已按备份快照恢复 {cur.rowcount} 条任务为运行状态；备份前即暂停的任务保持不变")
    finally:
        conn.close()


def latest_snapshot_file(group_name: str, backup_dir: Path) -> Path:
    """返回该任务组最新的备份快照文件；不存在则报错（禁止无快照全量恢复）"""
    files = sorted(backup_dir.glob(f"sj_job_{group_name}_*.json"), reverse=True)
    if not files:
        fail(f"backups 目录下未找到 {group_name} 的备份快照（sj_job_{group_name}_*.json），无法精确恢复，请人工处理")
    return files[0]


def clear_summary(env: str = None):
    """将 sj_job_summary 的 fail_num/stop_num/cancel_num 清零"""
    conn = connect(env=env)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sj_job_summary SET fail_num = 0, stop_num = 0, cancel_num = 0 "
                "WHERE fail_num != 0 OR stop_num != 0 OR cancel_num != 0"
            )
            conn.commit()
            log(f"sj_job_summary 已清零，影响 {cur.rowcount} 条")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="SnailJob 管理")
    parser.add_argument("action", choices=["backup", "pause", "resume", "clear-summary"])
    parser.add_argument("--group", help="group_name，例如 football_group / basketball_group")
    parser.add_argument("--backup-dir", default="./backups", help="备份目录")
    parser.add_argument("--env", help="目标环境，例如 prod/test")
    args = parser.parse_args()

    if args.action in ("backup", "pause", "resume") and not args.group:
        fail("--group 必填")

    if args.action == "backup":
        backup_jobs(args.group, Path(args.backup_dir), env=args.env)
    elif args.action == "pause":
        backup_jobs(args.group, Path(args.backup_dir), env=args.env)
        set_job_status(args.group, 0, env=args.env)
    elif args.action == "resume":
        # 按最新备份快照精确恢复：只恢复备份前运行的任务，备份前暂停的保持暂停
        restore_jobs(args.group, latest_snapshot_file(args.group, Path(args.backup_dir)), env=args.env)
    elif args.action == "clear-summary":
        clear_summary(env=args.env)


if __name__ == "__main__":
    main()
