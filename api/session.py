import datetime
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from functools import lru_cache
from datetime import datetime
from typing import AsyncGenerator, List, Optional, Type

from fastapi import APIRouter, Depends
from pydantic import BaseModel, TypeAdapter
from sse_starlette import EventSourceResponse, ServerSentEvent

from api.res import Response
from mod.event import (
    BaseEvent,
    DoneEvent,
    ErrorEvent,
    Event,
    MessageEvent,
    WaitEvent,
)
from mod.llm import AppConfig, OpenAILLM, LLM
from mod.mcp import MCPConfig
from mod.session import Session, SessionRepo, SessionStatus, get_db, init_db, TaskSessionRepo
from mod.task import AgentTaskRunner, Task, TaskRunner

router = APIRouter(prefix="/session", tags=["会话模块"])


class SessionService:
    def __init__(self, session_repo: SessionRepo):
        self.session = session_repo

    async def create_session(self) -> Session:
        session = Session(title="新会话")
        await self.session.save(session)
        return session

    async def get_all_sessions(self) -> List[Session]:
        return await self.session.get_all()

    async def delete_session(self, session_id: str) -> None:
        await self.session.delete_by_id(session_id)


def get_session_repo(
    db=Depends(get_db),
):
    return SessionRepo(db)


def get_session_service(
    repo: SessionRepo = Depends(get_session_repo),
):
    return SessionService(session_repo=repo)


def get_agent_service(
    repo: SessionRepo = Depends(get_session_repo),
):
    llm: LLM = OpenAILLM()
    app_config = AppConfig()

    return AgentService(
        session_repo=repo,
        llm=llm,
        app_config=app_config,
        mcp_config=app_config.mcp_config,
        task_cls=Task,
    )


@router.post(
    path="",
    response_model=Response,
    summary="创建任务会话",
    description="创建空白任务会话",
)
async def create_session_endpoint(
    session_service: SessionService = Depends(get_session_service),
) -> Response:
    session = await session_service.create_session()
    return Response.success(msg="创建对话成功", data={"session_id": session.id})


@router.get(
    path="",
    response_model=Response,
    summary="获取会话列表",
    description="获取会话列表",
)
async def get_all_sessions_endpoint(
    session_service: SessionService = Depends(get_session_service),
) -> Response:
    sessions = await session_service.get_all_sessions()
    session_items = []
    for session in sessions:
        session_items.append({
            "id": session.id,
            "title": session.title,
            "latest_message": session.latest_message,
            "status": session.status.value if isinstance(session.status, SessionStatus) else session.status,
            "time": session.time.isoformat() if isinstance(session.time, datetime) else str(session.time),
        })
    return Response.success(msg="列出会话成功", data=session_items)


@router.post(
    path="/{session_id}/delete",
    response_model=Response,
    summary="删除指定会话",
    description="删除指定会话",
)
async def delete_session_endpoint(
    session_id: str,
    session_service: SessionService = Depends(get_session_service),
) -> Response:
    await session_service.delete_session(session_id)
    return Response.success(msg="删除会话成功")


class ChatRequest(BaseModel):
    message: Optional[str] = None
    files: Optional[List[str]] = None
    event_id: Optional[str] = None
    timestamp: Optional[int] = None


class AgentService:
    def __init__(
        self,
        session_repo: SessionRepo,
        task_cls: Type[Task],
        llm: LLM,
        mcp_config: MCPConfig,
        app_config: Optional[AppConfig] = None,
    ):
        self.session = session_repo
        self.task_cls = task_cls
        self.mcp_config = mcp_config
        self.llm = llm
        self.app_config = app_config

    async def get_task(self, session: Session) -> Optional[Task]:
        task_id = session.task_id
        if not task_id:
            return None
        return self.task_cls.get(task_id)

    async def create_task(self, session: Session) -> Task:
        task_session_repo = TaskSessionRepo()
        task_runner: TaskRunner = AgentTaskRunner(
            session=task_session_repo,
            session_id=session.id,
            llm=self.llm,
            mcp_config=self.mcp_config,
        )
        task = self.task_cls.create(task_runner=task_runner)
        session.task_id = task.id
        await self.session.save(session)
        return task

    async def chat(
        self,
        session_id: str,
        message: Optional[str] = None,
        event_id: Optional[str] = None,
        timestamp: Optional[int] = None,
    ) -> AsyncGenerator[BaseEvent, None]:
        try:
            session = await self.session.get_by_id(session_id=session_id)
            if not session:
                raise RuntimeError(f"尝试与不存在的 session 聊天: {session_id}")
            task = await self.get_task(session)
            last_event_id = event_id

            if message:
                if session.status != SessionStatus.RUNNING or task is None or task.done:
                    task = await self.create_task(session)
                    if not task:
                        raise RuntimeError("创建 task 失败")

                await self.session.update_latest_message(
                    session_id=session_id,
                    message=message,
                )
                message_event = MessageEvent(
                    role="user",
                    message=message,
                    files=[],
                )

                input_event_id = await task.input_stream.put(message_event.model_dump_json())
                message_event.id = input_event_id

                await self.session.add_event(session_id, message_event)
                await task.invoke()
                last_event_id = input_event_id
            print(f"会话 {session_id} 已启动， 任务 {task.id if task else None}")

            if task is None:
                yield ErrorEvent(error="当前没有可轮询的任务，请先发送消息")
                return

            consecutive_empty = 0
            max_empty_polls = 2000
            poll_interval_ms = 50

            while True:
                event_id_new, event_str = await task.output_stream.get(
                    start_id=last_event_id, block_ms=0
                )
                last_event_id = event_id_new or last_event_id
                if event_str is None:
                    if task.done:
                        stream_empty = await task.output_stream.is_empty()
                        if stream_empty:
                            break
                    consecutive_empty += 1
                    if consecutive_empty > max_empty_polls:
                        break
                    await asyncio_sleep(poll_interval_ms / 1000.0)
                    continue
                consecutive_empty = 0

                try:
                    event = TypeAdapter(Event).validate_json(event_str)
                    event.id = event_id_new or ""
                except Exception as e:
                    print(f"事件解析失败 [{session_id}]: {e}, raw: {event_str[:200]}")
                    continue
                print(f"从会话 {session_id} 获取事件 {type(event).__name__}")

                yield event
                if isinstance(event, (DoneEvent, ErrorEvent, WaitEvent)):
                    break
            print("本轮会话结束")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"聊天出错 [{session_id}] {e}")
            error_event = ErrorEvent(error=str(e))
            try:
                await self.session.add_event(session_id, error_event)
            except Exception:
                pass
            yield error_event


def asyncio_sleep(seconds: float):
    import asyncio
    return asyncio.sleep(seconds)


@router.post(
    path="/{session_id}/chat",
    summary="向指定会话发起聊天请求（SSE 流式）",
    description="向指定会话发起聊天请求，返回 SSE 流式事件",
)
async def chat_endpoint(
    session_id: str,
    request: ChatRequest,
    agent_service: AgentService = Depends(get_agent_service),
) -> EventSourceResponse:
    async def event_generator() -> AsyncGenerator[ServerSentEvent, None]:
        async for event in agent_service.chat(
            session_id=session_id,
            message=request.message,
            event_id=request.event_id,
            timestamp=request.timestamp,
        ):
            try:
                data = event.model_dump_json()
            except Exception:
                event_type = getattr(event, "type", "unknown")
                data = '{"error":"序列化失败","type":"' + event_type + '"}'
            yield ServerSentEvent(event=getattr(event, "type", "event"), data=data)

    return EventSourceResponse(event_generator())


@router.on_event("startup")
async def on_startup():
    try:
        await init_db()
        print("数据库初始化完成")
    except Exception as e:
        print(f"数据库初始化失败（可能已存在）: {e}")
