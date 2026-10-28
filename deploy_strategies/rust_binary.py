"""Rust 单二进制发布策略（deploy_mode: rust_binary，Linux + systemd）

产物为 cargo --release 构建的 Linux 二进制（交叉编译需 build.rust_target，
见 build.py 的 rust 分支）；transfer 后 chmod 0755；ExecStart 直接执行
{app_home}/CURRENT/app——目标机零运行时依赖。
"""
import shlex

from .base import register
from .process_service import ProcessServiceStrategy


@register
class RustBinaryStrategy(ProcessServiceStrategy):
    name = "rust_binary"
    artifact_label = "二进制"

    def artifact_name(self, version: str) -> str:
        return f"{self.service}-{version or self.d.version}"

    def remote_entry(self) -> str:
        return "app"

    def post_transfer(self, server, remote_path: str):
        server.exec(f"chmod 0755 {shlex.quote(remote_path)}", timeout=15)

    def runtime_probe(self, server):
        pass  # 无解释器：目标机零依赖，预检只看 systemctl/磁盘（基类）
