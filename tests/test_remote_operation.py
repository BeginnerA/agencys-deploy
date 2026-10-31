"""Task 4：远端实例锁、暂存提交与严格停止命令。

计划文档 docs/superpowers/plans/2026-10-30-war-tomcat-deploy.md Task 4 的验收用例，
外加锁红线（fail-closed、令牌先行、注入编码、令牌脱敏）的加固用例。
修复轮 1（审查发现）：令牌绝不进命令文本（只走 stdin）、包装异常用脱敏 __cause__、
inspect 不回传未解析原文、version 白名单、严格校验和行、异常通道补测。
全部走 FakeServer，不做真实 SSH/远端操作。
"""
import hashlib
import json
import shlex
import unittest

from deploy_strategies import remote_operation as ro


class FakeRemote:
    """deploy.py RemoteServer.exec 的形状：返回 stdout，失败抛 RuntimeError。

    额外记录 stdin_data（stdis）：令牌/owner 载荷/last_result 内容只允许走 stdin。
    """

    def __init__(self, fail=None, stdout=""):
        self.calls = []
        self.stdis = []
        self.fail = fail or {}
        self.stdout = stdout

    def exec(self, cmd, timeout=60, stdin_data=None):
        self.calls.append(cmd)
        self.stdis.append(stdin_data)
        for key, err in self.fail.items():
            if key in cmd:
                raise RuntimeError(err)
        return self.stdout


class DeployShapedRemote:
    """模拟真实 RemoteServer.exec 的报错形状：异常消息内嵌完整命令与 stdout/stderr。"""

    def __init__(self, rc=1, stdout="", stderr="boom"):
        self.calls = []
        self.stdis = []
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr

    def exec(self, cmd, timeout=60, stdin_data=None):
        self.calls.append(cmd)
        self.stdis.append(stdin_data)
        raise RuntimeError(
            f"[host] 命令执行失败 (rc={self.rc}): {cmd}\n"
            f"stdout:\n{self.stdout}\nstderr:\n{self.stderr}")


class ScriptedRemote:
    """按序回放结果的替身：结果为 str（stdout）或 Exception（抛出）。"""

    def __init__(self, results):
        self.calls = []
        self.stdis = []
        self.results = list(results)

    def exec(self, cmd, timeout=60, stdin_data=None):
        self.calls.append(cmd)
        self.stdis.append(stdin_data)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _one_call(server):
    assert len(server.calls) == 1, server.calls
    return server.calls[0]


def _walk_cause(exc):
    """沿 __cause__ 链走到底，返回含异常自身在内的全部异常。"""
    chain = []
    cur = exc
    while cur is not None and cur not in chain:
        chain.append(cur)
        cur = cur.__cause__
    return chain


class RemoteOperationTest(unittest.TestCase):
    # ---- 计划文档 Step 1 的验收用例（按修复轮 1 的令牌-stdin 形状适配） ----

    def test_guarded_checks_token_and_gate(self):
        """guarded 自行 exec 并返回 stdout；令牌只走 stdin，命令文本零令牌。"""
        server = FakeRemote(stdout="ok")
        out = ro.guarded(server, "abc123", "deadbeef",
                         "systemctl start x.service")
        self.assertEqual(out, "ok")
        cmd = _one_call(server)
        self.assertIn("flock -x", cmd)
        self.assertIn("IFS= read -r __TOK || exit 71", cmd)
        self.assertIn('grep -F -q -f - "$OWNER"', cmd)
        self.assertIn("|| exit 73", cmd)
        self.assertIn("systemctl start x.service", cmd)
        self.assertNotIn("deadbeef", cmd)  # 令牌不进命令文本（exec 会 log 整条命令）
        self.assertEqual(server.stdis[0], "deadbeef\n")  # 令牌走 stdin 第一行

    def test_acquire_writes_owner_and_maps_busy(self):
        server = FakeRemote()
        ro.acquire(server, "abc123", "deadbeef", "deploy", "preflight")
        self.assertIn("test ! -e", server.calls[0])
        self.assertIn("umask 077", server.calls[0])
        self.assertIn("cat >", server.calls[0])  # owner 内容经 stdin 落盘
        self.assertIn("exit 75", server.calls[0])
        with self.assertRaises(ro.LockBusy):
            ro.acquire(FakeRemote({"test ! -e": "LOCKED"}), "abc123", "t",
                       "deploy", "x")

    def test_release_writes_result_then_removes_owner(self):
        server = FakeRemote()
        ro.release(server, "abc123", "deadbeef", {"operation_status": "ok"})
        joined = "\n".join(server.calls)
        self.assertIn("last-result.json", joined)
        self.assertIn("rm -f", joined)
        self.assertIn("owner.json", joined)

    def test_commit_reuses_same_sha_rejects_other(self):
        server = FakeRemote()
        self.assertEqual(ro.commit_staged(server, "abc", "t", "1.0.0",
                                          "/opt/a/.incoming/op1", "aa" * 32),
                         "created")
        self.assertIn("mv -T", server.calls[0])
        # 注意：计划文档里的复用夹具把摘要缩写成 "aa"，真实 sha256sum 永远输出
        # 完整 64 位十六进制；摘要比较必须全等（spec §216：内容不同一律拒绝），
        # 因此这里按真实输出形状给全量摘要。冲突夹具同样用真实形状（修复轮 1：
        # 伪造的 "bb  x" 缩写行不再被信任，见 test_existing_sha_requires_strict...）。
        reuse = FakeRemote({"sha256sum": "aa" * 32 + "  /opt/a/releases/1.0.0/app.war\n"})
        self.assertEqual(ro.commit_staged(reuse, "abc", "t", "1.0.0",
                                          "/opt/a/.incoming/op1", "aa" * 32),
                         "reused")
        with self.assertRaises(ro.VersionConflict):
            ro.commit_staged(
                FakeRemote({"sha256sum": "bb" * 32 + "  /opt/a/releases/1.0.0/app.war\n"}),
                "abc", "t", "1.0.0", "/opt/a/.incoming/op1", "aa" * 32)

    def test_stop_rejects_active_or_jobs(self):
        with self.assertRaises(ro.StopFailed):
            ro.stop_and_confirm(FakeRemote({"is-active": "active"}), "a", "t", "u")
        with self.assertRaises(ro.StopFailed):
            ro.stop_and_confirm(FakeRemote({"list-jobs": "1 u.service start running"}),
                                "a", "t", "u")

    def test_set_phase_is_token_guarded(self):
        server = FakeRemote()
        ro.set_phase(server, "abc", "deadbeef", "health")
        cmd = _one_call(server)
        self.assertIn("owner.phase", cmd)
        self.assertIn('grep -F -q -f - "$OWNER"', cmd)
        self.assertIn("exit 73", cmd)
        self.assertNotIn("deadbeef", cmd)
        self.assertEqual(server.stdis[0], "deadbeef\n")

    def test_inspect_returns_phase_without_token(self):
        class S:
            def exec(self, cmd, timeout=60, stdin_data=None):
                return ("@@OWNER@@\n{\"token\": \"t\", \"phase\": \"starting\"}\n"
                        "@@PHASE@@\nstarting\n@@RESULT@@\n{}")
        out = ro.inspect(S(), "abc")
        self.assertEqual(out["phase"], "starting")
        self.assertEqual(out["owner"]["phase"], "starting")

    # ---- 修复轮 1 红线：令牌绝不进任何远端命令文本 ----

    def test_no_token_in_any_recorded_command(self):
        """RemoteServer.exec 每次调用都 log 整条命令文本：guarded/acquire/release/
        commit/stop/set_phase/revoke 记录到的每一条命令都不得含令牌；
        令牌只允许出现在 stdin（校验行首行 / owner JSON / last_result 内容）。"""
        tok = "s3cret-tok-42"
        server = FakeRemote(stdout="CREATED\n")
        ro.guarded(server, "abc", tok, "systemctl start x.service")
        ro.acquire(server, "abc", tok, "deploy", "preflight")
        ro.set_phase(server, "abc", tok, "health")
        ro.commit_staged(server, "abc", tok, "1.0.0",
                         "/opt/a/.incoming/op1", "aa" * 32)
        ro.stop_and_confirm(server, "abc", tok, "u")
        ro.release(server, "abc", tok, {"ok": True})
        ro.revoke(server, "abc", "aa" * 32)  # revoke 无令牌；摘要是公开值
        self.assertEqual(len(server.calls), 7, server.calls)
        for i, cmd in enumerate(server.calls):
            self.assertNotIn(tok, cmd, f"第 {i} 条命令泄漏令牌: {cmd}")
        # 令牌只经 stdin：校验型调用 stdin 第一行即令牌；acquire 是含令牌的 owner JSON
        self.assertEqual(server.stdis[0], tok + "\n")  # guarded
        self.assertEqual(json.loads(server.stdis[1])["token"], tok)  # acquire
        for i in (2, 3, 4, 5):  # set_phase / commit / stop / release
            self.assertEqual(server.stdis[i].split("\n", 1)[0], tok, f"call #{i}")

    # ---- 锁红线加固用例 ----

    def test_lock_paths_layout(self):
        p = ro.lock_paths("abc")
        self.assertEqual(p["dir"], "/var/lib/smartops/tomcat/abc")
        self.assertEqual(p["gate"], "/var/lib/smartops/tomcat/abc/gate.lock")
        self.assertEqual(p["owner"], "/var/lib/smartops/tomcat/abc/owner.json")
        self.assertEqual(p["phase"], "/var/lib/smartops/tomcat/abc/owner.phase")
        self.assertEqual(p["result"],
                         "/var/lib/smartops/tomcat/abc/last-result.json")
        self.assertEqual(p["revoked"],
                         "/var/lib/smartops/tomcat/abc/owner.revoked.json")

    def test_guarded_quotes_hostile_inputs(self):
        """instance_id/令牌/命令含 shell 元字符时不得逃出各自参数位置；
        令牌整条不出现在命令文本里（修复轮 1：改走 stdin）。"""
        iid, tok = "a b;$(id)", "tok'; rm -rf /"
        server = FakeRemote()
        ro.guarded(server, iid, tok, "systemctl start x; reboot")
        cmd = _one_call(server)
        argv = shlex.split(cmd)
        self.assertEqual(argv[0], "flock")
        self.assertEqual(argv[1], "-x")
        self.assertEqual(argv[2], ro.lock_paths(iid)["gate"])
        self.assertEqual(argv[3], "bash")
        self.assertEqual(argv[4], "-c")
        script = argv[5]
        self.assertTrue(script.startswith("IFS= read -r __TOK || exit 71; "))
        self.assertIn('grep -F -q -f - "$OWNER"', script)
        self.assertIn("|| exit 73", script)
        self.assertTrue(script.endswith("systemctl start x; reboot"))
        self.assertNotIn(tok, cmd)  # 元字符令牌也只走 stdin
        self.assertEqual(server.stdis[0], tok + "\n")

    def test_acquire_owner_payload_travels_via_stdin(self):
        """owner.json 内容（含令牌）在 Python 构造、只经 stdin 落盘；命令行零明文。"""
        server = FakeRemote()
        ro.acquire(server, "abc", "tok123", "deploy", "preflight")
        cmd = _one_call(server)
        self.assertNotIn("tok123", cmd)
        self.assertNotIn("base64", cmd)  # 不再需要把载荷编码进命令行
        self.assertIn("umask 077", cmd)
        self.assertIn("cat >", cmd)
        self.assertIn("test ! -e", cmd)
        self.assertIn("exit 75", cmd)
        payload = json.loads(server.stdis[0])
        self.assertEqual(payload["token"], "tok123")
        self.assertEqual(payload["operation"], "deploy")
        self.assertEqual(payload["phase"], "preflight")
        self.assertIn("started_at", payload)

    def test_acquire_busy_makes_no_further_writes(self):
        server = FakeRemote({"test ! -e": "LOCKED"})
        with self.assertRaises(ro.LockBusy):
            ro.acquire(server, "abc", "t", "op", "x")
        self.assertEqual(len(server.calls), 1)  # 占用即拒绝，绝不覆盖现有 owner

    def test_acquire_non_locked_failure_is_not_busy(self):
        with self.assertRaises(RuntimeError) as ctx:
            ro.acquire(FakeRemote({"mv -f": "disk full"}), "abc", "t", "op", "x")
        self.assertNotIsInstance(ctx.exception, ro.LockBusy)

    def test_acquire_disk_failure_not_misread_as_busy(self):
        """deploy.exec 的消息内嵌脚本（含 echo LOCKED 字样），
        占用判定必须只看 stdout 段，不能在整段消息里 grep。"""
        remote = DeployShapedRemote(rc=1, stderr="disk full")
        with self.assertRaises(RuntimeError) as ctx:
            ro.acquire(remote, "abc", "tok123", "op", "x")
        self.assertNotIsInstance(ctx.exception, ro.LockBusy)
        self.assertNotIn("tok123", str(ctx.exception))

    def test_acquire_busy_via_deploy_shaped_error(self):
        remote = DeployShapedRemote(rc=75, stdout="LOCKED")
        with self.assertRaises(ro.LockBusy) as ctx:
            ro.acquire(remote, "abc", "tok123", "op", "x")
        self.assertNotIn("tok123", str(ctx.exception))

    def test_release_checks_token_before_any_write(self):
        server = FakeRemote()
        ro.release(server, "abc", "deadbeef", {"operation_status": "ok"})
        cmd = _one_call(server)
        self.assertLess(cmd.index("|| exit 73"), cmd.index("last-result.json"))
        self.assertLess(cmd.index("last-result.json"), cmd.index("rm -f"))
        self.assertIn("flock -x", cmd)
        self.assertNotIn("deadbeef", cmd)
        # last_result 内容走 stdin（令牌行之后，供脚本内 cat 落盘）
        self.assertEqual(
            server.stdis[0],
            "deadbeef\n" + json.dumps({"operation_status": "ok"},
                                      ensure_ascii=False, sort_keys=True) + "\n")

    def test_release_failed_result_write_keeps_lock(self):
        """fail-closed：结果写失败必须保锁，rm owner/phase 只能经 && 链
        在成功写入（cat+mv）之后执行，绝不能用 ; 顺序执行。"""
        server = FakeRemote()
        ro.release(server, "abc", "deadbeef", {"ok": True})
        script = shlex.split(_one_call(server))[5]
        p = ro.lock_paths("abc")
        chain = f"&& rm -f {shlex.quote(p['owner'])} {shlex.quote(p['phase'])}"
        self.assertIn(chain, script)
        self.assertNotIn("; rm -f", script)
        self.assertLess(script.index("mv -f"), script.index("&& rm -f"))
        # 结果写入失败（如磁盘满）→ 整条 exec 失败，锁保留
        broken = FakeRemote({"last-result.json": "disk full"})
        with self.assertRaises(RuntimeError):
            ro.release(broken, "abc", "deadbeef", {"ok": True})
        self.assertEqual(len(broken.calls), 1)

    def test_release_failure_propagates_without_unlocking(self):
        server = FakeRemote({"grep": "no owner"})
        with self.assertRaises(RuntimeError):
            ro.release(server, "abc", "deadbeef", {"ok": True})
        self.assertEqual(len(server.calls), 1)

    def test_commit_is_gate_guarded_and_verifies_incoming(self):
        server = FakeRemote()
        ro.commit_staged(server, "abc", "t", "1.0.0",
                         "/opt/a/.incoming/op1", "aa" * 32)
        cmd = _one_call(server)
        self.assertIn("flock -x", cmd)
        self.assertIn('grep -F -q -f - "$OWNER"', cmd)
        self.assertIn("|| exit 73", cmd)
        self.assertIn("sha256sum -c -", cmd)
        self.assertIn("|| exit 81", cmd)
        self.assertIn("|| exit 82", cmd)

    def test_commit_reuse_cleanup_is_guarded(self):
        reuse = FakeRemote(
            {"sha256sum": "aa" * 32 + "  /opt/a/releases/1.0.0/app.war\n"})
        self.assertEqual(ro.commit_staged(reuse, "abc", "deadbeef", "1.0.0",
                                          "/opt/a/.incoming/op1", "aa" * 32),
                         "reused")
        self.assertEqual(len(reuse.calls), 2)
        self.assertIn('grep -F -q -f - "$OWNER"', reuse.calls[1])
        self.assertIn("|| exit 73", reuse.calls[1])
        self.assertNotIn("deadbeef", reuse.calls[1])
        self.assertIn("rm -rf", reuse.calls[1])
        self.assertEqual(reuse.stdis[1], "deadbeef\n")

    def test_commit_rejects_non_incoming_path(self):
        with self.assertRaises(ValueError):
            ro.commit_staged(FakeRemote(), "abc", "t", "1.0.0",
                             "/opt/a/tmp/op1", "aa" * 32)

    def test_commit_rejects_bad_digest(self):
        with self.assertRaises(ValueError):
            ro.commit_staged(FakeRemote(), "abc", "t", "1.0.0",
                             "/opt/a/.incoming/op1", "zz" * 32)

    def test_commit_rejects_bad_version(self):
        """version 只允许 ^[A-Za-z0-9._-]+$（拼接 release 路径的防穿越护栏）；
        拒绝时不发起任何远端调用。"""
        server = FakeRemote()
        for bad in ("../evil", "1.0.0;reboot", "a b", "v1/2", "1.0.0\n", "",
                    "版本一"):
            with self.assertRaises(ValueError):
                ro.commit_staged(server, "abc", "t", bad,
                                 "/opt/a/.incoming/op1", "aa" * 32)
        self.assertEqual(server.calls, [])
        self.assertEqual(ro.commit_staged(server, "abc", "t", "1.0.0-RC_2.b",
                                          "/opt/a/.incoming/op1", "aa" * 32),
                         "created")

    def test_commit_stdout_markers_created_and_reused(self):
        """rc=0 且带标记：fake 返回 CREATED/REUSED 标记原文时分别判定。"""
        created = ScriptedRemote(["CREATED\n"])
        self.assertEqual(ro.commit_staged(created, "abc", "tok", "1.0.0",
                                          "/opt/a/.incoming/op1", "aa" * 32),
                         "created")
        self.assertEqual(len(created.calls), 1)
        reused = ScriptedRemote(["REUSED\n"])
        self.assertEqual(ro.commit_staged(reused, "abc", "tok", "1.0.0",
                                          "/opt/a/.incoming/op1", "aa" * 32),
                         "reused")
        self.assertEqual(len(reused.calls), 1)

    def test_existing_sha_requires_strict_checksum_line(self):
        """现网摘要行必须是严格的 `<64位小写hex>␣␣<release_war>` 校验和行；
        伪造/变形的行一律不可信：取不到 → 原样失败，绝不误判复用/冲突。"""
        release_war = "/opt/a/releases/1.0.0/app.war"
        for bad in ("bb  x\n",  # 计划文档里的伪造缩写行
                    "BB" * 32 + "  " + release_war + "\n",  # 大写 hex
                    "aa" * 32 + " " + release_war + "\n",  # 单空格
                    "aa" * 32 + "  /opt/a/.incoming/op1/app.war\n",  # 暂存路径冒充
                    "aa" * 32 + "  " + release_war + " \tx\n"):  # 行尾混入路径
            with self.assertRaises(RuntimeError) as ctx:
                ro.commit_staged(FakeRemote({"sha256sum": bad}), "abc", "tok123",
                                 "1.0.0", "/opt/a/.incoming/op1", "aa" * 32)
            self.assertNotIsInstance(ctx.exception, ro.VersionConflict)
            self.assertNotIn("tok123", str(ctx.exception))
        # 严格形状才被信任：同摘要 → 复用收尾
        strict = FakeRemote({"sha256sum": "aa" * 32 + "  " + release_war + "\n"})
        self.assertEqual(ro.commit_staged(strict, "abc", "tok123", "1.0.0",
                                          "/opt/a/.incoming/op1", "aa" * 32),
                         "reused")

    def test_stop_script_is_strict(self):
        server = FakeRemote()
        ro.stop_and_confirm(server, "abc", "deadbeef", "u b")
        cmd = _one_call(server)
        argv = shlex.split(cmd)  # 整条命令必须可解析：unit 含空格也不逃逸参数位
        self.assertEqual(argv[0], "flock")
        self.assertEqual(argv[2], ro.lock_paths("abc")["gate"])
        self.assertEqual(argv[3], "bash")
        self.assertEqual(argv[4], "-c")
        script = argv[5]
        # 令牌校验先于任何写动作（systemctl stop 之前）
        self.assertLess(script.index("|| exit 73"), script.index("systemctl stop"))
        self.assertIn("systemctl stop 'u b'.service || exit 11", script)
        # 不能 stop || true；必须确认 inactive、无待执行 job、cgroup 无进程
        self.assertNotIn("stop || true", script)
        self.assertIn("= inactive || exit 12", script)
        self.assertIn("list-jobs", script)
        self.assertIn("|| exit 13", script)
        self.assertIn("cgroup.procs", script)
        self.assertIn("exit 14", script)
        self.assertNotIn("MainPID", script)
        self.assertNotIn("deadbeef", cmd)
        self.assertEqual(server.stdis[0], "deadbeef\n")

    def test_stop_cgroup_residue_raises_stopfailed(self):
        """exit 14（cgroup 残留进程）必须走 StopFailed。"""
        with self.assertRaises(ro.StopFailed):
            ro.stop_and_confirm(FakeRemote({"cgroup.procs": "123"}),
                                "abc", "t", "u")

    def test_set_phase_is_atomic_and_fails_loud(self):
        server = FakeRemote()
        ro.set_phase(server, "abc", "deadbeef", "health")
        cmd = _one_call(server)
        self.assertIn("owner.phase.tmp", cmd)
        self.assertIn("mv -f", cmd)
        self.assertNotIn("deadbeef", cmd)
        with self.assertRaises(RuntimeError):
            ro.set_phase(FakeRemote({"grep": "revoked"}), "abc", "deadbeef", "x")

    def test_revoke_requires_owner_digest_and_isolates(self):
        server = FakeRemote()
        ro.revoke(server, "abc", "aa" * 32)
        cmd = _one_call(server)
        self.assertIn("flock -x", cmd)
        self.assertIn("sha256sum", cmd)
        self.assertIn("|| exit 77", cmd)
        self.assertIn("owner.revoked.json", cmd)
        p = ro.lock_paths("abc")
        self.assertIn(f"mv -f {shlex.quote(p['owner'])} {shlex.quote(p['revoked'])}",
                      cmd)
        self.assertIn(f"rm -f {shlex.quote(p['phase'])}", cmd)
        self.assertNotIn(f"rm -f {shlex.quote(p['owner'])}", cmd)

    def test_revoke_rejects_bad_digest_format(self):
        with self.assertRaises(ValueError):
            ro.revoke(FakeRemote(), "abc", "not-a-digest")

    def test_revoke_failure_propagates(self):
        with self.assertRaises(RuntimeError):
            ro.revoke(FakeRemote({"sha256sum": "mismatch"}), "abc", "aa" * 32)

    def test_revoke_absent_lock_raises(self):
        """锁不存在（cat 失败 → 空串摘要 ≠ confirm_digest → exit 77）必须报错。"""
        with self.assertRaises(RuntimeError):
            ro.revoke(FakeRemote({"cat": "No such file or directory"}),
                      "abc", "aa" * 32)

    def test_inspect_malformed_content_never_returns_raw(self):
        """损坏的 owner/last_result 内容可能含令牌：只回传 sha256 摘要+长度，
        绝不回传原文（修复轮 1：{"raw": raw[:200]} 会把令牌带出去）。"""
        garbage = 'corrupted{"token": "tok123"} not-json'
        result_raw = '["tok123"]'

        class S:
            def exec(self, cmd, timeout=60, stdin_data=None):
                return ("@@OWNER@@\n" + garbage +
                        "\n@@PHASE@@\n\n@@RESULT@@\n" + result_raw)

        out = ro.inspect(S(), "abc")
        self.assertEqual(out["owner"],
                         {"raw_digest":
                          hashlib.sha256(garbage.encode("utf-8")).hexdigest(),
                          "raw_len": len(garbage)})
        self.assertEqual(out["last_result"],
                         {"raw_digest":
                          hashlib.sha256(result_raw.encode("utf-8")).hexdigest(),
                          "raw_len": len(result_raw)})
        self.assertNotIn("tok123", json.dumps(out))

    def test_inspect_owner_token_is_redacted(self):
        class S:
            def exec(self, cmd, timeout=60, stdin_data=None):
                return ("@@OWNER@@\n" + json.dumps(
                    {"token": "secret-token-value", "phase": "deploy"}) +
                    "\n@@PHASE@@\ndeploy\n@@RESULT@@\n{}")
        out = ro.inspect(S(), "abc")
        self.assertNotIn("secret-token-value", json.dumps(out))
        self.assertTrue(str(out["owner"]["token"]).startswith("sha256:"))

    # ---- 令牌不得进入异常消息与 __cause__ 链 ----

    def test_exceptions_never_carry_token(self):
        tok = "tok123"
        with self.assertRaises(ro.LockBusy) as busy:
            ro.acquire(DeployShapedRemote(rc=75, stdout="LOCKED"), "abc", tok,
                       "op", "x")
        self.assertNotIn(tok, str(busy.exception))
        with self.assertRaises(RuntimeError) as rel:
            ro.release(DeployShapedRemote(), "abc", tok, {"ok": True})
        self.assertNotIn(tok, str(rel.exception))
        with self.assertRaises(ro.StopFailed) as stop:
            ro.stop_and_confirm(DeployShapedRemote(), "abc", tok, "u")
        self.assertNotIn(tok, str(stop.exception))
        with self.assertRaises(RuntimeError) as ph:
            ro.set_phase(DeployShapedRemote(), "abc", tok, "health")
        self.assertNotIn(tok, str(ph.exception))
        # 版本冲突：远端摘要不同 → VersionConflict，消息同样脱敏
        remote = DeployShapedRemote(
            stdout="bb" * 32 + "  /opt/a/releases/1.0.0/app.war\n")
        with self.assertRaises(ro.VersionConflict) as ver:
            ro.commit_staged(remote, "abc", tok, "1.0.0",
                             "/opt/a/.incoming/op1", "aa" * 32)
        self.assertNotIn(tok, str(ver.exception))
        # 无从判定远端摘要（如暂存缺失 exit 81）→ 原样失败，不得误判为复用
        remote2 = DeployShapedRemote(stderr="no such file")
        with self.assertRaises(RuntimeError) as unclear:
            ro.commit_staged(remote2, "abc", tok, "1.0.0",
                             "/opt/a/.incoming/op1", "aa" * 32)
        self.assertNotIsInstance(unclear.exception, ro.VersionConflict)
        self.assertNotIn(tok, str(unclear.exception))

    def test_cause_chain_never_carries_token(self):
        """包装异常必须用脱敏后的 __cause__（修复轮 1）：exec 报错内嵌完整命令与
        stdout/stderr，原样作 __cause__ 会把令牌沿异常链带进日志。
        远端把令牌回显到 stderr 的故障形状下，沿 __cause__ 递归走到底，
        任何一层都不得含令牌。"""
        tok = "tok123"

        def assert_clean(ctx):
            chain = _walk_cause(ctx.exception)
            self.assertTrue(chain, "必须存在 __cause__ 链")
            for exc in chain:
                self.assertNotIn(tok, str(exc),
                                 f"{type(exc).__name__} 泄漏令牌")

        with self.assertRaises(RuntimeError) as acq:
            ro.acquire(DeployShapedRemote(rc=1, stderr=f"disk full; leaked={tok}"),
                       "abc", tok, "op", "x")
        self.assertNotIsInstance(acq.exception, ro.LockBusy)
        assert_clean(acq)
        with self.assertRaises(ro.LockBusy) as busy:
            ro.acquire(DeployShapedRemote(rc=75, stdout="LOCKED"), "abc", tok,
                       "op", "x")
        assert_clean(busy)
        with self.assertRaises(RuntimeError) as rel:
            ro.release(DeployShapedRemote(rc=1, stderr=f"boom {tok}"),
                       "abc", tok, {"ok": True})
        assert_clean(rel)
        with self.assertRaises(RuntimeError) as ph:
            ro.set_phase(DeployShapedRemote(rc=1, stderr=f"boom {tok}"),
                         "abc", tok, "health")
        assert_clean(ph)
        with self.assertRaises(ro.StopFailed) as stop:
            ro.stop_and_confirm(DeployShapedRemote(rc=1, stderr=f"boom {tok}"),
                                "abc", tok, "u")
        assert_clean(stop)
        # commit：无从判定摘要 → 原样失败；摘要不符 → VersionConflict；链都要干净
        with self.assertRaises(RuntimeError) as unclear:
            ro.commit_staged(DeployShapedRemote(rc=1, stderr=f"boom {tok}"),
                             "abc", tok, "1.0.0", "/opt/a/.incoming/op1",
                             "aa" * 32)
        assert_clean(unclear)
        conflict = DeployShapedRemote(
            rc=1, stdout="bb" * 32 + "  /opt/a/releases/1.0.0/app.war\n",
            stderr=f"boom {tok}")
        with self.assertRaises(ro.VersionConflict) as ver:
            ro.commit_staged(conflict, "abc", tok, "1.0.0",
                             "/opt/a/.incoming/op1", "aa" * 32)
        assert_clean(ver)


if __name__ == "__main__":
    unittest.main()
