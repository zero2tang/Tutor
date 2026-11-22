"""公开题面生成查询，独立处理查询图片，按来源题族隔离划分。"""

from pathlib import Path
from .ai import Clients
from .dataset import query_seeds, validate_text
from .files import read_rows, write_rows, write_json, read_json, digest, mutable

QUERY_PROMPT = """你是数学检索评测查询设计员，只依据公开题面和题图，不求答案，不添加隐含条件。
输出JSON {"queries":[{"text":"..."},{"text":"..."}]}，恰好两条不同表达，均保留足够数学条件。
exact：改写措辞但保持原题全部数值、选项、条件和所求；不能带题号或资源路径。
isomorphic：请求寻找相同数学关系和操作结构的另一题，不能只描述同知识点。
same_method：请求寻找可用同一解题方法的例题，表达方法需要的条件，但不能给出原题答案。
不得使用答案、解析、来源题号、URL或图片文件名。带图时图片保持原样，文字条件不能与图冲突。
两条表达需独立自然，禁止仅更换一句请求前缀。"""


async def enrich(dataset, *, limit=None, snapshot_only=False):
    dataset = Path(dataset)
    mutable(dataset)
    if snapshot_only:
        return
    clients = Clients(dataset)
    try:
        corpus = read_rows(dataset / "corpus.jsonl")
        pending = [r for r in corpus if r["transcription_status"] == "pending"]
        for row in pending[:limit] if limit else pending:
            text, transcription = await clients.transcription(row)
            validate_text(text)
            row.update(
                text=text,
                transcription=transcription,
                transcription_status="auto_generated_pending_human_review",
            )
            write_rows(dataset / "corpus.jsonl", corpus)
            print(f"transcribed {row['id']}", flush=True)
        if any(r["transcription_status"] == "pending" for r in corpus):
            return
        lookup = {r["id"]: r for r in corpus}
        queries = read_rows(dataset / "queries.jsonl")
        completed = {q["source_group"] for q in queries}
        seeds = [s for s in query_seeds(dataset) if s["source_group"] not in completed]
        for seed in seeds[:limit] if limit else seeds:
            source = lookup[seed["source_id"]]
            value = await clients.json_call(
                "query_generation",
                {
                    "intent": seed["intent"],
                    "text": source["text"],
                    "input_kind": seed["input_kind"],
                    "evidence_kind": "public_surface_and_frozen_diagram_transcription",
                },
                [],
                QUERY_PROMPT,
            )
            generated = value.get("queries", [])
            if len(generated) != 2 or len({r.get("text") for r in generated}) != 2:
                raise ValueError("查询生成必须返回两条不同表达")
            batch = []
            for i, item in enumerate(generated):
                validate_text(item["text"])
                assets = source["assets"] if seed["input_kind"] == "image" else []
                row = {
                    **seed,
                    "id": "q_" + digest([seed, i])[:16],
                    "module": source["module"],
                    "native_text": item["text"],
                    "assets": assets,
                    "snapshot": {"stem": item["text"], "options": {}},
                    "known_targets": (
                        [source["id"]] if seed["intent"] == "exact" else []
                    ),
                    "excluded_ids": (
                        [
                            r["id"]
                            for r in corpus
                            if r["duplicate_key"] == source["duplicate_key"]
                        ]
                        if seed["intent"] != "exact"
                        else []
                    ),
                }
                row["text"], row["transcription"] = await clients.transcription(
                    row, query=True
                )
                validate_text(row["text"])
                batch.append(row)
            queries.extend(batch)
            write_rows(dataset / "queries.jsonl", queries)
            print(
                f"queries {seed['intent']}/{seed['input_kind']} {len(queries)}",
                flush=True,
            )
        manifest = read_json(dataset / "manifest.json")
        manifest["status"] = (
            "queries_ready"
            if len(queries) == manifest["query_count"]
            else "queries_partial"
        )
        write_json(dataset / "manifest.json", manifest)
    finally:
        await clients.close()
