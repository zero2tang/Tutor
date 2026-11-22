"""下载 Qwen 官方 ModelScope 模型快照并逐文件校验。"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
from urllib.parse import quote

from .. import ROOT
from ..common.files import write_json


BASE = "https://modelscope.cn"
MODELS = ("Qwen3-Reranker-0.6B", "Qwen3-VL-Reranker-2B")
CACHE = ROOT / "data/cache/reranker_models"
CURL = ["curl.exe", "--ssl-revoke-best-effort", "--fail", "--silent", "--show-error",
        "--location", "--connect-timeout", "20", "--retry", "2"]


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_ranges(url, partial, size):
    # 大文件分段下载；独立的区间文件可以在中断后继续使用。
    width = 32 * 1024 * 1024
    ranges = [(start, min(start + width, size) - 1) for start in range(0, size, width)]

    def fetch(pair):
        start, end = pair
        path = partial.with_name(f"{partial.name}.{start}")
        if not path.is_file() or path.stat().st_size != end - start + 1:
            status = subprocess.check_output(CURL + [
                "--max-time", "90", "--range", f"{start}-{end}", "--output", str(path),
                "--write-out", "%{http_code}", url,
            ]).decode("ascii")
            if status != "206" or path.stat().st_size != end - start + 1:
                raise ValueError(f"模型分段返回异常：{start}-{end}, HTTP {status}")
        return path

    with ThreadPoolExecutor(max_workers=8) as executor:
        paths = list(executor.map(fetch, ranges))
    with partial.open("wb") as target:
        for path in paths:
            with path.open("rb") as source:
                shutil.copyfileobj(source, target, 4 * 1024 * 1024)
    return paths


def download(model):
    model_id = "Qwen/" + model
    metadata = subprocess.check_output(CURL + [
        "--max-time", "60", f"{BASE}/api/v1/models/{model_id}/repo/files?Revision=master&Recursive=true"
    ])
    files = json.loads(metadata)["Data"]["Files"]
    files = [f for f in files if f.get("Sha256") and f["Path"] != ".gitattributes"]
    if not any(f["Path"] == "model.safetensors" and f["Size"] > 1_000_000_000 for f in files):
        raise ValueError("官方清单缺少模型权重")
    folder = CACHE / model
    folder.mkdir(parents=True, exist_ok=True)

    def fetch(info):
        rel = PurePosixPath(info["Path"])
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("不支持的模型文件路径")
        destination = folder.joinpath(*rel.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file() and destination.stat().st_size == info["Size"] and sha256(destination) == info["Sha256"]:
            return
        partial = destination.with_name(destination.name + ".partial")
        url = f"{BASE}/models/{model_id}/resolve/master/{quote(rel.as_posix())}"
        if rel.suffix == ".safetensors":
            print(f"{model}: downloading {rel} ({info['Size']:,} bytes)", flush=True)
        segments = []
        if not partial.exists() or partial.stat().st_size < info["Size"]:
            if rel.suffix == ".safetensors":
                large_url = f"{BASE}/api/v1/models/{model_id}/repo?Revision=master&FilePath={quote(rel.as_posix())}"
                segments = download_ranges(large_url, partial, info["Size"])
            else:
                subprocess.run(CURL + ["--max-time", "60", "--continue-at", "-", "--output", str(partial), url], check=True)
        if partial.stat().st_size != info["Size"] or sha256(partial) != info["Sha256"]:
            raise ValueError(f"{model}/{rel} 与官方大小或 SHA256 不一致：{partial.stat().st_size}")
        partial.replace(destination)
        for segment in segments:
            segment.unlink()
        if rel.suffix == ".safetensors":
            print(f"{model}: SHA256 verified", flush=True)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(fetch, sorted(files, key=lambda f: -f["Size"])))
    write_json(folder / "tutor_snapshot.json", {"source": BASE + "/models/" + model_id,
        "files": {f["Path"]: {"sha256": f["Sha256"], "bytes": f["Size"]} for f in files}})
    print(folder, flush=True)


if __name__ == "__main__":
    models = sys.argv[1:] or MODELS
    for name in models:
        if name not in MODELS:
            raise ValueError("只允许本实验的两个 Qwen 模型")
        download(name)
