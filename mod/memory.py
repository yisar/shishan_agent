from typing import List, Dict, Any, Optional

from pydantic import BaseModel

from mod.event import Message
"""
记忆系统，尚未实现，后续利用 redis 实现，短期记忆存 redis 内存，长期记忆存 redis 持久化
"""
class Memory(BaseModel):
    messages: List[Dict[str, Any]] = []

    @staticmethod
    def get_message_role(message: Dict[str, Any]) -> str:
        return message.get("role")

    def add_message(self, message: Dict[str, Any]) -> None:
        self.messages.append(message)

    def add_messages(self, messages: List[Dict[str, Any]]) -> None:
        self.messages.extend(messages)

    def get_messages(self) -> List[Dict[str, Any]]:
        return self.messages

    def get_last_message(self) -> Optional[Dict[str, Any]]:
        return self.messages[-1] if len(self.messages) > 0 else None

    def roll_back(self, message:Message) -> None:
        # 删除最后一条消息
        self.messages = self.messages[:-1]

    def compact(self) -> None:
        # 记忆压缩，对于工具调用后的消息进行剔除
        for message in self.messages:
            if self.get_message_role(message) == "tool":
                if message.get("function_name") in ["search"]:
                    message["content"] = "(removed)"
                    print(f"剔除工具结果{message['function_name']}")
    @property
    def empty(self) -> bool:
        return len(self.messages) == 0