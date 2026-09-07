from enum import Enum
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from datetime import datetime
from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    String,
    cast,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.orm import sessionmaker
from mod.event import BaseEvent, Plan, PlanEvent
from mod.memory import Memory
import uuid
from sqlalchemy.orm import declarative_base

from pydantic import BaseModel, ConfigDict, Field


"""
梳理关系：
session(会话/对话)
    -task(任务, loop) - sandbox(沙箱)
        -plan
        -step(loop)


一个会话，包含一个或多个任务，每个任务开启一个沙箱
每个任务包含一个 plan, 每个 plan 有多个步骤

流程：
1. 新开会话，创建任务和开启沙箱
2. 制定plan, 拆分步骤执行, 执行过程中不断更新计划，直到彻底完成计划
3. 执行完整个任务后，可以创建新的任务，循环 1 2
"""


class SessionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"


Base = declarative_base()


class Session(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    sandbox_id: Optional[str] = None
    task_id: Optional[str] = None
    title: str = ""
    latest_message: str = ""
    events: List[BaseEvent] = Field(default_factory=list)
    files: List[str] = Field(default_factory=list)
    memory: dict = Field(default_factory=dict)
    time: datetime = Field(default_factory=datetime.now)
    status: SessionStatus = SessionStatus.PENDING

    def get_latest_plan(self) -> Optional[Plan]:
        for event in reversed(self.events):
            if isinstance(event, PlanEvent):
                return event.plan
        return None


class SessionRepo:
    def __init__(self, db_session: AsyncSession):
        self.db_session = db_session

    async def save(self, session: Session) -> None:
        stmt = select(SessionModel).where(SessionModel.id == session.id)
        result = await self.db_session.execute(stmt)
        record = result.scalar_one_or_none()

        if not record:
            record = SessionModel.from_db_table(session=session)
            self.db_session.add(record)
            await self.db_session.commit()
            return
        record.update_db_table(session)
        await self.db_session.commit()

    async def get_all(self) -> List[Session]:
        stmt = select(SessionModel).order_by(SessionModel.time.desc())

        result = await self.db_session.execute(stmt)
        records = result.scalars().all()
        return [record.to_db_table() for record in records]

    async def get_by_id(self, session_id: str) -> Optional[Session]:
        stmt = select(SessionModel).where(SessionModel.id == session_id)
        result = await self.db_session.execute(stmt)
        record = result.scalar_one_or_none()

        return record.to_db_table() if record is not None else None

    async def delete_by_id(self, session_id: str) -> None:
        stmt = delete(SessionModel).where(SessionModel.id == session_id)
        await self.db_session.execute(stmt)
        await self.db_session.commit()

    async def update_title(self, session_id: str, title: str) -> None:
        stmt = (
            update(SessionModel)
            .where(SessionModel.id == session_id)
            .values(title=title)
        )
        result = await self.db_session.execute(stmt)
        await self.db_session.commit()
        if result.rowcount == 0:
            raise ValueError(f"会话不存在，请核实后重试 {session_id}")

    async def update_status(self, session_id: str, status: SessionStatus) -> None:
        stmt = (
            update(SessionModel)
            .where(SessionModel.id == session_id)
            .values(status=status.value)
        )
        result = await self.db_session.execute(stmt)
        await self.db_session.commit()
        if result.rowcount == 0:
            raise ValueError(f"会话不存在，请核实后重试 {session_id}")

    async def update_latest_message(
        self, session_id: str, message: str, time: Optional[datetime] = None
    ) -> None:
        if time is None:
            time = datetime.now()
        stmt = (
            update(SessionModel)
            .where(SessionModel.id == session_id)
            .values(latest_message=message, time=time)
        )
        result = await self.db_session.execute(stmt)
        await self.db_session.commit()
        if result.rowcount == 0:
            raise ValueError(f"会话不存在，请核实后重试 {session_id}")

    async def add_event(self, session_id: str, event: BaseEvent) -> None:
        event_data = event.model_dump(mode="json")

        stmt = (
            update(SessionModel)
            .where(SessionModel.id == session_id)
            .values(
                events=func.coalesce(SessionModel.events, cast([], JSON))
                + cast([event_data], JSON)
            )
        )
        result = await self.db_session.execute(stmt)
        await self.db_session.commit()
        if result.rowcount == 0:
            raise ValueError(f"会话不存在，请核实后重试 {session_id}")

    async def add_file(self, session_id: str, file: str) -> None:
        stmt = (
            update(SessionModel)
            .where(SessionModel.id == session_id)
            .values(
                files=func.coalesce(SessionModel.files, cast([], JSON))
                + cast([file], JSON)
            )
        )
        result = await self.db_session.execute(stmt)
        await self.db_session.commit()
        if result.rowcount == 0:
            raise ValueError(f"会话不存在，请核实后重试 {session_id}")

    async def remove_file(self, session_id: str, file_id: str) -> None:
        stmt = (
            select(SessionModel).where(SessionModel.id == session_id).with_for_update()
        )
        result = await self.db_session.execute(stmt)
        record = result.scalar_one_or_none()

        if not record:
            raise ValueError(f"会话不存在，请核实后重试 {session_id}")

        if not record.files:
            return
        lenth = len(record.files)
        new_files = [file for file in record.files if file != file_id]

        if len(new_files) == lenth:
            return
        record.files = new_files
        await self.db_session.commit()

    async def save_memory(
        self, session_id: str, agent_name: str, memory: Memory
    ) -> None:
        memory_data = memory.model_dump(mode="json")

        stmt = (
            select(SessionModel).where(SessionModel.id == session_id)
        )
        result = await self.db_session.execute(stmt)
        record = result.scalar_one_or_none()
        if not record:
            raise ValueError(f"会话不存在，请核实后重试 {session_id}")

        current_memory = record.memory or {}
        current_memory[agent_name] = memory_data

        stmt_update = (
            update(SessionModel)
            .where(SessionModel.id == session_id)
            .values(memory=current_memory)
        )
        await self.db_session.execute(stmt_update)
        await self.db_session.commit()

    async def get_memory(self, session_id: str, agent_name: str) -> Memory:
        stmt = select(SessionModel.memory).where(
            SessionModel.id == session_id
        )

        result = await self.db_session.execute(stmt)
        memory_data = result.scalar_one_or_none()

        if memory_data and isinstance(memory_data, dict) and agent_name in memory_data:
            return Memory(**memory_data[agent_name])
        return Memory(messages=[])


class SessionModel(Base):
    __tablename__ = "sessions"
    __allow_unmapped__ = True
    id = Column(String, primary_key=True, index=True)
    sandbox_id = Column(String, nullable=True)
    task_id = Column(String, nullable=True)
    title = Column(String, default="")
    latest_message = Column(String, default="")
    events = Column(JSON, default=list)
    files = Column(JSON, default=list)
    memory = Column(JSON, default=dict)
    time = Column(DateTime, default=datetime.now)
    status = Column(String, default=SessionStatus.PENDING.value)

    @classmethod
    def from_db_table(cls, session: Session) -> "SessionModel":
        base = session.model_dump(
            mode="python", exclude={"memory", "files", "events"}
        )
        json_data = session.model_dump(mode="json", include={"memory", "files", "events"})
        if isinstance(base.get("status"), SessionStatus):
            base["status"] = base["status"].value
        return cls(**base, **json_data)

    def update_db_table(self, session: Session) -> "SessionModel":
        base = session.model_dump(
            mode="python", exclude={"memory", "files", "events"}
        )
        json_data = session.model_dump(mode="json", include={"memory", "files", "events"})
        if isinstance(base.get("status"), SessionStatus):
            base["status"] = base["status"].value
        for field, value in {**base, **json_data}.items():
            setattr(self, field, value)
        return self

    def to_db_table(self) -> Session:
        memory_val = self.memory if isinstance(self.memory, dict) else {}
        events_val = self.events if isinstance(self.events, list) else []
        files_val = self.files if isinstance(self.files, list) else []
        data = {
            "id": self.id,
            "sandbox_id": self.sandbox_id,
            "task_id": self.task_id,
            "title": self.title or "",
            "latest_message": self.latest_message or "",
            "events": events_val,
            "files": files_val,
            "memory": memory_val,
            "time": self.time,
            "status": self.status or SessionStatus.PENDING.value,
        }
        return Session.model_validate(data)


DATABASE_URL = "sqlite+aiosqlite:///./sessions.db"


def _make_engine():
    return create_async_engine(
        DATABASE_URL, connect_args={"check_same_thread": False}
    )


async def init_db():
    engine = _make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


async def get_db():
    engine = _make_engine()
    SessionLocal = sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with SessionLocal() as db:
        yield db
    await engine.dispose()


class TaskSessionRepo(SessionRepo):
    """TaskRunner 专用：自带独立 AsyncSession，生命周期随 TaskRunner.destroy"""

    def __init__(self):
        self._engine = _make_engine()
        self._session_local = sessionmaker(
            bind=self._engine, class_=AsyncSession, expire_on_commit=False
        )
        self._session = self._session_local()
        super().__init__(self._session)

    async def close(self) -> None:
        try:
            await self._session.close()
        except Exception:
            pass
        try:
            await self._engine.dispose()
        except Exception:
            pass
