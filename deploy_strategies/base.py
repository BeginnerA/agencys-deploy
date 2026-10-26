#!/usr/bin/env python3
"""发布策略层：把发布生命周期收敛为「预检/传输/切换/验证/回滚」五阶段契约

编排层（deploy.Deployer）只负责通用调度（滚动/金丝雀/通知/SnailJob），具体"怎么发"
由本包按项目配置 deploy_mode 查注册表分发；新增发布方式 = 新增一个策略文件并
@register，不改 Deployer 核心（与 Nacos「未配置返回空列表让遍历零次」同构的
能力标志跳过哲学）。

一致性约定（全策略统一）：switch/rollback 执行前先把 deployer.target_modified
置 True——语义是"目标状态已被修改，失败时编排层必须尝试回滚"；未置位的阶段
（check/transfer 失败）编排层保持旧版本原状仅恢复 Nacos 上线。
字段原名 container_modified（容器专属语义），现统一为 target_modified，
container_modified 保留为 Deployer 上的读写兼容别名，既有策略无需同步改名。
"""
import sys
from pathlib import Path

# scripts/ 目录加入模块搜索路径（独立导入策略包时可自行兜底；deploy.py 已插入时为幂等空操作）
_SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from common import log  # noqa: E402

# 注册表：deploy_mode -> 策略类
_REGISTRY = {}


class DeployStrategy:
    """发布策略基类：五阶段接口 + 能力标志

    能力标志供编排层自动跳过方式专属步骤：
    - supports_nacos    服务注册中心优雅上下线（容器模式）
    - supports_canary   金丝雀分批/人工卡点
    - supports_prefetch 制品并行预传输到全部服务器（transfer 与 switch 分离）
    - supports_restart  docker restart 类原地重启任务
    - version_scheme    版本号语义：semver（x.y.z）/ date（YYYYMMDD）
    """
    name = ""
    supports_nacos = False
    supports_canary = False
    supports_prefetch = False
    supports_restart = False
    version_scheme = "semver"

    def __init__(self, deployer):
        self.d = deployer

    # ---------- 阶段 0：本地预检与版本推导 ----------

    def check_local(self):
        """构建/发布前校验本地产物就绪；默认无动作（容器模式产物即本地镜像）"""

    def query_current_version(self):
        """查询线上当前版本（自动回滚兜底/自动版本号）；未部署返回 None"""
        raise NotImplementedError

    def next_version(self, current: str) -> str:
        """基于线上当前版本推导下一版本号（--auto-version 语义）"""
        raise NotImplementedError

    # ---------- 五阶段生命周期 ----------

    def check(self, server):
        """预检：目标服务器就绪性（目录/工具/凭据），必须在造成任何变更前抛错"""
        raise NotImplementedError

    def prepare(self, server):
        """发布前置动作（如 docker login），必须幂等；默认无动作"""

    def transfer(self, server, version: str = None):
        """产物送达目标机（幂等：目标已有所需状态时跳过）；可被 prefetch 并行调用"""
        raise NotImplementedError

    def prefetch(self, servers: list) -> list:
        """并行预传输产物到全部服务器，返回失败主机列表（失败不中断，滚动阶段重试）

        默认委托 Deployer.prefetch_images（容器直传模式既有实现）；其他发布方式按需覆盖。
        """
        return self.d.prefetch_images(servers)

    def switch(self, server):
        """原子切换到 self.d.version；开始造成变更前置位 target_modified"""
        raise NotImplementedError

    def verify(self, server, server_entry: dict = None):
        """切换后的健康验证；失败抛异常触发编排层回滚"""
        raise NotImplementedError

    def rollback(self, server):
        """切回 self.d.previous_version；无回滚版本时告警跳过（与既有语义一致）"""
        raise NotImplementedError

    def rollback_source_missing(self, server) -> bool:
        """回滚源预检：True = 该主机没有目标版本产物，显式回滚应直接判失败

        默认 False（容器/静态由各自 rollback 内部处理缺源）；进程模式产物是本机目录，
        缺目录时「跳过回滚但任务成功」会误导操作者，因此在编排层提前拦截。
        """
        return False


def register(cls):
    if not getattr(cls, "name", ""):
        raise ValueError(f"策略类 {cls.__name__} 缺少 name")
    _REGISTRY[cls.name] = cls
    return cls


def create_strategy(deploy_mode: str, deployer):
    """按 deploy_mode 实例化策略；未知模式在构造期即报错（而非发布到一半才发现）"""
    deploy_mode = deploy_mode or "container"
    cls = _REGISTRY.get(deploy_mode)
    if cls is None:
        raise ValueError(f"未知 deploy_mode: {deploy_mode!r}（可选: {sorted(_REGISTRY)}）")
    return cls(deployer)


def registered_modes() -> list:
    """已注册的发布方式（路由校验与前端选项读取同一来源，新增策略无需改两处）"""
    return sorted(_REGISTRY)
