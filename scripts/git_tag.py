"""发布成功后为服务源码仓库打 Git Tag（v{version}，可带环境后缀）

仓库路径由 projects.yaml 的 git_repo 字段指定（相对 deploy.project_root）。
Git 操作失败仅告警，不阻断发布流程。
"""
import subprocess
from pathlib import Path

from common import log


def tag_project(proj_cfg: dict, version: str, project_root: str, push: bool = True,
                suffix: str = "") -> bool:
    """为项目对应的源码仓库打 tag；返回是否成功（失败返回 False 并告警）

    suffix 非空时 tag 名为 v{version}-{suffix}（如 v1.0.30-test），
    用于区分测试/生产等不同环境的发布 tag；留空保持 v{version}。
    """
    repo = (proj_cfg.get("git_repo") or "").strip()
    image_name = proj_cfg.get("image_name", "")
    if not repo:
        log(f"{image_name}: 未配置 git_repo，跳过 Git Tag")
        return False
    repo_path = Path(project_root) / repo
    if not (repo_path / ".git").exists():
        log(f"警告: {repo_path} 不是 Git 仓库，跳过 Git Tag")
        return False
    tag = f"v{version}-{suffix}" if suffix else f"v{version}"
    try:
        subprocess.run(["git", "-C", str(repo_path), "tag", tag],
                       check=True, capture_output=True, text=True)
        if push:
            subprocess.run(["git", "-C", str(repo_path), "push", "origin", tag],
                           check=True, capture_output=True, text=True)
        log(f"Git Tag 完成: {repo} {tag}" + ("（已推送远端）" if push else "（未推送）"))
        return True
    except subprocess.CalledProcessError as e:
        log(f"警告: Git Tag 失败（不影响发布）: {e.stderr.strip() or e}")
        return False
