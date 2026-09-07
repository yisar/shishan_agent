from pydantic import BaseModel, Field
from typing import Optional, TypeVar, Generic
T = TypeVar("T")


class Response(BaseModel, Generic[T]):
    code: int = 200
    msg: str = "success"
    data: Optional[T] = Field(default_factory=dict)

    @staticmethod
    def success(msg: str = "success", data: Optional[T] = None) -> "Response[T]":
        return Response(code=200, msg=msg, data=data if data is not None else {})

    @staticmethod
    def fail(
        code: int, msg: str = "success", data: Optional[T] = None
    ) -> "Response[T]":
        return Response(code=code, msg=msg, data=data if data is not None else {})
