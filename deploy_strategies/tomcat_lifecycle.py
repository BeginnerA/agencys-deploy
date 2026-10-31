"""WAR 发布/恢复/回滚/重启：所有入口都返回结构化结果（逐主机 HostResult）。

严格生命周期：摘流 → 确认停止 → 切换 → 启动 → 就绪探活 → 回流；
- stop 必须经 stop_and_confirm 确认，start 后必须 is-active，就绪一律重新探活
  （_snapshot 的 healthy 只是基线占位，不能当作已验证）；
- 任何一步失败都按当前阶段映射为 HostResult，绝不把失败吞成成功；
- 改变受管状态的远端动作全部经 guarded 在 gate 内核对令牌后执行，令牌只走
  stdin，不存在绕过令牌的裸命令；
- 摘流失败或停止未确认时状态未知，保留实例占用（lock_state="held"）交人工
  核验，绝不在状态未知时回流流量或宣称成功。
"""
import hashlib
import secrets
import time
from dataclasses import asdict, dataclass

from . import tomcat_instance
from .remote_operation import (LOCK_ROOT, acquire, commit_staged, guarded,
                               release, set_phase, shq, stop_and_confirm)
from .tomcat_config import health_request_path
from .tomcat_health import probe_http

# 单次探活的超时上限：与 probe_http 的默认/内部上限一致；
# 实际取 min(上限, 剩余截止时间)，保证任何一次探活都不会越过总截止时间
# （spec §5431：总等待与单请求超时共同受截止时间约束）
_PROBE_TIMEOUT_CAP = 5.0


@dataclass
class HostResult:
    ok: bool
    operation_status: str
    phase: str
    current_version: str = ""
    recovery_target: str = ""
    process_state: str = "unknown"
    app_health: str = "unknown"
    recovery_status: str = "not_needed"
    traffic_status: str = "unknown"
    lock_state: str = "released"
    error: str = ""

    def to_dict(self):
        return asdict(self)


class WarTomcatLifecycle:
    def __init__(self, deployer, cfg, service, unit, instance_id=None):
        self.d = deployer
        self.cfg = cfg
        self.service = service
        self.unit = unit
        # instance_id(server) 由外部注入（WAR 从归属文件解析，测试可替换）
        self._instance_id = instance_id or (lambda server: "")
        self._token = None
        self._last_current = ""
        self.phase = ""

    def instance_id(self, server):
        return self._instance_id(server)

    def _phase(self, server, instance_id, token, name):
        """阶段只用于观测：写失败不能打断真实发布，监控显示旧阶段也胜过无阶段。"""
        self.phase = name
        try:
            set_phase(server, instance_id, token, name)
        except Exception:
            pass

    def _state(self, server):
        return {
            "owner": f"{LOCK_ROOT}/{self.instance_id(server)}/owner.json",
            "app_home": self.cfg["app_home"],
        }

    def _snapshot(self, server):
        app = self.cfg["app_home"]
        text = server.exec(
            f"echo @@LINK@@; readlink -f {shq(app + '/CURRENT')} 2>/dev/null || true; "
            f"echo @@SHA@@; sha256sum {shq(app + '/CURRENT/app.war')} 2>/dev/null || true",
            timeout=15)
        link = text.split("@@LINK@@", 1)[-1].split("@@SHA@@", 1)[0].strip()
        sha = text.split("@@SHA@@", 1)[-1].strip().split(" ", 1)[0]
        version = link.rsplit("/", 1)[-1] if link else ""
        # healthy 恒为 False：这里只做基线记录，健康与否必须重新 probe
        return {"version": version, "sha": sha, "healthy": False}

    def _read_manifest(self, server):
        return tomcat_instance.read_manifest(server, self.cfg["app_home"])

    def _read_maintenance(self, server):
        return tomcat_instance.read_maintenance(server, self.cfg["app_home"])

    def _clear_maintenance(self, server, instance_id, token):
        tomcat_instance.clear_maintenance(server, self.cfg["app_home"],
                                          instance_id, token)

    def _restore_traffic(self, server, server_entry, snapshot):
        """结束维护时按停机前快照恢复原权重；原停用流量不强制开启。"""
        if not getattr(self.d, "nacos", None) or not getattr(
                self.d.nacos, "configured", False):
            return {"ok": True, "status": "not_configured"}
        if snapshot and hasattr(self.d, "restore_traffic"):
            try:
                self.d.restore_traffic(self.d._nacos_ip(server_entry), snapshot)
                return {"ok": True, "status": "restored"}
            except Exception as e:
                return {"ok": False, "status": "failed", "error": str(e)}
        return self._traffic(server, "online", server_entry=server_entry)

    def _preflight(self, server, allow_maintenance=False):
        """归属、维护状态与固定配置一致性：不一致时必须在停机前拒绝"""
        manifest = self._read_manifest(server)
        if not manifest:
            raise RuntimeError("实例尚未由管理员接入，拒绝发布")
        if manifest.get("instance_id") != self.instance_id(server):
            raise RuntimeError("实例归属标识不一致，拒绝发布")
        if ((manifest.get("maintenance") or {}).get("active")
                and not allow_maintenance):
            raise RuntimeError("实例处于维护状态，需先完成配置更新并结束维护")
        plan = tomcat_instance.preview(
            server, self.cfg, manifest.get("service") or self.service,
            manifest.get("project_id"), manifest.get("env"), self.unit,
            manifest["instance_id"])
        if plan["digest"] != manifest.get("config_digest"):
            raise RuntimeError("实例固定配置已漂移，请重新预览并更新配置")
        drift = tomcat_instance.verify_instance(
            server, self.cfg["app_home"], self.unit, manifest)
        if drift:
            raise RuntimeError("实例受管文件或 unit 已漂移: " + ", ".join(drift))
        return manifest

    def _release_exists(self, server, version):
        try:
            server.exec(f"test -f {shq(self.cfg['app_home'] + '/releases/' + version + '/app.war')}",
                        timeout=15)
            return True
        except Exception:
            # exec 对 rc!=0 抛异常：源缺失与通信故障都按缺失处理（fail-closed）
            return False

    def _stage(self, server, version, operation_id):
        """上传 → 远端校验 → gate 内提交；必须由调用方持有实例锁。

        摘要在这里对本地制品实时计算（deployer.strategy 由 Task 9 注入）；
        incoming 路径是平台派生的 .incoming/<operation_id>，不碰受管状态，
        真正提交进 releases/ 由 commit_staged 在 gate 内完成。
        """
        sha = _artifact_sha(self.d.strategy, version)
        app = self.cfg["app_home"]
        incoming = f"{app}/.incoming/{operation_id}"
        server.exec(f"mkdir -p {shq(incoming)}", timeout=15)
        local = self.d.strategy.local_artifact(version)
        server.sftp_put_with_progress(str(local), f"{incoming}/app.war",
                                      label=f"{self.service}-{version}.war")
        server.exec(f"echo {shq(sha + '  ' + incoming + '/app.war')} | sha256sum -c -",
                    timeout=60)
        return commit_staged(server, self.instance_id(server), self._token,
                             version, incoming, sha)

    def _stop(self, server, instance_id, token):
        stop_and_confirm(server, instance_id, token, self.unit)

    def _activate(self, server, instance_id, token, version):
        app = self.cfg["app_home"]
        command = (f"ln -sfn {shq(app + '/releases/' + version)} {shq(app + '/.CURRENT.tmp')} "
                   f"&& mv -Tf {shq(app + '/.CURRENT.tmp')} {shq(app + '/CURRENT')} && "
                   f"printf '%s\\n' {shq(version)} > {shq(app + '/.deploy_version')} && "
                   f"chmod 0644 {shq(app + '/.deploy_version')}")
        guarded(server, instance_id, token, command, timeout=30)

    def _clean(self, server, instance_id, token):
        app = self.cfg["app_home"]
        targets = [app + "/instance/webapps", app + "/instance/work",
                   app + "/instance/temp"]
        command = "; ".join(
            f"find {shq(p)} -mindepth 1 -maxdepth 1 -exec rm -rf -- {{}} +"
            for p in targets)
        guarded(server, instance_id, token, command, timeout=60)

    def _start(self, server, instance_id, token):
        guarded(server, instance_id, token,
                f"systemctl start {shq(self.unit)}.service && "
                f"systemctl is-active {shq(self.unit)}.service", timeout=60)

    def _probe(self, server):
        """有界就绪轮询：deadline 到点即停，且单次探活超时随剩余截止时间收缩。

        与 poll_http 同形的结果契约（ok/ms/status/error + attempts），但每次
        probe_http 的 timeout 取 min(上限, 剩余截止时间)，任何一次探活都不会
        越过总截止时间（Task 5 评审裁定）；tomcat_health 属 Task 5 文件不便
        改动，故在此落地。
        """
        r = self.cfg["readiness"]
        deadline = time.monotonic() + max(0, r["timeout"])
        attempts = 0
        last = {"ok": False, "ms": 0, "status": None, "error": "未执行"}
        while True:
            remaining = deadline - time.monotonic()
            attempts += 1
            last = probe_http(server, self.cfg["port"], health_request_path(self.cfg),
                              expected_status=r["expected_status"],
                              body_contains=r["body_contains"],
                              timeout=max(0.05, min(_PROBE_TIMEOUT_CAP, remaining)))
            if last["ok"] or time.monotonic() >= deadline:
                break
            time.sleep(max(0.1, min(r["interval"], deadline - time.monotonic())))
        last["attempts"] = attempts
        return last

    def _probe_or_raise(self, server):
        """探活结果检查：轮询失败不抛异常，必须在这里折算为失败（fail-closed）。"""
        pr = self._probe(server)
        if not pr.get("ok"):
            raise RuntimeError("健康检查未通过: "
                               + str(pr.get("error") or pr.get("status")))
        return pr

    def _traffic(self, server, action, server_entry=None):
        if not getattr(self.d, "nacos", None) or not getattr(self.d.nacos, "configured", False):
            return {"ok": True, "status": "not_configured"}
        try:
            if action == "offline":
                self.d.nacos_offline(self.d._nacos_ip(server_entry))
            else:
                self.d.nacos_online(self.d._nacos_ip(server_entry), weight=1.0)
            return {"ok": True, "status": action}
        except Exception as e:
            return {"ok": False, "status": "failed", "error": str(e)}

    def _acquire(self, server, instance_id):
        token = secrets.token_hex(32)
        acquire(server, instance_id, token, "war_tomcat", "preflight")
        self._token = token
        return token

    def _release(self, server, instance_id, token, result):
        release(server, instance_id, token, result.to_dict())

    def _finish(self, server, instance_id, token, result):
        """落 last-result 并移除占用，随后返回结构化结果。

        占用协议保证释放失败时 owner 保留（fail-closed）；发布/回滚/重启本体
        已到达终态后，记账失败不得被当成操作失败去触发进一步恢复，也不得让
        异常吞掉结论——把失败如实写进 lock_state 与 error 随结果上交。
        """
        try:
            self._release(server, instance_id, token, result)
        except Exception as rel_error:
            result.lock_state = "held"
            result.error = ((result.error + "; ") if result.error else "") \
                + f"释放占用失败: {rel_error}"
        return result

    def deploy(self, server, server_entry=None):
        instance_id = self.instance_id(server)
        token = self._acquire(server, instance_id)
        phase = "preflight"
        modified = False
        target = getattr(self.d, "version", "")
        previous = ""
        self._phase(server, instance_id, token, "preflight")
        try:
            snap = self._snapshot(server)
            previous = snap["version"]
            self._last_current = previous
            self._preflight(server)
            self._stage(server, target, secrets.token_hex(8))
            phase = "traffic"
            self._phase(server, instance_id, token, "traffic")
            off = self._traffic(server, "offline", server_entry=server_entry)
            if not off["ok"]:
                # 摘流结果未知时不得停机；外部请求可能仍在执行，保留占用等人工核验
                return HostResult(False, "failed", phase, previous, previous,
                                  "running", "unknown", "not_needed", "unknown",
                                  "held", f"摘流失败: {off.get('error', '')}")
            phase = "stopping"
            self._phase(server, instance_id, token, "stopping")
            try:
                self._stop(server, instance_id, token)
            except Exception as stop_error:
                return self._stop_unknown(server, instance_id, token, target,
                                          previous, phase, stop_error,
                                          server_entry)
            modified = True
            phase = "switching"
            self._phase(server, instance_id, token, "switching")
            self._activate(server, instance_id, token, target)
            self._clean(server, instance_id, token)
            phase = "starting"
            self._phase(server, instance_id, token, "starting")
            self._start(server, instance_id, token)
            phase = "health"
            self._phase(server, instance_id, token, "health")
            self._probe_or_raise(server)
            tr = self._traffic(server, "online", server_entry=server_entry)
            result = HostResult(True, "published", "done", target, previous,
                                "running", "healthy", "not_needed",
                                "restored" if tr["ok"] else "failed")
            if not tr["ok"]:
                result.ok = False
                result.operation_status = "traffic_failed"
                result.error = tr.get("error", "")
            return self._finish(server, instance_id, token, result)
        except Exception as e:
            if not modified:
                # 停机前失败：CURRENT 与运行实例未动，不触发恢复，也不改流量
                result = HostResult(False, "failed", phase, previous, previous,
                                    "running", "unknown", "not_needed",
                                    "unknown", "released", str(e))
                return self._finish(server, instance_id, token, result)
            return self._recover(server, instance_id, token, target, previous,
                                 phase, e, server_entry)

    def _stop_unknown(self, server, instance_id, token, target, previous,
                      phase, error, server_entry):
        """stop 未确认：只有旧版重新探活成功才能恢复流量并释放；否则保留 owner。

        CURRENT 在确认停止前从未切换，现网仍是 previous，结果按 previous 如实
        回报（target 尚未生效，不能写成 current_version）。
        """
        try:
            pr = self._probe(server)
        except Exception:
            pr = {"ok": False, "error": "探活异常"}
        if not pr.get("ok"):
            return HostResult(False, "unknown", phase, previous, previous,
                              "unknown", "unknown", "unknown", "unknown",
                              "held", f"{error}；旧版本状态未确认")
        tr = self._traffic(server, "online", server_entry=server_entry)
        result = HostResult(False, "failed", phase, previous, previous,
                            "running", "healthy", "not_needed",
                            "restored" if tr["ok"] else "failed",
                            "released", str(error))
        return self._finish(server, instance_id, token, result)

    def _recover(self, server, instance_id, token, target, previous, phase,
                 error, server_entry):
        self._phase(server, instance_id, token, "recovery")
        try:
            self._stop(server, instance_id, token)
        except Exception as stop_error:
            return HostResult(False, "unknown", phase, target, previous,
                              "unknown", "unknown", "unknown", "unknown",
                              "held", f"{error}; 停止失败: {stop_error}")
        if not previous:
            self._traffic(server, "offline", server_entry=server_entry)
            result = HostResult(False, "failed", phase, target, "", "stopped",
                                "unavailable", "unavailable", "offline",
                                "released", str(error))
            return self._finish(server, instance_id, token, result)
        if not self._release_exists(server, previous):
            self._traffic(server, "offline", server_entry=server_entry)
            result = HostResult(False, "failed", "recovery", previous, previous,
                                "stopped", "unavailable", "blocked", "offline",
                                "released", f"{error}；恢复源缺失")
            return self._finish(server, instance_id, token, result)
        try:
            self._traffic(server, "offline", server_entry=server_entry)
            self._phase(server, instance_id, token, "switching")
            self._activate(server, instance_id, token, previous)
            self._clean(server, instance_id, token)
            self._phase(server, instance_id, token, "starting")
            self._start(server, instance_id, token)
            self._phase(server, instance_id, token, "health")
            self._probe_or_raise(server)
            tr = self._traffic(server, "online", server_entry=server_entry)
            result = HostResult(False, "failed", "recovered", previous, previous,
                                "running", "healthy", "succeeded",
                                "restored" if tr["ok"] else "failed",
                                "released", str(error))
        except Exception as recovery_error:
            try:
                self._stop(server, instance_id, token)
                result = HostResult(False, "failed", "recovery", previous,
                                    previous, "stopped", "unhealthy", "failed",
                                    "offline", "released",
                                    f"{error}; 恢复失败: {recovery_error}")
            except Exception as stop_error:
                result = HostResult(False, "unknown", "recovery", previous,
                                    previous, "unknown", "unknown", "unknown",
                                    "unknown", "held",
                                    f"{error}; 恢复失败: {recovery_error}; "
                                    f"停止失败: {stop_error}")
                return result
        return self._finish(server, instance_id, token, result)

    def rollback(self, server, target_version, server_entry=None, explicit=True):
        instance_id = self.instance_id(server)
        token = self._acquire(server, instance_id)
        self._phase(server, instance_id, token, "rollback_preflight")
        try:
            snap = self._snapshot(server)
            self._preflight(server)
        except Exception as e:
            return HostResult(False, "unknown", "rollback_preflight", "",
                              target_version, "unknown", "unknown", "unknown",
                              "unknown", "held", f"预检失败: {e}")
        if not self._release_exists(server, target_version):
            result = HostResult(False, "failed", "rollback_preflight",
                                snap["version"], target_version, "running",
                                "unknown", "blocked", "unknown", "released",
                                "回滚源缺失或校验失败")
            return self._finish(server, instance_id, token, result)
        self._phase(server, instance_id, token, "traffic")
        off = self._traffic(server, "offline", server_entry=server_entry)
        if not off["ok"]:
            return HostResult(False, "failed", "traffic", snap["version"],
                              target_version, "running", "unknown",
                              "blocked", "unknown", "held",
                              f"摘流失败: {off.get('error', '')}")
        self._phase(server, instance_id, token, "rollback_stop")
        try:
            self._stop(server, instance_id, token)
        except Exception as stop_error:
            return HostResult(False, "unknown", "rollback_stop",
                              snap["version"], target_version, "unknown",
                              "unknown", "unknown", "unknown", "held",
                              f"停止失败: {stop_error}")
        try:
            self._phase(server, instance_id, token, "switching")
            self._activate(server, instance_id, token, target_version)
            self._clean(server, instance_id, token)
            self._phase(server, instance_id, token, "starting")
            self._start(server, instance_id, token)
            self._phase(server, instance_id, token, "health")
            self._probe_or_raise(server)
            tr = self._traffic(server, "online", server_entry=server_entry)
            result = HostResult(True, "rolled_back", "done",
                                target_version, snap["version"], "running",
                                "healthy", "not_needed",
                                "restored" if tr["ok"] else "failed")
            return self._finish(server, instance_id, token, result)
        except Exception as e:
            return self._rollback_failed(server, instance_id, token,
                                         target_version, e)

    def _rollback_failed(self, server, instance_id, token, target_version,
                         error):
        """显式回滚自身失败：停止不健康实例并如实报告，不反向恢复另一个版本"""
        try:
            self._stop(server, instance_id, token)
        except Exception as stop_error:
            return HostResult(False, "unknown", "rollback", "", target_version,
                              "unknown", "unknown", "failed", "unknown", "held",
                              f"{error}；停止失败: {stop_error}")
        snap = self._snapshot(server)
        result = HostResult(False, "failed", "rollback", snap["version"],
                            target_version, "stopped", "unhealthy", "failed",
                            "offline", "released", str(error))
        return self._finish(server, instance_id, token, result)

    def restart(self, server, server_entry=None, release_maintenance=False):
        instance_id = self.instance_id(server)
        manifest = self._read_manifest(server)
        maintenance = manifest.get("maintenance") or {}
        if maintenance.get("active") and not release_maintenance:
            raise RuntimeError("实例处于维护状态，需明确结束维护后再启动")
        if release_maintenance:
            maintenance = self._read_maintenance(server) or maintenance
        snapshot = ((maintenance.get("traffic_snapshot") or {})
                    if release_maintenance else {})
        token = self._acquire(server, instance_id)
        current = ""
        self._phase(server, instance_id, token, "restart_preflight")
        try:
            current = self._snapshot(server)["version"]
            self._preflight(server, allow_maintenance=release_maintenance)
        except Exception as e:
            return HostResult(False, "unknown", "restart_preflight", current,
                              "", "unknown", "unknown", "not_needed", "unknown",
                              "held", f"预检失败: {e}")
        self._phase(server, instance_id, token, "traffic")
        off = self._traffic(server, "offline", server_entry=server_entry)
        if not off["ok"]:
            return HostResult(False, "failed", "traffic", current, "",
                              "running", "unknown", "not_needed", "unknown",
                              "held", f"摘流失败: {off.get('error', '')}")
        self._phase(server, instance_id, token, "restart_stop")
        try:
            self._stop(server, instance_id, token)
        except Exception as stop_error:
            return HostResult(False, "unknown", "restart_stop", current, "",
                              "unknown", "unknown", "not_needed", "unknown",
                              "held", f"停止失败: {stop_error}")
        try:
            self._phase(server, instance_id, token, "starting")
            self._start(server, instance_id, token)
            self._phase(server, instance_id, token, "health")
            self._probe_or_raise(server)
            if release_maintenance:
                tr = self._restore_traffic(server, server_entry, snapshot)
                if tr["ok"]:
                    try:
                        self._clear_maintenance(server, instance_id, token)
                    except Exception as clear_error:
                        result = HostResult(
                            False, "restarted", "done", current, "",
                            "running", "healthy", "not_needed", "restored",
                            "released", f"维护标志清除失败: {clear_error}")
                        return self._finish(server, instance_id, token, result)
            else:
                tr = self._traffic(server, "online", server_entry=server_entry)
            result = HostResult(True, "restarted", "done", current, "",
                                "running", "healthy", "not_needed",
                                "restored" if tr["ok"] else "failed")
            if not tr["ok"]:
                result.ok = False
                result.operation_status = "traffic_failed"
                result.error = tr.get("error", "")
            return self._finish(server, instance_id, token, result)
        except Exception as e:
            try:
                self._stop(server, instance_id, token)
            except Exception as stop_error:
                return HostResult(False, "unknown", "restart", current, "",
                                  "unknown", "unknown", "not_needed",
                                  "unknown", "held",
                                  f"{e}；停止失败: {stop_error}")
            result = HostResult(False, "failed", "restart", current, "",
                                "stopped", "unhealthy", "not_needed",
                                "offline", "released", str(e))
            return self._finish(server, instance_id, token, result)


def _artifact_sha(strategy, version):
    local = strategy.local_artifact(version)
    h = hashlib.sha256()
    with open(local, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
