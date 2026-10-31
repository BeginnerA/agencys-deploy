"""WAR/Tomcat 严格发布策略：所有主机动作走结构化生命周期。

deploy_mode: war_tomcat。发布/回滚/重启一律委托 WarTomcatLifecycle 并返回
逐主机 HostResult（编排层 _strict_deploy/_rollback_all_strict 只消费结果，
不再走旧五阶段/Nacos 编排）；实例身份只从归属文件解析（resolve_instance_id），
未接入的主机直接失败，不做无锁预传输，也不本地推导任何远端状态。
"""
import hashlib
import re
import shlex
from pathlib import Path

from . import tomcat_instance
from .base import DeployStrategy, register
from .tomcat_config import (derive_war_unit, parse_war_config,
                            validate_war_config)
from .tomcat_lifecycle import HostResult, WarTomcatLifecycle


@register
class WarTomcatStrategy(DeployStrategy):
    name = "war_tomcat"
    is_process = True
    artifact_label = "WAR"
    supports_nacos = True
    supports_canary = True
    supports_prefetch = False
    supports_restart = True
    strict_host_lifecycle = True
    version_scheme = "semver"

    def __init__(self, deployer):
        super().__init__(deployer)
        # Task 8 评审裁定：lifecycle._stage 经 self.d.strategy 取 local_artifact
        # 计算制品摘要，构造期必须把自身挂回部署器，否则发布时 strategy 属性
        # 仍为 None（Deployer.__init__ 随后赋同一对象，幂等）
        deployer._strategy = self
        proj = getattr(deployer, "proj_cfg", None) or {}
        raw = proj.get("runtime_config")
        errors = validate_war_config(raw)
        if errors:
            raise ValueError("WAR 配置不合法: " + "；".join(errors))
        self.cfg = parse_war_config(raw)
        self.app_home = self.cfg["app_home"]
        self.service = proj.get("image_name") or deployer.project
        self.unit = derive_war_unit(self.cfg, self.service)
        self.last_results = {}
        self._ids = {}
        self.lifecycle = WarTomcatLifecycle(
            deployer, self.cfg, self.service, self.unit,
            instance_id=self.instance_id)

    def instance_id(self, server):
        """每台主机只从归属文件解析一次；未接入时直接失败，不生成临时身份。"""
        host = getattr(server, "host", "")
        if host not in self._ids:
            self._ids[host] = tomcat_instance.resolve_instance_id(
                server, self.app_home)
        return self._ids[host]

    def artifact_name(self, version: str) -> str:
        return f"{self.service}-{version or self.d.version}.war"

    def local_artifact(self, version: str = None) -> Path:
        return Path(getattr(self.d, "dist_dir", Path("dist"))) / \
            self.artifact_name(version or self.d.version)

    def check_local(self):
        p = self.local_artifact()
        side = p.with_name(p.name + ".sha256")
        if not p.is_file():
            raise RuntimeError(f"本地 WAR 不存在: {p}（先执行构建）")
        if not side.is_file():
            raise RuntimeError(f"缺少校验文件: {side}（构建会自动生成）")
        expected = side.read_text(encoding="utf-8").strip().split()[0]
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        if h.hexdigest() != expected:
            raise RuntimeError(f"WAR 校验和不一致: {p}（请重新构建）")

    def query_current_version(self):
        import deploy as deploy_mod
        from common import (default_servers_file, get_ssh_config, load_yaml,
                            merge_server_ssh)
        servers = load_yaml(Path(default_servers_file(env=self.d.env))).get(
            "servers", [])
        if not servers:
            raise RuntimeError("服务器清单为空，无法查询当前版本")
        first = servers[0]
        server = deploy_mod.RemoteServer(
            first["ip"], merge_server_ssh(get_ssh_config(self.d.config), first))
        try:
            link = server.exec(
                f"readlink -f {shlex.quote(self.app_home + '/CURRENT')} "
                f"2>/dev/null || true", timeout=15).strip()
            if link:
                return link.rstrip("/").rsplit("/", 1)[-1]
            mark = server.exec(
                f"cat {shlex.quote(self.app_home + '/.deploy_version')} "
                f"2>/dev/null || true", timeout=15).strip()
            return mark or None
        finally:
            server.close()

    def next_version(self, current: str) -> str:
        import deploy as deploy_mod
        return deploy_mod.bump_patch_version(current)

    def prefetch(self, servers: list) -> list:
        return []  # 严格路径在持锁后暂存上传，不做无锁预传输

    def rollback_source_missing(self, server) -> bool:
        return False  # 缺源由 rollback_host 在停机前返回 blocked

    def _record(self, server, result):
        self.last_results[server.host] = result.to_dict()
        return result

    def deploy_host(self, server, server_entry: dict = None):
        return self._record(server, self.lifecycle.deploy(server, server_entry))

    def rollback_host(self, server, target_version: str,
                      server_entry: dict = None):
        # T8 评审 carry-forward：版本名是 API 入口参数，必须先校验字符集，
        # 防 `../` 形状越过 releases/ 解析（shq 只防注入，不防路径穿越）
        if not re.fullmatch(r"[A-Za-z0-9._-]+", target_version or ""):
            return HostResult(False, "failed", "rollback_preflight", "",
                              target_version, "running", "unknown", "blocked",
                              "unknown", "released", "回滚版本名非法")
        return self._record(
            server, self.lifecycle.rollback(server, target_version, server_entry))

    def restart_host(self, server, server_entry: dict = None,
                     release_maintenance: bool = False):
        return self._record(
            server, self.lifecycle.restart(server, server_entry,
                                           release_maintenance))
