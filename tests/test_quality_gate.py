"""质量门禁脚本自身的用例（P2-6）

门禁是「发布前最后一道闸」，它自己必须：
  1. 语法步能真的抓到语法错误（不能只看目录是否存在）；
  2. 保持只读——不往源码树写 `__pycache__`（便携版打包把 `__pycache__` 列为
     禁入物，跑一次门禁就让工作区变"脏"是不可接受的副作用）；
  3. `--strict` 能把「工具缺失」从 SKIP 变成 FAIL（CI 靠它区分"过了"与"没跑"，
     工作流侧的契约见 tests/test_ci_quality_gate.py）；
  4. 本仓库真的能过静态检查——门禁生效的第一天就要有人被它拦住。

安全约束：只在临时目录里造文件，不碰仓库源码。
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import quality_gate


class StepCompileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gate_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_detects_syntax_error(self):
        pkg = self.tmp / "broken"
        pkg.mkdir()
        (pkg / "a.py").write_text("def f(:\n", encoding="utf-8")
        status, detail = quality_gate._compile_path(pkg)
        self.assertEqual(status, "bad")
        self.assertIn("a.py", detail)

    def test_accepts_valid_sources(self):
        pkg = self.tmp / "ok"
        pkg.mkdir()
        (pkg / "a.py").write_text("X = 1\n", encoding="utf-8")
        status, detail = quality_gate._compile_path(pkg)
        self.assertEqual(status, "ok", msg=detail)

    def test_does_not_write_bytecode_into_source_tree(self):
        (self.tmp / "m.py").write_text("print('hi')\n", encoding="utf-8")
        quality_gate._compile_path(self.tmp)
        self.assertEqual(list(self.tmp.rglob("__pycache__")), [],
                         "语法检查不得在源码树留字节码产物")
        # 单个文件同样不落 __pycache__
        (self.tmp / "broken.py").write_text("def f(:\n", encoding="utf-8")
        quality_gate._compile_path(self.tmp / "broken.py")
        self.assertEqual(list(self.tmp.rglob("__pycache__")), [])


class ProjectTargetsTest(unittest.TestCase):
    def test_all_declared_targets_exist(self):
        """PY_TARGETS 里的路径必须都在（改名/删目录后门禁不能假绿）"""
        missing = [t for t in quality_gate.PY_TARGETS if not (quality_gate.ROOT / t).exists()]
        self.assertEqual(missing, [])

    def test_step_compile_passes_on_this_repo(self):
        status, detail = quality_gate.step_compile()
        self.assertEqual(status, quality_gate.PASS, msg=detail)

    def test_js_tests_exist(self):
        """前端无头用例目录不能为空：为空时 js 步会假绿（只做了语法检查）"""
        self.assertTrue(quality_gate.JS_TESTS,
                        "tests/js/*_test.js 缺失，前端检查形同虚设")


class StepStrictFlagTest(unittest.TestCase):
    """--strict 是 CI 唯一能区分「检查通过」与「检查没跑」的开关

    本地门禁对缺工具宽容（记 SKIP，不该因环境阻断发布）；CI 里 ruff/node 属于
    应当齐备的依赖，缺失必须 FAIL——否则整条 lint 链可以长期空转而全绿。
    """

    def test_lint_step_fails_when_ruff_missing_only_under_strict(self):
        real = quality_gate.import_module

        def fake_import(name):
            if name == "ruff":
                raise ImportError("no ruff")
            return real(name)

        quality_gate.import_module = fake_import
        try:
            self.assertEqual(quality_gate.step_lint(strict=False)[0], quality_gate.SKIP)
            self.assertEqual(quality_gate.step_lint(strict=True)[0], quality_gate.FAIL)
        finally:
            quality_gate.import_module = real

    def test_js_step_fails_when_node_missing_only_under_strict(self):
        real_which = quality_gate.shutil.which
        quality_gate.shutil.which = lambda _name: None
        try:
            self.assertEqual(quality_gate.step_js(strict=False)[0], quality_gate.SKIP)
            self.assertEqual(quality_gate.step_js(strict=True)[0], quality_gate.FAIL)
        finally:
            quality_gate.shutil.which = real_which


class RepoLintCleanTest(unittest.TestCase):
    """门禁是可执行规范：仓库自己必须过 ruff，否则 CI 首跑即红

    在本地就红，比等 CI 早一轮；未装 ruff 的机器跳过——与门禁对缺工具的宽容口径一致。
    """

    def test_repo_is_lint_clean(self):
        try:
            import ruff  # noqa: F401
        except ImportError:
            self.skipTest("未安装 ruff（pip install -r requirements-dev.txt）")
        status, detail = quality_gate.step_lint(strict=True)
        self.assertEqual(status, quality_gate.PASS, msg=detail[-3000:])


if __name__ == "__main__":
    unittest.main()
