"""教材资料摘要、来源与允许读取的原文类型；这些是业务公开投影。"""

from copy import deepcopy

BLOCK_KINDS = {
    "definition",
    "rule",
    "theorem",
    "concept",
    "technique",
    "paragraph",
    "text",
}


SOURCE_FIELDS = {
    "document_id",
    "page",
    "locator",
    "bbox",
    "source_uri",
    "line",
    "column",
}


def source_view(source):
    return {
        key: deepcopy(value) for key, value in source.items() if key in SOURCE_FIELDS
    }


def resource_summary(
    resource_id,
    kind,
    title,
    text,
    course_id,
    source_ref,
    source_refs=None,
    origin="source_record",
):
    return {
        "resource_id": resource_id,
        "kind": kind,
        "title": title[:240],
        "summary": text[:500],
        "origin": origin,
        "source_refs": source_refs or [{"source_uri": source_ref}],
        "citation": {
            "id": resource_id,
            "title": title[:240],
            "source_ref": source_ref,
            "course_id": course_id,
        },
    }
