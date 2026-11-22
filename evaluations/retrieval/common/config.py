"""配置只控制召回通道，四组共享其余参数。"""

from dataclasses import dataclass
from pathlib import Path
from .files import digest, read_json, file_hash

HOME = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = HOME / "datasets/math_100_q24_v1"
SEED = 20261023
INTENTS = ("exact", "isomorphic", "same_method")
CHANNELS = {"keyword", "text_vector", "multimodal"}


@dataclass(frozen=True)
class Experiment:
    id: str
    title: str
    channels: tuple[str, ...]
    candidate_limit: int = 50
    rrf_k: int = 60

    def as_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "channels": list(self.channels),
            "candidate_limit": self.candidate_limit,
            "rrf_k": self.rrf_k,
        }


def experiments():
    result = []
    for path in sorted((HOME / "experiments").glob("*/config.json")):
        value = read_json(path)
        value["channels"] = tuple(value["channels"])
        exp = Experiment(**value)
        if (
            not exp.channels
            or set(exp.channels) - CHANNELS
            or len(set(exp.channels)) != len(exp.channels)
        ):
            raise ValueError("召回通道无效或重复")
        if exp.candidate_limit != 50 or exp.rrf_k != 60:
            raise ValueError("v1 比较必须共用每通道50题和RRF常数60")
        result.append(exp)
    if len({e.id for e in result}) != len(result):
        raise ValueError("实验编号重复")
    return result


def model_config(settings):
    return {
        "text_model": settings.embedding_model,
        "text_dim": settings.embedding_dim,
        "text_endpoint": settings.embedding_url,
        "visual_model": settings.exercise_mm_model,
        "visual_dim": settings.exercise_mm_dim,
        "visual_endpoint": settings.exercise_mm_url,
        "judge_model": settings.llm_model,
        "judge_endpoint": settings.llm_url,
        "representation": "benchmark-transcription-v1",
        "query_prompt": "benchmark-query-v1",
        "label_prompt": "benchmark-label-v1",
    }


def experiment_hash():
    return digest([e.as_dict() for e in experiments()])


def implementation_snapshot():
    root = HOME.parents[1]
    paths = [
        HOME / "common" / name
        for name in (
            "metrics.py",
            "retrievers.py",
            "config.py",
            "running.py",
            "ragas_worker.py",
        )
    ]
    paths += [
        root / "backend/app" / name
        for name in (
            "domains/courses/retrieval/fusion.py",
            "infrastructure/clients/retrieval_index.py",
        )
    ]
    return {
        str(path.relative_to(root)).replace("\\", "/"): file_hash(path)
        for path in paths
    }


def query_subset(queries, limit=None, smoke=False):
    if smoke:
        selected, seen = [], set()
        for query in queries:
            key = (query["intent"], query["input_kind"])
            if key not in seen:
                seen.add(key)
                selected.append(query)
        return selected
    return queries[:limit] if limit else queries
