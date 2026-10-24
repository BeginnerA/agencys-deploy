"""前端 Markdown 渲染安全回归：调用 Node 执行 tests/js/md_to_html_test.js"""
import shutil
import subprocess
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_NODE = shutil.which("node")


@unittest.skipUnless(_NODE, "node 不可用，跳过前端渲染测试")
class TestMarkdownRender(unittest.TestCase):

    def test_md_to_html_escapes_link_attributes(self):
        proc = subprocess.run(
            [_NODE, str(_ROOT / "tests" / "js" / "md_to_html_test.js")],
            cwd=_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
