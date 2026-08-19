"""验证服务器容器的实际内网 IP"""
import sys, paramiko, os
os.environ["PLATFORM_DEPLOY_ENV"] = "test"
sys.path.insert(0, 'scripts')
from common import load_config, get_ssh_config, connect_ssh, merge_server_ssh, load_yaml, default_servers_file
from pathlib import Path

cfg = load_config(env='test')
ssh_cfg = get_ssh_config(cfg)
servers = load_yaml(Path(default_servers_file(env='test'))).get('servers', [])
srv = [s for s in servers if s['ip'] == '47.95.172.17'][0]
ssh_cfg2 = merge_server_ssh(ssh_cfg, srv)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
connect_ssh(c, srv['ip'], ssh_cfg2)

# 查容器 IP
_, o, _ = c.exec_command("docker inspect hqb-expert --format '{{.NetworkSettings.IPAddress}}'", timeout=15)
print(f"容器 Docker 内网 IP: {o.read().decode().strip()}")

# 查宿主机内网 IP
_, o, _ = c.exec_command("hostname -I", timeout=15)
print(f"宿主机 IP: {o.read().decode().strip()}")

# 查配置中的 nacos_ip
print(f"配置中的 nacos_ip: {srv.get('nacos_ip', '(未配置)')}")

c.close()
