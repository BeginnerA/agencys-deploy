#!/usr/bin/env python3
"""智能运维平台 便携版启动器：自动选端口 → 启动平台 → 打开浏览器。

- 若检测到平台已在运行（端口上响应本平台登录页），直接打开浏览器，不重复启动；
- 端口从 5678 起自动避让（最多 20 个）；
- 关闭本窗口即停止平台。
"""
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
START_PORT = 5678
MAX_TRIES = 20
READY_TIMEOUT = 90  # 首启需自动建库并导入出厂配置

BANNER = r"""
==================================================
  智能运维平台 便携版
==================================================
"""


def _login_page_body(port: int):
    """端口上返回本平台登录页时给出页面文本，否则 None"""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/", timeout=1.5) as resp:
            if resp.status != 200:
                return None
            return resp.read(8192).decode("utf-8", "ignore")
    except Exception:
        return None


def _is_platform(port: int) -> bool:
    body = _login_page_body(port)
    return bool(body) and "智能运维平台" in body


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _pick_port():
    """返回 (port, already_running)：优先复用已在跑的本平台实例"""
    for port in range(START_PORT, START_PORT + MAX_TRIES):
        if _is_platform(port):
            return port, True
        if _port_free(port):
            return port, False
    raise SystemExit(
        f"[launcher] 端口 {START_PORT}-{START_PORT + MAX_TRIES - 1} 全部被占用，无法启动。")


def main() -> int:
    print(BANNER)
    port, running = _pick_port()
    url = f"http://127.0.0.1:{port}/"

    if running:
        print(f"[launcher] 平台已在运行：{url}")
        webbrowser.open(url)
        print("[launcher] 已打开浏览器。停止平台：任务管理器结束 python.exe 进程。")
        return 0

    print(f"[launcher] 正在启动平台（端口 {port}）…… 首次启动会自动建库，请稍候")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "webapp" / "app.py"), "--port", str(port)],
        cwd=str(ROOT),
    )

    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"[launcher] 启动失败（退出码 {proc.returncode}），请查看上方错误信息")
            return proc.returncode or 1
        if _is_platform(port):
            print(f"[launcher] 平台已就绪：{url}")
            print("[launcher] 浏览器已自动打开。本窗口保持开启即为运行中；关闭本窗口即停止平台。")
            webbrowser.open(url)
            return proc.wait()
        time.sleep(0.5)

    print(f"[launcher] 等待就绪超时（{READY_TIMEOUT}s），请手动访问 {url} 检查；本窗口保持开启。")
    return proc.wait()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[launcher] 已中断")
        sys.exit(1)
