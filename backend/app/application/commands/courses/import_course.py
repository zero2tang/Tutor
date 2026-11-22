"""课程教学数据导入与检索投影入口，旧力学命令保持兼容。"""

import argparse
import asyncio
import json
from pathlib import Path
import sys

from app.infrastructure.config import ROOT
from app.application.commands.courses.import_mechanics import import_course as import_mechanics
from app.domains.courses.retrieval.indexing import project_course


async def build(course_id: str = "mechanics") -> dict:
    return await project_course(course_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path)
    parser.add_argument("--course", choices=["mechanics", "math"], default="mechanics")
    parser.add_argument(
        "--index-only", action="store_true", help="只重建投影，复用已导入的课程事实"
    )
    parser.add_argument(
        "--import-only", action="store_true", help="只导入 MySQL，暂不调用向量服务"
    )
    args = parser.parse_args()
    if args.index_only and args.import_only:
        parser.error("--index-only 与 --import-only 不能同时使用")
    if not args.index_only:
        if args.course == "math":
            from app.application.commands.courses.import_math import import_course as import_math

            result = import_math(args.source or ROOT / "data/raw/xes3g5m/metadata")
        else:
            result = import_mechanics(
                args.source
                or Path(r"D:\Python项目\CBEGRec-main\data\Mechanical_Physics")
            )
        print(json.dumps(result, ensure_ascii=False), flush=True)
    if not args.import_only:
        print(
            json.dumps(asyncio.run(build(args.course)), ensure_ascii=False), flush=True
        )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
