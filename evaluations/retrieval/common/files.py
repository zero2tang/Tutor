"""原子文件、内容指纹和只追加的阶段回执。"""

import hashlib
import json
import time
from pathlib import Path


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def read_rows(path):
    path = Path(path)
    return (
        [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if path.exists()
        else []
    )


def atomic_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{time.time_ns()}.tmp")
    temp.write_text(value, encoding="utf-8")
    # Windows读取进度文件时会短暂阻止替换，有限重试后仍失败才报错。
    for attempt in range(8):
        try:
            temp.replace(path)
            break
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.01 * (attempt + 1))


def write_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_rows(path, rows):
    atomic_text(
        path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    )


def append_row(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        stream.flush()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def mutable(dataset):
    if (Path(dataset) / "superseded.json").exists():
        raise ValueError("此数据快照已被新规模替代，禁止继续远程处理")
    if (Path(dataset) / "freeze.json").exists():
        raise ValueError("数据集已冻结；修改数据或标签必须创建新版本目录")


def pin(path, value):
    """同一路径只能恢复相同配置，不能覆盖另一次实验。"""
    old = read_json(path)
    if old is not None and old != value:
        raise ValueError(f"配置或来源变化，请新建版本：{Path(path).name}")
    if old is None:
        write_json(path, value)


def verify_freeze(dataset):
    frozen = read_json(Path(dataset) / "freeze.json")
    if not frozen:
        raise ValueError("请先完成 label 和 freeze，禁止在可变标签上正式评分")
    for name, expected in frozen["files"].items():
        if file_hash(Path(dataset) / name) != expected:
            raise ValueError("冻结文件已变化：" + name)
    return frozen
