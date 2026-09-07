import dotenv
import json
import os
import requests
from openai import OpenAI
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall

dotenv.load_dotenv()
GAODE_URL = f"https://mcp.amap.com/mcp?key={os.getenv('GAODE_KEY')}"

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
        self.tools = []
        self.headers = {
            "Accept":"application/json, text/event-stream",
            "Content-Type":"application/json"
        }
        self.init_gaode_mcp()

    def init_gaode_mcp(self):
        # 1. 请求高德可用工具列表
        tool_list_res = requests.post(GAODE_URL,headers=self.headers,json={
            "jsonrpc":"2.0",
            "id":1,
            "method":"tools/list",
            "params":{}
        })
        tool_list_res.raise_for_status()
        tool_list_data = tool_list_res.json()

        # 2. 存储可用工具列表
        self.tools = [{
            "type":"function",
            "function":{
                "name":tool["name"],
                "description":tool["description"],
                "parameters":tool["inputSchema"]
            }
        } for tool in tool_list_data["result"]["tools"]]

    def call_gaode_mcp(self,name:str, arguments:dict)->str:

        tool_call_res = requests.post(GAODE_URL, headers=self.headers, json={
            "jsonrpc":"2.0",
            "id":1,
            "method":"tools/call",
            "params":{
                "name":name,
                "arguments":arguments,

            }
        })

        tool_call_res.raise_for_status()
        tool_call_data = tool_call_res.json()

        return tool_call_data["result"]["content"][0]["text"]



    def process_query(self, query=None):
        if query is not None:
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

                result = self.call_gaode_mcp(name=name, arguments=args)
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
