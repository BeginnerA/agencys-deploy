"""CI 工作流契约：门禁在 GitHub Actions 上必须真的会跑到

每条断言对应一种"CI 配了但没用"的失败方式：不触发 master → 主干提交无人拦；
不装 requirements.txt → 单测 import Flask 就炸；不装 requirements-dev.txt 或 node →
严格模式下对应步判 FAIL；漏 --strict → 缺工具被静默记 SKIP，门禁形同空转。

门禁脚本自身的语义与"仓库必须过 lint"在 tests/test_quality_gate.py。
"""
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


def _load_workflows():
    """返回 [(文件路径, 解析后的 YAML)]；目录不存在时断言失败"""
    if not WORKFLOWS.is_dir():
        raise AssertionError(f"CI 工作流目录不存在：{WORKFLOWS}")
    out = []
    for f in sorted(list(WORKFLOWS.glob("*.yml")) + list(WORKFLOWS.glob("*.yaml"))):
        out.append((f, yaml.safe_load(f.read_text(encoding="utf-8"))))
    if not out:
        raise AssertionError(f"{WORKFLOWS} 下没有工作流文件")
    return out


def _gate_workflows():
    """挑出真正调用 quality_gate.py 的工作流（其余工作流不受本文件约束）"""
    hits = []
    for path, cfg in _load_workflows():
        for job in (cfg.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                if "quality_gate" in str(step.get("run", "")):
                    hits.append((path, cfg, job, step))
    return hits


class TestWorkflowContract(unittest.TestCase):
    def test_a_workflow_runs_the_gate_in_strict_mode(self):
        hits = _gate_workflows()
        self.assertTrue(hits, "没有任何工作流执行 scripts/quality_gate.py")
        for path, _, _, step in hits:
            self.assertIn("--strict", str(step["run"]).split(),
                          f"{path.name} 未用 --strict：工具缺失会被静默记为 SKIP，"
                          f"CI 形同没跑 lint")

    def test_gate_job_triggers_on_master(self):
        for path, cfg, _, _ in _gate_workflows():
            # YAML 1.1 把裸 on 解析成 True，GitHub 两种写法都认
            trig = cfg.get("on", cfg.get(True))
            self.assertIsInstance(trig, dict, f"{path.name} 的 on 触发器写法无法解析")
            branches = ((trig.get("push") or {}).get("branches")
                        or (trig.get("pull_request") or {}).get("branches") or [])
            self.assertIn("master", branches,
                          f"{path.name} 不在 master 上触发，门禁永远不会拦住主干提交")

    def test_gate_job_installs_runtime_and_dev_requirements(self):
        for path, cfg, job, _ in _gate_workflows():
            blob = "\n".join(str(s.get("run", "")) for s in job.get("steps") or [])
            for req in ("requirements.txt", "requirements-dev.txt"):
                self.assertIn(req, blob,
                              f"{path.name} 未安装 {req}："
                              f"{'单测 import Flask/paramiko 会失败' if req == 'requirements.txt' else '严格模式下 lint 步判 FAIL'}")

    def test_gate_job_provides_node_for_the_frontend_step(self):
        for path, cfg, job, _ in _gate_workflows():
            uses = " ".join(str(s.get("uses", "")) for s in job.get("steps") or [])
            blob = "\n".join(str(s.get("run", "")) for s in job.get("steps") or [])
            self.assertTrue("setup-node" in uses or "node" in blob,
                            f"{path.name} 没有 node，严格模式下前端步判 FAIL")

    def test_gate_job_python_version_matches_runtime_image(self):
        """Dockerfile 跑 python:3.11-slim、ruff.toml target-version=py311：
        CI 用更新的解释器会放行 3.12 才有的语法，发布镜像里才炸。"""
        image = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("python:3.11", image, "Dockerfile 基础镜像已变更，本用例需同步")
        for path, cfg, job, _ in _gate_workflows():
            versions = [str((s.get("with") or {}).get("python-version"))
                        for s in job.get("steps") or [] if "python-version" in (s.get("with") or {})]
            self.assertTrue(versions, f"{path.name} 未固定 python-version")
            self.assertTrue(any(v.startswith("3.11") for v in versions),
                            f"{path.name} 的 python-version={versions}，与 3.11 运行环境不一致")

    def test_gate_job_bounds_its_own_runtime(self):
        """无 timeout 的挂死步骤会占用 runner 到 GitHub 的默认上限（360 分钟）。"""
        for path, cfg, job, _ in _gate_workflows():
            self.assertIsInstance(job.get("timeout-minutes"), int,
                                  f"{path.name} 缺 timeout-minutes")

    def test_gate_job_is_not_granted_write_permissions(self):
        """不声明 permissions 时 GITHUB_TOKEN 继承仓库默认（可能是 write）。
        门禁只读源码，装第三方依赖的环节一旦被供应商链污染，write 权限会把泄露变成改写仓库。"""
        for path, cfg, job, _ in _gate_workflows():
            perms = job.get("permissions", cfg.get("permissions"))
            self.assertIsNotNone(perms, f"{path.name} 未声明 permissions")
            contents = perms.get("contents") if isinstance(perms, dict) else perms
            self.assertIn(contents, ("read", "none", None),
                          f"{path.name} 的 contents 权限是 {contents!r}")


if __name__ == "__main__":
    unittest.main()
