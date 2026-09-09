"""Agent 模块（Phase 1-4）

目录职责：
    config.py      LLM 网关与运行参数（环境变量 / platform_settings）
    llm.py         OpenAI 兼容网关封装（function calling）
    tools.py       工具注册表（read/write 两级权限，全部平台只读/建议能力）
    core.py        ReAct 编排循环 + 会话持久化
    advisor.py     Phase 2 决策点：灰度/失败归因/告警归因/发布预检
    executor.py    Phase 3 发布执行：计划 → 审批 → 任务通道 → 状态同步
    memory.py      Phase 4 长期记忆：故障案例库 + 相似检索
    scheduler.py   后台建议调度（灰度/告警/失败任务轮询）
    mcp_server.py  Phase 4 MCP Server（stdio，供外部 Agent 调用）
    cli.py         终端对话入口（Phase 1 命令行形态）
    routes.py      Flask 蓝图（对话/审批/建议/案例 API）
"""
