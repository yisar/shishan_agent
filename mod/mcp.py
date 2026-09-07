from contextlib import AsyncExitStack
from enum import Enum
import inspect
import os
from mcp import ClientSession, StdioServerParameters, stdio_client
from pydantic import BaseModel, ConfigDict, model_validator
from mcp.client.streamable_http import streamablehttp_client
from typing import Callable, Generic, List, Dict, Any, Optional, TypeVar

"""
MCP 相关，负责增加，删除，修改，管理 mcp 服务
"""
T = TypeVar("T")


class ToolResult(BaseModel, Generic[T]):
    success: bool = True
    message: Optional[str] = ""
    data: Optional[T] = None


def tool(
    name: str, description: str, params: Dict[str, Dict[str, Any]], required: List[str]
) -> Callable:
    def decorator(func):
        tool_schema = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": params,
                    "required": required,
                },
            },
        }

        func._tool_name = name
        func._tool_description = description
        func._tool_schema = tool_schema
        return func

    return decorator


class BaseTool:
    name: str = ""

    def __init__(self) -> None:
        self._tools_cache: Optional[List[Any]] = None

    @classmethod
    def filter_params(cls, method: Callable, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        filtered_kwargs = {}
        sign = inspect.signature(method)

        for k, v in kwargs.items():
            if k in sign.parameters:
                filtered_kwargs[k] = v
        return filtered_kwargs

    async def invoke(self, tool_name: str, **kwargs) -> ToolResult:
        for _, method in inspect.getmembers(self, inspect.ismethod):
            if (
                hasattr(method, "_tool_name")
                and getattr(method, "_tool_name") == tool_name
            ):
                filter_kwargs = self.filter_params(method, kwargs)
                return await method(**filter_kwargs)

        raise ValueError(f"工具[{tool_name}]未找到")

    def has_tool(self, tool_name: str) -> bool:
        for _, method in inspect.getmembers(self, inspect.ismethod):
            if (
                hasattr(method, "_tool_name")
                and getattr(method, "_tool_name") == tool_name
            ):
                return True
        return False

    def get_tools(self):
        if self._tools_cache is not None:
            return self._tools_cache
        tools = []
        for _, method in inspect.getmembers(self, inspect.ismethod):
            if hasattr(method, "_tool_schema"):
                tools.append(getattr(method, "_tool_schema"))

        self._tools_cache = tools
        return tools


class MCPTransport(str, Enum):
    STDIO = "stdio"
    SSE = "sse"
    STREAMABLE_HTTP = "streamable_http"


class McpServerConfig(BaseModel):
    transport: MCPTransport = MCPTransport.STREAMABLE_HTTP
    enable: bool = True
    description: Optional[str] = None
    env: Optional[Dict[str, Any]] = None

    command: Optional[str] = None
    args: Optional[List[str]] = None

    url: Optional[str] = None
    headers: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def validate_mcp_server_config(self):
        if self.transport in [MCPTransport.SSE, MCPTransport.STREAMABLE_HTTP]:
            if not self.url:
                raise ValueError("sse和stream http模式下，必须传递url")
        if self.transport == MCPTransport.STDIO:
            if not self.command:
                raise ValueError("stdio模式下，必须传递command")

        return self


class MCPConfig(BaseModel):
    mcpServers: Dict[str, McpServerConfig] = {}


class MCPClientManager:
    def __init__(self, mcp_config: Optional[MCPConfig]) -> None:
        self.mcp_config = mcp_config
        self.exit_stack: AsyncExitStack = AsyncExitStack()
        self.clients: Dict[str, ClientSession] = {}
        self.tools: Dict[str, List[Any]] = {}
        self.inited: bool = False

    @property
    def tool_list(self) -> Dict[str, List[Any]]:
        return self.tools

    async def init(self) -> None:
        if self.inited:
            return
        try:
            if self.mcp_config:
                print(f"已加载 {len(self.mcp_config.mcpServers)} 个MCP服务")
                await self.connect_mcp_servers()
            self.inited = True
            print("MCP客户端管理器加载成功")
        except Exception as e:
            print(f"MCP客户端管理器加载失败 {e}")

    async def connect_mcp_servers(self) -> None:
        if not self.mcp_config:
            return
        for server_name, server_config in self.mcp_config.mcpServers.items():
            try:
                if not server_config.enable:
                    continue
                transport = server_config.transport
                if transport == MCPTransport.STDIO:
                    await self.connect_stdio_mcp_server(server_name, server_config)
                elif transport == MCPTransport.STREAMABLE_HTTP:
                    await self.connect_streamable_http_mcp_server(
                        server_name, server_config
                    )
            except Exception as e:
                print(f"链接mcp服务器异常 {server_name} {e}")

    async def connect_stdio_mcp_server(self, server_name, server_config):
        command = server_config.command
        args = server_config.args
        env = server_config.env or {}
        if not command:
            raise ValueError(f"MCP服务 {server_name} 缺少 command 参数")
        server_params = StdioServerParameters(
            command=command, args=args, env={**os.environ, **env}
        )
        stdio_transport = await self.exit_stack.enter_async_context(
            stdio_client(server_params)
        )

        read_stream, write_stream = stdio_transport
        session: ClientSession = await self.exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        self.clients[server_name] = session
        await self.cache_mcp_tools(server_name, session)
        print(f"链接MCP服务成功 {server_name}")

    async def connect_streamable_http_mcp_server(self, server_name, server_config):
        url = server_config.url
        if not url:
            raise ValueError(f"MCP服务 {server_name} 缺少 url 参数")

        streamable_http_transport = await self.exit_stack.enter_async_context(
            streamablehttp_client(url=url, headers=server_config.headers)
        )
        if len(streamable_http_transport) == 3:
            read_stream, write_stream, _ = streamable_http_transport
        else:
            read_stream, write_stream = streamable_http_transport
        session: ClientSession = await self.exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        self.clients[server_name] = session
        await self.cache_mcp_tools(server_name, session)
        print(f"链接MCP服务成功 {server_name}")

    async def cache_mcp_tools(self, server_name, session: ClientSession):
        try:
            tools_res = await session.list_tools()
            tools = tools_res.tools if tools_res else []
            self.tools[server_name] = tools
            print(f"MCP服务 {server_name} 提供了 {len(tools)} 个工具")
        except Exception as e:
            print(f"获取 MCP tool list 失败 {server_name} {e}")
            self.tools[server_name] = []

    async def get_all_tools(self) -> List[Dict[str, Any]]:
        all_tools = []
        for server_name, tools in self.tools.items():
            for tool in tools:
                if server_name.startswith("mcp_"):
                    tool_name = f"{server_name}_{tool.name}"
                else:
                    tool_name = f"mcp_{server_name}_{tool.name}"

                tool_schema = {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": f"[{server_name}] {tool.description}",
                        "parameters": getattr(tool, "inputSchema", {"type": "object", "properties": {}}),
                    },
                }
                all_tools.append(tool_schema)
        return all_tools

    async def invoke(self, tool_name: str, args: Dict[str, Any]) -> ToolResult:
        try:
            real_server_name = None
            real_tool_name = None

            if not self.mcp_config:
                raise ValueError(f"工具 {tool_name} 不存在 (MCP未配置)")

            for server_name in self.mcp_config.mcpServers.keys():
                prefix = (
                    server_name
                    if server_name.startswith("mcp_")
                    else "mcp_" + server_name
                )
                if tool_name.startswith(f"{prefix}_"):
                    real_server_name = server_name
                    real_tool_name = tool_name[len(prefix) + 1 :]
                    break

            if not real_server_name or not real_tool_name:
                raise ValueError(f"工具 {tool_name} 不存在")

            session = self.clients.get(real_server_name)
            if not session:
                return ToolResult(
                    success=False,
                    message=f"MCP {real_server_name} 会话不存在 {tool_name}",
                )

            result = await session.call_tool(real_tool_name, args)

            if result:
                content = []
                if hasattr(result, "content") and result.content:
                    for item in result.content:
                        if hasattr(item, "text"):
                            content.append(item.text)
                        else:
                            content.append(str(item))
                return ToolResult(
                    success=True, data="\n".join(content) if content else "工具执行成功"
                )
            else:
                return ToolResult(success=True, data="工具执行成功")
        except Exception as e:
            print(f"调用 MCP Tool {tool_name} 失败 {e}")
            return ToolResult(
                success=False, message=f"调用 MCP Tool {tool_name} 失败 {e}"
            )

    async def cleanup(self) -> None:
        await self.exit_stack.aclose()
        self.clients.clear()
        self.tools.clear()
        self.inited = False
        print("清除MCP缓存成功")


class MCPTool(BaseTool):
    name: str = "mcp"

    def __init__(self) -> None:
        super().__init__()
        self.inited = False
        self.tools_list: List[Dict[str, Any]] = []
        self.manager: Optional[MCPClientManager] = None

    async def init(self, mcp_config: Optional[MCPConfig] = None):
        if not self.inited:
            self.manager = MCPClientManager(mcp_config=mcp_config)
            await self.manager.init()
            self.tools_list = await self.manager.get_all_tools()
            self.inited = True

    def get_tools(self):
        return self.tools_list

    def has_tool(self, tool_name: str) -> bool:
        for tool in self.tools_list:
            if tool.get("function", {}).get("name") == tool_name:
                return True
        return False

    async def invoke(self, tool_name: str, **kwargs) -> ToolResult:
        if not self.manager:
            return ToolResult(success=False, message="MCP管理器未初始化")
        return await self.manager.invoke(tool_name, kwargs)

    async def cleanup(self) -> None:
        if self.manager:
            await self.manager.cleanup()
