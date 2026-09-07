from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from api import llm, session
app = FastAPI(
    title="FastAPI 入门示例", description="最简单的 FastAPI 演示程序", version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

app.include_router(llm.router)
app.include_router(session.router)



# import redis



# success = r.set('foo', 'bar')
# # True

# result = r.get('foo')
# print(result)

