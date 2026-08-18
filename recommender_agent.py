from dotenv import load_dotenv
load_dotenv()

import os
from langchain_tavily import TavilySearch
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.sqlite import SqliteSaver
import sqlite3
from langchain.agents import create_agent

web_search = TavilySearch(
    max_results=8,
    topic="general",
)

model = init_chat_model(
    model="deepseek-v4-pro",
    model_provider="openai",
    base_url=os.getenv("DEEPSEEK_BASE_URL"),
    api_key=os.getenv("DEEPSEEK_API_KEY")
)

connection = sqlite3.connect("resources/personal_recommender.db", check_same_thread=False)
checkpointer = SqliteSaver(connection)
checkpointer.setup()

system_prompt = """
你是一名资深小说推荐员。收到用户提供的“小说题材/关键词/偏好”后，请按以下流程操作：
1. 理解用户需求：解析题材关键词与可选偏好（例如：年代、字数、风格、受众年龄、是否要系列等）。
2. 智能书单检索：优先调用 web_search 工具，以解析出的关键词为核心，检索可行的小说候选（优先检索权威书评、读者推荐与书籍简介）。
3. 多维度评估与排序：对检索到的候选小说，从以下维度量化打分并排序：
   - 题材匹配度（与用户题材/关键词的贴合程度）
   - 读者口碑/评分（流行度与评价）
   - 文学/写作质量（来自书评或奖项信息）
   - 适读性（阅读难度、篇幅、是否易上手）
   将简单、匹配度高且口碑良好的书排在前面。
4. 结构化输出：将排序后的推荐整理为报告，包含每本书的标题、作者、简短简介、得分与推荐理由、参考来源链接（或封面图链接）和适合人群/场景提示。
5. 如果 web_search 无法返回充分候选，说明原因并在最后给出 3-5 本基于模型知识的备选书目（标注“模型推荐”）。

严格按照流程工作，并优先使用 web_search 工具检索候选书目。
"""

agent = create_agent(
    model=model,
    tools=[web_search],
    system_prompt=system_prompt,
    checkpointer=checkpointer
)

def recommend_by_genre(genre: str, preferences: str = "") -> str:
    user_prompt = f"""
用户题材: {genre}
用户偏好: {preferences}
请严格按照 system_prompt 中定义的流程输出：先调用 web_search 检索候选并评估，然后输出排序后的推荐报告（每本书包含标题、作者、简介、得分、推荐理由、参考链接）。
    """
    config = {"configurable": {"thread_id":"recommend_thread_001"}}
    input_messages = [("user", user_prompt)]
    result = agent.invoke({"messages":input_messages}, config=config)
    final_response = result["messages"][-1].content
    return final_response