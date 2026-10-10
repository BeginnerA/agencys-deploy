# tests/test_monitor_perf.py
"""监控/日志采集效率优化回归测试

覆盖本次性能改造的核心行为：
1. _parse_host_metrics：/proc/stat 累计值（cpu_raw）解析（取代 vmstat 采样）
2. _apply_cpu_delta：跨轮差分 CPU 使用率（含首轮无基准 / 计数回绕）
3. _build_extended_cmds：扩展指标 4 条命令（合并 sleep、单 ss、删 SSL 死代码）
4. _parse_docker_resources：仅保留服务容器
5. _parse_container_status：3 字段格式兼容 + uptime/restart 解析
6. 主机去重：同主机多环境只探测 1 次、服务名取并集、快照仅写新鲜轮
7. _split_marker_output：单通道批量采集的 @@LOGCUT 分段
8. 日志批量采集：单主机 1 条 SSH + 同名环境去重（文件不重复追加）
9. save_response_time 清理节流（600s 窗口）
"""
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from webapp import db as dbm
from webapp.log_collector import LogCollector, _marker, _split_marker_output
from webapp.monitor import (
    MonitorScheduler, _build_extended_cmds, _parse_container_status,
    _parse_docker_resources, _parse_host_metrics,
)


class TestHostMetricsParse(unittest.TestCase):
    """_parse_host_metrics：/proc/stat 首行 → cpu_raw（差分基准）"""

    def test_cpu_raw_and_metrics(self):
        out = (
            "cpu  1000 20 300 8000 50 0 40 0 0 0\n"
            "0.50 0.40 0.30 1/100 12345\n"
            "4\n"
            "MemTotal:       16000000 kB\n"
            "MemFree:         4000000 kB\n"
            "Buffers:          500000 kB\n"
            "Cached:          3500000 kB\n"
            "/dev/vda1      10485760 5242880 5242880  50% /\n"
        )
        m = _parse_host_metrics(out)
        # idle = 8000 + 50 = 8050；total = 9410；busy = 1360
        self.assertEqual(m["cpu_raw"], {"busy": 1360, "total": 9410})
        # cpu 由差分计算，解析阶段保持 None
        self.assertIsNone(m["cpu"])
        # mem = (16000000 - 8000000) / 16000000
        self.assertEqual(m["mem"], 50.0)
        self.assertEqual(m["disk"], 50.0)
        # load = 0.50 / 4 核 = 12.5%
        self.assertEqual(m["load"], 12.5)

    def test_no_cpu_line(self):
        out = ("0.10 0.20 0.30 1/200 9999\n"
               "2\n"
               "MemTotal: 1000 kB\n"
               "MemFree: 400 kB\n")
        m = _parse_host_metrics(out)
        self.assertIsNone(m["cpu_raw"])
        self.assertIsNone(m["cpu"])
        self.assertEqual(m["mem"], 60.0)
        self.assertIsNone(m["disk"])


class TestApplyCpuDelta(unittest.TestCase):
    """_apply_cpu_delta：两轮 /proc/stat 累计值差分（无 sleep 采样）"""

    def setUp(self):
        self.sched = MonitorScheduler(":memory:", 30)

    def test_first_round_no_baseline(self):
        hm = {"cpu": None, "cpu_raw": {"busy": 100, "total": 1000}}
        self.sched._apply_cpu_delta("1.2.3.4", hm)
        # 首轮无基准：cpu 保持 None，且 cpu_raw 作为内部状态被移除
        self.assertIsNone(hm["cpu"])
        self.assertNotIn("cpu_raw", hm)

    def test_second_round_delta(self):
        self.sched._apply_cpu_delta("1.2.3.4",
                                    {"cpu": None, "cpu_raw": {"busy": 100, "total": 1000}})
        hm = {"cpu": None, "cpu_raw": {"busy": 350, "total": 2000}}
        self.sched._apply_cpu_delta("1.2.3.4", hm)
        # d_busy=250, d_total=1000 → 25.0%
        self.assertEqual(hm["cpu"], 25.0)

    def test_counter_wraparound_discarded(self):
        self.sched._apply_cpu_delta("1.2.3.4",
                                    {"cpu": None, "cpu_raw": {"busy": 100, "total": 1000}})
        hm = {"cpu": None, "cpu_raw": {"busy": 5, "total": 10}}  # 主机重启后计数回绕
        self.sched._apply_cpu_delta("1.2.3.4", hm)
        self.assertIsNone(hm["cpu"])


class TestBuildExtendedCmds(unittest.TestCase):
    """_build_extended_cmds：4 条命令，共享 1 次 sleep，单次 ss，无 SSL 死代码"""

    def test_structure(self):
        cmds = _build_extended_cmds()
        self.assertEqual(len(cmds), 4)
        joined = "\n".join(cmds)
        # 删除 SSL 探测死代码（health_hosts 恒为空，证书检查由平台侧 ssl_monitor 负责）
        self.assertNotIn("openssl", joined)
        # 网络 + 磁盘 IO 共享一次 sleep 1（原为两次独立采样窗口）
        self.assertEqual(joined.count("sleep 1"), 1)
        # TCP 分布：单 ss + awk 聚合（原为 3 个 ss 进程）
        self.assertEqual(joined.count("ss -tan"), 1)


class TestParseDockerResources(unittest.TestCase):
    """_parse_docker_resources：仅保留服务容器（stats 已按服务名过滤）"""

    def test_filter_by_service_names(self):
        out = ("svc-a|12.5%|30.0%\n"
               "infra-nginx|99.9%|88.8%\n"
               "svc-b|0.5%|1.2%\n")
        res = _parse_docker_resources(out, {"svc-a", "svc-b"})
        self.assertEqual(set(res), {"svc-a", "svc-b"})
        self.assertEqual(res["svc-a"], {"cpu": 12.5, "mem": 30.0})
        self.assertEqual(res["svc-b"], {"cpu": 0.5, "mem": 1.2})


class TestParseContainerStatus(unittest.TestCase):
    """_parse_container_status：兼容 2/3 字段格式 + uptime/restart 解析"""

    def test_running_with_three_fields(self):
        out = "hqb-auth|Up 3 days (healthy)|reg/x:1.0.1\n"
        res = _parse_container_status(out)
        self.assertEqual(res["hqb-auth"]["uptime_hours"], 72.0)
        self.assertEqual(res["hqb-auth"]["restart_count"], 0)

    def test_restart_count_parsed(self):
        out = "svc-x|Up 5 hours (restarted 2 times)|img\n"
        res = _parse_container_status(out)
        self.assertEqual(res["svc-x"]["restart_count"], 2)
        self.assertEqual(res["svc-x"]["uptime_hours"], 5.0)

    def test_stopped_container(self):
        out = "svc-y|Exited (0) 2 hours ago|img\n"
        res = _parse_container_status(out)
        self.assertEqual(res["svc-y"]["restart_count"], 1)
        self.assertEqual(res["svc-y"]["uptime_hours"], -1)

    def test_two_field_legacy_format(self):
        out = "svc-z|Up 30 minutes\n"
        res = _parse_container_status(out)
        self.assertEqual(res["svc-z"]["uptime_hours"], 0.5)


def _make_project(db, name):
    return dbm.create_record(db, "projects", dict(
        name=name, code=name, description="", git_org="",
        created_at=int(time.time())))


def _make_env(db, proj_id, name="prod"):
    return dbm.create_record(db, "environments", dict(
        project_id=proj_id, name=name, is_default=1,
        registry="{}", nacos="{}", mysql_snailjob="{}",
        ssh='{"user":"root","password":"envpw"}',
        build="{}", deploy="{}", monitor_enabled=1))


def _make_svc(db, proj_id, service_name):
    return dbm.create_record(db, "services", dict(
        project_id=proj_id, name=service_name, image_name=service_name,
        service_name=service_name, docker_context="", dockerfile="",
        build_order="[]", nacos_groups="[]", dubbo_services="[]",
        snailjob_group="", health_url="", health_port=0,
        log_error_keywords="[]", clear_log_dir="", git_repo="", enabled=1))


class TestProbeDedup(unittest.TestCase):
    """主机跨环境去重 + 快照只写新鲜轮（集成：_probe_envs → _probe_env）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db_path)
        # 两个项目各建同名 "prod" 环境（共用同一台服务器 1.2.3.4）
        self.proj1 = _make_project(self.db_path, "perf-p1")
        self.proj2 = _make_project(self.db_path, "perf-p2")
        self.env1 = _make_env(self.db_path, self.proj1, "prod")
        self.env2 = _make_env(self.db_path, self.proj2, "prod")
        _make_svc(self.db_path, self.proj1, "svc-a")
        _make_svc(self.db_path, self.proj2, "svc-b")
        srv = dbm.create_record(self.db_path, "servers", dict(
            host="1.2.3.4", ssh_port=22, ssh_user="root", ssh_password="pw",
            ssh_key_file="", nacos_ip="", remark="", enabled=1))
        dbm.bind_server(self.db_path, self.env1, srv)
        dbm.bind_server(self.db_path, self.env2, srv)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _install_fake_probe(self):
        calls = []
        uptime_seq = iter([12.0, 13.0])

        def fake_probe(host, port, ssh_cfg, service_names, timeout=10,
                       skip_extended=False):
            calls.append({"host": host, "service_names": set(service_names),
                          "skip_extended": skip_extended})
            up = next(uptime_seq)
            result = {
                "ssh_ok": True,
                "containers": {"svc-a": "reg/svc-a:1.0.0",
                               "svc-b": "reg/svc-b:2.0.0"},
                "containers_stopped": {},
                "container_list": ["svc-a", "svc-b"],
                # 第一轮 ps -a 输出每轮新鲜（节流轮也更新运行时长）
                "container_status": {
                    "svc-a": {"restart_count": 0, "uptime_hours": up},
                    "svc-b": {"restart_count": 0, "uptime_hours": up},
                },
                "resources": {"svc-a": {"cpu": 1.0, "mem": 2.0},
                              "svc-b": {"cpu": 3.0, "mem": 4.0}},
                "host_metrics": {"cpu": 10.0, "mem": 30.0,
                                 "disk": 40.0, "load": 5.0},
                "extended": {},
                "error": "",
            }
            if not skip_extended:
                result["extended"] = {
                    "swap": 10.0, "net_rx": 100, "net_tx": 200,
                    "disk_read": 5, "disk_write": 6,
                    "tcp": {"ESTABLISHED": 10, "TIME_WAIT": 1, "CLOSE_WAIT": 0},
                    "docker_df": {"images": 1, "containers": 2, "volumes": 3},
                }
            return result

        return calls, fake_probe

    def test_host_dedup_and_snapshot_throttle(self):
        calls, fake_probe = self._install_fake_probe()
        sched = MonitorScheduler(self.db_path, 30)
        with mock.patch("webapp.monitor._probe_server_full",
                        side_effect=fake_probe) as fp, \
                mock.patch("webapp.db.save_monitor_snapshot") as snap:
            # 新鲜轮：_round 2 → 3（3 % 3 == 0 → 采集扩展指标）
            sched._round = 2
            sched._cycle()
            # 两个环境绑定同一主机 → 只探测 1 次，服务名为并集，扩展指标新鲜
            self.assertEqual(fp.call_count, 1)
            args = fp.call_args[0]
            self.assertEqual(args[0], "1.2.3.4")
            self.assertEqual(args[3], {"svc-a", "svc-b"})
            self.assertIs(args[5], False)
            # 两个环境各自组装视图（不含重复 SSH）
            ov1 = sched.get_overview(self.proj1, "prod")
            ov2 = sched.get_overview(self.proj2, "prod")
            self.assertEqual(ov1["services"]["svc-a"]["status"], "online")
            self.assertEqual(ov1["services"]["svc-a"]["version"], "1.0.0")
            self.assertEqual(ov2["services"]["svc-b"]["status"], "online")
            # 容器列表缓存供日志采集复用
            self.assertEqual(sched._server_containers["1.2.3.4"]["containers"],
                             ["svc-a", "svc-b"])
            # 新鲜轮：两个环境各落 1 次快照
            self.assertEqual(snap.call_count, 2)

            # 节流轮：_round 3 → 4（跳过扩展指标）
            sched._cycle()
            self.assertEqual(fp.call_count, 2)
            self.assertIs(fp.call_args[0][5], True)
            # 节流轮不落库（写放大 3× → 1×）
            self.assertEqual(snap.call_count, 2)
            # 缓存仍持续更新：uptime 变新鲜、swap 保留上一轮值
            ext = sched._cache[self.env1]["extended_metrics"]["1.2.3.4"]
            self.assertEqual(ext["container_status"]["svc-a"]["uptime_hours"], 13.0)
            self.assertEqual(ext["swap"], 10.0)


class TestSplitMarkerOutput(unittest.TestCase):
    """_split_marker_output：@@LOGCUT 单通道分段"""

    def test_split(self):
        out = ("@@LOGCUT:t1:c1\nline1\nline2\n"
               "@@LOGCUT:t1:c2\n"
               "@@LOGCUT:t1:c3\nline3\n")
        res = _split_marker_output(out, "t1", ["c1", "c2", "c3", "c4"])
        self.assertEqual(res["c1"], "line1\nline2\n")
        self.assertEqual(res["c2"], "")       # 标记存在但无日志 → 空拉取
        self.assertEqual(res["c3"], "line3\n")
        self.assertIsNone(res["c4"])          # 标记缺失 → 失败重试

    def test_marker_helper(self):
        self.assertEqual(_marker("abc", "api"), "@@LOGCUT:abc:api")


class TestLogBatchCollect(unittest.TestCase):
    """日志批量采集：单主机单通道 + 同名环境去重（集成：_collect_round）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.log_dir = os.path.join(self.tmp, "logs")
        # 两个项目的同名环境 "prod" 共用同一台服务器 9.9.9.9
        proj1 = _make_project(self.db, "log-p1")
        proj2 = _make_project(self.db, "log-p2")
        env1 = _make_env(self.db, proj1, "prod")
        env2 = _make_env(self.db, proj2, "prod")
        srv = dbm.create_record(self.db, "servers", dict(
            host="9.9.9.9", ssh_port=22, ssh_user="root", ssh_password="pw",
            ssh_key_file="", nacos_ip="", remark="", enabled=1))
        dbm.bind_server(self.db, env1, srv)
        dbm.bind_server(self.db, env2, srv)
        self.ssh_calls = []

        class _FakeMonitor:
            _server_containers = {
                "9.9.9.9": {"containers": ["c1", "c2"], "ts": time.time()}}

        self.ssh_output = (
            "@@LOGCUT:deadbeef:c1\n"
            "2026-08-18T10:30:01.123456789Z hello-c1\n"
            "@@LOGCUT:deadbeef:c2\n"
            "2026-08-18T10:30:02.123456789Z hello-c2\n")
        self.collector = LogCollector(self.db, self.log_dir, interval=30,
                                      ssh_func=self._fake_ssh,
                                      monitor=_FakeMonitor())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_ssh(self, host, port, ssh_cfg, cmd, timeout=10):
        self.ssh_calls.append({"host": host, "cmd": cmd})
        return self.ssh_output

    def test_batch_dedup_same_env_name(self):
        with mock.patch("webapp.log_collector.secrets.token_hex",
                        return_value="deadbeef"):
            self.collector._collect_round()
        # 单主机 1 条 SSH（原为 环境数 × 容器数 条），两容器各一条 docker logs
        self.assertEqual(len(self.ssh_calls), 1)
        cmd = self.ssh_calls[0]["cmd"]
        self.assertEqual(cmd.count("docker logs"), 2)
        self.assertEqual(cmd.count("@@LOGCUT:deadbeef:"), 2)
        self.assertIn("--since 5m", cmd)  # 首轮无游标 → 5m 窗口
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = os.path.join(self.log_dir, "collect", "prod", "c1",
                            f"{today}.log")
        content = Path(path).read_text(encoding="utf-8")
        # 同名环境去重：同一份日志只追加 1 次（未去重会重复追加）
        self.assertEqual(content.count("hello-c1"), 1)
        conn = dbm.get_conn(self.db)
        try:
            rows = conn.execute(
                "SELECT env, container, last_ts FROM log_scan_state"
                " ORDER BY container").fetchall()
        finally:
            conn.close()
        # 同名环境去重后每容器仅 1 行游标，且游标为 +1μs 精确值
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["env"], "prod")
        self.assertEqual(rows[0]["last_ts"], "2026-08-18T10:30:01.123457+00:00")

    def test_missing_marker_records_failure(self):
        # c2 标记缺失（命令中途失败/容器消失）→ 记失败，c1 不受影响
        self.ssh_output = ("@@LOGCUT:deadbeef:c1\n"
                           "2026-08-18T10:30:01.123456789Z hello-c1\n")
        with mock.patch("webapp.log_collector.secrets.token_hex",
                        return_value="deadbeef"):
            self.collector._collect_round()
        self.assertIn(("prod", "9.9.9.9", "c2"), self.collector._fail_counts)
        self.assertNotIn(("prod", "9.9.9.9", "c1"), self.collector._fail_counts)


class TestResponseTimeCleanupThrottle(unittest.TestCase):
    """save_response_time：过期清理 600s 节流（原每次写入全表 DELETE）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        dbm._rth_last_cleanup = 0.0

    def tearDown(self):
        dbm._rth_last_cleanup = 0.0
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _insert_expired(self, days=8):
        conn = dbm.get_conn(self.db)
        try:
            conn.execute(
                "INSERT INTO response_time_history"
                " (env_id, service, host, response_ms, ok, created_at)"
                " VALUES (1, 's', 'h', 10, 1, ?)",
                (int(time.time()) - days * 86400,))
            conn.commit()
        finally:
            conn.close()

    def _count(self):
        conn = dbm.get_conn(self.db)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM response_time_history").fetchone()[0]
        finally:
            conn.close()

    def test_first_call_cleans_expired(self):
        self._insert_expired()
        dbm.save_response_time(self.db, 1, "s", "h", 12.0, True)
        # 插入 1 行 + 清理 1 行过期 → 仅剩新行
        self.assertEqual(self._count(), 1)

    def test_second_call_within_window_keeps_expired(self):
        dbm.save_response_time(self.db, 1, "s", "h", 12.0, True)  # 首次清理
        self._insert_expired()
        dbm.save_response_time(self.db, 1, "s", "h", 13.0, True)  # 600s 内
        # 过期行未被清理（1 首行 + 1 过期行 + 1 新行）
        self.assertEqual(self._count(), 3)


if __name__ == "__main__":
    unittest.main()
