from enum import Enum
from datetime import datetime
from pydantic import BaseModel, Field
from mod.mcp import ToolResult
from typing import Annotated, List, Dict, Any, Literal, Optional, Union
import uuid

"""
Event Stream 类型定义

主要分四种 Event
1. Task(后台总任务) -> Plan(计划) -> Step(步骤-子任务) -> Tool(工具)
每个 Event 都有创建，完成，错误，等类型
"""
class ExecStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Step(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    status: ExecStatus = ExecStatus.PENDING
    result: Optional[str] = None
    error: Optional[str] = None
    success: bool = False
    files: List[str] = Field(default_factory=list)

    @property
    def done(self) -> bool:
        return self.status in [ExecStatus.COMPLETED, ExecStatus.FAILED]


class File(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    path: str = ""
    url: str = ""
    ext: str = ""
    size: int = 0


class Message(BaseModel):
    message: str = ""
    files: List[str] = Field(default_factory=list)


class Plan(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    goal: str = ""
    lang: str = ""
    steps: List[Step] = Field(default_factory=list)
    message: str = ""
    status: ExecStatus = ExecStatus.PENDING
    error: Optional[str] = None

    @property
    def done(self) -> bool:
        return self.status in [ExecStatus.COMPLETED, ExecStatus.FAILED]

    def next_step(self) -> Optional[Step]:
        return next((step for step in self.steps if not step.done), None)


class PlanEventStatus(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    DONE = "done"


class StepEventStatus(str, Enum):
    STARTED = "started"
    DONE = "done"
    FAILED = "failed"


class ToolEventStatus(str, Enum):
    CALLING = "calling"
    CALLED = "called"


class BaseEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: Literal[""] = ""
    created_at: datetime = Field(default_factory=datetime.now)


class PlanEvent(BaseEvent):
    type: Literal["plan"] = "plan"
    plan: Plan = Field(default_factory=Plan)
    status: PlanEventStatus = PlanEventStatus.CREATED


class TitleEvent(BaseEvent):
    type: Literal["title"] = "title"
    title: str = ""


class StepEvent(BaseEvent):
    type: Literal["step"] = "step"
    step: Step = Field(default_factory=Step)
    status: StepEventStatus = StepEventStatus.STARTED


class MessageEvent(BaseEvent):
    type: Literal["message"] = "message"
    role: Literal["user", "assistant"] = "assistant"
    message: str = ""
    files: List[File] = Field(default_factory=list)


class MCPToolContent(BaseModel):
    result: Any = None


ToolContent = Union[MCPToolContent]


class ToolEvent(BaseEvent):
    type: Literal["tool"] = "tool"
    tool_call_id: str = ""
    tool_name: str = ""
    tool_content: Optional[ToolContent] = None
    function_name: str = ""
    function_args: Dict[str, Any] = Field(default_factory=dict)
    result: Optional[ToolResult] = None
    status: Optional[ToolEventStatus] = None


class WaitEvent(BaseEvent):
    type: Literal["wait"] = "wait"


class ErrorEvent(BaseEvent):
    type: Literal["error"] = "error"
    error: str = ""


class DoneEvent(BaseEvent):
    type: Literal["done"] = "done"


Event = Annotated[
    Union[
        PlanEvent,
        TitleEvent,
        StepEvent,
        MessageEvent,
        ToolEvent,
        WaitEvent,
        ErrorEvent,
        DoneEvent,
    ],
    Field(discriminator="type"),
]
