import dotenv
from openai import OpenAI
from pydantic import BaseModel, Field

dotenv.load_dotenv()


class SplitTask(BaseModel):
    count: int = Field(..., gt=0, le=10, description="任务数量")
    tasks: list[str] = Field(..., description="拆分任务信息")


client = OpenAI()

system_prompt = """你是一个任务调用机器人，用户将提问一个问题，请拆解这个问题为多个串联的小任务，拆解任务数量不超过10个，最终必须以json格式输出，
其中，count字段为拆解任务数量，tasks字段为拆分任务数组（数组内每个元素都是一个字符串）

示例输入：
今天武汉天气怎么样？

示例输出：
{
    "count": 3,
    "tasks": ["调用浏览器搜索今天的时间", "调用浏览器搜索武汉的天气", "综合搜索的结果内容调用 LLM 整理最终答案并输出答案"]
}
"""

while True:
    user_prompt = input("Query: ").strip()
    if user_prompt.lower() == "quit":
        break

    response = client.chat.completions.create(
        model="mimo-v2.5",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
    )

    split_task = SplitTask.model_validate_json(response.choices[0].message.content)

    print("拆解任务数: ", split_task.count)
    for idx, task in enumerate(split_task.tasks):
        print(f"{idx}, {task}")
