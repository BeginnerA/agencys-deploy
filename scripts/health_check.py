"""健康检查：HTTP 探活 + TCP 端口连通 + 远程容器运行/日志错误关键字检查"""
import sys
import time
import re
import socket
import argparse
import requests
import paramiko
from common import load_config, get_project_config, get_ssh_config, connect_ssh, merge_server_ssh, log, as_number


def check_http(url: str, timeout: int = 10, require_health_body: bool = True) -> bool:
    """HTTP 探活。

    require_health_body=True（默认，容器/Actuator 语义）：200 且 body 为空或含 UP/ok 才健康；
    require_health_body=False（静态站点语义）：任意 200 即健康——静态首页 index.html 无 UP 体，
    沿用 Actuator body 判定会把「已成功换入」的静态站点误判为失败（与 monitor 静态探活同源口径）。
    """
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code == 200:
            if not require_health_body:
                # 静态站点：200 即视为在线，不校验响应体内容
                return True
            # Spring Boot Actuator 会返回 UP；空 body 端点（如静态健康页）视为健康
            body = resp.text
            return (not body.strip()) or "UP" in body or "ok" in body.lower()
        return False
    except Exception as e:
        log(f"健康检查请求失败: {e}")
        return False


def check_tcp_port(host: str, port: int, timeout: int = 5) -> bool:
    """TCP 端口连通性探测（从本机直连 host:port）：仅判断端口是否在接受连接。
    注意：微服务业务端口通常只在宿主机 localhost/内网暴露，云安全组不对公网放行，
    从平台机直连公网 IP 必被静默丢包（报 timed out 而非 refused），会造成健康检查
    假超时误判。发布/回退路径应优先用 check_tcp_port_via_ssh 在服务器内部探测，
    本函数仅在无 SSH 通道时作为最后兜底。"""
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        sock.close()
        return True
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        log(f"TCP 端口探测失败 {host}:{port}: {e}")
        return False


def check_tcp_port_via_ssh(host: str, port: int, ssh_cfg: dict) -> bool:
    """经 SSH 在目标服务器内部对 127.0.0.1:port 做 TCP 探测。

    发布链路本就持有 SSH 通道，从服务器内部探测回环端口才是「应用已在监听」的权威口径，
    且不依赖云安全组对外暴露——规避平台机直连公网业务端口被丢包导致的假超时误回滚。
    探测机制本身不可用（SSH 连不上 / 无 bash 无 /dev/tcp）返回 None，由调用方回退其他检查；
    明确 UP/DOWN 返回 True/False。"""
    try:
        client = get_ssh_client(host, ssh_cfg)
    except Exception as e:
        log(f"内部 TCP 探测无法建立 SSH 通道（回退其他检查）: {e}")
        return None
    try:
        # timeout 兜底避免 hang；bash /dev/tcp 打开回环端口即视为应用已监听
        cmd = (f"timeout 3 bash -c 'exec 3<>/dev/tcp/127.0.0.1/{int(port)}' "
               f"2>/dev/null && echo __UP__ || echo __DOWN__")
        stdin, stdout, stderr = client.exec_command(cmd, timeout=15)
        out = stdout.read().decode("utf-8", errors="ignore")
        if "__UP__" in out:
            return True
        if "__DOWN__" in out:
            log(f"服务器内部 TCP 探测 127.0.0.1:{port} 未通过（应用尚未监听该端口）")
            return False
        # 无 bash/无 /dev/tcp：命令未产出判定标记，视为探测机制不可用
        err = stderr.read().decode("utf-8", errors="ignore").strip()
        log(f"内部 TCP 探测未获得判定结果（回退其他检查）: {out.strip() or err[:120] or '(无输出)'}")
        return None
    except Exception as e:
        log(f"内部 TCP 探测执行异常（回退其他检查）: {e}")
        return None
    finally:
        client.close()


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
    # 数字字段容错：Dashboard 清空数字字段会存为空串；非正数同样无效（timeout<=0 时 deadline 立即过期必判超时）
    timeout = as_number(config["deploy"].get("health_timeout"), 120)
    interval = as_number(config["deploy"].get("health_interval"), 5)
    if timeout <= 0:
        timeout = 120
    if interval <= 0:
        interval = 5
    service_name = proj["service_name"]
    health_port = proj.get("health_port") or 0

    if health_url:
        log(f"开始健康检查: {health_url}，超时 {timeout} 秒")
        is_ok = lambda: check_http(health_url)
    elif health_port:
        # 未配置 health_url 但配了业务端口：在服务器内部对回环端口做 TCP 连通检查。
        # 微服务业务端口常不对公网放行（云安全组只挡外部），从平台机直连公网 IP 必超时
        # 导致假失败误回滚，故经既有 SSH 通道在 127.0.0.1 本地探测（与 monitor 探活同源口径）。
        log(f"未配置 health_url，改用服务器内部回环 TCP 端口连通检查: 127.0.0.1:{health_port}，超时 {timeout} 秒")

        def is_ok():
            verdict = check_tcp_port_via_ssh(host, health_port, ssh_cfg)
            if verdict is True:
                return True
            # verdict False（回环未监听）或 None（探测机制不可用）：回退容器运行状态检查。
            # 微服务常只在 docker 内部网络监听、未发布到宿主机回环（或无 bash），
            # 此时回环探 DOWN 并不代表应用异常，误判会触发无谓回滚；容器 Up
            # 且后续日志无错误关键字（wait_for_healthy 紧接的 check_container_logs）即视为健康。
            try:
                return check_container_running(host, service_name, ssh_cfg)
            except Exception as e:
                log(f"容器运行状态检查失败（保持轮询直至超时）: {e}")
                return False
    else:
        # 既无 health_url 也无 health_port：跳过网络检查，退化为容器运行状态检查，避免盲发
        log(f"未配置 health_url/health_port，改用容器运行状态检查: {service_name}，超时 {timeout} 秒")
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
