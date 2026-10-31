"""远端实例锁与严格命令：HTTP 之外只做进程/文件操作。

锁协议（spec §6）：
- 每实例固定 inode 的 ``gate.lock``，只加锁不删除/重建；
- ``owner.json`` 是跨阶段的持久占用记录，占用时第二操作直接拒绝（LockBusy），无 TTL 抢占；
- 每个改变受管状态的远端动作都经 :func:`guarded` 在 gate 内先核对 owner 令牌再执行；
- 正常结束在 gate 内核对令牌、落 last-result、最后移除 owner。

令牌安全（spec §348）：RemoteServer.exec 每次调用都会 ``log`` 整条命令文本，
因此令牌绝不进入命令文本——guarded 脚本从 stdin 第一行读令牌
（``IFS= read -r __TOK || exit 71``），用固定串模式（``grep -F -q -f -``，
模式来自 stdin）核对 owner.json 含该令牌（exit 73）后才执行动作；
acquire/release 的落盘内容（owner JSON / last-result JSON）也一律经 stdin。
异常侧：包装异常用脱敏消息，并以 :func:`_sanitized_cause` 重建的脱敏异常作
``__cause__``（exec 报错内嵌命令与 stdout/stderr，原样链入会把机密带进异常链）。
"""
import hashlib
import json
import re
import shlex
import time

LOCK_ROOT = "/var/lib/smartops/tomcat"


class LockBusy(RuntimeError):
    """实例已被其它逻辑操作占用；绝不覆盖现有 owner。"""


class VersionConflict(RuntimeError):
    """目标版本已存在但内容不同；拒绝覆盖，要求换版本号或管理员处理。"""


class StopFailed(RuntimeError):
    """无法确认 unit 已停止（活跃、有待执行 job 或 cgroup 残留进程）。"""


def shq(value: str) -> str:
    return shlex.quote(str(value))


def lock_paths(instance_id: str) -> dict:
    d = f"{LOCK_ROOT}/{instance_id}"
    return {"dir": d, "gate": f"{d}/gate.lock", "owner": f"{d}/owner.json",
            "phase": f"{d}/owner.phase",
            "result": f"{d}/last-result.json", "revoked": f"{d}/owner.revoked.json"}


def _redact(text: str, *secrets: str) -> str:
    out = text
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return out


def _sanitized_cause(e: BaseException, *secrets: str) -> BaseException:
    """重建 ``e``：同类异常、参数脱敏；构造失败则退回 ``RuntimeError(脱敏消息)``。

    exec 报错内嵌完整命令与 stdout/stderr，原样作 ``__cause__`` 会沿异常链
    泄漏机密，故链入前必须重建（spec §348）。
    """
    try:
        clean = type(e)(*(_redact(a, *secrets) if isinstance(a, str) else a
                          for a in e.args))
    except Exception:
        clean = None
    if clean is None:
        try:
            clean = RuntimeError(_redact(str(e), *secrets))
        except Exception:
            clean = RuntimeError("***")
    return clean


def _error_stdout(text: str) -> str:
    """取出 deploy.exec 报错消息里的 stdout 段；无该段（如测试替身）则视为全文。"""
    if "stdout:" in text:
        return text.split("stdout:", 1)[1].split("stderr:", 1)[0].strip()
    return text.strip()


def guarded(server, instance_id: str, token: str, command: str,
            timeout: int = 120, *, stdin_rest: str = "") -> str:
    """gate 内核对令牌后执行 ``command``，返回 stdout。

    令牌只经 stdin 传递（``stdin_data`` 不进平台日志，远端 ps 也不可见）：
    脚本先 ``IFS= read -r __TOK || exit 71`` 读入令牌（stdin 缺失即失败），
    再 ``printf %s "$__TOK" | grep -F -q -f - "$OWNER"`` 以固定串模式核对
    owner.json 含该令牌（exit 73），之后才执行 ``command``。
    ``stdin_rest`` 追加在令牌行之后，供 ``command`` 内读取（如 release 经
    ``cat`` 落 last-result.json）。
    """
    p = lock_paths(instance_id)
    script = (f"IFS= read -r __TOK || exit 71; "
              f"OWNER={shq(p['owner'])}; "
              f'printf %s "$__TOK" | grep -F -q -f - "$OWNER" || exit 73; '
              f"{command}")
    cmd = f"flock -x {shq(p['gate'])} bash -c {shq(script)}"
    # 原样转发 exec 结果/异常：命令文本已不含令牌，且 commit_staged 需要从
    # exec 报错里解析现网 sha256sum 校验和行；消息级脱敏由各公开包装函数负责
    # （_redact + _sanitized_cause），不能在这里做——短令牌（如单字符）会把
    # 报错里的路径/校验和行一并打码，破坏失败判定。
    return server.exec(cmd, timeout=timeout, stdin_data=token + "\n" + stdin_rest)


def acquire(server, instance_id: str, token: str, operation: str, phase: str) -> None:
    """写入 owner 占用记录；owner 已存在时抛 LockBusy，绝不覆盖。

    owner.json 内容（含令牌）在 Python 构造并经 stdin 落盘，不进命令文本；
    占用检查先于任何写入（fail-closed：占用即单次 exec 拒绝，无副作用），
    ``umask 077`` 保证落盘文件 0600 权限。
    """
    p = lock_paths(instance_id)
    payload = json.dumps({"token": token, "operation": operation, "phase": phase,
                          "started_at": int(time.time())}, sort_keys=True)
    script = (f"test ! -e {shq(p['owner'])} || {{ echo LOCKED; exit 75; }}; "
              f"umask 077; "
              f"cat > {shq(p['owner'] + '.tmp')} && "
              f"mv -f {shq(p['owner'] + '.tmp')} {shq(p['owner'])} && "
              f"printf %s {shq(phase)} > {shq(p['phase'] + '.tmp')} && "
              f"mv -f {shq(p['phase'] + '.tmp')} {shq(p['phase'])}")
    try:
        server.exec(f"mkdir -p {shq(p['dir'])} && "
                    f"flock -x {shq(p['gate'])} bash -c {shq(script)}",
                    timeout=30, stdin_data=payload + "\n")
    except RuntimeError as e:
        # 只认 stdout 段里的 LOCKED：exec 报错内嵌的命令文本本身含 "echo LOCKED"，
        # 在整段消息里匹配会把磁盘满等故障误判成占用。
        if "LOCKED" in _error_stdout(str(e)):
            msg = f"实例 {instance_id} 已被占用"
            raise LockBusy(msg) from _sanitized_cause(e, token)
        raise RuntimeError(_redact(str(e), token)) from _sanitized_cause(e, token)


def release(server, instance_id: str, token: str, result: dict) -> None:
    """gate 内核对令牌 → 落 last-result → 最后移除 owner/phase。

    last_result 内容经 stdin（令牌行之后）落盘；写结果与移除 owner 用 ``&&``
    串联：任一步失败即整体失败并保留锁（fail-closed），绝不半开锁。
    """
    p = lock_paths(instance_id)
    script = (f"umask 077; "
              f"cat > {shq(p['result'] + '.tmp')} && "
              f"mv -f {shq(p['result'] + '.tmp')} {shq(p['result'])} && "
              f"rm -f {shq(p['owner'])} {shq(p['phase'])}")
    try:
        guarded(server, instance_id, token, script, timeout=30,
                stdin_rest=json.dumps(result, ensure_ascii=False,
                                      sort_keys=True) + "\n")
    except RuntimeError as e:
        raise RuntimeError(_redact(str(e), token)) from _sanitized_cause(e, token)


def set_phase(server, instance_id: str, token: str, phase: str) -> None:
    """best-effort 更新当前操作阶段；只允许持有同一令牌的 owner 写。

    失败会抛异常，由调用方决定忽略（阶段只用于观测，不能打断真实发布）。
    """
    p = lock_paths(instance_id)
    script = (f"printf %s {shq(phase)} > {shq(p['phase'] + '.tmp')} && "
              f"mv -f {shq(p['phase'] + '.tmp')} {shq(p['phase'])}")
    try:
        guarded(server, instance_id, token, script, timeout=15)
    except RuntimeError as e:
        raise RuntimeError(_redact(str(e), token)) from _sanitized_cause(e, token)


_VERSION = re.compile(r"^[A-Za-z0-9._-]+$")
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def _existing_sha(text: str, release_war: str):
    """从 exec 失败消息里取现网制品的 sha256sum 输出；取不到返回 None。

    只信任严格的校验和行形状 ``<64位小写hex>␣␣<release_war路径>``
    （两个空格、小写十六进制、行尾无混入内容）；伪造/变形的行一律取不到
    （fail-closed），绝不据此误判复用或冲突。
    """
    m = re.search(r"^([0-9a-f]{64})  " + re.escape(release_war) + r"[ \t]*$",
                  text, re.MULTILINE)
    return m.group(1) if m else None


def commit_staged(server, instance_id: str, token: str, version: str,
                  incoming_dir: str, sha256: str) -> str:
    """暂存提交：同摘要复用（清理暂存）、异摘要拒绝；存在检查与提交在同一 gate 内。

    返回 "created" 或 "reused"。``incoming_dir`` 必须是平台派生的
    ``<app_home>/.incoming/<operation_id>``，调用方不得直接接受用户输入；
    ``version`` 只允许 ``[A-Za-z0-9._-]+``（拼接 release 路径的防穿越护栏）。
    摘要不是机密，可作为参数进命令文本。
    """
    if "/.incoming/" not in str(incoming_dir):
        raise ValueError("incoming_dir 必须是平台派生的 .incoming 暂存路径")
    if not _HEX64.fullmatch(str(sha256)):
        raise ValueError("sha256 必须是 64 位十六进制摘要")
    version = str(version)
    if not _VERSION.fullmatch(version):
        raise ValueError("version 只能包含字母、数字与 . _ -")
    app = incoming_dir.rsplit("/.incoming/", 1)[0]
    release_dir = f"{app}/releases/{version}"
    incoming_war = incoming_dir + "/app.war"
    release_war = release_dir + "/app.war"
    script = (
        f"test -f {shq(incoming_war)} || exit 81; "
        f"echo {shq(sha256 + '  ' + incoming_war)} | sha256sum -c - >/dev/null || exit 82; "
        f"if [ -e {shq(release_dir)} ]; then "
        f"existing=$(sha256sum {shq(release_war)} | awk '{{print $1}}') || exit 76; "
        f"if [ \"$existing\" = {shq(sha256)} ]; then "
        f"rm -rf {shq(incoming_dir)}; echo REUSED; exit 0; fi; "
        f"sha256sum {shq(release_war)}; exit 76; "
        f"fi; "
        f"mkdir -p {shq(app + '/releases')} && "
        f"mv -T {shq(incoming_dir)} {shq(release_dir)} && echo CREATED"
    )
    try:
        out = guarded(server, instance_id, token, script, timeout=120).strip()
    except RuntimeError as e:
        text = str(e)
        existing = _existing_sha(text, release_war)
        if existing is None:
            # 暂存缺失/校验失败/通信故障等：无从判定，原样失败，不得当成复用。
            raise RuntimeError(_redact(text, token)) from _sanitized_cause(e, token)
        if existing.lower() != str(sha256).strip().lower():
            raise VersionConflict(
                f"版本 {version} 已存在且内容不同，拒绝覆盖；"
                f"请改用新版本号或由管理员离线处理") from _sanitized_cause(e, token)
        try:
            guarded(server, instance_id, token,
                    f"rm -rf {shq(incoming_dir)}", timeout=30)
        except RuntimeError as e2:
            raise RuntimeError(_redact(str(e2), token)) from _sanitized_cause(e2, token)
        return "reused"
    if out.endswith("REUSED"):
        return "reused"
    # rc=0 即已提交（mv 成功或同内容复用收尾）；真实远端会带 CREATED/REUSED 标记。
    return "created"


def stop_and_confirm(server, instance_id: str, token: str, unit: str) -> None:
    """严格停止：stop 失败即失败（不是 ``stop || true``），且必须确认
    unit inactive、无待执行 systemd job、主 cgroup 无残余进程；
    不以 MainPID=0 作为已停止证据。任一不满足抛 StopFailed，锁保留。"""
    script = (
        f"systemctl stop {shq(unit)}.service || exit 11; "
        f"test \"$(systemctl is-active {shq(unit)}.service 2>/dev/null || true)\" "
        f"= inactive || exit 12; "
        f"test -z \"$(systemctl list-jobs --no-legend --unit={shq(unit)}.service)\" "
        f"|| exit 13; "
        f"cg=$(systemctl show -p ControlGroup --value {shq(unit)}.service); "
        f"if [ -n \"$cg\" ] && [ \"$cg\" != / ] && [ -d \"/sys/fs/cgroup$cg\" ]; then "
        f"find \"/sys/fs/cgroup$cg\" -name cgroup.procs -type f "
        f"-exec grep -q '[0-9]' {{}} \\; -print -quit | grep -q . && exit 14; "
        f"fi"
    )
    try:
        guarded(server, instance_id, token, script, timeout=60)
    except RuntimeError as e:
        msg = _redact(f"{unit} 未确认停止: {e}", token)
        raise StopFailed(msg) from _sanitized_cause(e, token)


def inspect(server, instance_id: str) -> dict:
    """只读检查 owner/phase/last-result，不需要令牌。

    owner 里的原始令牌替换为其 sha256 摘要：spec §252/§348 要求原始令牌
    不进前端/审计；占用校验始终在远端完成，下游不需要明文令牌。
    无法解析/非对象的内容绝不回传原文（可能含令牌），只回传
    ``raw_digest``（sha256）与 ``raw_len``。
    """
    p = lock_paths(instance_id)
    out = server.exec(
        f"echo @@OWNER@@; cat {shq(p['owner'])} 2>/dev/null || true; "
        f"echo @@PHASE@@; cat {shq(p['phase'])} 2>/dev/null || true; "
        f"echo @@RESULT@@; cat {shq(p['result'])} 2>/dev/null || true", timeout=15)
    owner = out.split("@@OWNER@@", 1)[-1].split("@@PHASE@@", 1)[0].strip()
    phase = out.split("@@PHASE@@", 1)[-1].split("@@RESULT@@", 1)[0].strip()
    result = out.split("@@RESULT@@", 1)[-1].strip()

    def _raw_ref(raw: str) -> dict:
        return {"raw_digest": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                "raw_len": len(raw)}

    def load(raw):
        if not raw:
            return {}
        try:
            loaded = json.loads(raw)
        except ValueError:
            return _raw_ref(raw)
        return loaded if isinstance(loaded, dict) else _raw_ref(raw)

    loaded = load(owner)
    token = loaded.get("token")
    if token:
        loaded["token"] = "sha256:" + hashlib.sha256(
            str(token).encode("utf-8")).hexdigest()
    return {"owner": loaded, "phase": phase or str(loaded.get("phase") or ""),
            "last_result": load(result)}


def revoke(server, instance_id: str, confirm_digest: str) -> None:
    """管理员恢复入口：在 gate 内核对 owner.json 内容摘要后才隔离旧令牌。

    摘要不符或锁不存在（cat 失败 → 空串摘要不匹配 → exit 77；或 mv 失败）
    都会报错，不会凭空解除占用；旧 owner 移入 owner.revoked.json 而非删除。
    摘要是公开值，可作为参数进命令文本。
    """
    p = lock_paths(instance_id)
    digest = str(confirm_digest).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("confirm_digest 必须是 owner.json 内容的 sha256 十六进制摘要")
    script = (
        f"test \"$(cat {shq(p['owner'])} | sha256sum | awk '{{print $1}}')\" "
        f"= {shq(digest)} || exit 77; "
        f"mv -f {shq(p['owner'])} {shq(p['revoked'])}; rm -f {shq(p['phase'])}"
    )
    server.exec(f"flock -x {shq(p['gate'])} bash -c {shq(script)}", timeout=30)
