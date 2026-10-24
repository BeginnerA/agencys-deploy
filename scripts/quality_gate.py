#!/usr/bin/env python3
"""一键质量门禁：发布前/CI 里跑同一套检查，任一步失败即非零退出

设计取舍：
    - 无打包器、无转译（前端是原生 JS SPA），所以前端只能做「语法 + 无头单测」两道检查；
    - ruff / node 属可选工具：缺失时该步记为 SKIP 而非 FAIL（缺依赖不该让发布中断），
      但 `--strict` 下 SKIP 也视为失败，用于 CI 这种依赖应当齐备的环境；
    - 所有步骤都是只读的：语法步用 ast.parse（不在源码树落 __pycache__），
      单测走 `-p no:cacheprovider`，全程不读写 data/ 运行数据。

用法：
    python scripts/quality_gate.py              # 全量（约 1-2 分钟）
    python scripts/quality_gate.py --quick      # 跳过 Python 单测（语法/静态/前端）
    python scripts/quality_gate.py --only js    # 只跑某一步（compile|lint|pytest|js）
    python scripts/quality_gate.py --strict     # 工具缺失也算失败（CI 用）
"""
import argparse
import ast
import shutil
import subprocess
import sys
import time
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Python 源码目录/文件：语法与字节码一致性检查的范围
PY_TARGETS = ["webapp", "agent", "scripts", "deploy_strategies",
              "deploy.py", "deploy_pipeline.py", "menu.py", "tests"]
JS_DIR = ROOT / "webapp" / "static" / "js"
JS_TESTS = sorted((ROOT / "tests" / "js").glob("*_test.js"))

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class Step:
    def __init__(self, name, title, fn):
        self.name = name
        self.title = title
        self.fn = fn


def _run(cmd, cwd=None):
    """执行子进程，返回 (rc, 合并输出尾部)"""
    proc = subprocess.run(cmd, cwd=str(cwd or ROOT), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                          errors="replace")
    out = proc.stdout or ""
    tail = out if len(out) <= 4000 else out[-4000:]
    return proc.returncode, tail


def _iter_py_files(path: Path):
    if path.is_file():
        yield path
        return
    for p in sorted(path.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        yield p


def _compile_path(path: Path) -> tuple:
    """纯语法检查（ast.parse）：返回 ("ok", 文件数) 或 ("bad", 错误列表)

    用 ast.parse 而不是 compileall：前者不落 `__pycache__`——门禁必须是只读的
    （便携版打包把源码树里的 `__pycache__` 列为禁入物，跑一次检查就让工作区变脏
    是不可接受的副作用），也不需要写盘就能给出同样的语法结论。
    """
    problems = []
    count = 0
    for f in _iter_py_files(path):
        count += 1
        try:
            ast.parse(f.read_bytes(), filename=str(f))
        except SyntaxError as e:
            problems.append(f"{f}:{e.lineno}: {e.msg}")
        except (OSError, ValueError, UnicodeDecodeError) as e:
            problems.append(f"{f}: 无法解析（{e}）")
    if problems:
        return "bad", "\n".join(problems[:20])
    return "ok", str(count)


def step_compile():
    """语法关：全部 Python 源码能过 ast 解析，且不执行任何模块代码"""
    bad = []
    total = 0
    for target in PY_TARGETS:
        path = ROOT / target
        if not path.exists():
            bad.append(f"{target}（路径不存在）")
            continue
        status, detail = _compile_path(path)
        if status == "bad":
            bad.append(f"{target}:\n{detail}")
        else:
            total += int(detail)
    if bad:
        return FAIL, "语法错误:\n" + "\n".join(bad)[-2000:]
    return PASS, f"{len(PY_TARGETS)} 个目标 / {total} 个文件语法通过"


def step_lint(strict=False):
    """ruff 静态检查：未安装时 SKIP（--strict 下算 FAIL）"""
    try:
        import_module("ruff")
    except ImportError:
        msg = "未安装 ruff（pip install -r requirements-dev.txt）"
        return (FAIL if strict else SKIP), msg
    rc, tail = _run([sys.executable, "-m", "ruff", "check", "."])
    if rc == 0:
        return PASS, "ruff check 无告警"
    return FAIL, tail


def step_pytest():
    rc, tail = _run([sys.executable, "-m", "pytest", "-q",
                     "-p", "no:cacheprovider"])
    last = [ln for ln in tail.strip().splitlines() if ln.strip()][-1:] or [""]
    if rc == 0:
        return PASS, last[0]
    return FAIL, tail


def step_js(strict=False):
    """前端：node --check 全量语法 + tests/js 无头单测；无 node 时 SKIP"""
    if shutil.which("node") is None:
        msg = f"未找到 node，跳过 {len(JS_TESTS)} 个前端用例"
        return (FAIL if strict else SKIP), msg
    problems = []
    checked = 0
    for js in sorted(JS_DIR.rglob("*.js")):
        rc, tail = _run(["node", "--check", str(js)])
        checked += 1
        if rc != 0:
            problems.append(f"语法错误 {js.relative_to(ROOT)}\n{tail}")
    for t in JS_TESTS:
        rc, tail = _run(["node", str(t)])
        if rc != 0:
            problems.append(f"用例失败 {t.name}\n{tail}")
    if problems:
        return FAIL, "\n".join(problems)[-3000:]
    return PASS, f"{checked} 个 JS 语法检查 + {len(JS_TESTS)} 个前端用例通过"


def main():
    parser = argparse.ArgumentParser(description="发布前质量门禁")
    parser.add_argument("--quick", action="store_true",
                        help="跳过 Python 单测（仅语法/静态/前端检查）")
    parser.add_argument("--only", choices=["compile", "lint", "pytest", "js"],
                        help="只执行其中一个步骤")
    parser.add_argument("--strict", action="store_true",
                        help="可选工具（ruff/node）缺失也算失败，供 CI 使用")
    args = parser.parse_args()

    steps = [Step("compile", "Python 语法编译", step_compile),
             Step("lint", "ruff 静态检查",
                  lambda: step_lint(args.strict)),
             Step("pytest", "Python 单元测试", step_pytest),
             Step("js", "前端语法与无头用例", lambda: step_js(args.strict))]
    if args.only:
        steps = [s for s in steps if s.name == args.only]
    if args.quick:
        steps = [s for s in steps if s.name != "pytest"]

    results = []
    print(f"[gate] 质量门禁开始（root={ROOT.name}，python={sys.version.split()[0]}）")
    for s in steps:
        t0 = time.time()
        try:
            status, detail = s.fn()
        except Exception as exc:  # 步骤自身异常也算失败，不能静默通过
            status, detail = FAIL, f"步骤异常: {exc!r}"
        results.append((s.title, status, detail))
        print(f"[gate] {s.title:<18} {status:<5} {time.time() - t0:6.1f}s")

    width = max(len(t) for t, _, _ in results)
    print("\n" + "=" * 60)
    failed = []
    for title, status, detail in results:
        print(f"  {title:<{width}}  {status}")
        if status == FAIL:
            failed.append(title)
            print("    " + detail.replace("\n", "\n    "))
    if failed:
        print(f"[gate] 未通过：{', '.join(failed)}")
        return 1
    print("[gate] 全部检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
