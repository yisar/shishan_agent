import dotenv
import json
from openai import OpenAI

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
        self.messages.append({"role": "user", "content": query})
        response = self.client.chat.completions.create(
            model=self.model,
            messages=self.messages,
            tools=self.tools,
            tool_choice="auto",
        )

        response_message = response.choices[0].message
        tool_calls = response_message.tool_calls
        self.messages.append(response_message.model_dump())

        if tool_calls:
            for tool_call in tool_calls:
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
            response2 = self.client.chat.completions.create(
                model=self.model,
                messages=self.messages,
                tools=self.tools,
                tool_choice="none",
            )
            self.messages.append(response2.choices[0].message.model_dump())
            return "Assistant: " + response2.choices[0].message.content
        else:
            return "Assistant: " + response_message.content

    def chat_loop(self):
        while True:
            try:
                query = input("\nQuery: ").strip()
                if query.lower() == "quit":
                    break

                print(self.process_query(query))
            except Exception as e:
                print(e)


if __name__ == "__main__":
    ReActAgent().chat_loop()