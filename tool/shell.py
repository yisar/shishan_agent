import asyncio
import os
from typing import Optional

from mod.mcp import BaseTool, ToolResult, tool

"""
Shell 工具实现了两种模式：
1. ShellTool - 作为 MCP 服务器通过 stdio 对外提供
2. ShellToolWrapper - 在进程内直接使用，不依赖 MCP

{
  "mcpServers": {
    "linux-sandbox-shell": {
      "command": "sudo",
      "args": ["uv", "run", "./tool/shell.py"]
    }
  }
}
"""


class ShellToolWrapper(BaseTool):
    """进程内直接使用的 shell 工具包装器，非 root 环境下安全降级为本地执行"""

    name: str = "local-shell"

    def __init__(self):
        super().__init__()
        self.working_dir = os.path.abspath(os.getcwd())

    @tool(
        name="shell_exec_command",
        description="在当前环境中执行 shell 命令，返回标准输出和标准错误",
        params={
            "command": {"type": "string", "description": "要执行的 shell 命令"},
            "timeout": {"type": "number", "description": "超时秒数，默认 60 秒"},
        },
        required=["command"],
    )
    async def shell_exec_command(self, command: str, timeout: Optional[float] = 60) -> ToolResult:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_dir,
                shell=True,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
                stdout = stdout_bytes.decode("utf-8", errors="replace")
                stderr = stderr_bytes.decode("utf-8", errors="replace")
                output_parts = []
                if stdout:
                    output_parts.append(f"STDOUT:\n{stdout}")
                if stderr:
                    output_parts.append(f"STDERR:\n{stderr}")
                data = "\n\n".join(output_parts) if output_parts else "(命令执行无输出)"
                if proc.returncode != 0:
                    data = f"[exit_code={proc.returncode}]\n{data}"
                return ToolResult(
                    success=proc.returncode == 0,
                    data=data,
                    message=f"exit_code={proc.returncode}",
                )
            except TimeoutError:
                proc.kill()
                return ToolResult(
                    success=False,
                    message=f"命令执行超时({timeout}s)，已终止",
                    data="",
                )
        except Exception as e:
            return ToolResult(
                success=False,
                message=f"执行 shell 命令失败: {e}",
                data="",
            )

    @tool(
        name="shell_list_dir",
        description="列出指定目录下的文件和子目录",
        params={
            "path": {"type": "string", "description": "目录路径，默认当前目录"},
        },
        required=[],
    )
    async def shell_list_dir(self, path: Optional[str] = None) -> ToolResult:
        try:
            target = os.path.join(self.working_dir, path) if path else self.working_dir
            if not os.path.exists(target):
                return ToolResult(success=False, message=f"目录不存在: {target}")
            entries = []
            for name in sorted(os.listdir(target)):
                full = os.path.join(target, name)
                info = "d" if os.path.isdir(full) else "f"
                entries.append(f"{info} {name}")
            return ToolResult(success=True, data="\n".join(entries), message=f"列出 {target} 成功")
        except Exception as e:
            return ToolResult(success=False, message=f"列出目录失败: {e}")

    @tool(
        name="shell_read_file",
        description="读取文本文件内容",
        params={
            "path": {"type": "string", "description": "文件路径"},
        },
        required=["path"],
    )
    async def shell_read_file(self, path: str) -> ToolResult:
        try:
            target = os.path.join(self.working_dir, path) if not os.path.isabs(path) else path
            if not os.path.exists(target):
                return ToolResult(success=False, message=f"文件不存在: {target}")
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return ToolResult(success=True, data=content, message=f"读取 {target} 成功({len(content)} 字符)")
        except Exception as e:
            return ToolResult(success=False, message=f"读取文件失败: {e}")

    @tool(
        name="shell_write_file",
        description="写入文本文件（覆盖或创建）",
        params={
            "path": {"type": "string", "description": "文件路径"},
            "content": {"type": "string", "description": "文件内容"},
        },
        required=["path", "content"],
    )
    async def shell_write_file(self, path: str, content: str) -> ToolResult:
        try:
            target = os.path.join(self.working_dir, path) if not os.path.isabs(path) else path
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
            return ToolResult(success=True, data=target, message=f"写入 {target} 成功({len(content)} 字符)")
        except Exception as e:
            return ToolResult(success=False, message=f"写入文件失败: {e}")


try:
    from mcp.server import Server
    from mcp.server.stdio import serve_standard_input_output
    from mod.sandbox import Sandbox

    _HAS_MCP_SANDBOX = True
except Exception:
    _HAS_MCP_SANDBOX = False


if _HAS_MCP_SANDBOX:

    class ShellTool:
        name = "linux-sandbox-shell"

        def __init__(self):
            self.server = Server(self.name)
            self.sandbox = None
            self._register_tools()

        def _register_tools(self):
            for name in dir(self):
                method = getattr(self, name)
                if hasattr(method, "_tool_schema"):
                    self.server.tool()(method)

        @tool(
            name="start_sandbox",
            description="启动 Linux 沙箱环境，必须使用 root 权限运行",
            params={},
            required=[],
        )
        async def start_sandbox(self) -> str:
            if not hasattr(os, "getuid"):
                return "当前系统不支持 Linux 沙箱 (Windows/Mac 不支持 OverlayFS)"
            if os.getuid() != 0:
                return "错误：必须使用 root 权限运行沙箱"

            self.sandbox = Sandbox()
            self.sandbox.start()
            return f"沙箱启动成功 ID={self.sandbox.sandbox_id}"

        @tool(
            name="exec_command",
            description="在沙箱中执行 shell 命令",
            params={
                "command": {"type": "string", "description": "要执行的 shell 命令"},
                "user": {"type": "string", "description": "执行命令的用户，默认 root"},
            },
            required=["command"],
        )
        async def exec_command(self, command: str, user: str = "root") -> str:
            if not self.sandbox:
                return "沙箱未启动，使用本地 shell"
            res = self.sandbox.exec_command([command], run_as_user=user)
            return f"STDOUT:\n{res.stdout}\n\nSTDERR:\n{res.stderr}"

        @tool(
            name="view_history",
            description="查看沙箱执行历史，支持关键词过滤",
            params={
                "keyword": {"type": "string", "description": "过滤关键词，为空则查看全部"}
            },
            required=[],
        )
        async def view_history(self, keyword: Optional[str] = None) -> str:
            if not self.sandbox:
                return "沙箱未运行"
            return self.sandbox.view(keyword)

        @tool(name="stop_sandbox", description="停止并销毁沙箱", params={}, required=[])
        async def stop_sandbox(self) -> str:
            if self.sandbox:
                self.sandbox.stop()
                self.sandbox = None
                return "沙箱已安全销毁"
            return "沙箱未运行"

        async def run(self):
            await serve_standard_input_output(self.server)

    if __name__ == "__main__":
        mcp_server = ShellTool()
        asyncio.run(mcp_server.run())
