"""纯 Java 进程发布策略（deploy_mode: jar_service，Linux + systemd）

进程族共享基类（process_service.ProcessServiceStrategy）的 Java 形态：
产物为可执行 jar（dist/{service}-{version}.jar + .sha256），
ExecStart = {java_bin} {jvm_opts} -jar {app_home}/CURRENT/app.jar {app_args}。
五阶段语义与基类完全一致；jar 的默认 readiness（http + /actuator/health）由
process_config 按模式分派，行为与首期实现逐字一致。
"""
import shlex
from pathlib import Path

from .base import register
from .jar_config import artifact_filename
from .process_service import ProcessServiceStrategy


@register
class JarServiceStrategy(ProcessServiceStrategy):
    name = "jar_service"
    artifact_label = "jar"

    def artifact_name(self, version: str) -> str:
        return artifact_filename(self.service, version or self.d.version)

    def remote_entry(self) -> str:
        return "app.jar"

    def runtime_probe(self, server):
        server.exec(f"command -v {shlex.quote(self.cfg['java_bin'])} >/dev/null",
                    timeout=15)

    # ---------- 兼容别名：既有测试与调用点使用 jar 命名 ----------

    def jar_name(self, version: str = None) -> str:
        return self.artifact_name(version or self.d.version)

    def local_jar(self, version: str = None) -> Path:
        return self.local_artifact(version or self.d.version)
