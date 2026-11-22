"""应用生命周期装配：数据库、业务服务、执行器与后台任务。"""

import asyncio
from contextlib import asynccontextmanager, AsyncExitStack
from fastapi import FastAPI
from app.infrastructure.clients.graph import GraphClient
from app.infrastructure.clients.llm import ModelClient
from app.infrastructure.clients.search import SearchClient
from app.application.persistence import initialize_database
from app.infrastructure.config import get_settings
from app.infrastructure.persistence.database import engine
from app.infrastructure.persistence.memory_documents import memory_document_storage
from app.domains.courses.retrieval.service import RetrievalService
from app.application.agent.runner import AgentRunner
from app.domains.courses.catalog import CatalogService
from app.application.learning.service import LearningService
from app.domains.sessions.service import SessionService
from app.domains.students.service import StudentService
from app.application.agent.tools import TeachingTools
from app.application.learning.storage import checkpoint_storage, cleanup_cold_threads
from app.domains.memory.service import MemoryService
from app.domains.memory.recording.jobs import MemoryWorker
from app.domains.memory.concepts.service import BusinessMemoryPipeline
from app.domains.memory.scopes.service import HierarchicalMemoryService
from app.application.question_retrieval.service import QuestionRetrievalService


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncExitStack() as resources:
        initialize_database()
        saver = await resources.enter_async_context(checkpoint_storage())
        documents = await resources.enter_async_context(
            memory_document_storage(get_settings().postgres_dsn(), engine=engine())
        )
        models, search, graph = ModelClient(), SearchClient(), GraphClient()
        question_retrieval = QuestionRetrievalService(models, search)
        retrieval = RetrievalService(models, search, graph, question_retrieval=question_retrieval)
        memory = MemoryService(models)
        hierarchical_memory = HierarchicalMemoryService(models, database=memory.database)
        BusinessMemoryPipeline(models, memory, hierarchy=hierarchical_memory)
        memory.configure_graph(saver, store=documents.async_store)
        runner = AgentRunner(models, TeachingTools(retrieval), memory=memory)
        student = StudentService()
        student.memory_service = memory
        learning = LearningService(runner, checkpointer=saver)
        learning.student.memory_service = memory
        runner.tools.student.memory_service = memory
        memory_worker = MemoryWorker(memory)
        sessions = SessionService()
        sessions.recover()
        await cleanup_cold_threads(learning.graph, saver)
        app.state.services = {
            "models": models,
            "search": search,
            "graph": graph,
            "retrieval": retrieval,
            "question_retrieval": question_retrieval,
            "runner": runner,
            "catalog": CatalogService(),
            "sessions": sessions,
            "student": student,
            "learning": learning,
            "memory": memory,
            "hierarchical_memory": hierarchical_memory,
        }
        app.state.tasks = set()
        memory_worker.start()
        yield
        await memory_worker.stop()
        for task in list(app.state.tasks):
            task.cancel()
        await asyncio.gather(*app.state.tasks, return_exceptions=True)
        await asyncio.gather(models.close(), search.close(), graph.close())
