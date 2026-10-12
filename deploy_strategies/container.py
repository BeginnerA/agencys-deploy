#!/usr/bin/env python3
"""容器发布策略（deploy_mode: container，默认）

现有 docker compose 发布形态的原样搬迁（重构非重写）：镜像分发（push/direct 双模式）、
Nacos 优雅上下线、金丝雀、semver 版本号。实现仍驻留 Deployer 上的同名方法
（update_container / rollback_container / ensure_image_ready / prefetch_images /
ensure_docker_login），本策略是编排层与这些实现之间的适配层——保持既有测试
patch 点（Deployer 方法、deploy.wait_for_healthy）不变。
"""
from .base import DeployStrategy, register


@register
class ContainerStrategy(DeployStrategy):
    name = "container"
    supports_nacos = True
    supports_canary = True
    supports_prefetch = True
    supports_restart = True
    version_scheme = "semver"

    # ---------- 阶段 0 ----------

    def check_local(self):
        # 本地镜像就绪性由 _ensure_local_tar / docker pull 在传输阶段兜底报错
        return

    def query_current_version(self):
        """SSH 查询首台服务器容器当前镜像 tag → 版本号；委托既有模块级函数"""
        import deploy as deploy_mod
        return deploy_mod.get_current_remote_version(self.d.project, self.d.config, env=self.d.env)

    def next_version(self, current: str) -> str:
        import deploy as deploy_mod
        return deploy_mod.bump_patch_version(current)

    # ---------- 五阶段 ----------

    def check(self, server):
        # 容器模式无独立预检阶段（登录检查在 prepare、镜像在 transfer 校验）
        return

    def prepare(self, server):
        self.d.ensure_docker_login(server)

    def transfer(self, server, version: str = None):
        # 容器模式的制品送达融合在 switch 内（update_container 的 ensure_image_ready /
        # docker pull 本身幂等，滚动阶段天然自带重试），无独立预送达语义；
        # 预传输并行由 prefetch → Deployer.prefetch_images 承担（直传模式）
        return False

    def switch(self, server):
        self.d.update_container(server)

    def verify(self, server, server_entry: dict = None):
        self.d.wait_health(server.host, server_entry)

    def rollback(self, server):
        self.d.rollback_container(server)
