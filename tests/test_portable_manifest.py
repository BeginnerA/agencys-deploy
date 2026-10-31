"""便携包白名单必须覆盖运行期导入的根模块（含懒导入）。"""
import ast
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_SPEC = importlib.util.spec_from_file_location(
    "package_portable", ROOT / "scripts" / "package_portable.py")
pp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pp)

ROOT_MODULES = {p.stem for p in ROOT.glob("*.py")}
SCAN_DIRS = ("webapp", "deploy_strategies", "agent")


class PortableManifestTest(unittest.TestCase):
    def test_whitelisted_files_exist(self):
        for name in pp.SOURCE_FILES:
            self.assertTrue((ROOT / name).is_file(), name)

    def test_runtime_root_modules_are_packaged(self):
        missing = set()
        for dirname in SCAN_DIRS:
            for path in sorted((ROOT / dirname).rglob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [a.name for a in node.names]
                    elif (isinstance(node, ast.ImportFrom) and node.level == 0
                          and node.module):
                        names = [node.module]
                    for name in names:
                        top = name.split(".")[0]
                        if top in ROOT_MODULES and top + ".py" not in pp.SOURCE_FILES:
                            missing.add(
                                f"{path.relative_to(ROOT)} -> {top}.py")
        self.assertEqual(sorted(missing), [], "便携包缺少运行期根模块")

    def test_war_modules_import(self):
        import importlib
        for name in ("deploy_strategies.tomcat_config",
                     "deploy_strategies.remote_operation",
                     "deploy_strategies.tomcat_health",
                     "deploy_strategies.tomcat_instance",
                     "deploy_strategies.tomcat_lifecycle",
                     "deploy_strategies.war_tomcat",
                     "webapp.routes.tomcat_routes"):
            importlib.import_module(name)
