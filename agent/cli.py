"""Agent CLI：终端对话入口（Phase 1 对话能力的命令行形态）

用法：
    python agent/cli.py                          # 交互式会话（角色 operator）
    python agent/cli.py --once "pay 服务最近有异常吗"
    python agent/cli.py --session ops-20260909   # 指定会话 ID（跨进程续聊）
    python agent/cli.py --role viewer             # 只读角色
    python agent/cli.py --db data/platform.db
"""
import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def run_once(db_path: str, user: dict, text: str, session_id: str) -> int:
    from agent import core as core_mod
    try:
        result = core_mod.chat_once(db_path, user, text, session_id=session_id)
    except Exception as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 1
    print(result.get("reply", ""))
    run_id = result.get("plan_run_id")
    if run_id:
        print(f"\n[审批单] 已生成发布审批单 #{run_id}（awaiting_approval），"
              "请由管理员在 Web 端「智能助手 → 审批单」中审批或驳回。")
    return 0


def interactive(db_path: str, user: dict, session_id: str) -> int:
    print("智能运维助手 CLI（输入 exit/quit 退出；Ctrl+C 中断本轮）")
    while True:
        try:
            text = input(f"[{user.get('username', 'cli')}] 你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.lower() in ("exit", "quit", "q"):
            break
        run_once(db_path, user, text, session_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="agencys-deploy Agent CLI")
    parser.add_argument("--db", default=str(_ROOT / "data" / "platform.db"))
    parser.add_argument("--role", default="operator",
                        choices=("viewer", "operator", "admin"))
    parser.add_argument("--user", default="cli", help="对话署名（审计用）")
    parser.add_argument("--session", default="cli-default")
    parser.add_argument("--once", default="", help="执行单轮后退出")
    args = parser.parse_args()
    user = {"id": 0, "username": args.user, "role": args.role}
    if not Path(args.db).exists():
        print(f"数据库不存在: {args.db}（请先启动 webapp 初始化）", file=sys.stderr)
        return 2
    if args.once:
        return run_once(args.db, user, args.once, args.session)
    interactive(args.db, user, args.session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
