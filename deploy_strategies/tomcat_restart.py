"""重启任务的最小发布上下文：只提供 Nacos 摘挂流与版本字段。

作为 WarTomcatLifecycle 的最小 `d`（发布器）：不承载完整 Deployer 编排，
只补齐严格重启所需的能力——DB 环境配置直连 Nacos、按服务/分组摘流挂流、
摘流前快照与按快照恢复。
"""
import json

# scripts/ 目录无 __init__.py，`import scripts.nacos_manager` 会生成第二个模块
# 实例（配置覆盖失效，见 deploy.py 同款注释）；deploy_strategies.base 已把
# scripts/ 加入模块搜索路径，按仓库既有约定以顶层模块名导入
from nacos_manager import NacosClient


class RestartContext:
    def __init__(self, env_obj, rec):
        env_obj = env_obj or {}
        self.version = ""
        self.previous_version = ""
        self.nacos = NacosClient(config={"nacos": env_obj.get("nacos") or {}})
        self.services = [rec.get("service_name") or rec["name"]]
        groups = rec.get("nacos_groups") or []
        if isinstance(groups, str):
            try:
                groups = json.loads(groups)
            except ValueError:
                groups = []
        self.groups = groups or ([self.nacos.group] if self.nacos.configured
                                 else [])

    def _nacos_ip(self, server_entry=None):
        if not server_entry:
            return ""
        return server_entry.get("nacos_ip") or server_entry.get("host") or ""

    def nacos_offline(self, host):
        for svc in self.services:
            for grp in self.groups:
                self.nacos.offline_ip(svc, host, group=grp)
        for svc in self.services:
            for grp in self.groups:
                self.nacos.wait_for_absence(svc, host, timeout=60, group=grp)

    def nacos_online(self, host, weight=1.0):
        restored = False
        for svc in self.services:
            for grp in self.groups:
                if self.nacos.online_ip(svc, host, weight, group=grp):
                    restored = True
        if not restored:
            return
        for svc in self.services:
            for grp in self.groups:
                self.nacos.wait_for_healthy(svc, host, timeout=120, group=grp)

    def capture_traffic(self, host):
        """摘流前快照：记录该 IP 在各服务/分组下的端口、开关、权重与集群。"""
        snapshot = {}
        for svc in self.services:
            for grp in self.groups:
                rows = []
                for h in self.nacos.list_instances(svc, group=grp):
                    if h.get("ip") != host:
                        continue
                    rows.append({
                        "port": h.get("port"),
                        "enabled": bool(h.get("enabled")),
                        "weight": float(h.get("weight", 0) or 0),
                        "cluster": h.get("clusterName", "DEFAULT"),
                        "ephemeral": h.get("ephemeral"),
                    })
                if rows:
                    snapshot[f"{svc}|{grp}"] = rows
        return snapshot

    def restore_traffic(self, host, snapshot):
        """按快照恢复；原本 enabled=false 的实例不强制开启。"""
        restored = set()
        for key, rows in (snapshot or {}).items():
            svc, _, grp = key.partition("|")
            for r in rows:
                if not r.get("enabled"):
                    continue
                self.nacos.set_instance(
                    svc, host, r.get("port"), enabled=True,
                    weight=float(r.get("weight", 1) or 1),
                    cluster=r.get("cluster") or "DEFAULT",
                    group=grp or None, ephemeral=r.get("ephemeral"))
                restored.add((svc, grp))
        for svc, grp in sorted(restored):
            self.nacos.wait_for_healthy(svc, host, timeout=120,
                                        group=grp or None)
