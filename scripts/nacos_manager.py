"""Nacos 实例管理：下线、上线、改权重、查询

兼容 Nacos 1.x Open API。若使用 Nacos 2.x，通常 1.x API 仍兼容，
如有差异请在 config/deploy.yaml 中调整 url 或自行适配。
"""
import sys
import time
import requests
from urllib.parse import urlencode
from common import load_config, get_nacos_auth, log, fail


class NacosClient:
    def __init__(self, env: str = None):
        config = load_config(env=env)
        auth = get_nacos_auth(config)
        self.base = auth["url"]
        self.namespace = auth["namespace"]
        self.group = auth["group"]
        self.username = auth["username"]
        self.password = auth["password"]
        self._token = None
        self._token_expire = 0
        # 下线实例快照：{(service_name, group, ip): [{port, cluster, ephemeral}]}
        # 实例被 enabled=false 后 list 接口不再返回它，上线必须依赖快照直接恢复
        self._snapshots = {}
        # 记录哪些 (service_name, group, ip) 在 offline 时确实找到了实例
        # 用于 online 时判断是否需要等待 Nacos 健康（未快照说明回滚前就未注册，无需等待）
        self._snapshotted_keys = set()

    def _ensure_token(self):
        if not self.username:
            return {}
        now = time.time()
        if self._token and now < self._token_expire - 60:
            return {"accessToken": self._token}
        url = f"{self.base}/nacos/v1/auth/users/login"
        try:
            resp = requests.post(url, data={"username": self.username, "password": self.password}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            self._token = data.get("accessToken")
            # token 默认有效期 18000 秒
            self._token_expire = now + data.get("tokenTtl", 18000)
            return {"accessToken": self._token}
        except Exception as e:
            # 抛异常而非直接退出：让 deploy_server 有机会回滚容器/恢复上线
            raise RuntimeError(f"Nacos 登录失败: {e}")

    def _params(self, extra: dict = None, group: str = None) -> dict:
        params = {
            "namespaceId": self.namespace,
            "groupName": group or self.group,
        }
        params.update(self._ensure_token())
        if extra:
            params.update(extra)
        return params

    def list_instances(self, service_name: str, group: str = None) -> list:
        url = f"{self.base}/nacos/v1/ns/instance/list"
        params = self._params({"serviceName": service_name, "healthyOnly": "false"}, group=group)
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return data.get("hosts", [])
        except Exception as e:
            raise RuntimeError(f"查询 Nacos 服务实例失败 [{service_name}/{group or self.group}]: {e}")

    def set_instance(self, service_name: str, ip: str, port: int, enabled: bool, weight: float = 1.0, cluster: str = "DEFAULT", group: str = None, ephemeral: bool = None):
        """enabled=False/weight=0 即下线；enabled=True/weight=1 即上线

        ephemeral 须与实例注册时一致（Spring Cloud 注册的临时实例为 True），
        缺失时更新可能作用于错误的实例存储，故调用方应尽量传入。
        """
        url = f"{self.base}/nacos/v1/ns/instance"
        params = self._params({
            "serviceName": service_name,
            "ip": ip,
            "port": port,
            "enabled": str(enabled).lower(),
            "weight": weight,
            "clusterName": cluster,
        }, group=group)
        if ephemeral is not None:
            params["ephemeral"] = str(ephemeral).lower()
        grp_label = group or self.group
        try:
            resp = requests.put(url, params=params, timeout=10)
            resp.raise_for_status()
            log(f"Nacos [{service_name}/{grp_label}] {ip}:{port} enabled={enabled} weight={weight} => {resp.text}")
        except Exception as e:
            raise RuntimeError(f"更新 Nacos 实例失败 [{service_name}/{grp_label} {ip}:{port}]: {e}")

    def offline_ip(self, service_name: str, ip: str, group: str = None):
        """把指定 IP 下的所有实例全部下线，并记录快照供上线恢复"""
        hosts = self.list_instances(service_name, group=group)
        grp = group or self.group
        snap_key = (service_name, grp, ip)
        snapshots = []
        found = False
        for h in hosts:
            if h.get("ip") == ip:
                self.set_instance(service_name, ip, h.get("port"), enabled=False, weight=0, cluster=h.get("clusterName", "DEFAULT"), group=group, ephemeral=h.get("ephemeral"))
                snapshots.append({
                    "port": h.get("port"),
                    "cluster": h.get("clusterName", "DEFAULT"),
                    "ephemeral": h.get("ephemeral"),
                })
                found = True
        if found:
            self._snapshots[snap_key] = snapshots
            self._snapshotted_keys.add(snap_key)
        else:
            log(f"Nacos [{service_name}/{grp}] 未找到 IP={ip} 的实例，可能已下线")

    def online_ip(self, service_name: str, ip: str, weight: float = 1.0, group: str = None) -> bool:
        """把指定 IP 下的所有实例全部上线并设置权重
    
        优先用下线时记录的快照直接恢复（实例下线后 list 接口不再返回它，
        靠 list 查找会永远找不到）；再用 list 兜底处理新注册的实例。
    
        Returns:
            True  - 找到并恢复了实例（快照或 list 兜底）
            False - 未找到任何实例（无快照且 list 中不可见），调用方不应等待健康检查
        """
        grp = group or self.group
        snap_key = (service_name, grp, ip)
        restored = 0
        for s in self._snapshots.pop(snap_key, []):
            self.set_instance(service_name, ip, s["port"], enabled=True, weight=weight, cluster=s["cluster"], group=group, ephemeral=s.get("ephemeral"))
            restored += 1
        # 兜底：容器重启后应用重新注册的实例（默认 enabled=true），权重不符时纠正
        hosts = self.list_instances(service_name, group=group)
        for h in hosts:
            if h.get("ip") == ip and (not h.get("enabled") or float(h.get("weight", 0)) != weight):
                self.set_instance(service_name, ip, h.get("port"), enabled=True, weight=weight, cluster=h.get("clusterName", "DEFAULT"), group=group, ephemeral=h.get("ephemeral"))
                restored += 1
        if restored == 0:
            log(f"Nacos [{service_name}/{grp}] 无下线快照且 list 中不可见 IP={ip} 的实例，未执行上线（可能尚未注册）")
            return False
        return True

    def wait_for_absence(self, service_name: str, ip: str, timeout: int = 60, group: str = None):
        """等待指定 IP 在该服务下无健康实例"""
        grp_label = group or self.group
        deadline = time.time() + timeout
        while time.time() < deadline:
            hosts = self.list_instances(service_name, group=group)
            if not any(h.get("ip") == ip and h.get("healthy") for h in hosts):
                log(f"Nacos [{service_name}/{grp_label}] IP={ip} 已确认无健康实例")
                return True
            time.sleep(2)
        raise RuntimeError(f"等待 Nacos [{service_name}/{grp_label}] IP={ip} 下线超时")

    def wait_for_healthy(self, service_name: str, ip: str, timeout: int = 120, group: str = None):
        """等待指定 IP 在该服务下有健康实例"""
        grp_label = group or self.group
        deadline = time.time() + timeout
        while time.time() < deadline:
            hosts = self.list_instances(service_name, group=group)
            if any(h.get("ip") == ip and h.get("healthy") for h in hosts):
                log(f"Nacos [{service_name}/{grp_label}] IP={ip} 已健康上线")
                return True
            time.sleep(2)
        raise RuntimeError(f"等待 Nacos [{service_name}/{grp_label}] IP={ip} 健康上线超时")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Nacos 实例管理")
    parser.add_argument("action", choices=["list", "offline", "online"])
    parser.add_argument("--service", required=True, help="服务名")
    parser.add_argument("--ip", help="目标服务器 IP")
    parser.add_argument("--weight", type=float, default=1.0, help="上线时权重")
    parser.add_argument("--group", help="Nacos 分组（默认使用配置中的 group）")
    parser.add_argument("--env", help="目标环境，例如 prod/test")
    args = parser.parse_args()

    client = NacosClient(env=args.env)
    if args.action == "list":
        hosts = client.list_instances(args.service, group=args.group)
        for h in hosts:
            print(f"{h.get('ip')}:{h.get('port')} healthy={h.get('healthy')} enabled={h.get('enabled')} weight={h.get('weight')}")
    elif args.action == "offline":
        if not args.ip:
            fail("--ip 必填")
        client.offline_ip(args.service, args.ip, group=args.group)
    elif args.action == "online":
        if not args.ip:
            fail("--ip 必填")
        client.online_ip(args.service, args.ip, args.weight, group=args.group)


if __name__ == "__main__":
    main()
