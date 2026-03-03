#!/usr/bin/env python3
"""
VK MCP 轻量级客户端 — 供 Shell 钩子调用

通过 MCP stdio 协议与 VK 通信，执行 REST API 不支持的操作。
主要用途：在 cleanup 脚本中自动创建审查 Session。

用法:
    python3 scripts/vk-mcp-client.py start_review_session \
        --repo-id <repo_id> \
        --base-branch <branch> \
        --issue-id <issue_id> \
        --title "Review: ..." \
        --executor CODEX \
        --prompt "审查指令..."

    python3 scripts/vk-mcp-client.py update_issue \
        --issue-id <issue_id> \
        --status "Done"

环境变量:
    PORT — VK 端口 (默认 9527)
"""

import argparse
import json
import os
import select
import subprocess
import sys
import time


class VKMCPClient:
    """VK MCP stdio 客户端"""

    def __init__(self, port: str = "9527"):
        self.port = port
        self.proc = None
        self._req_id = 0

    def connect(self) -> bool:
        """启动 MCP 进程并完成初始化握手"""
        env = {**os.environ, "PORT": self.port}
        self.proc = subprocess.Popen(
            ["npx", "-y", "vibe-kanban@latest", "--mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        # 初始化握手
        resp = self._call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "vk-hook", "version": "0.1"},
            },
        )
        if not resp:
            return False

        # 发送 initialized 通知
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        time.sleep(0.3)
        return True

    def close(self):
        """关闭 MCP 进程"""
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=5)

    def call_tool(self, tool_name: str, arguments: dict) -> dict | None:
        """调用 MCP tool 并返回结果"""
        resp = self._call("tools/call", {"name": tool_name, "arguments": arguments})
        if not resp or "result" not in resp:
            return None

        # 提取 text content
        content = resp["result"].get("content", [])
        if content and content[0].get("type") == "text":
            try:
                return json.loads(content[0]["text"])
            except json.JSONDecodeError:
                return {"raw": content[0]["text"]}

        return resp["result"]

    # ---- 便捷方法 ----

    def start_review_session(
        self,
        title: str,
        repo_id: str,
        base_branch: str,
        executor: str,
        issue_id: str | None = None,
        prompt_override: str | None = None,
    ) -> str | None:
        """启动审查 Session，返回 workspace_id"""
        args = {
            "title": title,
            "repos": [{"repo_id": repo_id, "base_branch": base_branch}],
            "executor": executor,
        }
        if issue_id:
            args["issue_id"] = issue_id
        if prompt_override:
            args["prompt_override"] = prompt_override

        result = self.call_tool("start_workspace_session", args)
        if result and "workspace_id" in result:
            return result["workspace_id"]
        return None

    def update_issue(self, issue_id: str, status: str) -> bool:
        """更新 Issue 状态"""
        result = self.call_tool("update_issue", {"issue_id": issue_id, "status": status})
        return bool(result and "issue" in result)

    # ---- 内部方法 ----

    def _send(self, msg: dict):
        self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
        self.proc.stdin.flush()

    def _recv(self, timeout: int = 30) -> dict | None:
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if ready:
            line = self.proc.stdout.readline()
            if line.strip():
                return json.loads(line)
        return None

    def _call(self, method: str, params: dict) -> dict | None:
        self._req_id += 1
        self._send({"jsonrpc": "2.0", "id": self._req_id, "method": method, "params": params})
        return self._recv()


def cmd_start_review(args):
    """子命令：启动审查 Session"""
    client = VKMCPClient(port=args.port)
    if not client.connect():
        print("ERROR: 无法连接 VK MCP", file=sys.stderr)
        sys.exit(1)

    try:
        workspace_id = client.start_review_session(
            title=args.title,
            repo_id=args.repo_id,
            base_branch=args.base_branch,
            executor=args.executor,
            issue_id=args.issue_id,
            prompt_override=args.prompt,
        )
        if workspace_id:
            print(workspace_id)  # 输出 workspace_id 供 shell 脚本使用
        else:
            print("ERROR: 创建 Session 失败", file=sys.stderr)
            sys.exit(1)
    finally:
        client.close()


def cmd_update_issue(args):
    """子命令：更新 Issue 状态"""
    client = VKMCPClient(port=args.port)
    if not client.connect():
        print("ERROR: 无法连接 VK MCP", file=sys.stderr)
        sys.exit(1)

    try:
        ok = client.update_issue(args.issue_id, args.status)
        if ok:
            print(f"OK: Issue → {args.status}")
        else:
            print("ERROR: 更新 Issue 失败", file=sys.stderr)
            sys.exit(1)
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description="VK MCP 轻量级客户端")
    parser.add_argument("--port", default=os.environ.get("PORT", "9527"), help="VK 端口")
    sub = parser.add_subparsers(dest="command", required=True)

    # start_review_session
    p1 = sub.add_parser("start_review_session", help="启动审查 Session")
    p1.add_argument("--repo-id", required=True)
    p1.add_argument("--base-branch", required=True)
    p1.add_argument("--issue-id")
    p1.add_argument("--title", required=True)
    p1.add_argument("--executor", required=True, choices=["CODEX", "CLAUDE_CODE", "GEMINI"])
    p1.add_argument("--prompt", help="审查提示词")
    p1.set_defaults(func=cmd_start_review)

    # update_issue
    p2 = sub.add_parser("update_issue", help="更新 Issue 状态")
    p2.add_argument("--issue-id", required=True)
    p2.add_argument("--status", required=True)
    p2.set_defaults(func=cmd_update_issue)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
