"""从论文官方入口下载 XES3G5M，仅解包教学元数据和配图。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tarfile
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath


FILE_ID = "1eFiIYyh5O2V90RA0brammGH6EpHvPDQe"
SOURCE = "https://github.com/ai4ed/XES3G5M"


class DownloadForm(HTMLParser):
    """读取公开下载页的确认参数，不执行页面脚本。"""

    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "input" and values.get("type") == "hidden" and values.get("name"):
            self.fields[str(values["name"])] = values.get("value") or ""


def open_download():
    url = f"https://drive.google.com/uc?export=download&id={FILE_ID}"
    response = urllib.request.urlopen(url, timeout=60)
    if "text/html" not in response.headers.get("Content-Type", ""):
        return response
    with response:
        form = DownloadForm()
        form.feed(response.read(128_000).decode("utf-8"))
    if form.fields.get("id") != FILE_ID or "confirm" not in form.fields:
        raise RuntimeError("官方链接未返回可识别的公开下载表单")
    allowed = {key: value for key, value in form.fields.items() if key in {"id", "export", "confirm", "uuid"}}
    response = urllib.request.urlopen(
        "https://drive.usercontent.google.com/download?" + urllib.parse.urlencode(allowed),
        timeout=60,
    )
    if "text/html" in response.headers.get("Content-Type", ""):
        response.close()
        raise RuntimeError("下载入口返回网页，未取得数据文件")
    return response


def download_archive(archive: Path) -> str:
    archive.parent.mkdir(parents=True, exist_ok=True)
    checksum = hashlib.sha256()
    if archive.exists():
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                checksum.update(chunk)
        return checksum.hexdigest()
    partial = archive.with_suffix(archive.suffix + ".part")
    with open_download() as response, partial.open("wb") as stream:
        total = 0
        checkpoint = 0
        while chunk := response.read(1024 * 1024):
            stream.write(chunk)
            checksum.update(chunk)
            total += len(chunk)
            if total - checkpoint >= 32 * 1024 * 1024:
                print(f"已下载 {total // (1024 * 1024)} MiB", flush=True)
                checkpoint = total
    if not tarfile.is_tarfile(partial):
        raise RuntimeError("下载结果不是有效压缩数据包")
    partial.replace(archive)
    return checksum.hexdigest()


def extract_metadata(archive: Path, destination: Path, *, metadata_prefix: bool = False) -> dict:
    root = destination.resolve()
    root.mkdir(parents=True, exist_ok=True)
    extracted: list[str] = []
    metadata_boundary_observed = False
    with tarfile.open(archive, "r|gz") as source:
        for member in source:
            parts = PurePosixPath(member.name).parts
            # 官方包的 metadata 子树先于交互目录；已有完整子树时不继续读取交互内容。
            if metadata_prefix and extracted and "metadata" not in parts and member.isfile():
                metadata_boundary_observed = True
                break
            if "metadata" not in parts or not member.isfile():
                continue
            relative_parts = parts[parts.index("metadata") :]
            relative = PurePosixPath(*relative_parts)
            wanted = (
                str(relative) in {"metadata/questions.json", "metadata/kc_routes_map.json"}
                or (len(relative_parts) > 2 and relative_parts[1] == "images")
            )
            if not wanted or ".." in relative_parts:
                continue
            target = root.joinpath(*relative_parts).resolve()
            if not target.is_relative_to(root):
                raise RuntimeError("数据包包含越界路径")
            target.parent.mkdir(parents=True, exist_ok=True)
            incoming = source.extractfile(member)
            if incoming is None:
                continue
            with incoming, target.open("wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
            extracted.append(str(relative))
    required = {"metadata/questions.json", "metadata/kc_routes_map.json"}
    if not required.issubset(extracted):
        raise RuntimeError("数据包缺少预期的两份核心元数据文件")
    if metadata_prefix and not metadata_boundary_observed:
        raise RuntimeError("尚未观察到完整教学元数据子树的结束边界")
    questions = json.loads((root / "metadata/questions.json").read_text(encoding="utf-8"))
    routes = json.loads((root / "metadata/kc_routes_map.json").read_text(encoding="utf-8"))
    leaves = {route.split("----")[-1].strip() for item in questions.values() for route in item["kc_routes"]}
    image_names = {PurePosixPath(name).name for name in extracted if name.startswith("metadata/images/")}
    image_stems = {PurePosixPath(name).stem for name in image_names}
    references = set(re.findall(r"(?:question|analysis)_\d+-image_\d+(?:\.(?:png|jpg|jpeg|gif))?", json.dumps(questions, ensure_ascii=False)))
    missing_images = sorted(name for name in references if name not in image_names and PurePosixPath(name).stem not in image_stems)
    if missing_images:
        raise RuntimeError(f"题目引用的配图缺失，共 {len(missing_images)} 个")
    # 记录每个实际保留文件的摘要，前缀下载不冒充完整压缩包校验。
    file_hashes = {name: hashlib.sha256(root.joinpath(*PurePosixPath(name).parts).read_bytes()).hexdigest() for name in extracted}
    (root / "file_checksums.json").write_text(json.dumps(file_hashes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "source_url": SOURCE,
        "download_file_id": FILE_ID,
        "questions": len(questions),
        "kc_route_map_entries": len(routes),
        "observed_leaf_names": len(leaves),
        "image_files": sum(name.startswith("metadata/images/") for name in extracted),
        "referenced_image_keys": len(references),
        "missing_image_references": missing_images,
        "extracted_files": len(extracted),
        "interaction_files_extracted": 0,
        "metadata_subtree_complete": True,
        "archive_complete": not metadata_prefix,
    }


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path("tmp/datasets/XES3G5M.tar.gz"))
    parser.add_argument("--output", type=Path, default=Path("data/raw/xes3g5m"))
    parser.add_argument("--metadata-prefix", type=Path, help="从已含完整 metadata 子树的下载前缀恢复，只校验保留的教学文件")
    args = parser.parse_args()
    if args.metadata_prefix:
        manifest = extract_metadata(args.metadata_prefix, args.output, metadata_prefix=True)
    else:
        digest = download_archive(args.archive)
        manifest = extract_metadata(args.archive, args.output)
        manifest["archive_sha256"] = digest
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
