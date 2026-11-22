"""配置只从环境文件和进程环境读取，不将密钥写入日志。"""

from dataclasses import dataclass, field
from functools import lru_cache
from io import StringIO
import os
from pathlib import Path
import re

from dotenv import dotenv_values
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Settings:
    database_host: str
    database_port: int
    database_user: str
    database_password: str = field(repr=False)
    database_name: str = "stem_tutor"
    es_url: str = "http://127.0.0.1:9200"
    es_user: str = ""
    es_password: str = field(default="", repr=False)
    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = field(default="123456", repr=False)
    llm_url: str = ""
    llm_key: str = field(default="", repr=False)
    llm_model: str = "deepseek-flash"
    embedding_url: str = ""
    embedding_key: str = field(default="", repr=False)
    embedding_model: str = "text-embedding-v4"
    embedding_dim: int = 1024
    api_port: int = 8008
    course_id: str = "mechanics"
    # 窗口来自部署配置；项目总请求预算与模型容量分开，不依据模型名推测。
    context_window_tokens: int = 32768
    context_request_tokens: int = 32768
    context_input_limit_tokens: int = 0
    context_safety_tokens: int = 2048
    context_image_tokens: int = 4096
    context_soft_ratio: float = 0.70
    context_preventive_ratio: float = 0.90
    context_summary_output_tokens: int = 1800
    context_summary_timeout_seconds: float = 45
    context_keep_recent_turns: int = 4
    context_tool_growth_tokens: int = 1024
    context_compression_enabled: bool = True
    exercise_search_courses: str = "math,junior_math_7a"
    exercise_mm_enabled: bool = False
    exercise_rerank_enabled: bool = False
    exercise_rerank_url: str = ""
    exercise_qwen_rerank_limit: int = 20
    exercise_mm_model: str = "qwen3-vl-embedding"
    exercise_mm_dim: int = 1024
    exercise_mm_url: str = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"
    exercise_candidates: int = 30
    exercise_rerank_limit: int = 6
    exercise_search_namespace: str = "main"
    exercise_mm_intents: str = "exact,isomorphic,same_method"
    exercise_rerank_intents: str = "exact,isomorphic,same_method"

    def database_url(self, *, database: bool = True) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.database_user,
            password=self.database_password,
            host=self.database_host,
            port=self.database_port,
            database=self.database_name if database else "postgres",
        )

    def postgres_dsn(self) -> str:
        return self.database_url().set(drivername="postgresql").render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    shared = os.getenv(
        "TUTOR_SHARED_ENV", r"E:\projects\contract\contract-compliance\backend\.env"
    )
    # 项目覆盖项只保存连接差异；原有合同业务库名不予继承。
    source = Path(shared)
    assignments = (
        "\n".join(
            line
            for line in source.read_text(encoding="utf-8-sig").splitlines()
            if re.match(r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*=", line)
        )
        if source.exists()
        else ""
    )
    values = {
        **dotenv_values(stream=StringIO(assignments)),
        **dotenv_values(ROOT / "backend" / ".env"),
        **os.environ,
    }
    return Settings(
        database_host=values.get("TUTOR_DB_HOST", "127.0.0.1"),
        database_port=int(values.get("TUTOR_DB_PORT", "5432")),
        database_user=values.get("TUTOR_DB_USER", "postgres"),
        database_password=values.get("TUTOR_DB_PASSWORD", ""),
        database_name=values.get("TUTOR_DATABASE", "stem_tutor"),
        es_url=values.get("ES_URL", "http://127.0.0.1:9200").rstrip("/"),
        es_user=values.get("ES_USERNAME", ""),
        es_password=values.get("ES_PASSWORD", ""),
        neo4j_uri=values.get("TUTOR_NEO4J_URI", "bolt://127.0.0.1:7687"),
        neo4j_user=values.get("TUTOR_NEO4J_USER", "neo4j"),
        neo4j_password=values.get("TUTOR_NEO4J_PASSWORD", "123456"),
        llm_url=values.get("DEEPSEEK_BASE_URL", "").rstrip("/"),
        llm_key=values.get("DEEPSEEK_API_KEY", ""),
        llm_model=values.get("LLM_MODEL", "deepseek-flash"),
        embedding_url=values.get("DASHSCOPE_BASE_URL", "").rstrip("/"),
        embedding_key=values.get("DASHSCOPE_API_KEY", ""),
        embedding_model=values.get("EMBEDDING_MODEL", "text-embedding-v4"),
        embedding_dim=int(values.get("EMBEDDING_DIM", "1024")),
        exercise_search_courses=values.get("TUTOR_EXERCISE_SEARCH_COURSES", "math,junior_math_7a"),
        exercise_mm_enabled=str(values.get("TUTOR_EXERCISE_MM_ENABLED", "false")).lower() in {"true", "1"},
        exercise_rerank_enabled=str(values.get("TUTOR_EXERCISE_RERANK_ENABLED", "false")).lower() in {"true", "1"},
        exercise_rerank_url=values.get("TUTOR_EXERCISE_RERANK_URL", "").strip().rstrip("/"),
        exercise_qwen_rerank_limit=int(values.get("TUTOR_EXERCISE_QWEN_RERANK_LIMIT", "20")),
        exercise_mm_model=values.get("TUTOR_EXERCISE_MM_MODEL", "qwen3-vl-embedding"),
        exercise_mm_dim=int(values.get("TUTOR_EXERCISE_MM_DIM", "1024")),
        exercise_mm_url=values.get("TUTOR_EXERCISE_MM_URL", "https://dashscope.aliyuncs.com/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"),
        exercise_candidates=int(values.get("TUTOR_EXERCISE_CANDIDATES", "30")),
        exercise_rerank_limit=int(values.get("TUTOR_EXERCISE_RERANK_LIMIT", "6")),
        exercise_search_namespace=values.get("TUTOR_EXERCISE_SEARCH_NAMESPACE", "main"),
        exercise_mm_intents=values.get("TUTOR_EXERCISE_MM_INTENTS", "exact,isomorphic,same_method"),
        exercise_rerank_intents=values.get("TUTOR_EXERCISE_RERANK_INTENTS", "exact,isomorphic,same_method"),
        context_window_tokens=int(values.get("TUTOR_CONTEXT_WINDOW_TOKENS", "32768")),
        context_request_tokens=int(values.get("TUTOR_CONTEXT_REQUEST_TOKENS", "32768")),
        context_input_limit_tokens=int(values.get("TUTOR_CONTEXT_INPUT_LIMIT_TOKENS", "0")),
        context_safety_tokens=int(values.get("TUTOR_CONTEXT_SAFETY_TOKENS", "2048")),
        context_image_tokens=int(values.get("TUTOR_CONTEXT_IMAGE_TOKENS", "4096")),
        context_soft_ratio=float(values.get("TUTOR_CONTEXT_SOFT_RATIO", "0.70")),
        context_preventive_ratio=float(values.get("TUTOR_CONTEXT_PREVENTIVE_RATIO", "0.90")),
        context_summary_output_tokens=int(values.get("TUTOR_CONTEXT_SUMMARY_OUTPUT_TOKENS", "1800")),
        context_summary_timeout_seconds=float(values.get("TUTOR_CONTEXT_SUMMARY_TIMEOUT_SECONDS", "45")),
        context_keep_recent_turns=int(values.get("TUTOR_CONTEXT_KEEP_RECENT_TURNS", "4")),
        context_tool_growth_tokens=int(values.get("TUTOR_CONTEXT_TOOL_GROWTH_TOKENS", "1024")),
        context_compression_enabled=str(values.get("TUTOR_CONTEXT_COMPRESSION_ENABLED", "true")).lower() in {"1", "true", "yes"},
    )
