#!/usr/bin/env python3
"""智能运维平台入口：python webapp/app.py [--host 127.0.0.1] [--port 8099]"""
import argparse
import sys
from pathlib import Path

# 将项目根目录加入模块搜索路径，使 `from webapp import ...` 与 scripts 模块可用
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import create_app

app = create_app()


def main():
    parser = argparse.ArgumentParser(description="智能运维平台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--monitor-interval", type=int, default=60,
                        help="实时探测周期（秒），0 关闭监控")
    args = parser.parse_args()
    if args.monitor_interval > 0:
        from webapp.monitor import init_monitor
        app.config["MONITOR_INTERVAL"] = args.monitor_interval
        init_monitor(app)
        print(f"[monitor] 探测调度已启动，周期 {args.monitor_interval}s")
        from webapp.alert_engine import init_engine
        init_engine(app)
        print(f"[alert] 告警引擎已启动，评估周期 {args.monitor_interval}s")
        from webapp.log_collector import init_collector
        init_collector(app)
        from webapp.deploy_scheduler import DeployScheduler
        deploy_sched = DeployScheduler(app.config["DB_PATH"],
                                        args.monitor_interval)
        deploy_sched.start()
        print(f"[deploy-sched] 发布调度器已启动，周期 {args.monitor_interval}s")
    print(f"[platform] http://{args.host}:{args.port}")
    if app.config.get("ONBOARDED_ADMIN"):
        # 仅首次引导创建 admin 时提示；密码不打印明文，避免启动日志泄露默认凭据
        print("[platform] 已创建默认管理员 admin，首次登录强制改密"
              "（初始密码见 DASHBOARD_PASSWORD 环境变量，默认值见 README）")
    app.run(host=args.host, port=args.port, debug=args.debug,
            use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
