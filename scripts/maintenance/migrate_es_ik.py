"""复制 Tutor 的在用索引并发布 IK 版本，保留原索引。"""

import argparse
import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.infrastructure.clients.search import SearchClient
from app.infrastructure.clients.search_analysis import IK_TEXT_ANALYSIS


QUESTION_ALIAS = re.compile(r"stem_exercise_[0-9a-f]{12}_(text|visual)")
GENERATED_SETTINGS = {"creation_date", "provided_name", "uuid", "version", "history_uuid"}


async def current_sources(search):
    names = []
    for alias, legacy in search.LEGACY_INDICES.items():
        names.append((alias, legacy))
    aliases = await search.http.get("/_alias/stem_exercise_*")
    if aliases.status_code != 404:
        aliases.raise_for_status()
        names.extend((alias, None) for alias in sorted({
            alias for entry in aliases.json().values() for alias in entry["aliases"]
            if QUESTION_ALIAS.fullmatch(alias)
        }))

    sources = {}
    for alias, legacy in names:
        response = await search.http.get("/_alias/" + alias)
        if response.status_code == 404:
            if legacy and (await search.http.head("/" + legacy)).status_code != 404:
                sources[alias] = (legacy, None)
            continue
        response.raise_for_status()
        resolved = response.json()
        if len(resolved) != 1:
            raise ValueError(f"别名 {alias} 指向多个索引，停止迁移。")
        source, info = next(iter(resolved.items()))
        prefix = legacy if legacy else alias + "_"
        if not source.startswith(prefix) and not (legacy and source.startswith(alias + "_ik_")):
            raise ValueError(f"别名 {alias} 指向非预期索引 {source}，停止迁移。")
        sources[alias] = (source, info["aliases"][alias])
    return sources


def mapping_for_ik(mapping):
    result = deepcopy(mapping)
    changed = False
    for field in ("title", "text"):
        properties = result["properties"].get(field)
        if properties is None:
            continue
        if properties.get("type") != "text" or properties.get("analyzer") not in ("cjk", "ik_max_word"):
            raise ValueError(f"字段 {field} 的类型或分析器与预期不一致。")
        if any(properties.get(k) != v for k, v in IK_TEXT_ANALYSIS.items()):
            properties.update(IK_TEXT_ANALYSIS)
            changed = True
    return result, changed


async def check_dictionaries(search):
    for analyzer in IK_TEXT_ANALYSIS.values():
        for term in ("等差数列求公差", "矩形薄板单元"):
            response = await search.request("POST", "/_analyze", json={"analyzer": analyzer, "text": term})
            if term not in {item["token"] for item in response["tokens"]}:
                raise ValueError(f"{analyzer} 未识别术语 {term}，请检查节点的 IK 扩展词典。")


async def counts(search, index):
    overall = await search.request("GET", f"/{index}/_count")
    vectors = await search.request("POST", f"/{index}/_count", json={
        "query": {"exists": {"field": "embedding"}}
    })
    return overall["count"], vectors["count"]


async def migrate(apply):
    search = SearchClient()
    try:
        sources = await current_sources(search)
        if apply:
            await check_dictionaries(search)
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        actions = []
        for alias, (source, alias_settings) in sources.items():
            old = (await search.request("GET", f"/{source}"))[source]
            mapping, changed = mapping_for_ik(old["mappings"])
            if not changed and alias_settings is not None:
                print(f"{alias}: already IK ({source})", flush=True)
                continue
            target = f"{source}_ik_{stamp}"
            print(f"{alias}: {source} -> {target}", flush=True)
            if not apply:
                continue

            settings = {k: v for k, v in old["settings"]["index"].items() if k not in GENERATED_SETTINGS}
            await search.request("POST", f"/{source}/_refresh")
            before = await counts(search, source)
            await search.request("PUT", f"/{target}", json={
                "settings": {"index": settings}, "mappings": mapping,
            })
            result = await search.request(
                "POST", "/_reindex", params={"refresh": "true"}, timeout=300,
                json={"source": {"index": source}, "dest": {"index": target, "op_type": "create"}},
            )
            if result.get("timed_out") or result.get("failures") or result.get("created") != before[0]:
                raise RuntimeError(f"{source} 复制异常：{result}")
            if await counts(search, target) != before or await counts(search, source) != before:
                raise RuntimeError(f"{source} 文档或向量计数不一致，未发布新索引。")
            if alias_settings is not None:
                actions.append({"remove": {"index": source, "alias": alias, "must_exist": True}})
            actions.append({"add": {"index": target, "alias": alias, **(alias_settings or {})}})
            print(json.dumps({"index": target, "documents": before[0], "vectors": before[1]}), flush=True)

        if apply and actions:
            if await current_sources(search) != sources:
                raise RuntimeError("复制期间别名发生变化，未发布新索引。")
            result = await search.request("POST", "/_aliases", json={"actions": actions})
            if not result.get("acknowledged") or result.get("errors"):
                raise RuntimeError(f"发布别名未确认：{result}")
            print("IK aliases published.", flush=True)
    finally:
        await search.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="暂停 Tutor 写入后，复制并发布新索引。")
    asyncio.run(migrate(parser.parse_args().apply))
