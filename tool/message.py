from typing import Optional

from mod.mcp import BaseTool, ToolResult, tool


class MessageTool(BaseTool):
    def __init__(self):
        super().__init__()

    @tool(
        name="message_notify_user",
        description="向用户发送操作消息，且无需用户回复。用于确认用户收到消息，提供进度更新，报告任务完成情况等",
        params={
            "text": {"type": "string", "description": "要显示给用户的消息文本"}
        },
        required=["text"],
    )
    async def message_notify_user(self, text: str) -> ToolResult:
        return ToolResult(success=True, data="Continue...")

    @tool(
        name="message_ask_user",
        description="向用户发起提问，并等待回复。用于请求澄清，寻求确认，或收集额外信息等",
        params={
            "text": {"type": "string", "description": "要显示给用户的问题文本"},
            "user_takeover": {
                "type": "string",
                "enum": ["none", "confirm"],
                "description": "(可选)，建议用户接管的操作，比如建议用户点击确认按钮进行确认",
            },
        },
        required=["text"],
    )
    async def message_ask_user(self, text: str, user_takeover: Optional[str] = None) -> ToolResult:
        return ToolResult(success=True)
