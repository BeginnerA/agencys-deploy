"""platform.db 空间回收脚本：auto_vacuum 转换 + VACUUM 重建 + optimize

背景：SQLite 删除数据后页面进入 freelist 复用，文件大小不缩减（auto_vacuum=OFF）。
平台长期运行（日志/快照/响应时间高频写入 + 过期清理）后 platform.db 会积累
大量空闲页（实测 10.7GB 文件含 7GB+ 空洞）。本脚本：
1) PRAGMA auto_vacuum=INCREMENTAL（建表后设置需 VACUUM 生效；之后删除的页可被
   采集器每日 incremental_vacuum 增量回收）；
2) VACUUM 重建库文件（回收全部空洞，期间需要约等于库大小的临时空间）；
3) PRAGMA optimize 刷新统计信息。

⚠ 必须先停止平台（webapp/采集器）再执行：VACUUM 需要对库文件独占写入。

用法：
    python scripts/db_vacuum.py                 # 默认 data/platform.db
    python scripts/db_vacuum.py --db path/to.db
"""
import argparse
import os
import sqlite3
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite 数据库空间回收（VACUUM）")
    parser.add_argument("--db", default=os.path.join(_ROOT, "data", "platform.db"),
                        help="数据库文件路径（默认 data/platform.db）")
    args = parser.parse_args()
    db_path = args.db
    if not os.path.isfile(db_path):
        print(f"[db-vacuum] 数据库不存在: {db_path}")
        return 1
    size_before = os.path.getsize(db_path)
    print(f"[db-vacuum] 目标: {db_path}（{size_before / 1024 ** 2:.1f} MB）")
    t0 = time.time()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        fl_before = conn.execute("PRAGMA freelist_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        print(f"[db-vacuum] 回收前 freelist: {fl_before} 页 "
              f"≈ {fl_before * page_size / 1024 ** 2:.1f} MB")
        # auto_vacuum 转换需 VACUUM 生效；先设置再重建
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.commit()
        print("[db-vacuum] VACUUM 重建中（大库耗时较长，请耐心等待）...")
        conn.execute("VACUUM")
        conn.commit()
        conn.execute("PRAGMA optimize")
        conn.commit()
        fl_after = conn.execute("PRAGMA freelist_count").fetchone()[0]
        mode = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
    except sqlite3.OperationalError as e:
        print(f"[db-vacuum] 执行失败（平台是否仍在运行占用数据库？）: {e}")
        return 1
    finally:
        conn.close()
    size_after = os.path.getsize(db_path)
    print(f"[db-vacuum] 完成，用时 {time.time() - t0:.1f}s")
    print(f"[db-vacuum] 文件: {size_before / 1024 ** 2:.1f} MB → "
          f"{size_after / 1024 ** 2:.1f} MB"
          f"（释放 {(size_before - size_after) / 1024 ** 2:.1f} MB）")
    print(f"[db-vacuum] auto_vacuum={mode}（2=incremental 已启用），"
          f"残余 freelist: {fl_after} 页")
    return 0


if __name__ == "__main__":
    sys.exit(main())
