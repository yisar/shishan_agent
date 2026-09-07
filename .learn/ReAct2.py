import dotenv
import json
from openai import OpenAI
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall

dotenv.load_dotenv()


def calculator(expr: str) -> str:
    result = eval(expr)
    return json.dumps({"result": result})


class ReActAgent:
    def __init__(self):
        self.client = OpenAI()
        self.messages = [
            {
                "role": "system",
                "content": "你是一个强大的聊天机器人，请根据用户的提问进行回答，如果需要调用工具，请优先直接调用，不知道请直接回答不知道",
            }
        ]
        self.model = "mimo-v2.5"
        self.available_tools = {"calculator": calculator}
        self.tools = [
            {  # 一个数学表达式计算的 tool
                "type": "function",
                "function": {
                    "name": "calculator",
                    "description": "一个可以计算数学表达式的计算器",
                    "parameters": {
                        "properties": {
                            "expr": {
                                "type": "string",
                                "description": "需要计算的数学表达式, 例如 123+456+789",
                            }
                        }
                    },
                    "required": ["expr"],
                },
            }
        ]

    def process_query(self, query):
        if query != "":
            self.messages.append({"role": "user", "content": query})
        print("Assistant: ", end="", flush=True)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=self.messages,
            tools=self.tools,
            tool_choice="auto",
            stream=True,
        )

        is_tool_call = False
        content = ""
        reasoning = ""
        tool_calls_obj: dict[str, ChoiceDeltaToolCall] = {}

        for chunk in response:
            if not chunk.choices:
                continue
            chunk_content = chunk.choices[0].delta.content
            chunk_reasoning = chunk.choices[0].delta.reasoning_content
            chunk_tool_calls = chunk.choices[0].delta.tool_calls

            if chunk_content:
                content += chunk_content
            if chunk_tool_calls:
                for chunk_tool in chunk_tool_calls:
                    if tool_calls_obj.get(chunk_tool.index) is None:
                        tool_calls_obj[chunk_tool.index] = chunk_tool
                    else:
                        tool_calls_obj[
                            chunk_tool.index
                        ].function.arguments += chunk_tool.function.arguments

            if chunk_content:
                print(chunk_content, end="", flush=True)
            if is_tool_call is False:
                if chunk_tool_calls:
                    is_tool_call = True

        tool_call_json = [tool_call for tool_call in tool_calls_obj.values()]

        self.messages.append(
            {
                "role": "assistant",
                "content": content if content != "" else None,
                "tool_calls": tool_call_json if tool_call_json else None,
                "reasoning_content": reasoning if reasoning else None
            }
        )

        if is_tool_call:
            for tool_call in tool_call_json:
                print("工具调用", tool_call.function.name)
                name = tool_call.function.name
                args = json.loads(tool_call.function.arguments)
                fn = self.available_tools[name]

                result = fn(**args)
                print(f"Tool [{name}] Result {result}")
                self.messages.append(
                    {
                        "tool_call_id": tool_call.id,
                        "role": "tool",
                        "name": name,
                        "content": result,
                    }
                )
        
        self.process_query()

    def chat_loop(self):
        while True:
            try:
                query = input("\nQuery: ").strip()
                if query.lower() == "quit":
                    break

                self.process_query(query)
            except Exception as e:
                print(e)


if __name__ == "__main__":
    ReActAgent().chat_loop()
