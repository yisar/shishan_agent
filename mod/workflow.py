from abc import ABC, abstractmethod
from typing import AsyncGenerator, List, Optional
from enum import Enum

from mod.agent import PlanAgent, ReActAgent
from mod.event import (
    BaseEvent,
    DoneEvent,
    ExecStatus,
    Message,
    MessageEvent,
    Plan,
    PlanEvent,
    PlanEventStatus,
    TitleEvent,
)
from mod.llm import LLM
from mod.mcp import BaseTool, MCPTool
from mod.session import SessionRepo, SessionStatus
from tool.message import MessageTool
from tool.shell import ShellToolWrapper


class FlowStatus(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    UPDATING = "updating"
    SUMMARIZING = "summarizing"
    COMPLETED = "completed"


class BaseFlow(ABC):
    @abstractmethod
    async def invoke(self, message: Message) -> AsyncGenerator[BaseEvent, None]: ...

    @property
    @abstractmethod
    def done(self) -> bool: ...


class WorkFlow(BaseFlow):
    def __init__(
        self,
        session_id: str,
        llm: LLM,
        session: SessionRepo,
    ):
        super().__init__()
        self.session_id = session_id
        self.session = session
        self.status = FlowStatus.IDLE
        self.plan: Optional[Plan] = None

        self.tools: List[BaseTool] = [
            ShellToolWrapper(),
            MessageTool(),
            MCPTool(),
        ]

        self.planner = PlanAgent(
            session_id=session_id, session=session, llm=llm, tools=self.tools
        )

        self.reactor = ReActAgent(
            session_id=session_id, session=session, llm=llm, tools=self.tools
        )
        print(f"初始化两个 Agent 成功 {session_id}")

    async def invoke(self, message: Message) -> AsyncGenerator[BaseEvent, None]:
        session = await self.session.get_by_id(self.session_id)
        if not session:
            raise ValueError(f"会话不存在 {self.session_id}")

        if session.status != SessionStatus.PENDING:
            await self.planner.roll_back(message)
            await self.reactor.roll_back(message)

        if session.status == SessionStatus.RUNNING:
            self.status = FlowStatus.PLANNING

        if session.status == SessionStatus.WAITING:
            self.status = FlowStatus.EXECUTING

        await self.session.update_status(self.session_id, SessionStatus.RUNNING)
        self.plan = session.get_latest_plan()

        step = None

        while True:
            if self.status == FlowStatus.IDLE:
                self.status = FlowStatus.PLANNING
            elif self.status == FlowStatus.PLANNING:
                print("调用PlanAgent")
                async for event in self.planner.create_plan(message=message):
                    if (
                        isinstance(event, PlanEvent)
                        and event.status == PlanEventStatus.CREATED
                    ):
                        self.plan = event.plan
                        print(f"plan创建成功, 包含{len(event.plan.steps)}个步骤")
                        if self.plan.title:
                            yield TitleEvent(title=self.plan.title)
                        if self.plan.message:
                            yield MessageEvent(role="assistant", message=self.plan.message)
                    yield event

                self.status = FlowStatus.EXECUTING
                if not self.plan or len(self.plan.steps) == 0:
                    print("任务创建失败，快进到已完成")
                    self.status = FlowStatus.COMPLETED
            elif self.status == FlowStatus.EXECUTING:
                if self.plan is None:
                    self.status = FlowStatus.COMPLETED
                    continue
                self.plan.status = ExecStatus.RUNNING
                step = self.plan.next_step()
                if not step:
                    self.status = FlowStatus.SUMMARIZING
                    continue
                print(f"开始执行步骤 {step.id}::{step.description[:50]}...")
                async for event in self.reactor.execute_step(self.plan, step, message):
                    yield event
                print(f"压缩记忆 {self.reactor.name}")
                await self.reactor.compact_memory()
                self.status = FlowStatus.UPDATING
            elif self.status == FlowStatus.UPDATING:
                if self.plan is None or step is None:
                    self.status = FlowStatus.COMPLETED
                    continue
                print("开始更新计划")
                async for event in self.planner.update_plan(self.plan, step):
                    yield event
                print("计划更新完成，接下来执行子步骤")
                self.status = FlowStatus.EXECUTING
            elif self.status == FlowStatus.SUMMARIZING:
                print("所有步骤执行完成，总结中")
                async for event in self.reactor.summarize():
                    yield event
                self.status = FlowStatus.COMPLETED
            elif self.status == FlowStatus.COMPLETED:
                if self.plan is not None:
                    self.plan.status = ExecStatus.COMPLETED
                self.status = FlowStatus.IDLE
                if self.plan is not None:
                    yield PlanEvent(status=PlanEventStatus.DONE, plan=self.plan)
                break

        yield DoneEvent()
        print(f"任务流处理完毕 {self.session_id}")

    @property
    def done(self) -> bool:
        return self.status == FlowStatus.IDLE
