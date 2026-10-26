"""纯 Java 进程发布策略（deploy_mode: jar_service，Linux + systemd）

产物按版本不可变存放，CURRENT 符号链接原子切换，systemd unit 的 ExecStart 固定
引用 {app_home}/CURRENT/app.jar：发布与回滚都只是「换链接 + 重启单元」，回滚不需要重传。
权威版本状态在目标机（.deploy_version 标记 + CURRENT 链接），平台库只做展示缓存。

与容器模式的根本差异：单实例重启必然有停机窗口，切换语义是「停机→换链接→启动」，
不承诺零停机；Nacos 上下线仍由编排层按能力标志调度，与运行时方式无关。
"""
import shlex
import time
from pathlib import Path

from .base import DeployStrategy, register
from .jar_config import (artifact_filename, parse_runtime_config,
                         sha256_file)
from common import (log, load_yaml, default_servers_file, merge_server_ssh,
                    get_ssh_config)


@register
class JarServiceStrategy(DeployStrategy):
    name = "jar_service"
    supports_nacos = True
    supports_canary = True
    supports_prefetch = True
    supports_restart = True
    version_scheme = "semver"

    def __init__(self, deployer):
        super().__init__(deployer)
        proj = getattr(deployer, "proj_cfg", None) or {}
        self.cfg = parse_runtime_config(proj.get("runtime_config"))
        if not self.cfg["app_home"]:
            raise ValueError(
                "deploy_mode=jar_service 必须配置 runtime_config.app_home"
                "（如 /opt/apps/hqb-auth）")
        self.app_home = self.cfg["app_home"]
        self.service = (proj.get("image_name") or
                        getattr(deployer, "project", "app"))
        self.unit = self.cfg["unit_name"] or self.service
        self.jars_dir = f"{self.app_home}/releases"
        self.curr_link = f"{self.app_home}/CURRENT"

    # ---------- 产物定位 ----------

    def jar_name(self, version: str = None) -> str:
        return artifact_filename(self.service, version or self.d.version)

    def local_jar(self, version: str = None) -> Path:
        return Path(getattr(self.d, "dist_dir", Path("dist"))) / self.jar_name(version)

    # ---------- 阶段 0：本地预检与版本推导 ----------

    def check_local(self):
        p = self.local_jar()
        if not p.exists():
            raise RuntimeError(
                f"本地 jar 不存在: {p}（先执行构建，或把产物与 .sha256 放入 dist 目录）")
        side = p.with_name(p.name + ".sha256")
        if not side.exists():
            raise RuntimeError(f"缺少校验文件: {side}（构建会自动生成）")
        expected = side.read_text(encoding="utf-8").strip().split()[0]
        if expected != sha256_file(p):
            raise RuntimeError(
                f"jar 校验和不一致: {p}（产物被改动或旁文件过期，请重新构建）")

    def query_current_version(self):
        """SSH 首台读 {app_home}/.deploy_version；无标记视为首次发布（返回 None）"""
        import deploy as deploy_mod
        servers = load_yaml(Path(default_servers_file(env=self.d.env))).get("servers", [])
        if not servers:
            raise RuntimeError("服务器清单为空，无法查询当前版本")
        first = servers[0]
        ssh_cfg = merge_server_ssh(get_ssh_config(self.d.config), first)
        server = deploy_mod.RemoteServer(first["ip"], ssh_cfg)
        try:
            out = server.exec(
                f"cat {shlex.quote(self.app_home + '/.deploy_version')} 2>/dev/null || true",
                timeout=15).strip()
            return out or None
        finally:
            server.close()

    def next_version(self, current: str) -> str:
        import deploy as deploy_mod
        return deploy_mod.bump_patch_version(current)
