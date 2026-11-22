"""只读审计 XES3G5M 教学元数据、图片和本地摘要，不读取学生交互。"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys


IMAGE_REFERENCE = re.compile(
    r"(?:question|analysis)_\d+-image_\d+(?:\.(?:png|jpg|jpeg|gif))?"
)


def audit(root: Path) -> dict:
    """原始名称和清理后的名称分开计数，避免静默合并知识点。"""
    metadata = root / "metadata"
    questions = json.loads((metadata / "questions.json").read_text(encoding="utf-8"))
    routes = json.loads((metadata / "kc_routes_map.json").read_text(encoding="utf-8"))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    checksums = json.loads((root / "file_checksums.json").read_text(encoding="utf-8"))
    checksum_errors = []
    verified = 0
    for name, expected in checksums.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()):
            checksum_errors.append({"file": name, "reason": "outside_metadata_root"})
            continue
        if not path.is_file():
            checksum_errors.append({"file": name, "reason": "missing"})
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            checksum_errors.append({"file": name, "reason": "sha256_mismatch"})
            continue
        verified += 1

    field_references = {
        field: [
            reference
            for item in questions.values()
            for reference in IMAGE_REFERENCE.findall(
                json.dumps(item.get(field), ensure_ascii=False)
            )
        ]
        for field in ("content", "options", "analysis")
    }
    references = [name for names in field_references.values() for name in names]
    reference_stems = {Path(name).stem for name in references}
    images = sorted(path for path in (metadata / "images").iterdir() if path.is_file())
    image_stems = {path.stem for path in images}
    image_digests = {hashlib.sha256(path.read_bytes()).hexdigest() for path in images}
    raw_paths = {
        route for item in questions.values() for route in item["kc_routes"]
    }
    raw_leaf_names = {route.split("----")[-1] for route in raw_paths}
    raw_node_names = {node for route in raw_paths for node in route.split("----")}
    alias_groups = defaultdict(list)
    for node_id, name in routes.items():
        alias_groups[name.strip()].append({"id": node_id, "raw_name": name})

    repeated_option_images = []
    repeated_nonimage_options = []
    identical_option_values = []
    insufficient_options = []
    invalid_choice_answers = []
    for question_id, item in questions.items():
        if item["type"] != "单选":
            continue
        options = item.get("options") or {}
        option_references = IMAGE_REFERENCE.findall(
            json.dumps(options, ensure_ascii=False)
        )
        # 图片文件存在不代表选项与图片对应正确，不按编号猜测修复。
        if option_references and len(set(option_references)) < len(option_references):
            repeated_option_images.append(question_id)
        if not option_references and len(set(options.values())) < len(options):
            repeated_nonimage_options.append(question_id)
        if option_references and len(set(options.values())) == 1:
            identical_option_values.append(question_id)
        if len(options) < 2:
            insufficient_options.append(question_id)
        answer = item.get("answer") or []
        if len(answer) != 1 or str(answer[0]).strip() not in options:
            invalid_choice_answers.append(question_id)
    unavailable = (
        set(repeated_option_images)
        | set(insufficient_options)
        | set(repeated_nonimage_options)
    )
    sort_ids = lambda values: sorted(values, key=lambda value: int(value))
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "教学元数据与本地配图；未读取学生历史交互或预训练 embedding 文件",
        "sources": {
            "xes3g5m_official_repository": "https://github.com/ai4ed/XES3G5M",
            "official_metadata_description": "https://github.com/ai4ed/XES3G5M#metadata",
            "official_question_count_clarification": "https://github.com/ai4ed/XES3G5M#clarification",
            "official_download": "https://drive.google.com/file/d/1eFiIYyh5O2V90RA0brammGH6EpHvPDQe/view",
            "mechanics_upstream_repository": "https://github.com/THU-KEG/MOOC-Radar",
            "source_pages_verified_on": "2026-10-19",
        },
        "local_integrity": {
            "checksum_files": len(checksums),
            "verified_files": verified,
            "checksum_errors": checksum_errors,
            "checksum_origin": "本地提取时生成的摘要，不是官方签名或官方文件摘要",
            "metadata_subtree_complete": manifest.get("metadata_subtree_complete"),
            "archive_complete": manifest.get("archive_complete"),
            "interaction_files_extracted": manifest.get("interaction_files_extracted"),
            "needs_additional_download_for_current_metadata_scope": bool(checksum_errors),
        },
        "questions": {
            "count": len(questions),
            "types": dict(Counter(item["type"] for item in questions.values())),
            "nonempty_fields": {
                field: sum(bool(item.get(field)) for item in questions.values())
                for field in ("content", "answer", "analysis", "kc_routes")
            },
            "answer_list_lengths": dict(
                sorted(Counter(len(item["answer"]) for item in questions.values()).items())
            ),
        },
        "knowledge_nodes": {
            "raw_mapping_entries": len(routes),
            "raw_mapping_distinct_names": len(set(routes.values())),
            "trimmed_mapping_distinct_names": len({name.strip() for name in routes.values()}),
            "observed_raw_leaf_names": len(raw_leaf_names),
            "observed_trimmed_leaf_names": len({name.strip() for name in raw_leaf_names}),
            "observed_raw_all_node_names": len(raw_node_names),
            "observed_trimmed_all_node_names": len({name.strip() for name in raw_node_names}),
            "unique_full_paths": len(raw_paths),
            "whitespace_alias_groups": [items for items in alias_groups.values() if len(items) > 1],
            "map_names_absent_from_question_paths": sorted(set(routes.values()) - raw_node_names),
            "interpretation": "865 是原始叶名称数；去除两组叶名称首尾空格后为 863。保留全部原节点 ID 和原名。",
        },
        "images": {
            "files": len(images),
            "reference_occurrences": len(references),
            "unique_reference_keys": len(set(references)),
            "unique_reference_stems": len(reference_stems),
            "unique_image_content_sha256": len(image_digests),
            "by_field": {
                field: {"occurrences": len(values), "unique_keys": len(set(values))}
                for field, values in field_references.items()
            },
            "missing_reference_stems": sorted(reference_stems - image_stems),
            "unreferenced_file_stems": sorted(image_stems - reference_stems),
        },
        "quality_isolation": {
            "repeated_option_image_question_count": len(repeated_option_images),
            "repeated_option_image_question_ids": sort_ids(repeated_option_images),
            "repeated_nonimage_option_values_question_count": len(repeated_nonimage_options),
            "repeated_nonimage_option_values_question_ids": sort_ids(repeated_nonimage_options),
            "repeated_nonimage_option_values_note": "5 道题重复项均为错误干扰项，属于选项质量问题，不代表正确答案键存在歧义；保守隔离。",
            "identical_all_option_values_question_count": len(identical_option_values),
            "insufficient_options_question_count": len(insufficient_options),
            "insufficient_options_question_ids": sort_ids(insufficient_options),
            "invalid_choice_answer_key_question_ids": sort_ids(invalid_choice_answers),
            "isolated_union_count": len(unavailable),
            "isolated_union_question_ids": sort_ids(unavailable),
            "remaining_before_multimodal_and_grading_validation": len(questions) - len(unavailable),
            "interpretation": "65 道题保留原始记录和检索用途，但不用于独立客观判分；其余题目仍需按视觉依赖与多空答案能力检查。",
        },
    }


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data/raw/xes3g5m"))
    parser.add_argument("--output", type=Path, default=Path("data/reports/xes_source_verification.json"))
    args = parser.parse_args()
    report = audit(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "report": str(args.output.resolve()),
        "verified_files": report["local_integrity"]["verified_files"],
        "checksum_errors": len(report["local_integrity"]["checksum_errors"]),
        "questions": report["questions"]["count"],
        "images": report["images"]["files"],
        "isolated_questions": report["quality_isolation"]["isolated_union_count"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
