"""健康检查：HTTP 探活 + 远程容器日志错误关键字检查"""
import sys
import time
import re
import argparse
import requests
import paramiko
from common import load_config, get_project_config, get_ssh_config, connect_ssh, merge_server_ssh, log


def check_http(url: str, timeout: int = 10) -> bool:
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code == 200:
            # Spring Boot Actuator 会返回 UP；空 body 端点（如静态健康页）视为健康
            body = resp.text
            return (not body.strip()) or "UP" in body or "ok" in body.lower()
        return False
    except Exception as e:
        log(f"健康检查请求失败: {e}")
        return False


def get_ssh_client(host: str, ssh_cfg: dict) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        connect_ssh(client, host, ssh_cfg)
        return client
    except Exception as e:
        # 抛异常而非直接退出：让 deploy_server 有机会回滚容器/恢复 Nacos/SnailJob
        raise RuntimeError(f"SSH 连接 {host} 失败: {e}")


def check_container_running(host: str, service_name: str, ssh_cfg: dict) -> bool:
    """检查容器是否处于运行状态（docker ps 显示 Up），用于未配置 health_url 时的退化检查"""
    client = get_ssh_client(host, ssh_cfg)
    try:
        # 注意：str.format 会把 {{ 转义为 {，Go 模板双大括号必须写成四层 {{{{，
        # 否则 docker --format 收到无效模板 {.Status} 会原样输出导致永远判为未运行
        cmd = "docker ps --filter 'name=^{}$' --format '{{{{.Status}}}}'".format(service_name)
        stdin, stdout, stderr = client.exec_command(cmd, timeout=30)
        status = stdout.read().decode("utf-8", errors="ignore").strip()
        if status.startswith("Up"):
            return True
        log(f"容器 {service_name} 未运行: {status or '(不存在)'}")
        return False
    finally:
        client.close()


def check_container_logs(host: str, service_name: str, error_keywords: list, ssh_cfg: dict, tail: int = 100) -> bool:
    """检查容器日志是否包含启动错误关键字"""
    client = get_ssh_client(host, ssh_cfg)
    try:
        cmd = f"docker logs --tail {tail} {service_name} 2>&1"
        stdin, stdout, stderr = client.exec_command(cmd, timeout=30)
        logs = stdout.read().decode("utf-8", errors="ignore")
        for kw in error_keywords:
            if re.search(kw, logs, re.IGNORECASE):
                log(f"容器日志中发现错误关键字: {kw}")
                return False
        return True
    finally:
        client.close()


def wait_for_healthy(host: str, project_name: str, version: str = None, ssh_override: dict = None, env: str = None) -> bool:
    """等待目标服务器上的项目健康上线"""
    config = load_config(env=env)
    proj = get_project_config(project_name)
    ssh_cfg = get_ssh_config(config)
    if ssh_override:
        ssh_cfg = merge_server_ssh(ssh_cfg, ssh_override)
    health_url = (proj.get("health_url") or "").format(ip=host)
    timeout = config["deploy"].get("health_timeout", 120)
    interval = config["deploy"].get("health_interval", 5)
    service_name = proj["service_name"]

    if health_url:
        log(f"开始健康检查: {health_url}，超时 {timeout} 秒")
        is_ok = lambda: check_http(health_url)
    else:
        # 未配置 health_url：跳过 HTTP 检查，退化为容器运行状态检查，避免盲发
        log(f"未配置 health_url，改用容器运行状态检查: {service_name}，超时 {timeout} 秒")
        is_ok = lambda: check_container_running(host, service_name, ssh_cfg)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_ok():
            if check_container_logs(host, service_name, proj.get("log_error_keywords", []), ssh_cfg):
                log(f"健康检查通过: {host}")
                return True
            else:
                raise RuntimeError(f"健康检查失败: {host} 容器日志存在错误关键字")
        time.sleep(interval)
    raise RuntimeError(f"健康检查超时: {host}")


def main():
    parser = argparse.ArgumentParser(description="健康检查")
    parser.add_argument("--host", required=True, help="目标服务器 IP")
    parser.add_argument("--project", required=True, help="项目名称")
    parser.add_argument("--version", help="当前版本号")
    parser.add_argument("--env", help="目标环境，例如 prod/test")
    args = parser.parse_args()
    wait_for_healthy(args.host, args.project, args.version, env=args.env)


if __name__ == "__main__":
    main()
