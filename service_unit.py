"""systemd 单元生成与接入

平台对目标机的系统写入被刻意限制为三件事：写 unit 文件、daemon-reload、enable。
不安装运行时、不改内核参数、不动防火墙；对已存在且 ExecStart 不含本模式特征串
（process_config.exec_start_marker）的单元一律拒绝接管。
"""
import base64
import shlex
import sys
from pathlib import Path

# 仓库根模块（与 deploy.py 同级）需自行把 scripts/ 加入搜索路径：
# 不能依赖「先 import 了 deploy_strategies 才顺带生效」这种隐式顺序
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

from deploy_strategies.process_config import (build_exec_start, exec_start_marker,
                                              parse_runtime_config)
from common import log


def render_unit(runtime_config, service: str, deploy_mode: str = "jar_service") -> str:
    """按运行时配置渲染 unit：ExecStart 固定引用 CURRENT（或 venv），发布只换链接不改 unit"""
    cfg = parse_runtime_config(runtime_config, deploy_mode)
    work_dir = cfg["work_dir"] or f"{cfg['app_home']}/CURRENT"
    exec_start = build_exec_start(cfg, deploy_mode)
    lines = [
        "[Unit]",
        f"Description={service}",
        "After=network.target",
        "",
        "[Service]",
        "Type=simple",
        f"User={cfg['run_user']}" if cfg["run_user"] else "",
        f"WorkingDirectory={work_dir}",
        f"ExecStart={exec_start}",
        # 有 SIGTERM 处理器的运行时（Java、部分 Python 应用）正常退出码 143：
        # 不声明会被 systemd 判为失败并触发重启；无处理器的运行时此行无害
        "SuccessExitStatus=143",
        "Restart=on-failure",
        "RestartSec=5",
        f"TimeoutStopSec={cfg['stop_timeout']}",
        f"TimeoutStartSec={cfg['start_timeout']}",
        *[f"EnvironmentFile={p}" for p in cfg["env_files"]],
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    return "\n".join(line for line in lines if line)


def onboard_unit(server, unit: str, content: str, cfg: dict,
                 deploy_mode: str = "jar_service") -> None:
    """在单台服务器接入 unit（幂等）：拒绝接管、写文件、reload、enable

    cfg 为 parse_runtime_config 的产物（含 app_home 与模式专属字段），
    接管特征串按 deploy_mode 分派。
    """
    expected = exec_start_marker(cfg, deploy_mode)
    existing = server.exec(
        f"systemctl show -p ExecStart --value {shlex.quote(unit)}.service "
        f"2>/dev/null || true", timeout=15)
    if existing.strip() and expected not in existing:
        raise RuntimeError(
            f"拒绝接管非平台创建的单元 {unit}.service（ExecStart 未指向 {expected}）")
    path = f"/etc/systemd/system/{unit}.service"
    # base64 传输避免 heredoc/引号在多行 unit 内容中的转义问题
    payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
    server.exec(
        f"printf %s {shlex.quote(payload)} | base64 -d > {shlex.quote(path)} && "
        f"systemctl daemon-reload && systemctl enable {shlex.quote(unit)}.service",
        timeout=60)
    log(f"[{getattr(server, 'host', '?')}] 单元已接入: {unit}.service")
