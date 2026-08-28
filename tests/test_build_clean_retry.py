# -*- coding: utf-8 -*-
"""build.py 容错逻辑测试：瞬时 I/O 错误（clean 删除失败 / 读取生成文件失败）的识别"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build import is_clean_failure, is_transient_io_failure  # noqa: E402


GENERATED_FILE = ("D:\\work\\IdeaProjects\\狼途传媒\\红球吧\\hqb-expert\\target\\"
                  "generated-sources\\annotations\\com\\hongqiu8\\expert\\domain\\live\\entity\\"
                  "LiveConnectionInvitationRecordToPkInvitationRecordVoMapperImpl.java")


def test_clean_failure_detected():
    """maven-clean-plugin 删除 target 失败（本次发布的真实报错）应被识别"""
    out = (
        "[ERROR] Failed to execute goal org.apache.maven.plugins:"
        "maven-clean-plugin:3.2.0:clean (default-clean) on project hqb-expert: "
        "Failed to clean project: Failed to delete D:\\work\\hqb-expert\\target -> [Help 1]"
    )
    assert is_clean_failure(out)
    assert is_transient_io_failure(out)


def test_compile_failure_not_detected():
    """编译错误等确定性失败不应触发重试"""
    out = (
        "[ERROR] Failed to execute goal org.apache.maven.plugins:"
        "maven-compiler-plugin:3.14.0:compile (default-compile) on project hqb-expert: "
        "Compilation failure"
    )
    assert not is_clean_failure(out)
    assert not is_transient_io_failure(out)


def test_read_generated_file_failure_cn_detected():
    """中文环境 javac 读取生成文件失败（本次发布的真实报错）应被识别为瞬时 I/O 错误"""
    out = (
        "[ERROR] " + GENERATED_FILE + ": 读取" + GENERATED_FILE + "时出错"
    )
    assert not is_clean_failure(out)
    assert is_transient_io_failure(out)


def test_read_generated_file_failure_en_detected():
    """英文环境 error reading file 同样应被识别"""
    out = f"[ERROR] {GENERATED_FILE}: error reading file"
    assert is_transient_io_failure(out)


def test_real_compile_error_not_io():
    """真实编译错误（找不到符号）不应误判为瞬时 I/O 错误"""
    out = (
        "[ERROR] /D:/src/App.java:[10,20] 找不到符号\n"
        "[ERROR]   symbol:   method missing()\n"
        "[ERROR] -> [Help 1]"
    )
    assert not is_transient_io_failure(out)


def test_repackage_ioobe_detected():
    """spring-boot repackage 读 jar 索引越界（本次发布的真实报错）应识别为瞬时 I/O 错误"""
    out = (
        "[ERROR] Failed to execute goal org.springframework.boot:"
        "spring-boot-maven-plugin:4.2.0-M1:repackage (default) on project hqb-expert: "
        "Execution default of goal org.springframework.boot:"
        "spring-boot-maven-plugin:4.2.0-M1:repackage failed: "
        "Index 8 out of bounds for length 0 -> [Help 1]"
    )
    assert not is_clean_failure(out)
    assert is_transient_io_failure(out)


def test_repackage_other_error_not_io():
    """repackage 的其他确定性错误（如 main class 缺失）不应误判"""
    out = (
        "[ERROR] Failed to execute goal org.springframework.boot:"
        "spring-boot-maven-plugin:4.2.0-M1:repackage (default) on project hqb-expert: "
        "Unable to find main class -> [Help 1]"
    )
    assert not is_transient_io_failure(out)


def test_empty_output_not_detected():
    assert not is_clean_failure("")
    assert not is_clean_failure(None)
    assert not is_transient_io_failure("")
    assert not is_transient_io_failure(None)
