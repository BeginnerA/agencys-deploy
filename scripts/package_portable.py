#!/usr/bin/env python3
"""智能运维平台 便携包打包脚本：白名单组装 + 三重防泄漏 + 冒烟自检 + zip。

用法（在项目根目录执行；首次需联网下载运行时与依赖，之后走缓存）：
    python scripts/package_portable.py                # 全量：组装 + 冒烟 + 出 zip
    python scripts/package_portable.py --no-zip       # 只出文件夹（调试用）
    python scripts/package_portable.py --skip-smoke   # 跳过冒烟自检（仅调试，禁止用于发布）
    python scripts/package_portable.py --refresh      # 强制重新下载运行时/依赖
    python scripts/package_portable.py --clean        # 清理 dist-portable 后退出

产物：
    dist-portable/智能运维平台-便携版/              出厂态文件夹（解压即用）
    dist-portable/智能运维平台-便携版-{版本}.zip    发布用 zip

安全红线（脚本强制，不可绕过）：
    1. 白名单组装——data/、dist/、backups/、.venv/、.git/ 等永不进入包；
    2. 禁入断言——包内出现 *.db / secret_key / __pycache__ / 日志等即失败；
    3. 凭据探针——从源 data/（platform.db 的 servers/platform_settings/environments
       等全部运行数据表 + 各 YAML）收集真实值（服务器 IP、SSH 凭据、LLM/告警渠道
       密钥、连接串），并对包内文本做密钥形态识别；命中经公开面判定，泄漏即失败。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

# ================= 常量 =================
ROOT = Path(__file__).resolve().parent.parent
PY_VERSION = "3.12.10"           # 3.12 系列最后一个带 embeddable 包的版本
PY_TAG = "312"
EMBED_NAME = f"python-{PY_VERSION}-embed-amd64.zip"
EMBED_URLS = [
    f"https://www.python.org/ftp/python/{PY_VERSION}/{EMBED_NAME}",
    f"https://registry.npmmirror.com/-/binary/python/{PY_VERSION}/{EMBED_NAME}",
]
PKG_NAME = "智能运维平台-便携版"
RELEASE_VERSION = "v0.4.0"

DIST_DIR = ROOT / "dist-portable"
CACHE_DIR = DIST_DIR / ".cache"
WHEEL_DIR = CACHE_DIR / "wheels"
PKG_DIR = DIST_DIR / PKG_NAME
RUNTIME_DIR = PKG_DIR / "runtime" / "python"
SITE_PACKAGES = RUNTIME_DIR / "Lib" / "site-packages"
PORTABLE_SRC = ROOT / "scripts" / "portable"

# 白名单：进包的源码目录 / 文件
SOURCE_DIRS = ["agent", "webapp", "scripts", "deploy_strategies", "config"]
SOURCE_FILES = ["deploy.py", "deploy_pipeline.py", "menu.py",
                "requirements.txt", "README.md", "LICENSE"]
COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo",
                                     ".pytest_cache", "portable",
                                     "package_portable.py")

# 出厂启动器（scripts/portable → 包根）
PORTABLE_FILES = ["launcher.py", "启动平台.bat", "menu.bat", "deploy.bat",
                  "使用说明.txt", "NOTICE-第三方组件.txt"]

# 凭据探针扫描的文本后缀
SCAN_SUFFIXES = {".py", ".yaml", ".yml", ".md", ".txt", ".bat", ".ps1", ".sh",
                 ".js", ".css", ".html", ".json", ".ini", ".cfg", ".toml"}

# 探针通用值（模板示例 / 回环地址 / 常见默认，非泄漏）
GENERIC_VALUES = {
    "127.0.0.1", "0.0.0.0", "localhost", "example.com",
    "192.168.1.1", "192.168.1.2", "xxx.xxx.xxx.xxx",
    "~/.ssh/id_rsa_deploy", "DEFAULT_GROUP", "public",
}

# 包内文本强密钥形态（独立于探针清单的兜底；命中值同样走公开面豁免判定）
SHAPE_PATTERNS = {
    "sk- 密钥": re.compile(r"sk-[A-Za-z0-9]{16,}"),
    "LTAI 阿里云": re.compile(r"LTAI[A-Za-z0-9]{12,24}"),
    "AKIA AWS": re.compile(r"AKIA[0-9A-Z]{16}"),
    "ghp_ GitHub": re.compile(r"(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}"),
}

# 禁入断言：包内出现即失败
FORBIDDEN_DIRS = {"data", "dist", "backups", ".venv", ".git",
                  ".pytest_cache", "__pycache__", "collect", "archive"}
FORBIDDEN_SUFFIX = (".db", ".db-wal", ".db-shm", ".pyc", ".pyo", ".log", ".err")


def log(msg: str):
    print(f"{msg}", flush=True)


# ================= 工具 =================
def _rmtree_with_retry(path: Path, attempts: int = 5):
    """带重试的目录删除（防杀毒/句柄残留导致偶发占用）"""
    for _ in range(attempts):
        if not path.exists():
            return
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(1.0)
    if path.exists():
        raise SystemExit(f"[pack] 无法删除 {path}（可能被占用），请关闭相关进程后重试")


def _free_port(start: int = 39000) -> int:
    for port in range(start, start + 200):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("[pack] 找不到空闲端口")


def _tail(path: Path, n: int = 25) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        return "\n".join(lines[-n:]) or "(空)"
    except OSError:
        return "(无日志)"


def _check(cond, msg: str):
    if not cond:
        raise SystemExit(f"[pack] 冒烟断言失败：{msg}")


# ================= 阶段 1：下载运行时与依赖 =================
def _download(url: str, dest: Path, timeout: int = 120):
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=timeout) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    tmp.replace(dest)


def stage_fetch(refresh: bool):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    embed_zip = CACHE_DIR / EMBED_NAME
    if refresh or not embed_zip.exists() or embed_zip.stat().st_size < 5_000_000:
        last_err = None
        for url in EMBED_URLS:
            try:
                log(f"[fetch] 下载内置 Python：{url}")
                _download(url, embed_zip)
                last_err = None
                break
            except Exception as exc:  # noqa: BLE001 网络多源兜底
                last_err = exc
                log(f"[fetch] 该源失败（{exc}），尝试下一个源")
        if last_err is not None:
            raise SystemExit(f"[pack] 运行时下载失败：{last_err}")
    size_mb = embed_zip.stat().st_size / 1e6
    log(f"[fetch] 运行时就绪：{embed_zip.name}（{size_mb:.1f} MB）")

    WHEEL_DIR.mkdir(parents=True, exist_ok=True)
    wheels = list(WHEEL_DIR.glob("*.whl"))
    if refresh or len(wheels) < 15:
        for w in wheels:
            w.unlink()
        log(f"[fetch] 下载依赖 wheel（win_amd64 / py{'.'.join(PY_VERSION.split('.')[:2])}）")
        cmd = [sys.executable, "-m", "pip", "download",
               "-r", str(ROOT / "requirements.txt"),
               "--only-binary", ":all:", "--platform", "win_amd64",
               "--python-version", ".".join(PY_VERSION.split(".")[:2]),
               "-d", str(WHEEL_DIR), "--quiet"]
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError:
            raise SystemExit("[pack] 依赖下载失败（网络问题可重跑本脚本；"
                             "或先配置国内 pip 镜像）")
    wheels = sorted(WHEEL_DIR.glob("*.whl"))
    total_mb = sum(w.stat().st_size for w in wheels) / 1e6
    log(f"[fetch] 依赖就绪：{len(wheels)} 个 wheel（{total_mb:.1f} MB，缓存于 {WHEEL_DIR}）")


# ================= 阶段 2：白名单组装 =================
def stage_assemble():
    log("[assemble] 清理旧产物")
    _rmtree_with_retry(PKG_DIR)
    PKG_DIR.mkdir(parents=True)

    log("[assemble] 解压内置 Python 运行时")
    with zipfile.ZipFile(CACHE_DIR / EMBED_NAME) as zf:
        zf.extractall(RUNTIME_DIR)
    for junk in ("include", "libs"):  # 仅编译期使用，分发不需要
        shutil.rmtree(RUNTIME_DIR / junk, ignore_errors=True)
    (RUNTIME_DIR / f"python{PY_TAG}._pth").write_text(
        f"python{PY_TAG}.zip\n.\nLib\\site-packages\nimport site\n",
        encoding="utf-8")
    SITE_PACKAGES.mkdir(parents=True, exist_ok=True)

    log("[assemble] 安装依赖到包内 site-packages")
    cmd = [sys.executable, "-m", "pip", "install",
           "--no-index", "--find-links", str(WHEEL_DIR),
           "--target", str(SITE_PACKAGES),
           "--platform", "win_amd64",
           "--python-version", ".".join(PY_VERSION.split(".")[:2]),
           "--only-binary", ":all:", "--no-compile",
           "--no-warn-script-location", "--quiet",
           "-r", str(ROOT / "requirements.txt")]
    subprocess.run(cmd, check=True)

    log("[assemble] 拷贝平台源码（白名单）")
    for name in SOURCE_DIRS:
        shutil.copytree(ROOT / name, PKG_DIR / name, ignore=COPY_IGNORE)
    for name in SOURCE_FILES:
        shutil.copy2(ROOT / name, PKG_DIR / name)

    log("[assemble] 生成启动器与说明")
    for name in PORTABLE_FILES:
        src = PORTABLE_SRC / name
        if not src.exists():
            raise SystemExit(f"[pack] 缺少便携模板文件：{src}")
        if name == "使用说明.txt":
            text = src.read_text(encoding="utf-8").replace("{VERSION}", RELEASE_VERSION)
            (PKG_DIR / name).write_text(text, encoding="utf-8")
        else:
            shutil.copy2(src, PKG_DIR / name)

    file_count = sum(1 for p in PKG_DIR.rglob("*") if p.is_file())
    total_mb = sum(p.stat().st_size for p in PKG_DIR.rglob("*") if p.is_file()) / 1e6
    log(f"[assemble] 完成：{file_count} 个文件，{total_mb:.1f} MB")


# ================= 阶段 3：防泄漏 =================
def _collect_yaml_probes(values: set):
    """从 data/ 下运行时 YAML 收集真实值（键名启发式 + IP 形态）"""
    key_pwd = ("password", "passwd", "pwd", "secret", "token", "private_key")
    key_other = ("host", "ip", "user", "url", "domain", "server", "namespace",
                 "database", "key_file", "root", "repo", "image", "name",
                 "group", "registry", "nacos", "mysql")
    data_dir = ROOT / "data"
    if not data_dir.exists():
        return
    files = list(data_dir.rglob("*.yaml")) + list(data_dir.rglob("*.yml"))
    for fp in files:
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            m = re.match(r'\s*-?\s*([\w.\-]+)\s*:\s*(.+)$', line)
            if m:
                key = m.group(1).lower()
                raw = re.split(r'\s+#', m.group(2).strip(), maxsplit=1)[0].strip()
                val = raw.strip('"').strip("'").strip()
                if val and val not in ("~", "null", "''", '""'):
                    if any(k in key for k in key_pwd):
                        if len(val) >= 6:
                            values.add(val)
                    elif any(k in key for k in key_other) and len(val) >= 8:
                        values.add(val)
        for ip in re.findall(r'(?:\d{1,3}\.){3}\d{1,3}', text):
            values.add(ip)


def _walk_json(obj, values: set, key_hint: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk_json(v, values, str(k).lower())
    elif isinstance(obj, list):
        for item in obj:
            _walk_json(item, values, key_hint)
    elif isinstance(obj, str):
        v = obj.strip()
        if not v:
            return
        if any(k in key_hint for k in ("password", "secret", "token", "passwd", "pwd")):
            if len(v) >= 6:
                values.add(v)
        elif len(v) >= 8:
            values.add(v)


def _collect_db_probes(values: set):
    """从运行时数据库收集真实值。

    覆盖全部敏感数据面：servers 全表（服务器 IP / SSH 凭据 / 内网地址）、
    platform_settings 配置中心（LLM 网关 api_key、告警渠道密钥等）、
    environments JSON 列、custom_monitors / services / monitor_snapshots。
    """
    for db_name in ("platform.db", "dashboard.db"):
        dbp = ROOT / "data" / db_name
        if not dbp.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{dbp.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            try:
                for row in conn.execute("SELECT * FROM servers"):
                    for v in row:
                        v = str(v).strip() if v is not None else ""
                        if v:
                            values.add(v)
            except sqlite3.Error:
                pass
            try:
                for _key, raw in conn.execute(
                        "SELECT key, value FROM platform_settings"):
                    raw = str(raw or "").strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except (TypeError, ValueError):
                        obj = None
                    if obj is None:
                        values.add(raw)
                    else:
                        _walk_json(obj, values)
            except sqlite3.Error:
                pass
            try:
                for row in conn.execute(
                        "SELECT target, san_domains, owner FROM custom_monitors"):
                    for v in row:
                        if isinstance(v, str):
                            for part in re.split(r"[,\s;]+", v):
                                if part:
                                    values.add(part)
            except sqlite3.Error:
                pass
            try:
                for row in conn.execute(
                        "SELECT health_url, git_repo FROM services"):
                    for v in row:
                        if v:
                            values.add(str(v).strip())
            except sqlite3.Error:
                pass
            try:
                for (h,) in conn.execute(
                        "SELECT DISTINCT host FROM monitor_snapshots"):
                    if h:
                        values.add(str(h).strip())
            except sqlite3.Error:
                pass
            try:
                rows = conn.execute(
                    "SELECT registry, nacos, mysql_snailjob, ssh, build, deploy"
                    " FROM environments").fetchall()
                for row in rows:
                    for raw in row:
                        try:
                            obj = json.loads(raw or "{}")
                        except (TypeError, ValueError):
                            continue
                        _walk_json(obj, values)
            except sqlite3.Error:
                pass
        finally:
            conn.close()


def collect_probes() -> dict:
    raw: set = set()
    _collect_yaml_probes(raw)
    _collect_db_probes(raw)
    probes = {}
    for v in raw:
        if len(v) < 6:
            # 4-5 字符值（root、业务短名）误报率过高；主防线由白名单 + 禁入断言兜底
            continue
        if v in GENERIC_VALUES:
            continue
        if re.fullmatch(r'[*xX. ]+', v):  # 纯占位符（xxx、***、......）
            continue
        if re.fullmatch(r'[\d.\-:/ ]+', v) \
                and not re.fullmatch(r'1[3-9]\d{9}', v) \
                and not re.fullmatch(r'(?:\d{1,3}\.){3}\d{1,3}', v):
            continue  # 纯数字/日期/版本串（手机号与 IP 除外）
        probes[v] = True
    return probes


def _assert_no_forbidden():
    problems = []
    for p in sorted(PKG_DIR.rglob("*")):
        rel = p.relative_to(PKG_DIR).as_posix()
        if p.is_symlink():
            problems.append(f"符号链接：{rel}")
            continue
        name_low = p.name.lower()
        if p.is_dir():
            if name_low in FORBIDDEN_DIRS:
                problems.append(f"禁入目录：{rel}")
        else:
            if "__pycache__" in rel or name_low == "secret_key" \
                    or name_low.endswith(FORBIDDEN_SUFFIX):
                problems.append(f"禁入文件：{rel}")
    if problems:
        raise SystemExit("[pack] 禁入断言失败：\n  " + "\n  ".join(problems[:50]))
    log("[leak] 禁入断言通过（无 data/dist/venv/db/密钥/缓存 等）")


def _boundary_pattern(val: str):
    """词边界匹配：避免子串误报（如探针 10.0.0.1 误命中源码里的 10.0.0.11）"""
    return re.compile(r'(?<![0-9A-Za-z])' + re.escape(val) + r'(?![0-9A-Za-z])')


def _git_public_entries():
    """git 已跟踪的文本文件内容（将随公开仓库发布），用于命中值豁免判定"""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                             capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return []
    entries = []
    for rel in out.stdout.splitlines():
        fp = ROOT / rel
        if fp.suffix.lower() not in SCAN_SUFFIXES:
            continue
        try:
            if not fp.is_file() or fp.stat().st_size > 2_000_000:
                continue
            entries.append(fp.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return entries


def _scan_probe_hits(probes: dict):
    if not probes:
        log("[leak] 未从源 data/ 收集到探针值（data 为空？），跳过探针扫描")
        return
    checks = [(val, _boundary_pattern(val)) for val in sorted(probes)]
    hits: dict = {}  # 命中值 -> [包内文件相对路径, ...]
    shape_matched: set = set()  # 由密钥形态识别命中的值（打印与审计标注用）
    for fp in sorted(PKG_DIR.rglob("*")):
        if not fp.is_file():
            continue
        rel = fp.relative_to(PKG_DIR).as_posix()
        if rel.startswith("runtime/"):  # 官方运行时与 wheel 发行物，不扫
            continue
        if fp.suffix.lower() not in SCAN_SUFFIXES:
            continue
        if fp.stat().st_size > 2_000_000:
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for val, pat in checks:
            if val in text and pat.search(text):  # 先子串快筛，再词边界确认
                hits.setdefault(val, []).append(rel)
        for _label, pat in SHAPE_PATTERNS.items():  # 密钥形态兜底
            for m in pat.finditer(text):
                matched = m.group(0)
                if matched not in hits:
                    shape_matched.add(matched)
                hits.setdefault(matched, []).append(rel)
    if not hits:
        log(f"[leak] 凭据探针通过（{len(probes)} 个真实值 + {len(SHAPE_PATTERNS)} 类密钥形态"
            f" × 全包文本，0 命中）")
        return

    public_texts = _git_public_entries()
    benign, leaks = [], []
    for val, files in hits.items():
        pat = _boundary_pattern(val)
        if any(pat.search(t) for t in public_texts):
            benign.append((val, files))
        else:
            leaks.append((val, files))
    if benign:
        log(f"[leak] 提示：{len(benign)} 个命中值已存在于 git 跟踪内容中"
            f"（将随公开仓库发布），按公开信息处理：")
        for val, files in benign:
            tag = "（形态识别）" if val in shape_matched else ""
            sample = ", ".join(files[:3]) + (" 等" if len(files) > 3 else "")
            log(f"       · {val}{tag}（{len(files)} 个包内文件：{sample}）")
    if leaks:
        detail = "\n  ".join(f"{val} → {', '.join(files[:5])}"
                            for val, files in leaks[:50])
        raise SystemExit(
            "[pack] 凭据探针命中（真实值疑似泄漏，且不在公开仓库内容中）：\n  "
            + detail
            + "\n  请确认这些值的来源；若确属公开信息，需先让其进入 git 跟踪内容"
              "（或加入脚本 GENERIC_VALUES）后重跑")
    log(f"[leak] 凭据探针通过（{len(probes)} 个真实值，命中 {len(benign)} 个均为公开信息）")


def stage_leak_check():
    log("[leak] 防泄漏检查")
    _assert_no_forbidden()
    _scan_probe_hits(collect_probes())


# ================= 阶段 4：冒烟自检（包内运行时真实拉起） =================
def _wait_http(url: str, timeout: float = 5.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read(65536).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001 未就绪 / 连接失败均按失败处理
        return None, None


def stage_smoke():
    log("[smoke] 用包内运行时真实拉起平台（随机端口，--monitor-interval 0）")
    python_exe = RUNTIME_DIR / "python.exe"
    _check(python_exe.exists(), "包内 python.exe 缺失")

    port = _free_port()
    smoke_log = CACHE_DIR / "smoke.log"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("DASHBOARD_")
           and k.upper() not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
    out = open(smoke_log, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(python_exe), str(PKG_DIR / "webapp" / "app.py"),
         "--port", str(port), "--monitor-interval", "0"],
        cwd=str(PKG_DIR), stdout=out, stderr=subprocess.STDOUT, env=env)
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 60
        ready = False
        while time.time() < deadline:
            _check(proc.poll() is None,
                   f"服务提前退出（code={proc.returncode}）\n{_tail(smoke_log)}")
            status, body = _wait_http(base + "/")
            if status == 200:
                _check("智能运维平台" in body, "首页缺少平台特征串")
                ready = True
                break
            time.sleep(0.5)
        _check(ready, f"60s 内首页未就绪\n{_tail(smoke_log)}")

        payload = json.dumps({"username": "admin", "password": "admin"}).encode()
        req = urllib.request.Request(
            base + "/api/auth/login", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            login = json.loads(resp.read().decode("utf-8"))
        _check(bool(login.get("token")), "登录未返回 token")
        _check(login.get("user", {}).get("must_change_password") == 1,
               "首次登录应强制改密")

        for path in ("/screen", "/static/js/app.js", "/static/css/tokens.css"):
            status, _ = _wait_http(base + path)
            _check(status == 200, f"{path} 返回 {status}")

        dbp = PKG_DIR / "data" / "platform.db"
        _check(dbp.exists() and dbp.stat().st_size > 0, "自动建库失败")
        log(f"[smoke] 全部断言通过（自动建库 {dbp.stat().st_size / 1024:.0f} KB，"
            f"admin 首启与 JWT 登录链路正常）")
    finally:
        out.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True)

    # 冒烟属于出厂验证，运行期产物必须清理回出厂态
    _rmtree_with_retry(PKG_DIR / "data")
    for p in sorted(PKG_DIR.rglob("__pycache__"), reverse=True):
        shutil.rmtree(p, ignore_errors=True)
    log("[smoke] 冒烟产物已清理（data/ 与 __pycache__ 移除，保持出厂态）")


# ================= 阶段 5：zip =================
def stage_zip() -> Path:
    zip_path = DIST_DIR / f"{PKG_NAME}-{RELEASE_VERSION}.zip"
    if zip_path.exists():
        zip_path.unlink()
    log("[zip] 压缩打包")
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for fp in sorted(PKG_DIR.rglob("*")):
            if fp.is_file():
                zf.write(fp, f"{PKG_NAME}/" + fp.relative_to(PKG_DIR).as_posix())
                count += 1
    size = zip_path.stat().st_size
    sha256 = hashlib.sha256(zip_path.read_bytes()).hexdigest().upper()
    log(f"[zip] {zip_path.name}：{count} 个文件，{size / 1e6:.1f} MB")
    log(f"[zip] SHA256 {sha256}")
    return zip_path


# ================= 主流程 =================
def main():
    ap = argparse.ArgumentParser(description="智能运维平台便携包打包脚本")
    ap.add_argument("--no-zip", action="store_true", help="只出文件夹，不出 zip")
    ap.add_argument("--skip-smoke", action="store_true",
                    help="跳过冒烟自检（仅调试，禁止用于发布）")
    ap.add_argument("--refresh", action="store_true", help="强制重新下载运行时/依赖")
    ap.add_argument("--clean", action="store_true", help="清理 dist-portable 后退出")
    args = ap.parse_args()

    t0 = time.time()
    if args.clean:
        _rmtree_with_retry(DIST_DIR)
        log("[pack] 已清理 dist-portable")
        return

    log(f"[pack] 组装 {PKG_NAME} {RELEASE_VERSION}（内置 Python {PY_VERSION}）")
    stage_fetch(args.refresh)
    stage_assemble()
    stage_leak_check()

    if args.skip_smoke:
        log("[pack] 警告：已跳过冒烟自检，产物未经真实性验证，禁止用于发布")
    else:
        stage_smoke()
        _assert_no_forbidden()  # 冒烟清理后复检，防遗漏

    if args.no_zip:
        log(f"[pack] 完成（--no-zip）：{PKG_DIR}")
    else:
        zip_path = stage_zip()
        log(f"[pack] 完成：{zip_path}")
    log(f"[pack] 总耗时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
