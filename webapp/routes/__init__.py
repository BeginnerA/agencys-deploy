"""API 路由包：auth / config / deploy / dash

`task_busy_response()` 放在这里而非某个 route 模块内：发布任务有 Web / 定时计划 /
webhook 三个 HTTP 入口，被拒时的响应体必须一致（机器码 + 中文说明 + 占用方摘要）。
"""
from flask import jsonify


def task_busy_response(conflict: dict) -> tuple:
    """目标已有活动任务 → 409

    error 是与 window_warn 同族的机器码；message 给不做特判的调用方直接展示；
    active_task 让前端能跳去查看/终止占用方任务。
    """
    from ..db import describe_active_task
    return jsonify({"error": "active_task",
                    "message": describe_active_task(conflict),
                    "active_task": conflict}), 409
