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
        """venv 保障：缺失/损坏则重建；requirements 哈希变化则离线重装（switch/rollback 共用）

        哈希以 releases/{ver}/requirements.txt 的远端 sha256 为准；venv 位于
        app_home/venv（跨版本共享），.req_hash 记录上次安装的依赖指纹。
        标记缺失（首次、安装失败/中断）一律视为「依赖未知」，下一轮必然重装。
        """
        ver = version or self.d.version
        venv = f"{self.app_home}/venv"
        venv_py = f"{venv}/bin/python"
        marker = f"{venv}/.req_hash"
        req_remote = f"{self.jars_dir}/{ver}/{self.cfg['requirements'] or 'requirements.txt'}"
        wheels_dir = f"{self.jars_dir}/{ver}/wheels"
        # 发布包必须带依赖清单：sha256sum 对缺失文件同样返回 0，空哈希会让
        # 「空标记 == 空哈希」被误判为已安装而整体跳过安装，这里显式失败
        if not server.try_exec(f"test -f {shlex.quote(req_remote)}", timeout=15):
            raise RuntimeError(
                f"[{server.host}] 缺少依赖清单 {req_remote}："
                "发布包必须包含 runtime_config.requirements 指定的文件")
        remote_hash = server.exec(
            f"sha256sum {shlex.quote(req_remote)} | awk '{{print $1}}'",
            timeout=15).strip()
        if not remote_hash:
            raise RuntimeError(
                f"[{server.host}] 无法计算依赖清单哈希: {req_remote}"
                "（依赖未知，拒绝跳过安装）")
        server.exec(f"mkdir -p {shlex.quote(self.jars_dir)}", timeout=15)
        # 健康判定不能只看 pyvenv.cfg：系统 Python 升级后配置文件仍在，
        # 但 venv 解释器已指向不存在的旧版本，必须按解释器能否运行判断
        healthy = server.try_exec(
            f"test -f {shlex.quote(venv + '/pyvenv.cfg')} && "
            f"{shlex.quote(venv_py)} -V >/dev/null 2>&1", timeout=15)
        if not healthy:
            if server.try_exec(f"test -d {shlex.quote(venv)}", timeout=15):
                log(f"[{server.host}] venv 已损坏（pyvenv.cfg 存在但解释器不可用，"
                    f"常见于系统 Python 升级），删除重建: {venv}")
                server.exec(f"rm -rf {shlex.quote(venv)}", timeout=120)
            log(f"[{server.host}] 创建独立 venv: {venv}")
            server.exec(f"python3 -m venv {shlex.quote(venv)}", timeout=180)
            server.exec(f"rm -f {shlex.quote(marker)}", timeout=15)
        current = server.exec(
            f"cat {shlex.quote(marker)} 2>/dev/null || true", timeout=15).strip()
        if current == remote_hash:
            return
        log(f"[{server.host}] venv 依赖与 requirements 不一致，离线安装"
            f"（wheels: {wheels_dir}）")
        # 先清旧标记再安装：安装失败/中断时标记保持缺失，下一轮必然重装；
        # 若留着上一版本的指纹，自动回滚到该版本会误判「已装」而跳过重装
        server.exec(f"rm -f {shlex.quote(marker)}", timeout=15)
        server.exec(
            f"{shlex.quote(venv + '/bin/pip')} install --no-index "
            f"--find-links {shlex.quote(wheels_dir)} -r {shlex.quote(req_remote)}",
            timeout=900)
        server.exec(f"echo {shlex.quote(remote_hash)} > {shlex.quote(marker)}",
                    timeout=15)

    def rollback_preflight(self, server, version: str) -> bool:
        """回滚前置：目标版本必须带依赖清单与离线 wheels，否则回滚后也起不来

        返回 False 时 rollback 保持现状（不停单元、不换链接），避免服务先停机、
        再因依赖装不上而起不来。
        """
        base = f"{self.jars_dir}/{version}"
        req = f"{base}/{self.cfg['requirements'] or 'requirements.txt'}"
        wheels = f"{base}/wheels"
        ok = True
        if not server.try_exec(f"test -f {shlex.quote(req)}", timeout=15):
            log(f"[{server.host}] 回滚目标 releases/{version} 缺少依赖清单 {req}，"
                "跳过回滚")
            ok = False
        if not server.try_exec(f"test -d {shlex.quote(wheels)}", timeout=15):
            log(f"[{server.host}] 回滚目标 releases/{version} 缺少离线 wheel 目录 "
                f"{wheels}，跳过回滚")
            ok = False
        return ok
