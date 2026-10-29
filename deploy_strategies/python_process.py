"""Python 进程发布策略（deploy_mode: python_process，Linux + systemd）

产物为目录包（源码 + requirements.txt + wheels/，wheelhouse 离线依赖，见 build.py
python 分支），transfer 解压平铺到 releases/{ver}/；venv 是 app_home 级共享资产：
ExecStart = {app_home}/venv/bin/python CURRENT/{start_file}。
目标机前提：python3 + python3-venv（含 ensurepip）；安装全程 --no-index 不联网。
"""
import shlex

from common import log

from .base import register
from .process_service import ProcessServiceStrategy


@register
class PythonProcessStrategy(ProcessServiceStrategy):
    name = "python_process"
    artifact_kind = "archive"
    artifact_label = "压缩包"

    def artifact_name(self, version: str) -> str:
        return f"{self.service}-{version or self.d.version}.tar.gz"

    def remote_entry(self) -> str:
        return None  # archive：入口由 runtime_config.start_file 决定

    def runtime_probe(self, server):
        # ensurepip 缺失（Debian 未装 python3-venv）会让 venv 创建在切换期才失败，
        # 提前到预检拦下并给出可操作的报错
        server.exec("command -v python3 >/dev/null", timeout=15)
        server.exec("python3 -m ensurepip --version >/dev/null 2>&1 || "
                    "(echo '目标机缺少 python3-venv/ensurepip，请先安装（如 apt install python3-venv）' "
                    "&& exit 1)", timeout=15)

    def ensure_runtime(self, server, version: str):
        """venv 保障：缺失则创建；requirements 哈希变化则离线重装（switch/rollback 共用）

        哈希以 releases/{ver}/requirements.txt 的远端 sha256 为准；venv 位于
        app_home/venv（跨版本共享），.req_hash 记录上次安装的依赖指纹。
        """
        ver = version or self.d.version
        venv = f"{self.app_home}/venv"
        marker = f"{venv}/.req_hash"
        req_remote = f"{self.jars_dir}/{ver}/{self.cfg['requirements'] or 'requirements.txt'}"
        wheels_dir = f"{self.jars_dir}/{ver}/wheels"
        server.exec(f"mkdir -p {shlex.quote(self.jars_dir)}", timeout=15)
        if not server.try_exec(f"test -f {shlex.quote(venv + '/pyvenv.cfg')}", timeout=15):
            log(f"[{server.host}] 创建独立 venv: {venv}")
            server.exec(f"python3 -m venv {shlex.quote(venv)}", timeout=180)
            server.exec(f"rm -f {shlex.quote(marker)}", timeout=15)
        remote_hash = server.exec(
            f"sha256sum {shlex.quote(req_remote)} | awk '{{print $1}}'",
            timeout=15).strip()
        current = server.exec(
            f"cat {shlex.quote(marker)} 2>/dev/null || true", timeout=15).strip()
        if current == remote_hash:
            return
        log(f"[{server.host}] venv 依赖与 requirements 不一致，离线安装"
            f"（wheels: {wheels_dir}）")
        server.exec(
            f"{shlex.quote(venv + '/bin/pip')} install --no-index "
            f"--find-links {shlex.quote(wheels_dir)} -r {shlex.quote(req_remote)}",
            timeout=900)
        # 先清旧标记再写新标记：写入中断时宁可下轮重装，也不留下「假已装」指纹
        server.exec(f"rm -f {shlex.quote(marker)}", timeout=15)
        server.exec(f"echo {shlex.quote(remote_hash)} > {shlex.quote(marker)}",
                    timeout=15)
