import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build


def _war(path: Path, name="WEB-INF/web.xml"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(name, "<web-app/>")
    return path


class BuildWarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.proj = {"name": "orders", "image_name": "orders",
                     "deploy_mode": "war_tomcat", "build_order": [],
                     "runtime_config": {"artifact_pattern": "target/*.war"}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_single_candidate(self):
        expected = _war(self.root / "target" / "orders-1.0.0.war")
        self.assertEqual(build._select_war(self.proj, self.root), expected)

    def test_multiple_candidates_rejected(self):
        _war(self.root / "target" / "a.war")
        _war(self.root / "target" / "b.war")
        with self.assertRaises(SystemExit):
            build._select_war(self.proj, self.root)

    def test_missing_rejected(self):
        with self.assertRaises(SystemExit):
            build._select_war(self.proj, self.root)

    def test_make_artifact_sha(self):
        _war(self.root / "target" / "orders.war")
        dist = self.root / "dist"
        out = build._make_war_artifact(self.proj, self.root, "1.0.0",
                                       {"deploy": {"dist_dir": str(dist)}})
        self.assertEqual(out.name, "orders-1.0.0.war")
        self.assertTrue(out.with_name(out.name + ".sha256").is_file())

    def test_reject_traversal_member(self):
        bad = self.root / "target" / "bad.war"
        _war(bad, "../../etc/passwd")
        with self.assertRaises(ValueError):
            build._validate_war_zip(bad)
