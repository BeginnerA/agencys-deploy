"""jar_probe 纯函数：命令构造、输出解析、CPU 差分与每主机聚合"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import jar_probe


def _stat_line(pid=4242, utime=500, stime=200, rss_pages=20480):
    # 剥掉 "pid (comm) " 后：rest[0]=state, rest[11]=utime, rest[12]=stime,
    # rest[21]=rss（对应 /proc/<pid>/stat 的 3/14/15/24 字段）
    rest = ["S"] + ["0"] * 10 + [str(utime), str(stime)] + ["0"] * 8 + [str(rss_pages)]
    return f"{pid} (java app) " + " ".join(rest)


SAMPLE = "\n".join([
    "UPTIME:10000.00 20000.00",
    "CLK:100",
    "@@JARUNIT:tok:0",
    "LoadState=loaded",
    "ActiveState=active",
    "SubState=running",
    "MainPID=4242",
    "NRestarts=2",
    "ActiveEnterTimestampMonotonic=9600000000",
    "VER:1.0.1",
    "LINK:/opt/auth/releases/1.0.1",
    "STAT:" + _stat_line(),
    "@@JARUNIT:tok:1",
    "LoadState=not-found",
    "ActiveState=inactive",
    "SubState=dead",
    "MainPID=0",
    "NRestarts=0",
    "ActiveEnterTimestampMonotonic=0",
    "VER:",
    "LINK:",
])


class BuildProbeCmdTest(unittest.TestCase):
    def test_units_and_versions_are_quoted(self):
        cmd, token = jar_probe.build_probe_cmd(
            {"hqb-auth": "/opt/auth", "hqb api": {"app_home": "/opt/a b"}},
            token="tok")
        self.assertEqual(token, "tok")
        self.assertIn("@@JARUNIT:tok:0", cmd)
        self.assertIn("@@JARUNIT:tok:1", cmd)
        self.assertIn("systemctl show -p LoadState", cmd)
        self.assertIn("cat /opt/auth/.deploy_version", cmd)
        self.assertIn("readlink /opt/auth/CURRENT", cmd)
        self.assertIn("ghcq".replace("ghcq", "'hqb api'"), cmd)  # shlex.quote
        self.assertIn("'/opt/a b'", cmd)

    def test_units_order_is_stable(self):
        cmd, _ = jar_probe.build_probe_cmd({"b": "/b", "a": "/a"}, token="t")
        self.assertLess(cmd.index(":0"), cmd.index(":1"))


class ParseProbeOutputTest(unittest.TestCase):
    def test_parses_all_fields(self):
        parsed = jar_probe.parse_probe_output(SAMPLE, "tok",
                                             ["hqb-auth", "other"])
        self.assertEqual(parsed["uptime"], 10000.0)
        self.assertEqual(parsed["clk_tck"], 100)
        u = parsed["units"]["hqb-auth"]
        self.assertEqual(u["load_state"], "loaded")
        self.assertEqual(u["active_state"], "active")
        self.assertEqual(u["pid"], 4242)
        self.assertEqual(u["restarts"], 2)
        self.assertEqual(u["active_enter_mono"], 9600000000.0)
        self.assertEqual(u["version"], "1.0.1")
        self.assertEqual(u["link_version"], "1.0.1")
        self.assertEqual(u["cpu_ticks"], 700)
        self.assertEqual(u["rss_pages"], 20480)
        self.assertEqual(parsed["units"]["other"]["load_state"], "not-found")

    def test_missing_expected_unit_gets_not_found(self):
        parsed = jar_probe.parse_probe_output("", "tok", ["ghost"])
        self.assertEqual(parsed["units"]["ghost"]["load_state"], "not-found")
        self.assertEqual(parsed["units"]["ghost"]["version"], "")


class BuildViewTest(unittest.TestCase):
    def _raw(self, **kw):
        raw = {"load_state": "loaded", "active_state": "active",
               "sub_state": "running", "pid": 4242, "restarts": 1,
               "active_enter_mono": 3600_000_000, "version": "1.0.1",
               "link_version": "", "cpu_ticks": 700, "rss_pages": 25600}
        raw.update(kw)
        return raw

    def test_online_view(self):
        view = jar_probe.build_view(self._raw(), proc_uptime=7200.0, now=100.0)
        self.assertEqual(view["status"], "online")
        self.assertEqual(view["version"], "1.0.1")
        self.assertEqual(view["uptime_hours"], 1.0)
        self.assertEqual(view["rss_mb"], 100.0)
        self.assertIn("sample", view)

    def test_version_falls_back_to_current_link(self):
        view = jar_probe.build_view(self._raw(version="", link_version="1.0.9"))
        self.assertEqual(view["version"], "1.0.9")

    def test_not_found_and_stopped(self):
        self.assertEqual(jar_probe.build_view(
            self._raw(load_state="not-found", active_state="inactive")
        )["status"], "not_deployed")
        self.assertEqual(jar_probe.build_view(
            self._raw(active_state="failed", sub_state="failed")
        )["status"], "stopped")

    def test_no_uptime_when_monotonic_missing(self):
        view = jar_probe.build_view(self._raw(active_enter_mono=0))
        self.assertIsNone(view["uptime_hours"])


class CpuPctTest(unittest.TestCase):
    def test_normal_delta(self):
        prev = {"pid": 1, "cpu_ticks": 100, "ts": 100.0}
        self.assertEqual(jar_probe.compute_cpu_pct(prev, 1, 250, 103.0, 100), 50.0)

    def test_pid_change_or_no_baseline(self):
        self.assertIsNone(jar_probe.compute_cpu_pct(None, 1, 100, 2.0))
        self.assertIsNone(jar_probe.compute_cpu_pct(
            {"pid": 1, "cpu_ticks": 10, "ts": 1.0}, 2, 20, 2.0))
        self.assertIsNone(jar_probe.compute_cpu_pct(
            {"pid": 1, "cpu_ticks": 10, "ts": 1.0}, 1, 5, 2.0))


class AggregateHostsTest(unittest.TestCase):
    def test_status_precedence_and_version(self):
        agg = jar_probe.aggregate_hosts([
            {"host": "b", "status": "stopped", "version": "1.0.0"},
            {"host": "a", "status": "online", "version": "1.0.1"},
        ])
        self.assertEqual([h["host"] for h in agg["hosts"]], ["a", "b"])
        self.assertEqual(agg["status"], "online")
        self.assertEqual(agg["version"], "1.0.1")
        self.assertEqual(agg["online_hosts"], 1)

    def test_empty(self):
        agg = jar_probe.aggregate_hosts([])
        self.assertEqual(agg, {"status": "unknown", "version": "",
                               "hosts": [], "online_hosts": 0})


if __name__ == "__main__":
    unittest.main()
