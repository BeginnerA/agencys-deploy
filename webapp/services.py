"""后台守护服务启动编排。

各组件周期独立：实时监控（含告警引擎）可由 `--monitor-interval 0` 关闭，
但发布调度、AI 建议、SSL 探测、日志采集必须继续运行，避免“关监控 = 全停”。
"""
import os


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def start_background_services(app, monitor_interval=None, agent_interval=None,
                              deploy_interval=None):
    """启动后台守护线程，返回实际启动的组件名列表。

    - monitor_interval <= 0：只关闭实时探测与告警引擎
    - agent_interval <= 0：只关闭 AI 建议调度
    - deploy_interval 始终生效（发布计划需要按时触发）
    """
    if monitor_interval is None:
        monitor_interval = app.config.get(
            "MONITOR_INTERVAL", _env_int("DASHBOARD_MONITOR_INTERVAL", 30))
    monitor_interval = max(0, int(monitor_interval))
    if agent_interval is None:
        agent_interval = _env_int("DASHBOARD_AGENT_INTERVAL", 30)
    if deploy_interval is None:
        deploy_interval = _env_int("DASHBOARD_DEPLOY_INTERVAL", 30)

    started = []
    app.config["MONITOR_INTERVAL"] = monitor_interval

    if monitor_interval > 0:
        from webapp.monitor import init_monitor
        init_monitor(app)
        started.append("monitor")
        print(f"[monitor] 探测调度已启动，周期 {monitor_interval}s")
        # 告警引擎依赖 monitor 实例，仅在实时探测开启时启动
        from webapp.alert_engine import init_engine
        init_engine(app)
        started.append("alert-engine")
        print(f"[alert] 告警引擎已启动，评估周期 {monitor_interval}s")
    else:
        print("[monitor] 实时探测已关闭（--monitor-interval 0），"
              "发布调度、AI 建议、SSL 与日志服务继续运行")

    # SSL 证书探测与平台监控周期无关，使用自身 SSL_CHECK_INTERVAL
    from webapp.ssl_monitor import init_ssl_monitor
    init_ssl_monitor(app)
    started.append("ssl-monitor")
    print("[ssl-monitor] SSL 证书探测已启动")

    # 日志采集器在 monitor 关闭时退化为独立 docker ps 探测
    from webapp.log_collector import init_collector
    init_collector(app)
    started.append("log-collector")

    # 发布计划调度与监控解耦：monitor 关闭时也按时触发定时发布
    from webapp.deploy_scheduler import DeployScheduler
    deploy_sched = DeployScheduler(app.config["DB_PATH"], deploy_interval)
    deploy_sched.start()
    started.append("deploy-sched")
    print(f"[deploy-sched] 发布调度器已启动，周期 {deploy_interval}s")

    if agent_interval > 0:
        from agent.scheduler import start_scheduler
        start_scheduler(app.config["DB_PATH"], agent_interval)
        started.append("agent-sched")
        print(f"[agent] AI 建议调度已启动，周期 {agent_interval}s")

    return started
