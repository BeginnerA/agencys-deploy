"""Phase 4 MCP Server（stdio）：把平台工具开放给外部 Agent

协议：MCP stdio 传输（newline-delimited JSON-RPC 2.0），零第三方依赖。
外部 Agent（如 Claude Desktop / 各类 MCP 客户端）通过本 Server 可调用平台
全部工具（tools/list → tools/call），复用 agent/tools.py 的角色分级：
    AGENT_MCP_ROLE=viewer/operator/admin（默认 operator）
写工具（plan_operation/analyze_alert/case_save 等）仍遵守平台的审批约束
（发布类动作仅生成审批单，绝不绕过人工审批）。

用法：
    python agent/mcp_server.py [--db data/platform.db] [--role operator]
    # 或由 MCP 客户端以 stdio 方式拉起（无需参数，路径经 env/绝对路径约定）
"""
import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "agencys-deploy-agent"
SERVER_VERSION = "1.0.0"


class McpError(Exception):
    """携带 JSON-RPC error code 的协议错误"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class McpSession:
    def __init__(self, db_path: str, role: str):
        from agent import tools as tools_mod
        self.tools = tools_mod.visible_tools(role)
        self.role = role
        self.db_path = db_path
        self.ctx = type("ToolCtx", (), {
            "db_path": db_path, "user_id": 0, "username": "mcp",
            "role": role})()

    # ---------- JSON-RPC 分发 ----------

    def handle(self, msg: dict) -> dict:
        method = msg.get("method", "")
        params = msg.get("params") or {}
        if method == "initialize":
            return {"protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME,
                                   "version": SERVER_VERSION}}
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None  # 通知类消息无需响应
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self._tool_list()}
        if method == "tools/call":
            return self._tool_call(params)
        raise McpError(-32601, f"未知方法: {method}")

    # ---------- 工具 ----------

    def _tool_list(self) -> list:
        out = []
        for t in self.tools:
            params = dict(t.get("parameters") or {})
            out.append({"name": t["name"],
                        "description": t["description"],
                        "inputSchema": params or {"type": "object",
                                                  "properties": {}}})
        return out

    def _tool_call(self, params: dict) -> dict:
        name = params.get("name", "")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        from agent import tools as tools_mod
        text = tools_mod.execute_tool(self.role, name, args, self.ctx)
        try:
            payload = json.loads(text)
        except ValueError:
            payload = text
        is_error = isinstance(payload, dict) and "error" in payload
        return {"content": [{"type": "text", "text": text}],
                "isError": bool(is_error)}


# ---------- 事件循环 ----------

def serve(db_path: str, role: str) -> int:
    session = McpSession(db_path, role)
    # 握手前打印一条诊断日志（外部客户端通常忽略 stderr 之前的 stdout 输出）
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue  # 非法行直接丢弃，保持循环存活
        req_id = msg.get("id")
        try:
            result = session.handle(msg)
        except McpError as e:
            if req_id is None:
                continue
            sys.stdout.write(json.dumps(
                {"jsonrpc": "2.0", "id": req_id,
                 "error": {"code": e.code, "message": e.message}},
                ensure_ascii=False) + "\n")
        except Exception as e:  # 服务端内部错误也按协议返回，不让进程崩溃
            if req_id is None:
                continue
            sys.stdout.write(json.dumps(
                {"jsonrpc": "2.0", "id": req_id,
                 "error": {"code": -32603, "message": f"内部错误: {e}"}},
                ensure_ascii=False) + "\n")
        else:
            if result is None or req_id is None:
                continue  # 通知
            sys.stdout.write(json.dumps(
                {"jsonrpc": "2.0", "id": req_id, "result": result},
                ensure_ascii=False) + "\n")
        finally:
            sys.stdout.flush()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="agencys-deploy MCP Server (stdio)")
    parser.add_argument("--db", default=str(_ROOT / "data" / "platform.db"))
    parser.add_argument("--role", default="operator",
                        help="工具权限角色: viewer/operator/admin（默认 operator）")
    args = parser.parse_args()
    role = (args.role or "operator").lower()
    if role not in ("viewer", "operator", "admin"):
        print(f"非法角色: {role}", file=sys.stderr)
        return 2
    return serve(args.db, role)


if __name__ == "__main__":
    sys.exit(main())
