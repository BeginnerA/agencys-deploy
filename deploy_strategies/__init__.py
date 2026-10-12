#!/usr/bin/env python3
"""发布策略包：导入即注册所有可用策略"""
from .base import DeployStrategy, register, create_strategy
from . import container  # noqa: F401 — 触发 @register ContainerStrategy
from . import static_archive  # noqa: F401 — 触发 @register StaticArchiveStrategy

__all__ = ["DeployStrategy", "register", "create_strategy"]
