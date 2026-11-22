"""快照和配置边界测试不依赖真实外部服务。"""

import random
import pytest
from evaluations.retrieval.common.dataset import (
    proportional_sample,
    template_key,
    validate_text,
)
from evaluations.retrieval.common.files import (
    pin,
    read_json,
    write_json,
    file_hash,
    verify_freeze,
    mutable,
)
from evaluations.retrieval.common.labeling import validate_judgment
from evaluations.retrieval.common.files import atomic_text
from pathlib import Path


def test_proportional_sampling_is_deterministic():
    rows = [
        {"id": str(i), "module": "A" if i < 60 else "B", "question_type": "fill"}
        for i in range(100)
    ]
    first = proportional_sample(rows, 20, random.Random(9))
    assert first == proportional_sample(rows, 20, random.Random(9))
    assert sum(r["module"] == "A" for r in first) == 12
    assert len({r["id"] for r in first}) == 20


def test_insufficient_stratum_is_not_fabricated():
    with pytest.raises(ValueError, match="不足"):
        proportional_sample([], 10, random.Random(1))


def test_numeric_template_variants_share_family():
    assert template_key("有3件上衣和5条裤子", "计数") == template_key(
        "有7件上衣和9条裤子", "计数"
    )


@pytest.mark.parametrize(
    "text",
    [
        "math:123",
        "![图](/api/assets/math/a.png)",
        "question_1-image_0.png",
        "C:\\private\\test.png",
        "https://example.com/image",
    ],
)
def test_no_identifiers_or_paths_in_encoding(text):
    with pytest.raises(ValueError):
        validate_text(text)


def test_pin_prevents_overwriting_configuration(tmp_path):
    pin(tmp_path / "config.json", {"version": 1})
    pin(tmp_path / "config.json", {"version": 1})
    with pytest.raises(ValueError):
        pin(tmp_path / "config.json", {"version": 2})


def test_frozen_file_tampering_is_detected(tmp_path):
    write_json(tmp_path / "queries.jsonl", [])
    write_json(
        tmp_path / "freeze.json",
        {"files": {"queries.jsonl": file_hash(tmp_path / "queries.jsonl")}},
    )
    verify_freeze(tmp_path)
    with pytest.raises(ValueError, match="冻结"):
        mutable(tmp_path)
    write_json(tmp_path / "queries.jsonl", [1])
    with pytest.raises(ValueError, match="变化"):
        verify_freeze(tmp_path)


def test_uncertainty_cannot_become_a_negative():
    assert (
        validate_judgment({"grade": 0, "uncertain": True, "reason": "图片无法确认"})[
            "grade"
        ]
        is None
    )
    with pytest.raises(ValueError):
        validate_judgment({"grade": True, "uncertain": False, "reason": ""})


def test_atomic_replace_retries_transient_windows_reader_lock(tmp_path, monkeypatch):
    original = Path.replace
    attempts = []

    def replace(path, target):
        attempts.append(1)
        if len(attempts) == 1:
            raise PermissionError("临时读锁")
        return original(path, target)

    monkeypatch.setattr(Path, "replace", replace)
    atomic_text(tmp_path / "state.json", "complete")
    assert (tmp_path / "state.json").read_text() == "complete"
    assert len(attempts) == 2
