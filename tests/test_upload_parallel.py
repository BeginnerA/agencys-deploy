# tests/test_upload_parallel.py
"""直传模式 SFTP 并行分片上传测试：多通道写独立 offset、校验与失败清理"""
import io
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from deploy import Deployer, RemoteServer  # noqa: E402

REMOTE_DIR = "/tmp/platform-deploy"


class FakeHandle:
    """模拟 SFTPFile：内存 bytearray 上的 offset 写（锁串行化模拟服务端单进程处理）"""

    def __init__(self, store, lock, path, mode, drop_half=False):
        self.store = store
        self.lock = lock
        self.path = path
        self.drop_half = drop_half
        with lock:
            if "w" in mode and "+" not in mode:  # 'wb'：创建并截断
                store[path] = bytearray()
            elif path not in store:  # 'r+b'：要求文件已存在（先有 'wb' 通道创建）
                raise IOError(2, f"no such file: {path}")
        self.pos = 0

    def set_pipelined(self, pipelined=True):
        pass

    def seek(self, offset, whence=0):
        self.pos = offset

    def write(self, data):
        if self.drop_half:
            data = data[:len(data) // 2]  # 模拟服务端丢写：大小校验应失败
        with self.lock:
            buf = self.store[self.path]
            end = self.pos + len(data)
            if end > len(buf):
                buf.extend(b"\x00" * (end - len(buf)))
            buf[self.pos:end] = data
            self.pos = end

    def close(self):
        pass


class FakeChannel:
    """模拟 SFTP 通道：记录 settimeout 调用"""

    def __init__(self):
        self.timeout = None

    def settimeout(self, t):
        self.timeout = t


class FakeSFTP:
    """模拟 SFTPClient：store 为远端文件系统（多实例共享同一 store）"""

    def __init__(self, store, fail_paths=None):
        self.store = store
        self.lock = threading.Lock()
        self.fail_paths = fail_paths or set()
        self._channel = FakeChannel()

    def get_channel(self):
        return self._channel

    def open(self, path, mode="r"):
        return FakeHandle(self.store, self.lock, path, mode,
                          drop_half=path in self.fail_paths)

    def stat(self, path):
        if path not in self.store:
            raise IOError(2, f"no such file: {path}")
        return types.SimpleNamespace(st_size=len(self.store[path]))

    def remove(self, path):
        with self.lock:
            if path not in self.store:
                raise IOError(2, f"no such file: {path}")
            del self.store[path]

    def rename(self, src, dst):
        with self.lock:
            if src not in self.store:
                raise IOError(2, f"no such file: {src}")
            self.store[dst] = self.store.pop(src)

    def put(self, local_path, remote_path, callback=None, confirm=True):
        """单流上传：与真实 SFTPClient.put 同签名，供单流回退路径测试"""
        data = Path(local_path).read_bytes()
        with self.lock:
            self.store[remote_path] = bytearray(data)
        if callback:
            callback(len(data), len(data))


class FakeTransport:
    """模拟 Transport：记录 default_window_size 调优是否生效"""

    def __init__(self):
        self.default_window_size = None


class FakeSSHClient:
    """模拟 SSHClient：open_sftp 每次返回独立 channel（共享远端 store）"""

    def __init__(self, store, fail_paths=None):
        self.store = store
        self.fail_paths = fail_paths
        self.open_calls = 0
        self._transport = FakeTransport()

    def get_transport(self):
        return self._transport

    def open_sftp(self):
        self.open_calls += 1
        return FakeSFTP(self.store, fail_paths=self.fail_paths)


def make_local_file(payload: bytes) -> Path:
    local = Path(tempfile.mkdtemp()) / "img_1.0.0.tar.gz"
    local.write_bytes(payload)
    return local


class TestParallelUpload(unittest.TestCase):
    """分片并行上传：内容一致性、改名落盘、失败清理"""

    def setUp(self):
        self.store = {}
        self.rs = RemoteServer("10.0.0.9", {})
        self.fake_ssh = FakeSSHClient(self.store)
        mock.patch.object(self.rs, "client", return_value=self.fake_ssh).start()
        self.addCleanup(mock.patch.stopall)
        # 256KB 数据足够分片；并行阈值 patch 为小值，避免真实 64MB 数据
        self.payload = bytes(range(256)) * 1024
        self.local = make_local_file(self.payload)
        self.remote = f"{REMOTE_DIR}/img_1.0.0.tar.gz"

    def _put(self, streams=4):
        with mock.patch("deploy._SFTP_PARALLEL_MIN_SIZE", 1024), \
                mock.patch("deploy.log"):
            self.rs.sftp_put_with_progress(str(self.local), self.remote,
                                           label="img_1.0.0.tar.gz", streams=streams)

    def test_parallel_roundtrip_content_matches(self):
        """4 通道并行上传：远端内容与本地逐字节一致，.part 改名后不残留"""
        self._put(streams=4)
        self.assertEqual(bytes(self.store[self.remote]), self.payload)
        self.assertNotIn(self.remote + ".part", self.store)

    def test_parallel_channels_used(self):
        """并行路径确实多开了 channel：清理 + 4 个分片通道共 5 次 open_sftp"""
        self._put(streams=4)
        self.assertEqual(self.fake_ssh.open_calls, 5)

    def test_streams_clamped_to_8(self):
        """streams 超上限钳制为 8：分片数 = 8 且内容仍一致"""
        self._put(streams=100)
        self.assertEqual(self.fake_ssh.open_calls, 9)  # 清理 1 + 分片 8
        self.assertEqual(bytes(self.store[self.remote]), self.payload)

    def test_single_stream_uses_put(self):
        """streams=1 走传统单流 put：仅清理阶段 1 次 open_sftp"""
        self._put(streams=1)
        self.assertEqual(self.fake_ssh.open_calls, 1)
        self.assertEqual(bytes(self.store[self.remote]), self.payload)

    def test_small_file_falls_back_to_single_stream(self):
        """文件小于阈值：即使 streams=4 也回退单流（不 patch 阈值，256KB < 64MB）"""
        with mock.patch("deploy.log"):
            self.rs.sftp_put_with_progress(str(self.local), self.remote, streams=4)
        self.assertEqual(self.fake_ssh.open_calls, 1)
        self.assertEqual(bytes(self.store[self.remote]), self.payload)

    def test_size_mismatch_cleans_part_and_raises(self):
        """服务端丢写导致远端大小不一致：抛异常且清理 .part，不落正式名"""
        self.fake_ssh.fail_paths = {self.remote + ".part"}
        with self.assertRaises(RuntimeError) as ctx:
            self._put(streams=4)
        self.assertIn("校验失败", str(ctx.exception))
        self.assertNotIn(self.remote + ".part", self.store)
        self.assertNotIn(self.remote, self.store)

    def test_channel_failure_cleans_part_and_raises(self):
        """单通道写失败：抛异常且清理 .part"""
        broken = FakeSSHClient(self.store)
        original = broken.open_sftp

        def open_sftp_fail_on_channel3():
            broken.open_calls += 1
            sftp = original()
            if broken.open_calls == 4:  # 第 4 个 channel 打开时直接失败
                raise IOError(11, "connection reset")
            return sftp
        broken.open_sftp = open_sftp_fail_on_channel3
        mock.patch.object(self.rs, "client", return_value=broken).start()
        with self.assertRaises(RuntimeError) as ctx:
            self._put(streams=4)
        self.assertIn("并行上传", str(ctx.exception))
        self.assertNotIn(self.remote + ".part", self.store)
        self.assertNotIn(self.remote, self.store)

    def test_progress_log_emitted(self):
        """并行上传过程输出进度与完成日志"""
        with mock.patch("deploy._SFTP_PARALLEL_MIN_SIZE", 1024), \
                mock.patch("deploy.log") as log_mock:
            self.rs.sftp_put_with_progress(str(self.local), self.remote,
                                           label="img_1.0.0.tar.gz", streams=4)
        texts = [str(c) for c in log_mock.call_args_list]
        self.assertTrue(any("4 通道并行" in t for t in texts))
        self.assertTrue(any("上传完成" in t for t in texts))

    def test_transport_window_tuned(self):
        """sftp() 建立 SFTP 前调大 transport 窗口（下载方向提速）"""
        import paramiko
        self.rs.sftp()  # 触发缓存建立
        self.assertEqual(self.fake_ssh.get_transport().default_window_size,
                         paramiko.common.MAX_WINDOW_SIZE)


class TestTransferWiring(unittest.TestCase):
    """_transfer_image_to_server → sftp_put_with_progress 的 streams 接线"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = Deployer.__new__(Deployer)  # 绕过 __init__：upload_streams 取类默认
        self.d.image_prefix = ""
        self.d.image_name = "app"
        self.d.version = "1.0.30"
        self.d.dist_dir = Path(self.tmp)
        self.d.remote_tmp_dir = REMOTE_DIR
        self.d._save_lock = threading.Lock()

    def test_upload_streams_passthrough(self):
        """调用镜像包上传时透传 upload_streams（类默认 4）"""
        server = mock.Mock()
        # 两次查询：传输前 None（触发上传）、docker load 后校验返回一致
        server.image_id.side_effect = [None, "sha256:new"]
        proc = types.SimpleNamespace(stdout=io.BytesIO(b"data"),
                                     stderr=io.BytesIO(b""), wait=lambda: 0)
        with mock.patch("deploy.local_image_id", return_value="sha256:new"), \
                mock.patch("deploy.tar_image_id", return_value="sha256:new"), \
                mock.patch("deploy.subprocess.Popen", return_value=proc), \
                mock.patch("deploy.log"):
            self.d._transfer_image_to_server(server)
        server.sftp_put_with_progress.assert_called_once()
        self.assertEqual(server.sftp_put_with_progress.call_args.kwargs.get("streams"), 4)

    def test_compress_level_passthrough(self):
        """docker save 导出 gzip 时使用 image_compress_level（类默认 6）"""
        proc = types.SimpleNamespace(stdout=io.BytesIO(b"data"),
                                     stderr=io.BytesIO(b""), wait=lambda: 0)
        with mock.patch("deploy.local_image_id", return_value="sha256:new"), \
                mock.patch("deploy.tar_image_id", return_value=None), \
                mock.patch("deploy.subprocess.Popen", return_value=proc), \
                mock.patch("gzip.open") as gzip_open, \
                mock.patch("deploy.log"):
            self.d._ensure_local_tar()
        self.assertEqual(gzip_open.call_args.kwargs.get("compresslevel"), 6)

    def test_compress_level_override(self):
        """实例覆盖 image_compress_level=9 时 gzip 透传 9"""
        self.d.image_compress_level = 9
        proc = types.SimpleNamespace(stdout=io.BytesIO(b"data"),
                                     stderr=io.BytesIO(b""), wait=lambda: 0)
        with mock.patch("deploy.local_image_id", return_value="sha256:new"), \
                mock.patch("deploy.tar_image_id", return_value=None), \
                mock.patch("deploy.subprocess.Popen", return_value=proc), \
                mock.patch("gzip.open") as gzip_open, \
                mock.patch("deploy.log"):
            self.d._ensure_local_tar()
        self.assertEqual(gzip_open.call_args.kwargs.get("compresslevel"), 9)


if __name__ == "__main__":
    unittest.main()
