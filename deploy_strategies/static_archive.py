#!/usr/bin/env python3
"""静态压缩包发布策略（deploy_mode: static_archive）

面向 1Panel 托管的纯 nginx 静态站点：产物 {zip_prefix}-{version}.zip 经 SFTP 上传 →
远端解压到站点父目录的暂存区 → 同父目录内两次 rename 完成原子换入（空窗毫秒级）→
新包落 backup/ 目录作历代回滚源。无容器、无 Nacos、无 docker 命令。

切换语义（与 1Panel 手动「解压覆盖」对齐但不留新旧文件混杂）：
  R = site_root（如 /data/1panel/www/sites/{domain}/index）
  P = dirname(R)
  1. unzip → P/.deploy_staging_{project}/
  2. mv R  P/.site_prev_{project}   （同盘 rename，瞬时）
  3. mv 暂存产物目录 R               （同盘 rename，瞬时恢复）
回滚优先整目录还原 .site_prev（最快最准），其次从 backup 取历代 zip 重解压。

版本号是日期号（YYYYMMDD，当日重复发布追加 -N 后缀避免覆盖 backup 同名包），
非 semver；线上版本标记文件 {site_root}/.deploy_version 每次换入时写入。
"""
import os
import re
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from .base import DeployStrategy, register
from common import (log, as_number, load_yaml, default_servers_file,
                    merge_server_ssh, get_ssh_config)

# 日期版本号：YYYYMMDD 或 YYYYMMDD-N（当日二次/多次发布）
_DATE_VER_RE = re.compile(r"^(\d{8})(?:-(\d+))?$")


@register
class StaticArchiveStrategy(DeployStrategy):
    name = "static_archive"
    supports_nacos = False
    supports_canary = True
    supports_prefetch = True    # zip 上传可与其它服务器并行
    supports_restart = False    # 无容器可重启
    version_scheme = "date"

    def __init__(self, deployer):
        super().__init__(deployer)
        proj = getattr(deployer, "proj_cfg", None) or {}
        self.site_root = (proj.get("site_root") or "").rstrip("/")
        if not self.site_root:
            raise ValueError(
                f"deploy_mode=static_archive 必须配置 site_root"
                f"（1Panel 站点根，如 /data/1panel/www/sites/example.com/index）")
        self.parent = str(Path(self.site_root).parent.as_posix())
        self.backup_dir = (proj.get("backup_dir") or f"{self.parent}/backup").rstrip("/")
        self.entry_file = proj.get("entry_file") or "index.html"
        self.file_mode = proj.get("file_mode") or "0644"
        self.zip_prefix = proj.get("zip_prefix") or getattr(deployer, "project", "site")
        self.tmp_dir = (getattr(deployer, "remote_tmp_dir", "/tmp/platform-deploy")
                        or "/tmp/platform-deploy").rstrip("/")
        project = getattr(deployer, "project", "site")
        self._staging = f"{self.parent}/.deploy_staging_{project}"
        self._prev = f"{self.parent}/.site_prev_{project}"

    # ---------- 产物命名 ----------

    def zip_name(self, version: str) -> str:
        return f"{self.zip_prefix}-{version}.zip"

    # ---------- 阶段 0：本地预检与版本推导 ----------

    def check_local(self):
        dist_dir = getattr(self.d, "dist_dir", None)
        if not dist_dir:
            raise RuntimeError("未配置 deploy.dist_dir，无法定位本地静态包")
        local_zip = Path(dist_dir) / self.zip_name(self.d.version)
        if not local_zip.exists():
            raise RuntimeError(
                f"本地静态包不存在: {local_zip}（执行构建自动生成，或将 zip 手工放入 dist 目录）")

    def query_current_version(self):
        """SSH 首台服务器读取 {site_root}/.deploy_version；无标记（平台接入前的手工发布）
        则回退解析 backup/ 目录最新历代 zip 文件名中的日期号；均无返回 None（首次发布）"""
        import deploy as deploy_mod
        servers = load_yaml(Path(default_servers_file(env=self.d.env))).get("servers", [])
        if not servers:
            raise RuntimeError("服务器清单为空，无法查询当前版本")
        first = servers[0]
        ssh_cfg = merge_server_ssh(get_ssh_config(self.d.config), first)
        server = deploy_mod.RemoteServer(first["ip"], ssh_cfg)
        try:
            try:
                out = server.exec(f"cat {self.site_root}/.deploy_version 2>/dev/null",
                                  timeout=15).strip()
                if _DATE_VER_RE.match(out):
                    return out
            except Exception:
                pass
            # 兼容平台接入前的手工发布：backup 目录最新 {prefix}-{8位日期}.zip
            pattern = re.escape(self.zip_prefix) + r"-[0-9]{8}(?:-[0-9]+)?\.zip"
            out = server.exec(
                f"ls -1t {self.backup_dir} 2>/dev/null | grep -Em 1 '{pattern}' || true",
                timeout=15)
            m = re.search(r"(\d{8}(?:-\d+)?)", out or "")
            return m.group(1) if m else None
        finally:
            server.close()

    def next_version(self, current: str) -> str:
        """日期号推导：今天 > 线上版本 → 今天；当日重复发布 → 今天-N（避免覆盖 backup 同名包）"""
        today = time.strftime("%Y%m%d")
        if not current:
            return today
        m = _DATE_VER_RE.match(current)
        if not m:
            raise ValueError(f"线上版本号格式非法（应为 YYYYMMDD 或 YYYYMMDD-N）: {current}")
        if m.group(1) < today:
            return today
        # 线上已是今天（含 -N 后缀）：后缀 +1；无后缀则从 -2 起
        seq = int(m.group(2)) if m.group(2) else 1
        return f"{today}-{seq + 1}"

    # ---------- 五阶段 ----------

    def check(self, server):
        """预检（不改动任何站点数据）：unzip 可用、站点根已存在、backup/tmp 目录就绪"""
        server.exec("command -v unzip >/dev/null", timeout=15)
        server.exec(f"test -d {self.site_root}", timeout=15)
        server.exec(f"mkdir -p {self.backup_dir} {self.tmp_dir}", timeout=15)

    def prepare(self, server):
        # 静态模式无登录类前置动作；目录准备工作在 check/transfer 内幂等完成
        return

    def transfer(self, server, version: str = None):
        """上传 zip 到远端 tmp 目录（幂等：远端已存在同尺寸文件则跳过）"""
        ver = version or self.d.version
        zip_name = self.zip_name(ver)
        dist_dir = getattr(self.d, "dist_dir", None)
        if not dist_dir:
            raise RuntimeError("未配置 deploy.dist_dir，无法定位本地静态包")
        local_zip = Path(dist_dir) / zip_name
        if not local_zip.exists():
            raise RuntimeError(
                f"本地静态包不存在: {local_zip}（执行构建自动生成，或将 zip 手工放入 dist 目录）")
        remote = f"{self.tmp_dir}/{zip_name}"
        size = local_zip.stat().st_size
        if server.try_exec(f"test {remote} -size {size}c", timeout=15):
            log(f"[{server.host}] 远端已有同尺寸静态包，跳过上传: {zip_name}")
            return True
        log(f"[{server.host}] 上传静态包 {zip_name}（{size / 1024 / 1024:.1f} MB）-> {remote}")
        server.sftp_put_with_progress(str(local_zip), remote, label=zip_name,
                                      streams=getattr(self.d, "upload_streams", 1))
        return True

    def prefetch(self, servers: list) -> list:
        """并行预上传 zip 到全部服务器（本地产物缺失先一次性报错→全部记失败，滚动阶段重试）"""
        try:
            self.check_local()
        except Exception as e:
            log(f"静态包预传输跳过: {e}")
            return [s["ip"] for s in servers]
        import deploy as deploy_mod
        log(f">>> 并行预上传静态包到 {len(servers)} 台服务器")

        def _one(srv: dict) -> tuple:
            host = srv["ip"]
            server = deploy_mod.RemoteServer(host, merge_server_ssh(self.d.ssh_cfg, srv))
            try:
                self.transfer(server)
                log(f"[{host}] 静态包预传输完成")
                return host, True
            except Exception as e:
                log(f"[{host}] 静态包预传输失败: {e}")
                return host, False
            finally:
                server.close()

        with ThreadPoolExecutor(max_workers=max(1, min(len(servers), 8))) as pool:
            results = list(pool.map(_one, servers))
        failures = [host for host, ok in results if not ok]
        log(f"静态包预传输完成：成功 {len(servers) - len(failures)} 台" +
            (f"，失败 {len(failures)} 台: {failures}（滚动阶段自动重试）" if failures else "，全部就绪"))
        return failures

    def _extract_and_locate(self, server, remote_zip: str) -> str:
        """解压 remote_zip 到站点父目录内暂存区（保证与站点根同盘可原子 rename），
        返回包含入口文件的产物目录绝对路径。

        定位必须取「最浅层」的入口文件目录：先判暂存区根目录（产物直接置于 zip 根的
        常规形态），命中即用；否则退化到恰好一层包裹目录（兼容 zip 多包一层）。不能直接
        find -maxdepth 2 -quit，因为 dist 内可能同时存在嵌套子应用的 index.html（如
        im_web/index.html），readdir 顺序下会误选深层目录而把子应用当成站点根换入。"""
        st = self._staging
        server.exec(f"rm -rf {st} && mkdir -p {st} && unzip -q -o {remote_zip} -d {st}",
                    timeout=300)
        entry = server.exec(
            f"if [ -f {st}/{self.entry_file} ]; then echo {st}; "
            f"else d=$(find {st} -mindepth 2 -maxdepth 2 -name {self.entry_file} -print -quit); "
            f"if [ -n \"$d\" ]; then dirname \"$d\"; else echo MISSING; fi; fi",
            timeout=60).strip()
        if not entry or entry == "MISSING":
            raise RuntimeError(
                f"[{server.host}] 解压后未找到入口文件 {self.entry_file}"
                f"（zip 内容结构不符，发布中止，站点未被改动）")
        return entry

    def switch(self, server):
        """原子换入新版本：解压校验 → 新包落 backup → 两次同盘 rename 切换 → 权限归一 + 版本标记"""
        # 语义约定：自此之后站点状态已被修改，失败时编排层必须尝试回滚
        self.d.container_modified = True
        ver = self.d.version
        remote_zip = f"{self.tmp_dir}/{self.zip_name(ver)}"
        entry = self._extract_and_locate(server, remote_zip)
        # 单条命令链：备份归档 + 换入（任一步失败即中断，.site_prev 保留供回滚）
        server.exec(
            f"mv -f {remote_zip} {self.backup_dir}/{self.zip_name(ver)} && "
            f"rm -rf {self._prev} && "
            f"mv {self.site_root} {self._prev} && "
            f"mv {entry} {self.site_root} && "
            f"chown -R root:root {self.site_root} && "
            f"find {self.site_root} -type d -exec chmod 0755 {{}} + && "
            f"find {self.site_root} -type f -exec chmod {self.file_mode} {{}} + && "
            f"echo {ver} > {self.site_root}/.deploy_version && "
            f"rm -rf {self._staging}",
            timeout=180)
        log(f"[{server.host}] 站点已切换至 {ver}（历代包: {self.backup_dir}）")

    def verify(self, server, server_entry: dict = None):
        """健康验证：配置了 health_url 走 HTTP 严格探测循环（静态无容器日志可查，
        不经 wait_for_healthy 的 docker logs 分支）；未配置则退化为远端标记文件校验"""
        proj = getattr(self.d, "proj_cfg", None) or {}
        health_url = (proj.get("health_url") or "").format(ip=server.host)
        if health_url:
            from health_check import check_http
            deploy_cfg = (self.d.config or {}).get("deploy", {})
            timeout = as_number(deploy_cfg.get("health_timeout"), 60)
            interval = as_number(deploy_cfg.get("health_interval"), 5)
            if timeout <= 0:
                timeout = 60
            if interval <= 0:
                interval = 5
            log(f"开始静态站点健康检查: {health_url}，超时 {timeout} 秒")
            deadline = time.time() + timeout
            while time.time() < deadline:
                # 静态首页 200 即健康（不看响应体），沿用 Actuator body 判定会误报成功换入为失败
                if check_http(health_url, require_health_body=False):
                    log(f"健康检查通过: {health_url}")
                    return
                time.sleep(interval)
            raise RuntimeError(f"健康检查超时: {health_url}")
        # 无 HTTP 检查端点：校验换入的版本标记与入口文件确已就位
        server.exec(
            f"grep -qx '{self.d.version}' {self.site_root}/.deploy_version && "
            f"test -f {self.site_root}/{self.entry_file}",
            timeout=15)
        log(f"[{server.host}] 静态发布验证通过（.deploy_version={self.d.version}）")

    def rollback(self, server):
        """回滚：优先整目录还原换入前快照 .site_prev（毫秒级、最准）；
        快照不存在则从 backup/ 重解压 previous_version 历代包；均无则告警跳过"""
        ver = self.d.previous_version
        if not ver:
            log(f"[{server.host}] 未指定回滚版本（--previous-version），跳过静态回滚，站点保持现状")
            return
        has_prev = server.exec(
            f"if [ -d {self._prev} ]; then echo yes; else echo no; fi",
            timeout=15).strip()
        if has_prev == "yes":
            # 快速路径：换入前的整目录快照仍在，破损版本移入暂存名后原位还原
            server.exec(
                f"rm -rf {self._staging} && "
                f"mv {self.site_root} {self._staging} && "
                f"mv {self._prev} {self.site_root}",
                timeout=60)
            log(f"[{server.host}] 站点已从换入前快照整目录还原到 {ver}")
            return
        backup_zip = f"{self.backup_dir}/{self.zip_name(ver)}"
        if not server.try_exec(f"test -f {backup_zip}", timeout=15):
            log(f"[{server.host}] 无回滚源（.site_prev 快照与 {backup_zip} 均不存在），跳过回滚")
            return
        entry = self._extract_and_locate(server, backup_zip)
        # 慢路径：历代包重解压换入（backup 内原包保留不删，可反复回滚）
        server.exec(
            f"rm -rf {self.site_root}.rbroken && "
            f"mv {self.site_root} {self.site_root}.rbroken && "
            f"mv {entry} {self.site_root} && "
            f"chown -R root:root {self.site_root} 2>/dev/null; "
            f"echo {ver} > {self.site_root}/.deploy_version && "
            f"rm -rf {self._staging} {self.site_root}.rbroken",
            timeout=180)
        log(f"[{server.host}] 站点已从历代备份包重解压回滚到 {ver}")
