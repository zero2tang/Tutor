"""FastAPI 应用入口；网页前端由独立 Vite 项目提供。"""

from app.bootstrap import lifespan
from app.api.router import router
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.infrastructure.clients.llm import ModelError
from app.shared.execution import LearningConflict

app = FastAPI(title="理序学习系统 API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:4173",
    ],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)
app.include_router(router)


@app.exception_handler(LearningConflict)
async def conflicting_request(request: Request, exc: LearningConflict):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def invalid_request(request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(ModelError)
async def model_failure(request: Request, exc: ModelError):
    return JSONResponse(status_code=502, content={"detail": str(exc)})
