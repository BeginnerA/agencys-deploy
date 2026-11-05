"""构建命令执行器：输出实时透传 + 超时后整棵进程树终止

线上事故回归：Maven 构建在 Windows 上超时后并未真正结束——
subprocess.run(timeout=...) 只杀直接子进程（cmd.exe 包装层），
Maven 的 java.exe 孙进程仍持有输出管道，随后 communicate() 继续阻塞，
任务永不收尾，前端一直停在「加载中...」。
"""
import io
import os
import sys
import time
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build


class StreamingOutputTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_output_emitted_while_process_still_running(self):
        """长命令必须边跑边出日志，而不是结束后一次性吐出"""
        cmd = [sys.executable, "-u", "-c",
               "import time; print('阶段一完成', flush=True); time.sleep(2.0); print('阶段二完成')"]
        seen = []
        started = time.monotonic()

        def on_line(line):
            seen.append((time.monotonic() - started, line))

        rc, text = build._run_streaming(cmd, self.cwd, timeout=30, on_line=on_line)
        total = time.monotonic() - started

        self.assertEqual(rc, 0)
        self.assertIn("阶段一完成", text)
        self.assertIn("阶段二完成", text)
        self.assertEqual(len(seen), 2)
        # 第一行必须在进程结束前明显提前到达（留 1 秒余量避免慢机器误判）
        self.assertLess(seen[0][0], total - 1.0)
        self.assertEqual(seen[0][1], "阶段一完成")

    def test_default_output_format_kept(self):
        """默认回调沿用 '  | 行内容' 前缀，日志观感不变"""
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc, _ = build._run_streaming(
                [sys.executable, "-u", "-c", "print('hello-build')"],
                self.cwd, timeout=30)
        self.assertEqual(rc, 0)
        self.assertIn("  | hello-build", buf.getvalue())

    def test_missing_command_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            build._run_streaming(["definitely-not-a-real-command-xyz"],
                                 self.cwd, timeout=5)


class TimeoutKillsWholeTreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @unittest.skipUnless(os.name == "nt", "Windows 包装层进程树终止")
    def test_windows_cmd_wrapper_grandchild_is_killed(self):
        """cmd.exe 启动的孙进程（如 Maven 的 java.exe）必须随超时一起结束"""
        script = self.cwd / "slow.cmd"
        script.write_text("@echo off\r\nping -n 15 127.0.0.1 >nul\r\n", encoding="ascii")
        started = time.monotonic()
        with self.assertRaises(build.subprocess.TimeoutExpired):
            build._run_streaming(["cmd", "/c", str(script)], self.cwd, timeout=2)
        elapsed = time.monotonic() - started
        # 旧实现要等孙进程自然结束（约 14 秒）；整树终止后应接近 timeout
        self.assertLess(elapsed, 8.0)

    @unittest.skipUnless(os.name != "nt", "POSIX 进程组终止")
    def test_posix_process_group_is_killed(self):
        started = time.monotonic()
        with self.assertRaises(build.subprocess.TimeoutExpired):
            build._run_streaming(["sh", "-c", "sleep 30"], self.cwd, timeout=1)
        self.assertLess(time.monotonic() - started, 5.0)


class RunWrappersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_timeout_reports_and_exits(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            build.run([sys.executable, "-c", "import time; time.sleep(30)"],
                      self.cwd, timeout=1)
        self.assertIn("命令超时 (1s)", err.getvalue())

    def test_run_failure_reports_rc(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            build.run([sys.executable, "-c", "import sys; sys.exit(3)"],
                      self.cwd, timeout=30)
        self.assertIn("命令执行失败 (rc=3)", err.getvalue())

    def test_run_shell_timeout_reports_and_exits(self):
        cmd = "ping -n 30 127.0.0.1 > nul" if os.name == "nt" else "sleep 30"
        err = io.StringIO()
        started = time.monotonic()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            build.run_shell(cmd, self.cwd, timeout=1)
        self.assertIn("命令超时 (1s)", err.getvalue())
        self.assertLess(time.monotonic() - started, 8.0)

    def test_run_shell_streams_output(self):
        cmd = "echo shell-line" if os.name != "nt" else "echo shell-line"
        buf = io.StringIO()
        with redirect_stdout(buf):
            build.run_shell(cmd, self.cwd, timeout=30)
        self.assertIn("  | shell-line", buf.getvalue())


class TransientRetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _flaky_script(self, message):
        script = self.cwd / "flaky.py"
        script.write_text(
            "import pathlib, sys\n"
            "marker = pathlib.Path(__file__).with_suffix('.runs')\n"
            "runs = int(marker.read_text()) if marker.exists() else 0\n"
            "marker.write_text(str(runs + 1))\n"
            f"print({message!r})\n"
            "sys.exit(0 if runs else 1)\n",
            encoding="utf-8")
        return script

    def test_transient_failure_retried_once_then_succeeds(self):
        script = self._flaky_script(
            "[ERROR] Failed to execute goal org.apache.maven.plugins:"
            "maven-clean-plugin:3.2.0:clean (default-clean) on project app: "
            "Failed to clean project: Failed to delete target -> [Help 1]")
        buf = io.StringIO()
        with redirect_stdout(buf):
            build.run([sys.executable, str(script)], self.cwd, timeout=30,
                      retry_on_clean_failure=True, max_retries=1, retry_delay=0)
        self.assertIn("自动重试（第 1/1 次）", buf.getvalue())
        self.assertEqual((self.cwd / "flaky.runs").read_text(), "2")

    def test_deterministic_failure_not_retried(self):
        script = self._flaky_script("[ERROR] Compilation failure -> [Help 1]")
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            build.run([sys.executable, str(script)], self.cwd, timeout=30,
                      retry_on_clean_failure=True, max_retries=3, retry_delay=0)
        self.assertNotIn("自动重试", err.getvalue())
        self.assertEqual((self.cwd / "flaky.runs").read_text(), "1")


if __name__ == "__main__":
    unittest.main()
