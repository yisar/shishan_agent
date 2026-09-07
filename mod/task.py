from abc import ABC, abstractmethod
import asyncio
from enum import Enum
from typing import Any, AsyncGenerator, List, Dict, Optional, Tuple
import uuid
from pydantic import TypeAdapter

from mod.event import (
    DoneEvent,
    ErrorEvent,
    Event,
    Message,
    MessageEvent,
    TitleEvent,
    WaitEvent,
)
from mod.llm import LLM
from mod.mcp import MCPConfig, MCPTool
from mod.session import SessionRepo, SessionStatus
from mod.workflow import WorkFlow


"""
基于内存异步队列的消息队列实现，替代原 Redis Stream

任务是总称，后台异步执行，并基于内存队列实现消息传递
"""

class TaskRunner(ABC):
    @abstractmethod
    async def invoke(self, task: "Task") -> None: ...

    @abstractmethod
    async def destroy(self) -> None: ...

    @abstractmethod
    async def on_done(self, task: "Task") -> None: ...


class MessageQueue:
    def __init__(self, name: str):
        self.name = name
        self._queue: asyncio.Queue[Tuple[str, Any]] = asyncio.Queue()
        self._history: List[Tuple[str, Any]] = []
        self._lock = asyncio.Lock()

    async def put(self, message: Any) -> str:
        message_id = str(uuid.uuid4())
        async with self._lock:
            item = (message_id, message)
            self._history.append(item)
            await self._queue.put(item)
        return message_id

    async def get(
        self, start_id: Optional[str] = None, block_ms: int = 0
    ) -> Tuple[Optional[str], Optional[Any]]:
        try:
            if start_id is None or start_id == "0":
                if block_ms and block_ms > 0:
                    try:
                        async with asyncio.timeout(block_ms / 1000.0):
                            mid, data = await self._queue.get()
                            return mid, data
                    except TimeoutError:
                        return None, None
                else:
                    if self._queue.empty():
                        return None, None
                    mid, data = self._queue.get_nowait()
                    return mid, data
            else:
                async with self._lock:
                    found_idx = None
                    for i, (mid, _) in enumerate(self._history):
                        if mid == start_id:
                            found_idx = i
                            break
                    if found_idx is None:
                        return None, None
                    next_idx = found_idx + 1
                    if next_idx < len(self._history):
                        mid, data = self._history[next_idx]
                        return mid, data
                return None, None
        except Exception as e:
            print(f"MessageQueue.get error: {e}")
            return None, None

    async def pop(self) -> Tuple[Optional[str], Optional[Any]]:
        try:
            if self._queue.empty():
                return None, None
            mid, data = self._queue.get_nowait()
            return mid, data
        except Exception:
            return None, None

    async def clear(self) -> None:
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except Exception:
                break
        async with self._lock:
            self._history.clear()

    async def is_empty(self) -> bool:
        return self._queue.empty()

    async def size(self) -> int:
        return self._queue.qsize()

    async def delete(self, message_id: str) -> bool:
        async with self._lock:
            new_history = [(mid, data) for mid, data in self._history if mid != message_id]
            removed = len(new_history) != len(self._history)
            self._history = new_history
        return removed


class Task:
    task_registry: Dict[str, "Task"] = {}

    def __init__(self, task_runner: TaskRunner):
        self.task_runner = task_runner
        self._id = str(uuid.uuid4())
        self.exec_task: Optional[asyncio.Task] = None

        input_stream_name = f"task:input:{self._id}"
        output_stream_name = f"task:output:{self._id}"

        self._input_stream = MessageQueue(input_stream_name)
        self._output_stream = MessageQueue(output_stream_name)

        Task.task_registry[self._id] = self
        self._done_event = asyncio.Event()

    async def execute_task(self):
        try:
            await self.task_runner.invoke(self)
        except asyncio.CancelledError as e:
            print(f"任务取消 {e}")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"task execute error: {e}")
        finally:
            await self.finish_task()
            await self.cleanup()

    async def finish_task(self):
        if self.task_runner:
            try:
                on_done = self.task_runner.on_done(self)
                if asyncio.iscoroutine(on_done):
                    asyncio.create_task(on_done)
            except Exception as e:
                print(f"task on_done error: {e}")

    async def cleanup(self) -> None:
        if self._id in Task.task_registry:
            del Task.task_registry[self._id]
        self._done_event.set()

    async def invoke(self) -> None:
        if not self.done:
            return
        self.exec_task = asyncio.create_task(self.execute_task())
        print(f"任务开始执行 {self._id}")

    def cancel(self) -> None:
        if self.exec_task is not None and not self.exec_task.done():
            self.exec_task.cancel()
            print(f"任务已取消 {self._id}")
            asyncio.create_task(self.cleanup())
            return
        print("任务无需取消")

    @property
    def input_stream(self) -> MessageQueue:
        return self._input_stream

    @property
    def output_stream(self) -> MessageQueue:
        return self._output_stream

    @property
    def id(self) -> str:
        return self._id

    @property
    def done(self) -> bool:
        if self.exec_task is None:
            return True
        return self.exec_task.done()

    @classmethod
    def get(cls, task_id: str) -> Optional["Task"]:
        return Task.task_registry.get(task_id)

    @classmethod
    def create(cls, task_runner: TaskRunner) -> "Task":
        return cls(task_runner)

    @classmethod
    async def destroy(cls) -> None:
        for task_id in list(Task.task_registry.keys()):
            task = Task.task_registry.get(task_id)
            if task:
                task.cancel()
                if task.task_runner:
                    try:
                        await task.task_runner.destroy()
                    except Exception:
                        pass
        cls.task_registry.clear()


class AgentTaskRunner(TaskRunner):
    def __init__(
        self, llm: LLM, mcp_config: MCPConfig, session_id: str, session: SessionRepo
    ):
        self.session_id = session_id
        self.session = session
        self.mcp_config = mcp_config
        self.mcp_tool = MCPTool()
        self.flow = WorkFlow(session=session, session_id=session_id, llm=llm)

    async def put_and_add_event(self, task: Task, event: Event) -> None:
        event_id = await task.output_stream.put(event.model_dump_json())
        event.id = event_id
        try:
            await self.session.add_event(self.session_id, event)
        except Exception as e:
            print(f"add_event error: {e}")

    @classmethod
    async def pop_event(cls, task: Task) -> Optional[Event]:
        event_id, event_str = await task.input_stream.pop()
        if event_str is None:
            print("空事件")
            return None
        try:
            event = TypeAdapter(Event).validate_json(event_str)
            event.id = event_id or ""
            return event
        except Exception as e:
            print(f"pop_event parse error: {e}, event_str: {event_str}")
            return None

    async def invoke(self, task: Task) -> None:
        try:
            print("task runner 启动")
            await self.mcp_tool.init(mcp_config=self.mcp_config)
            tools = self.flow.tools
            has_mcp = False
            for t in tools:
                if isinstance(t, MCPTool):
                    has_mcp = True
                    break
            if not has_mcp:
                tools.append(self.mcp_tool)

            while not await task.input_stream.is_empty():
                event = await self.pop_event(task)
                if event is None:
                    break
                try:
                    if isinstance(event, MessageEvent):
                        message_obj = Message(message=event.message, files=[f.path for f in event.files] if event.files else [])
                    else:
                        message_obj = Message(message="", files=[])
                except Exception:
                    message_obj = Message(message="", files=[])

                async for flow_event in self.flow.invoke(message_obj):
                    await self.put_and_add_event(task, flow_event)
                    if isinstance(flow_event, TitleEvent):
                        await self.session.update_title(self.session_id, flow_event.title)
                    elif isinstance(flow_event, MessageEvent):
                        await self.session.update_latest_message(
                            self.session_id, flow_event.message
                        )
                    elif isinstance(flow_event, WaitEvent):
                        await self.session.update_status(
                            self.session_id, SessionStatus.WAITING
                        )
                        return
                    if not await task.input_stream.is_empty():
                        break

            await self.session.update_status(self.session_id, SessionStatus.COMPLETED)
        except asyncio.CancelledError:
            print("AgentTaskRunner 运行取消")
            await self.put_and_add_event(task, DoneEvent())
            await self.session.update_status(self.session_id, SessionStatus.COMPLETED)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"task runner 运行出错{self.session_id} {str(e)}")
            await self.put_and_add_event(
                task,
                ErrorEvent(error=f"task runner 运行出错{self.session_id} {str(e)}"),
            )
            await self.session.update_status(self.session_id, SessionStatus.COMPLETED)

    async def destroy(self) -> None:
        if self.mcp_tool:
            try:
                await self.mcp_tool.cleanup()
            except Exception:
                pass
        try:
            close_fn = getattr(self.session, "close", None)
            if close_fn is not None and asyncio.iscoroutinefunction(close_fn):
                await close_fn()
        except Exception:
            pass

    async def on_done(self, task: "Task") -> None:
        pass
