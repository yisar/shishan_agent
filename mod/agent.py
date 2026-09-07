from abc import ABC
import asyncio
from typing import Any, AsyncGenerator, Dict, List, Optional
import uuid
import json_repair

from mod.event import (
    ErrorEvent,
    Event,
    MessageEvent,
    PlanEvent,
    PlanEventStatus,
    StepEvent,
    StepEventStatus,
    ToolEvent,
    ToolEventStatus,
    WaitEvent,
)
from mod.memory import Memory
from mod.prompt import (
    CREATE_PLAN_PROMPT,
    PLAN_AGENT_PROMPT,
    REACT_EXEC_PROMPT,
    REACT_SUMMARY_PROMPT,
    REACT_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    UPDATE_PLAN_PROMPT,
)
from mod.event import File, ExecStatus, Message, Plan, Step
from mod.mcp import BaseTool, ToolResult
from mod.session import SessionRepo

"""
Multi Agent Workflow = PlanAgent + ReActAgent

PlanAgent: 将用户消息拆分为多个子任务+根据已经完成的子任务更新Plan，提示词：创建规划prompt，更新规划prompt
ReActAgent：循环迭代执行完每一个子任务，最终汇总所有子任务，提示词:执行任务 prompt，汇总子任务prompt

1. PlanAgent 生成规划
2. 循环取出规划的子步骤，让 ReActAgent 循环执行
3. ReActAgent 执行完子步骤后，需要将结果传递给 PlanAgent 让其更新规划
4. 循环 2 3 ...
5. 所有子步骤全都完成，ReActAgent 将子步骤的所有结果进行汇总总结

"""


class BaseAgent(ABC):
    name: str = ""
    _system_prompt: str = ""
    _format: Optional[str] = None
    _retry_interval: float = 1.0
    _tool_choice: Optional[str] = None

    def __init__(
        self,
        session_id: str,
        session: SessionRepo,
        llm,
        tools: List[BaseTool],
    ) -> None:
        self.session_id = session_id
        self.session = session
        self._llm = llm
        self._memory: Optional[Memory] = None
        self._tools = tools

    async def ensure_memory(self) -> None:
        if self._memory is None:
            self._memory = await self.session.get_memory(self.session_id, self.name)

    async def compact_memory(self) -> None:
        await self.ensure_memory()
        if self._memory:
            self._memory.compact()
            await self.session.save_memory(self.session_id, self.name, self._memory)

    async def roll_back(self, message: Message) -> None:
        await self.ensure_memory()
        last_message = self._memory.get_last_message()
        if (
            not last_message
            or not last_message.get("tool_calls")
            or len(last_message.get("tool_calls", [])) == 0
        ):
            return
        tool_call = last_message.get("tool_calls")[0]
        function_name = tool_call.get("function", {}).get("name")
        tool_call_id = tool_call.get("id")

        if function_name == "message_ask_user":
            self._memory.add_message(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "function_name": function_name,
                    "content": message.model_dump_json(),
                }
            )
        else:
            self._memory.roll_back(message)

        await self.session.save_memory(self.session_id, self.name, self._memory)

    async def add_to_memory(self, messages: List[Dict[str, Any]]):
        await self.ensure_memory()
        if self._memory.empty:
            self._memory.add_message({"role": "user", "content": self._system_prompt})
        self._memory.add_messages(messages)
        await self.session.save_memory(
            session_id=self.session_id, agent_name=self.name, memory=self._memory
        )

    async def invoke_llm(self, messages: List[Dict[str, Any]], fmt: Optional[str] = None):
        await self.add_to_memory(messages)

        response_format = {"type": fmt} if fmt else None
        last_error = None

        for _ in range(3):
            try:
                message = await self._llm.invoke(
                    messages=self._memory.get_messages(),
                    tools=self.get_available_tools(),
                    response_format=response_format,
                    tool_choice=self._tool_choice,
                )
                if message.get("role") == "assistant":
                    content = message.get("content")
                    tool_calls = message.get("tool_calls")
                    if (not content or (isinstance(content, str) and not content.strip())) and not tool_calls:
                        print("模型回复空内容，重试")
                        await asyncio.sleep(1)
                        continue
                    real_message = {
                        "role": "assistant",
                        "content": content,
                    }
                    if message.get("reasoning_content"):
                        real_message["reasoning_content"] = message.get("reasoning_content")
                    if tool_calls:
                        real_message["tool_calls"] = tool_calls[:1]
                    self._memory.add_message(real_message)
                    await self.session.save_memory(self.session_id, self.name, self._memory)
                    return real_message
                else:
                    self._memory.add_message(message)
                    await self.session.save_memory(self.session_id, self.name, self._memory)
                    return message
            except Exception as e:
                last_error = e
                print(f"调用llm报错 {e}")
                await asyncio.sleep(1)
                continue
        raise RuntimeError(f"调用 LLM 失败：{last_error}")

    async def invoke_tool(
        self, tool: BaseTool, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        err = ""
        for _ in range(3):
            try:
                return await tool.invoke(tool_name, **arguments)
            except Exception as e:
                err = str(e)
                print(f"调用工具 [{tool_name}] 出错，{e}")
                await asyncio.sleep(1)
                continue
        return ToolResult(success=False, message=err)

    def get_tool(self, tool_name: str) -> BaseTool:
        for tool in self._tools:
            if tool.has_tool(tool_name):
                return tool
        raise ValueError(f"未知工具 {tool_name}")

    def get_available_tools(self) -> List[Dict[str, Any]]:
        available_tools = []
        for tool in self._tools:
            available_tools.extend(tool.get_tools())
        return available_tools

    async def invoke(
        self, query: str, fmt: Optional[str] = None
    ) -> AsyncGenerator[Event, None]:
        fmt = fmt if fmt else self._format
        message = await self.invoke_llm([{"role": "user", "content": query}], fmt)

        for _ in range(100):
            if not message.get("tool_calls"):
                break
            tool_messages = []

            for tool_call in message["tool_calls"]:
                if not tool_call.get("function"):
                    continue
                tool_call_id = tool_call["id"] or str(uuid.uuid4())
                function_name = tool_call["function"]["name"]
                function_args = json_repair.loads(tool_call["function"].get("arguments", "{}") or "{}")

                tool = self.get_tool(function_name)
                yield ToolEvent(
                    tool_call_id=tool_call_id,
                    tool_name=tool.name,
                    function_name=function_name,
                    function_args=function_args,
                    status=ToolEventStatus.CALLING,
                )

                result = await self.invoke_tool(tool, function_name, function_args)

                yield ToolEvent(
                    tool_call_id=tool_call_id,
                    tool_name=tool.name,
                    function_name=function_name,
                    function_args=function_args,
                    result=result,
                    status=ToolEventStatus.CALLED,
                )

                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "function_name": function_name,
                        "content": result.model_dump_json(),
                    }
                )

            message = await self.invoke_llm(tool_messages)
        else:
            yield ErrorEvent(error="agent迭代超过最大迭代次数")

        yield MessageEvent(message=message.get("content") or "")


class PlanAgent(BaseAgent):
    name: str = "planner"
    _system_prompt: str = SYSTEM_PROMPT + PLAN_AGENT_PROMPT
    _format: Optional[str] = "json_object"
    _tool_choice: Optional[str] = "none"

    async def create_plan(self, message: Message) -> AsyncGenerator[Event, None]:
        files_str = "\n".join(message.files) if message.files else ""
        query = CREATE_PLAN_PROMPT.format(
            message=message.message,
            files=files_str,
        )
        async for event in self.invoke(query):
            if isinstance(event, MessageEvent):
                print(f"PlanAgent生成消息 {event.message}")
                try:
                    parsed_obj = json_repair.loads(event.message)
                    plan_dict = {
                        "id": str(uuid.uuid4()),
                        "title": parsed_obj.get("title", ""),
                        "goal": parsed_obj.get("goal", ""),
                        "lang": parsed_obj.get("lang", "zh"),
                        "message": parsed_obj.get("message", ""),
                        "status": ExecStatus.PENDING,
                        "steps": [],
                    }
                    for step_data in parsed_obj.get("steps", []):
                        plan_dict["steps"].append({
                            "id": step_data.get("id", str(uuid.uuid4())),
                            "description": step_data.get("description", ""),
                            "status": ExecStatus.PENDING,
                            "result": None,
                            "error": None,
                            "success": False,
                            "files": [],
                        })
                    plan = Plan(**plan_dict)
                    yield PlanEvent(plan=plan, status=PlanEventStatus.CREATED)
                except Exception as e:
                    print(f"解析 Plan 失败: {e}")
                    yield ErrorEvent(error=f"解析 Plan 失败: {e}")
            else:
                yield event

    async def update_plan(self, plan: Plan, step: Step) -> AsyncGenerator[Event, None]:
        step_dict = step.model_dump(mode="json") if isinstance(step, Step) else step
        plan_dict = plan.model_dump(mode="json") if isinstance(plan, Plan) else plan
        query = UPDATE_PLAN_PROMPT.format(
            plan=json_repair.dumps(plan_dict),
            step=json_repair.dumps(step_dict),
        )
        async for event in self.invoke(query):
            if isinstance(event, MessageEvent):
                print(f"PlanAgent更新消息 {event.message}")
                try:
                    parsed_obj = json_repair.loads(event.message)
                    steps_data = parsed_obj.get("steps", [])
                    new_steps = [Step(**s) for s in steps_data]
                    first_pending_index = None
                    for idx, s in enumerate(plan.steps):
                        if not s.done:
                            first_pending_index = idx
                            break
                    if first_pending_index is not None:
                        updated_steps = plan.steps[:first_pending_index]
                        updated_steps.extend(new_steps)
                        plan.steps = updated_steps
                    yield PlanEvent(plan=plan, status=PlanEventStatus.UPDATED)
                except Exception as e:
                    print(f"解析更新 Plan 失败: {e}")
                    yield ErrorEvent(error=f"解析更新 Plan 失败: {e}")
            else:
                yield event


class ReActAgent(BaseAgent):
    name: str = "re-act"
    _system_prompt: str = SYSTEM_PROMPT + REACT_SYSTEM_PROMPT
    _format: Optional[str] = "json_object"

    async def execute_step(
        self, plan: Plan, step: Step, message: Message
    ) -> AsyncGenerator[Event, None]:
        query = REACT_EXEC_PROMPT.format(
            message=message.message,
            files="\n".join(message.files),
            lang=plan.lang,
            step=step.description,
        )

        step.status = ExecStatus.STARTED
        yield StepEvent(step=step, status=StepEventStatus.STARTED)

        async for event in self.invoke(query=query):
            if isinstance(event, ToolEvent):
                if event.function_name == "message_ask_user":
                    if event.status == ToolEventStatus.CALLING:
                        yield MessageEvent(
                            role="assistant",
                            message=event.function_args.get("text", ""),
                        )
                    elif event.status == ToolEventStatus.CALLED:
                        yield WaitEvent()
                        return
                    continue

            elif isinstance(event, MessageEvent):
                content = event.message
                if content:
                    step.status = ExecStatus.COMPLETED
                    try:
                        parsed_obj = json_repair.loads(content)
                        new_step = Step(**parsed_obj) if isinstance(parsed_obj, dict) else parsed_obj

                        step.success = getattr(new_step, "success", False)
                        step.result = getattr(new_step, "result", None)
                        step.files = list(getattr(new_step, "files", []) or [])
                    except Exception as e:
                        print(f"ReActAgent结果解析警告(非致命): {e}")
                        step.success = True
                        step.result = content

                    yield StepEvent(step=step, status=StepEventStatus.DONE)

                    if step.result:
                        yield MessageEvent(role="assistant", message=str(step.result))
                continue
            elif isinstance(event, ErrorEvent):
                step.status = ExecStatus.FAILED
                step.error = event.error

                yield StepEvent(step=step, status=StepEventStatus.FAILED)
            yield event

        step.status = ExecStatus.COMPLETED

    async def summarize(
        self,
    ) -> AsyncGenerator[Event, None]:
        query = REACT_SUMMARY_PROMPT

        async for event in self.invoke(query=query):
            if isinstance(event, MessageEvent):
                print(f"Agent 汇总内容 {event.message}")
                msg_text = event.message
                files_list: List[File] = []
                try:
                    parsed_obj = json_repair.loads(event.message)
                    if isinstance(parsed_obj, dict):
                        if "message" in parsed_obj:
                            msg_text = str(parsed_obj["message"])
                        if "files" in parsed_obj and isinstance(parsed_obj["files"], list):
                            for f in parsed_obj["files"]:
                                if isinstance(f, str):
                                    files_list.append(File(path=f, name=f.split("/")[-1]))
                                elif isinstance(f, dict):
                                    files_list.append(File(**f))
                except Exception as e:
                    print(f"解析汇总结果警告(非致命): {e}")
                yield MessageEvent(
                    role="assistant",
                    message=msg_text,
                    files=files_list,
                )
            else:
                yield event
