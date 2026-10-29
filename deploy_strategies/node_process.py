"""Node 进程发布策略（deploy_mode: node_process，Linux + systemd）

产物为目录包（源码 + node_modules，npm ci/prune 后 tar.gz，见 build.py node 分支），
transfer 解压平铺到 releases/{ver}/；ExecStart = {node_bin} CURRENT/{start_file}。
"""
import shlex

from .base import register
from .process_service import ProcessServiceStrategy


@register
class NodeProcessStrategy(ProcessServiceStrategy):
    name = "node_process"
    artifact_kind = "archive"
    artifact_label = "压缩包"

    def artifact_name(self, version: str) -> str:
        return f"{self.service}-{version or self.d.version}.tar.gz"

    def remote_entry(self) -> str:
        return None  # archive：入口由 runtime_config.start_file 决定

    def runtime_probe(self, server):
        server.exec(f"command -v {shlex.quote(self.cfg['node_bin'])} >/dev/null",
                    timeout=15)

    def post_extract(self, server, tmp_dir: str, version: str):
        # Windows 平台机打 tar 丢失执行位：解压后就位前修 npm .bin 软链/脚本的 +x
        server.exec(
            f"chmod -R u+x {shlex.quote(tmp_dir + '/node_modules/.bin')} "
            f"2>/dev/null || true", timeout=60)
