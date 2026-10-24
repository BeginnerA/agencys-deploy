#!/usr/bin/env python3
"""智能运维平台入口：python webapp/app.py [--host 127.0.0.1] [--port 5678]"""
import argparse
import sys
from pathlib import Path

# 将项目根目录加入模块搜索路径，使 `from webapp import ...` 与 scripts 模块可用
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import create_app
from webapp.services import start_background_services

_app = None


def get_app():
    """惰性构建 app：import 本模块（例如只为取一个函数）不得触碰 data/ 真实库

    单进程只有一个 app，构建一次后缓存复用；测试用 mock.patch(create_app) 可拦截。
    WSGI 场景（gunicorn webapp.app:get_app）走默认目录，与 CLI 的 --data-dir 无关。
    """
    global _app
    if _app is None:
        _app = create_app()
    return _app


def parse_args(argv=None):
    """命令行参数。目录三项留给多实例：同一份代码可指向另一套 data/config/logs"""
    parser = argparse.ArgumentParser(description="智能运维平台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5678)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--data-dir", default=None,
                        help="数据根目录（platform.db / secret_key / 导出物 / 日志），"
                             "默认项目内 data/")
    parser.add_argument("--config-dir", default=None,
                        help="首启引导与配置导入的 YAML 来源，默认项目内 config/")
    parser.add_argument("--log-dir", default=None,
                        help="任务与采集日志目录，默认 <data-dir>/logs")
    parser.add_argument("--monitor-interval", type=int, default=None,
                        help="实时探测周期（秒），0 仅关闭实时探测与告警引擎；"
                             "默认读取 DASHBOARD_MONITOR_INTERVAL")
    parser.add_argument("--agent-interval", type=int, default=None,
                        help="AI 建议调度周期（秒），0 关闭；"
                             "默认读取 DASHBOARD_AGENT_INTERVAL（30）")
    parser.add_argument("--deploy-interval", type=int, default=None,
                        help="发布计划调度周期（秒）；"
                             "默认读取 DASHBOARD_DEPLOY_INTERVAL（30）")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    global _app
    _app = create_app(data_dir=args.data_dir, config_dir=args.config_dir,
                      log_dir=args.log_dir)
    app = _app
    start_background_services(
        app,
        monitor_interval=args.monitor_interval,
        agent_interval=args.agent_interval,
        deploy_interval=args.deploy_interval,
    )
    print(f"[platform] http://{args.host}:{args.port}")
    if app.config.get("ONBOARDED_ADMIN"):
        # 仅首次引导创建 admin 时提示；密码不打印明文，避免启动日志泄露默认凭据
        print("[platform] 已创建默认管理员 admin，首次登录强制改密"
              "（初始密码见 DASHBOARD_PASSWORD 环境变量，默认值见 README）")
    app.run(host=args.host, port=args.port, debug=args.debug,
            use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
