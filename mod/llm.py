import os
from typing import Any, Dict, Optional, Protocol, List
import uuid
from filelock import FileLock
from openai import AsyncOpenAI
import dotenv
from pathlib import Path
from pydantic import BaseModel
import yaml

from mod.a2a import A2AClientManager, A2AConfig, A2AServerConfig, HTTPA2AServerItem
from mod.mcp import  MCPClientManager, MCPConfig, MCPTransport

dotenv.load_dotenv()

"""
和模型相关的配置，比如 llm 的配置，mcp 的配置，存放到 config.yml 中
llm 是静态配置，模型名，base_url，密钥
mcp 则统一管理，链接远程 mcp
"""
class HTTPMCPServerItem(BaseModel):
    server_name: str = ""
    enabled: bool = True
    transport: MCPTransport = MCPTransport.STREAMABLE_HTTP
    tools: List[str] = []


class HTTPMCPServerResponse(BaseModel):
    mcp_servers: List[HTTPMCPServerItem] = []


class LLMConfig(BaseModel):
    base_url: str = "https://token-plan-cn.xiaomimimo.com/v1"
    api_key: str = "tp-cg00wuaxvwmh1gwiib9zettd8rz1yno7gmr6uvq3bg6z6f46"
    model_name: str = "mimo-v2.5"


class AppConfig(BaseModel):
    llm_config: LLMConfig = LLMConfig()
    mcp_config: MCPConfig = MCPConfig()
    a2a_config: A2AConfig = A2AConfig()


class LLM(Protocol):
    async def invoke(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        response_format: Optional[Dict[str, Any]] = None,
        tool_choice: Optional[str] = None,
    ) -> Dict[str, Any]: ...


class AppConfigService:
    def __init__(self):
        root_dir = Path.cwd()
        self.config_path = root_dir / "config.yml"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_file = self.config_path.with_suffix(".lock")

    def create_default_app_config(self):
        if not self.config_path.exists():
            default_app_config = AppConfig(
                llm_config=LLMConfig(), mcp_config=MCPConfig(), a2a_config=A2AConfig()
            )
            self.save(default_app_config)

    def load(self) -> AppConfig:
        self.create_default_app_config()
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                if data:
                    return AppConfig.model_validate(data)
                return AppConfig()
        except Exception as e:
            print(f"读取应用配置失败 {e}")
            return AppConfig()

    def save(self, app_config: AppConfig) -> Optional[AppConfig]:
        lock = FileLock(self.lock_file, timeout=5)
        try:
            with lock:
                data = app_config.model_dump(mode="json")
                with open(self.config_path, "w", encoding="utf-8") as f:
                    yaml.dump(data, f, allow_unicode=True, sort_keys=False)
                    return AppConfig.model_validate(data)
        except Exception as e:
            print(f"写入应用配置失败 {e}")
            return None

    def get_app_config(self) -> AppConfig:
        return self.load()

    def get_llm_config(self) -> LLMConfig:
        app_config = self.load()
        return app_config.llm_config

    def update_llm_config(self, llm_config: LLMConfig) -> LLMConfig:
        app_config = self.get_app_config()

        if not llm_config.api_key.strip():
            llm_config.api_key = app_config.llm_config.api_key
        app_config.llm_config = llm_config
        self.save(app_config)
        return app_config.llm_config

    async def upset_mcp_servers(self, mcp_config: MCPConfig) -> MCPConfig:
        app_config = self.load()
        app_config.mcp_config.mcpServers.update(mcp_config.mcpServers)
        self.save(app_config)
        return app_config.mcp_config

    async def delete_mcp_servers(self, name: str) -> MCPConfig:
        app_config = self.load()
        if name not in app_config.mcp_config.mcpServers:
            raise ValueError(f"MCP不存在 {name}")
        del app_config.mcp_config.mcpServers[name]
        self.save(app_config)
        return app_config.mcp_config

    async def set_mcp_enabled(self, name: str) -> MCPConfig:
        app_config = self.load()
        if name not in app_config.mcp_config.mcpServers:
            raise ValueError(f"MCP不存在 {name}")
        app_config.mcp_config.mcpServers[
            name
        ].enable = not app_config.mcp_config.mcpServers[name].enable
        self.save(app_config)
        return app_config.mcp_config

    async def get_mcp_servers(self) -> List[HTTPMCPServerItem]:
        app_config = self.load()
        mcp_servers = []
        mcp_client_manager = MCPClientManager(mcp_config=app_config.mcp_config)

        try:
            await mcp_client_manager.init()
            tools = mcp_client_manager.tool_list

            for server_name, server_config in app_config.mcp_config.mcpServers.items():
                tool_list = tools.get(server_name, [])
                tool_names = []
                for tool_item in tool_list:
                    if hasattr(tool_item, "name"):
                        tool_names.append(tool_item.name)
                mcp_servers.append(
                    HTTPMCPServerItem(
                        server_name=server_name,
                        enabled=server_config.enable,
                        transport=server_config.transport,
                        tools=tool_names,
                    )
                )
        finally:
            await mcp_client_manager.cleanup()

        return mcp_servers

    async def add_a2a_server(self, base_url: str) -> A2AConfig:
        app_config = self.load()

        a2a_server_config = A2AServerConfig(
            id=str(uuid.uuid4()), base_url=base_url, enabled=True
        )

        app_config.a2a_config.a2aServers.append(a2a_server_config)
        self.save(app_config)
        return app_config.a2a_config

    async def get_a2a_servers(self) -> List[HTTPA2AServerItem]:
        app_config = self.load()
        a2a_servers = []
        a2a_client_manager = A2AClientManager(a2a_config=app_config.a2a_config)

        try:
            await a2a_client_manager.init()
            agent_cards = a2a_client_manager.a2a_agent_cards
            for id, agent_card in agent_cards.items():
                capabilities = agent_card.get("capabilities") or {}
                enabled_map = {}
                for s in app_config.a2a_config.a2aServers:
                    enabled_map[s.id] = s.enabled
                a2a_servers.append(
                    HTTPA2AServerItem(
                        id=id,
                        name=agent_card.get("name", ""),
                        description=agent_card.get("description", ""),
                        input_modes=agent_card.get("defaultInputModes", []),
                        output_modes=agent_card.get("defaultOutputModes", []),
                        streaming=capabilities.get("streaming", False),
                        enabled=enabled_map.get(id, True),
                    )
                )
        finally:
            await a2a_client_manager.cleanup()
        return a2a_servers

    async def set_a2a_server_enabled(self, id: str) -> A2AConfig:
        app_config = self.load()

        idx = None
        for i, item in enumerate(app_config.a2a_config.a2aServers):
            if item.id == id:
                idx = i
                break

        if idx is not None:
            app_config.a2a_config.a2aServers[
                idx
            ].enabled = not app_config.a2a_config.a2aServers[idx].enabled
            self.save(app_config)
        return app_config.a2a_config

    async def remove_a2a_server(self, id: str) -> A2AConfig:
        app_config = self.load()

        idx = None
        for i, item in enumerate(app_config.a2a_config.a2aServers):
            if item.id == id:
                idx = i
                break

        if idx is not None:
            del app_config.a2a_config.a2aServers[idx]
            self.save(app_config)
        return app_config.a2a_config


class OpenAILLM:
    def __init__(self) -> None:
        config_service = AppConfigService()
        llm_config = config_service.get_llm_config()
        self.client = AsyncOpenAI(
            base_url=llm_config.base_url, api_key=llm_config.api_key
        )
        self.model_name = llm_config.model_name

    async def invoke(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        response_format: Optional[Dict[str, Any]] = None,
        tool_choice: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            kwargs: Dict[str, Any] = {
                "model": self.model_name,
                "messages": messages,
            }
            if response_format:
                kwargs["response_format"] = response_format
            if tools:
                kwargs["tools"] = tools
                kwargs["parallel_tool_calls"] = False
            if tool_choice:
                if tool_choice == "none":
                    kwargs["tool_choice"] = "none"
                else:
                    kwargs["tool_choice"] = tool_choice

            response = await self.client.chat.completions.create(**kwargs)
            return response.choices[0].message.model_dump()
        except Exception as e:
            print(f"调用LLM报错 {e}")
            raise ValueError(f"调用LLM报错: {e}")



if __name__ == "__main__":
    import asyncio

    async def main():
        llm = OpenAILLM()
        response = await llm.invoke([{"role": "user", "content": "Hi"}])
        print(response)

    asyncio.run(main())
