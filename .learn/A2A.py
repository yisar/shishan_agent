from openai import AsyncOpenAI
from a2a.server.agent_execution import AgentExecutor
from a2a.utils import new_agent_text_message
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.apps import A2AStarletteApplication
import uvicorn
import httpx
import uuid
import asyncio

"""
实现A2A协议，Agent to Agent，可以调用远程 Agent
我们的 PlanAgent 和 ReActAgent 也是通过这个协议做的，但可以自举

也就是说，当前我们实现了 MCP 协议，用来调用远程 tool 接口，实现了 A2A 协议，用来调用远程 Agent
对于 cloud agent 来说，已经足够了，但对于 local agent，还缺 skill 系统
思考：美团小美这种 c 端 agent，不需要完成 shell 任务的话，它还需要 skill 吗，它需要啥？
"""


class MimoAgent:
    @classmethod
    async def invoke(cls, query: str) -> str:
        client = AsyncOpenAI(
            base_url="https://token-plan-cn.xiaomimimo.com/v1",
            api_key="tp-c23kbkelg29wchgw7j9y0px3s6bcnqwkrf20ea3lpez5xwou",
        )

        response = await client.chat.completions.create(
            model="mimo-v2.5", messages=[{"role": "user", "content": query}]
        )

        return response.choices[0].message.content


class MimoAgentExecutor(AgentExecutor):
    def __init__(self):
        self.agent = MimoAgent()

    async def execute(self, context, event_queue):
        try:
            part = context.message.parts[0]
            query = part.root.text if hasattr(part, 'root') else part.text
        except Exception:
            query = "给我生成10个随机数"  # 保底

        answer = await self.agent.invoke(query)
        await event_queue.enqueue_event(new_agent_text_message(answer))

    async def cancel(self, context, event_queue):
        raise Exception("暂不支持取消")


base_url = "http://127.0.0.1:9999"


async def main():
    await asyncio.sleep(1)

    async with httpx.AsyncClient(timeout=60) as httpx_client:
        print("正在请求 Agent Card...")
        agent_card_response = await httpx_client.get(
            f"{base_url}/.well-known/agent-card.json"
        )
        agent_card_response.raise_for_status()
        print("Agent Card ", agent_card_response.json())

        agent_card = agent_card_response.json()
        url = agent_card.get("url")

        request_body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": str(uuid.uuid4()),
                    "role": "user",
                    "parts": [{"text": "给我生成10个随机数"}],
                }
            },
        }

        print("正在发送 Agent 请求...")
        agent_response = await httpx_client.post(f"{url}", json=request_body)
        agent_response.raise_for_status()
        
        print("收到响应: ", agent_response.json())


async def run_server_and_client(server_app):
    config = uvicorn.Config(server_app, host="0.0.0.0", port=9999, log_level="info")
    server = uvicorn.Server(config)
    
    server_task = asyncio.create_task(server.serve())
    
    # try:
    #     await main()
    # finally:
    #     server.should_exit = True
    await server_task


if __name__ == "__main__":
    skill = AgentSkill(
        id="calculator",
        name="计算器",
        description="支持计算各种数学表达式",
        tags=["计算器"],
        examples=["445*34", "211/34+12"],
    )

    agent_card = AgentCard(
        name="Momo智能体",
        description="这是一个可以调用momo大模型的智能体，需要复杂任务的时候调用",
        version="1.0.0",
        url=base_url,
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=False),
        skills=[skill],
        supports_authenticated_extended_card=False,
    )

    request_handler = DefaultRequestHandler(
        agent_executor=MimoAgentExecutor(), task_store=InMemoryTaskStore()
    )

    server = A2AStarletteApplication(
        agent_card=agent_card, http_handler=request_handler
    )
    
    server_app = server.build()

    asyncio.run(run_server_and_client(server_app))