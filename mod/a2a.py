from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional
import uuid

import httpx
from pydantic import BaseModel, Field

from mod.mcp import BaseTool, ToolResult, tool


class A2AServerConfig(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    base_url: str
    enabled: bool = True


class A2AConfig(BaseModel):
    a2aServers: List[A2AServerConfig] = []


class A2AClientManager:
    def __init__(self, a2a_config: Optional[A2AConfig] = None):
        self.a2a_config = a2a_config
        self.exit_stack: AsyncExitStack = AsyncExitStack()
        self.client: Optional[httpx.AsyncClient] = None
        self.agent_cards: Dict[str, Any] = {}
        self.inited: bool = False

    @property
    def a2a_agent_cards(self) -> Dict[str, Any]:
        return self.agent_cards

    async def get_a2a_agent_cards(self) -> None:
        if not self.a2a_config:
            return
        for a2a_server in self.a2a_config.a2aServers:
            if not a2a_server.enabled:
                continue
            try:
                agent_card_response = await self.client.get(
                    f"{a2a_server.base_url}/.well-known/agent-card.json"
                )
                agent_card_response.raise_for_status()
                agent_card = agent_card_response.json()

                agent_card["enabled"] = a2a_server.enabled

                self.agent_cards[a2a_server.id] = agent_card
                print(self.agent_cards)
            except Exception as e:
                print(f"加载 A2A client 失败 {a2a_server.id} {e}")
                continue

    async def invoke(self, agent_id: str, query: str) -> ToolResult:
        if agent_id not in self.agent_cards:
            return ToolResult(success=False, message=f"远程Agent不存在 {agent_id}")

        agent_card = self.agent_cards.get(agent_id)
        url = agent_card.get("url")
        if not url:
            base_url = ""
            if self.a2a_config:
                for s in self.a2a_config.a2aServers:
                    if s.id == agent_id:
                        base_url = s.base_url
                        break
            url = f"{base_url}/message"

        try:
            agent_response = await self.client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": str(uuid.uuid4()),
                    "method": "message/send",
                    "params": {
                        "message": {
                            "messageId": str(uuid.uuid4()),
                            "role": "user",
                            "parts": [{"text": query}],
                        }
                    },
                },
            )

            agent_response.raise_for_status()
            result = agent_response.json()

            return ToolResult(success=True, message="调用远程Agent成功", data=result)
        except Exception as e:
            print(f"调用远程 Agent 失败 {agent_id}:{url}:{e}")
            return ToolResult(
                success=False, message=f"调用远程 Agent 失败 {agent_id}:{url}:{e}"
            )

    async def init(self) -> None:
        if self.inited:
            return
        try:
            self.client = await self.exit_stack.enter_async_context(
                httpx.AsyncClient(timeout=60)
            )
            if self.a2a_config:
                print(f"加载{len(self.a2a_config.a2aServers)}个A2A服务")
            await self.get_a2a_agent_cards()
            print("A2A客户端加载成功")
            self.inited = True
        except Exception as e:
            print(e)
            print("A2A客户端初始化失败(非致命,继续运行)")
            self.inited = True

    async def cleanup(self) -> None:
        await self.exit_stack.aclose()
        self.agent_cards.clear()
        self.inited = False
        print("A2A清理缓存成功")


class A2ATool(BaseTool):
    name = "a2a"

    def __init__(self):
        super().__init__()
        self.inited: bool = False
        self.manager: Optional[A2AClientManager] = None

    async def init(self, a2a_config: Optional[A2AConfig] = None) -> None:
        if not self.inited:
            self.manager = A2AClientManager(a2a_config=a2a_config)
            await self.manager.init()
            self.inited = True

    @tool(
        name="get_remote_agent_cards",
        description="获取远程可调用的Agent卡片信息，包含id，名称，描述，技能，请求地址等",
        params={},
        required=[],
    )
    async def get_remote_cards(self) -> ToolResult:
        agent_cards = []
        if not self.manager:
            return ToolResult(success=False, message="A2A管理器未初始化")

        for id, agent_card in self.manager.agent_cards.items():
            agent_cards.append({"id": id, **agent_card})

        return ToolResult(
            success=True, message="返回 agent_cards 成功", data=agent_cards
        )

    @tool(
        name="call_remote_agent",
        description="调用远程agent，传入id+query，返回结果",
        params={
            "id": {"type": "string", "description": "调用远程 Agent 的 id 标识"},
            "query": {
                "type": "string",
                "description": "分配给该远程 Agent 的任务/需求query",
            },
        },
        required=["id", "query"],
    )
    async def call_remote_agent(self, id: str, query: str) -> ToolResult:
        if not self.manager:
            return ToolResult(success=False, message="A2A管理器未初始化")
        return await self.manager.invoke(agent_id=id, query=query)


class HTTPA2AServerItem(BaseModel):
    id: str = ""
    name: str = ""
    description: str = ""
    input_modes: List[str] = []
    output_modes: List[str] = []
    streaming: bool = False
    enabled: bool = True


class HTTPA2AServerResponse(BaseModel):
    a2a_servers: List[HTTPA2AServerItem] = []
