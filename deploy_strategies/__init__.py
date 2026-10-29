#!/usr/bin/env python3
"""发布策略包：导入即注册所有可用策略"""
from .base import DeployStrategy, register, create_strategy
from . import container  # noqa: F401 — 触发 @register ContainerStrategy
from . import static_archive  # noqa: F401 — 触发 @register StaticArchiveStrategy
from . import jar_service  # noqa: F401 — 触发 @register JarServiceStrategy（进程族基类的 Java 形态）
from . import go_binary  # noqa: F401 — 触发 @register GoBinaryStrategy
from . import rust_binary  # noqa: F401 — 触发 @register RustBinaryStrategy
from . import node_process  # noqa: F401 — 触发 @register NodeProcessStrategy

__all__ = ["DeployStrategy", "register", "create_strategy"]
