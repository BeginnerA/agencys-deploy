# -*- coding: utf-8 -*-
"""build.py 容错逻辑测试：clean 删除目录失败的识别"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build import is_clean_failure  # noqa: E402


def test_clean_failure_detected():
    """maven-clean-plugin 删除 target 失败（本次发布的真实报错）应被识别"""
    out = (
        "[ERROR] Failed to execute goal org.apache.maven.plugins:"
        "maven-clean-plugin:3.2.0:clean (default-clean) on project hqb-expert: "
        "Failed to clean project: Failed to delete D:\\work\\hqb-expert\\target -> [Help 1]"
    )
    assert is_clean_failure(out)


def test_compile_failure_not_detected():
    """编译错误等确定性失败不应触发 clean 重试"""
    out = (
        "[ERROR] Failed to execute goal org.apache.maven.plugins:"
        "maven-compiler-plugin:3.14.0:compile (default-compile) on project hqb-expert: "
        "Compilation failure"
    )
    assert not is_clean_failure(out)


def test_empty_output_not_detected():
    assert not is_clean_failure("")
    assert not is_clean_failure(None)
