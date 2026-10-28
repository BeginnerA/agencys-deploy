"""纯 Java 进程发布策略（deploy_mode: jar_service，Linux + systemd）

产物按版本不可变存放，CURRENT 符号链接原子切换，systemd unit 的 ExecStart 固定
引用 {app_home}/CURRENT/app.jar：发布与回滚都只是「换链接 + 重启单元」，回滚不需要重传。
权威版本状态在目标机（.deploy_version 标记 + CURRENT 链接），平台库只做展示缓存。

与容器模式的根本差异：单实例重启必然有停机窗口，切换语义是「停机→换链接→启动」，
不承诺零停机；Nacos 上下线仍由编排层按能力标志调度，与运行时方式无关。
"""
import shlex
import time
from pathlib import Path

from .base import DeployStrategy, register
from .jar_config import (artifact_filename, derive_unit_name,
                         parse_runtime_config, sha256_file)
from common import (log, load_yaml, default_servers_file, merge_server_ssh,
                    get_ssh_config)


@register
class JarServiceStrategy(DeployStrategy):
    name = "jar_service"
    is_process = True
    supports_nacos = True
    supports_canary = True
    supports_prefetch = True
    supports_restart = True
    version_scheme = "semver"

    def __init__(self, deployer):
        super().__init__(deployer)
        proj = getattr(deployer, "proj_cfg", None) or {}
        self.cfg = parse_runtime_config(proj.get("runtime_config"))
        if not self.cfg["app_home"]:
            raise ValueError(
                "deploy_mode=jar_service 必须配置 runtime_config.app_home"
                "（如 /opt/apps/hqb-auth）")
        self.app_home = self.cfg["app_home"]
        self.service = (proj.get("image_name") or
                        getattr(deployer, "project", "app"))
        self.unit = derive_unit_name(self.cfg, self.service)
        self.jars_dir = f"{self.app_home}/releases"
        self.curr_link = f"{self.app_home}/CURRENT"

    # ---------- 产物定位 ----------

    def jar_name(self, version: str = None) -> str:
        return artifact_filename(self.service, version or self.d.version)

    def local_jar(self, version: str = None) -> Path:
        return Path(getattr(self.d, "dist_dir", Path("dist"))) / self.jar_name(version)

    # ---------- 阶段 0：本地预检与版本推导 ----------

    def check_local(self):
        p = self.local_jar()
        if not p.exists():
            raise RuntimeError(
                f"本地 jar 不存在: {p}（先执行构建，或把产物与 .sha256 放入 dist 目录）")
        side = p.with_name(p.name + ".sha256")
        if not side.exists():
            raise RuntimeError(f"缺少校验文件: {side}（构建会自动生成）")
        expected = side.read_text(encoding="utf-8").strip().split()[0]
        if expected != sha256_file(p):
            raise RuntimeError(
                f"jar 校验和不一致: {p}（产物被改动或旁文件过期，请重新构建）")

    def query_current_version(self):
        """SSH 首台读 {app_home}/.deploy_version；无标记视为首次发布（返回 None）"""
        import deploy as deploy_mod
        servers = load_yaml(Path(default_servers_file(env=self.d.env))).get("servers", [])
        if not servers:
            raise RuntimeError("服务器清单为空，无法查询当前版本")
        first = servers[0]
        ssh_cfg = merge_server_ssh(get_ssh_config(self.d.config), first)
        server = deploy_mod.RemoteServer(first["ip"], ssh_cfg)
        try:
            out = server.exec(
                f"cat {shlex.quote(self.app_home + '/.deploy_version')} 2>/dev/null || true",
                timeout=15).strip()
            return out or None
        finally:
            server.close()

    def next_version(self, current: str) -> str:
        import deploy as deploy_mod
        return deploy_mod.bump_patch_version(current)

    # ---------- 五阶段 ----------

    def check(self, server):
        """预检：Java、systemd、目录可写、磁盘余量——全部在造成变更前完成"""
        server.exec(f"command -v {shlex.quote(self.cfg['java_bin'])} >/dev/null", timeout=15)
        server.exec("command -v systemctl >/dev/null", timeout=15)
        server.exec(
            f"mkdir -p {shlex.quote(self.jars_dir)} && test -w {shlex.quote(self.app_home)}",
            timeout=15)
        self._check_disk(server)

    def _check_disk(self, server):
        """按包大小的 1.5 倍检查可用空间：留出解压/临时文件与一次失败重传的余量"""
        p = self.local_jar()
        if not p.exists():
            return  # 本地产物缺失由 check_local/transfer 报错，这里不重复报
        need_kb = int(p.stat().st_size * 1.5 / 1024) + 1
        out = server.exec(
            f"df -Pk {shlex.quote(self.app_home)} | awk 'NR==2 {{print $4}}'",
            timeout=15).strip()
        try:
            avail_kb = int(out)
        except ValueError:
            log(f"[{server.host}] 无法解析磁盘余量（{out!r}），跳过空间预检")
            return
        if avail_kb < need_kb:
            raise RuntimeError(
                f"[{server.host}] 磁盘空间不足: {self.app_home} 剩余 {avail_kb}KB，"
                f"需要约 {need_kb}KB（含 1.5 倍余量）")

    def prepare(self, server):
        """确认单元已由平台接入，且不会被误当成别人创建的服务接管

        平台对 unit 的承诺是「只管理自己创建的」：ExecStart 不指向 CURRENT/app.jar
        的单元一律拒绝，避免把人工维护的服务改坏。
        """
        installed = server.try_exec(
            f"systemctl cat {shlex.quote(self.unit)}.service >/dev/null 2>&1", timeout=15)
        if not installed:
            raise RuntimeError(
                f"[{server.host}] systemd 单元 {self.unit}.service 未接入"
                f"（先在服务配置页执行「生成并接入单元」，需管理员确认）")
        exec_start = server.exec(
            f"systemctl show -p ExecStart --value {shlex.quote(self.unit)}.service",
            timeout=15)
        expected = f"{self.curr_link}/app.jar"
        if expected not in exec_start:
            raise RuntimeError(
                f"[{server.host}] 拒绝接管非平台创建的单元 {self.unit}.service："
                f"ExecStart 未指向 {expected}")

    def transfer(self, server, version: str = None):
        """上传到 releases/{version}/app.jar（幂等：远端 sha256 相同则跳过）"""
        ver = version or self.d.version
        local = self.local_jar(ver)
        if not local.exists():
            raise RuntimeError(f"本地 jar 不存在: {local}")
        local_sha = sha256_file(local)
        remote_dir = f"{self.jars_dir}/{ver}"
        remote = f"{remote_dir}/app.jar"
        server.exec(f"mkdir -p {shlex.quote(remote_dir)}", timeout=15)
        if server.try_exec(f"test -f {remote}", timeout=15):
            current = server.exec(
                f"sha256sum {shlex.quote(remote)} | awk '{{print $1}}'", timeout=60).strip()
            if current == local_sha:
                log(f"[{server.host}] 远端已有相同产物，跳过上传: {ver}")
                return True
        log(f"[{server.host}] 上传 jar {self.jar_name(ver)}"
            f"（{local.stat().st_size / 1024 / 1024:.1f} MB）-> {remote}")
        server.sftp_put_with_progress(
            str(local), remote, label=self.jar_name(ver),
            streams=getattr(self.d, "upload_streams", 1))
        # 传输后必须复核：SFTP 中断可能留下半包，宁可在这里失败也不要切到坏包
        remote_sha = server.exec(
            f"sha256sum {shlex.quote(remote)} | awk '{{print $1}}'", timeout=60).strip()
        if remote_sha != local_sha:
            raise RuntimeError(
                f"[{server.host}] 上传后校验和不一致（本地 {local_sha[:12]}…，"
                f"远端 {remote_sha[:12]}…）")
        return True

    def prefetch(self, servers: list) -> list:
        """并行预传（失败不中断，滚动阶段自动重试），与静态策略同构"""
        try:
            self.check_local()
        except Exception as e:
            log(f"jar 预传输跳过: {e}")
            return [s["ip"] for s in servers]
        import deploy as deploy_mod
        from concurrent.futures import ThreadPoolExecutor
        log(f">>> 并行预上传 jar 到 {len(servers)} 台服务器")

        def _one(srv: dict) -> tuple:
            host = srv["ip"]
            server = deploy_mod.RemoteServer(host, merge_server_ssh(self.d.ssh_cfg, srv))
            try:
                self.transfer(server)
                log(f"[{host}] jar 预传输完成")
                return host, True
            except Exception as e:
                log(f"[{host}] jar 预传输失败: {e}")
                return host, False
            finally:
                server.close()

        with ThreadPoolExecutor(max_workers=max(1, min(len(servers), 8))) as pool:
            results = list(pool.map(_one, servers))
        failures = [host for host, ok in results if not ok]
        log(f"jar 预传输完成：成功 {len(servers) - len(failures)} 台" +
            (f"，失败 {len(failures)} 台: {failures}（滚动阶段自动重试）" if failures else "，全部就绪"))
        return failures

    # ---------- 阶段 4/5：切换与就绪验证 ----------

    def switch(self, server):
        """停机 → 原子换链接 → 启动 → 写版本标记

        换链接开始即视为目标状态已变更（编排层据此在后续失败时回滚）；
        链接用「临时名 + mv -Tf」替换，避免 ln -sfn 在目录已存在时的嵌套行为。
        """
        self.d.target_modified = True
        ver = self.d.version
        target = f"{self.jars_dir}/{ver}"
        tmp = f"{self.app_home}/.CURRENT.tmp"
        server.exec(f"systemctl stop {shlex.quote(self.unit)}.service || true",
                    timeout=self.cfg["stop_timeout"] + 30)
        server.exec(
            f"ln -sfn {shlex.quote(target)} {shlex.quote(tmp)} && "
            f"mv -Tf {shlex.quote(tmp)} {shlex.quote(self.curr_link)}",
            timeout=60)
        server.exec(f"systemctl start {shlex.quote(self.unit)}.service",
                    timeout=self.cfg["start_timeout"] + 30)
        server.exec(
            f"echo {shlex.quote(ver)} > {shlex.quote(self.app_home + '/.deploy_version')} && "
            f"chmod 0644 {shlex.quote(self.app_home + '/.deploy_version')}",
            timeout=15)
        log(f"[{server.host}] 已切换 CURRENT -> releases/{ver} 并启动 {self.unit}.service")

    def verify(self, server, server_entry: dict = None):
        """进程存活 + 就绪探测；失败先抓 journal 尾部再抛错，避免只剩一句「超时」"""
        try:
            server.exec(f"systemctl is-active --quiet {shlex.quote(self.unit)}.service",
                        timeout=15)
        except Exception as e:
            self._dump_journal(server)
            raise RuntimeError(
                f"[{server.host}] 单元 {self.unit}.service 未处于 active 状态") from e
        rt = self.cfg["readiness"]
        if rt["type"] == "process":
            return
        deadline = time.time() + rt["timeout"]
        if rt["type"] == "http":
            from health_check import check_http
            url = f"http://{server.host}:{self.cfg['port']}{rt['path']}"
            while time.time() < deadline:
                if check_http(url, require_health_body=True):
                    log(f"[{server.host}] 健康检查通过: {url}")
                    return
                time.sleep(rt["interval"])
            self._dump_journal(server)
            raise RuntimeError(f"[{server.host}] 健康检查超时: {url}")
        from health_check import check_tcp_port, check_tcp_port_via_ssh
        while time.time() < deadline:
            # 与容器发布同口径：优先经 SSH 在目标机内部探 127.0.0.1（业务端口常不对公网
            # 放行，平台机直连会假超时）；探测机制不可用（None）时才回退直连
            verdict = check_tcp_port_via_ssh(
                server.host, self.cfg["port"],
                getattr(server, "ssh_cfg", {}) or {})
            if verdict is True or (verdict is None and
                                   check_tcp_port(server.host, self.cfg["port"])):
                log(f"[{server.host}] TCP {self.cfg['port']} 已就绪")
                return
            time.sleep(rt["interval"])
        self._dump_journal(server)
        raise RuntimeError(
            f"[{server.host}] TCP 端口 {self.cfg['port']} 在 {rt['timeout']}s 内未就绪")

    def _dump_journal(self, server, lines: int = 80):
        """失败现场取证：journal 尾部进任务日志，便于直接定位启动异常"""
        try:
            out = server.exec(
                f"journalctl -u {shlex.quote(self.unit)}.service -n {lines} --no-pager "
                f"2>/dev/null || true", timeout=30)
            if out.strip():
                log(f"[{server.host}] {self.unit}.service 最近日志:\n{out.rstrip()}")
        except Exception as e:
            log(f"[{server.host}] 读取 journal 失败: {e}")

    # ---------- 阶段 5：回滚 ----------

    def rollback(self, server):
        """链接指回 previous_version 并重启；本机没有该版本目录时告警跳过

        产物按版本不可变保存，回滚只需换链接 + 重启，无需重传；目标版本目录缺失
        说明该主机从未发过该版本（或产物被清理），此处保持现状由编排层/人工介入。
        """
        ver = self.d.previous_version
        if not ver:
            log(f"[{server.host}] 未指定回滚版本（--previous-version），跳过回滚，服务保持现状")
            return
        target = f"{self.jars_dir}/{ver}"
        if not server.try_exec(f"test -d {shlex.quote(target)}", timeout=15):
            log(f"[{server.host}] 无回滚源（{target} 不存在），跳过回滚")
            return
        tmp = f"{self.app_home}/.CURRENT.tmp"
        self.d.target_modified = True
        server.exec(f"systemctl stop {shlex.quote(self.unit)}.service || true",
                    timeout=self.cfg["stop_timeout"] + 30)
        server.exec(
            f"ln -sfn {shlex.quote(target)} {shlex.quote(tmp)} && "
            f"mv -Tf {shlex.quote(tmp)} {shlex.quote(self.curr_link)}",
            timeout=60)
        server.exec(f"systemctl restart {shlex.quote(self.unit)}.service",
                    timeout=self.cfg["start_timeout"] + 30)
        server.exec(
            f"echo {shlex.quote(ver)} > {shlex.quote(self.app_home + '/.deploy_version')}",
            timeout=15)
        log(f"[{server.host}] 已回滚 CURRENT -> releases/{ver}")

    def rollback_source_missing(self, server) -> bool:
        """显式回滚前置检查：本机缺少 previous_version 产物目录即判定该主机回滚失败"""
        ver = self.d.previous_version
        if not ver:
            return False  # 无目标版本属于「跳过回滚」，由 rollback 自己说明
        return not server.try_exec(
            f"test -d {shlex.quote(self.jars_dir + '/' + ver)}", timeout=15)
