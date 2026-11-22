"""离线迁移：完整备份、资源预检、空库单事务导入和只读校验。"""

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from app.infrastructure.persistence.database import engine
from scripts.maintenance.postgres_migration.transfer import (
    export_mysql, import_snapshot, preflight, receipt_path, verify_snapshot,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["export", "preflight", "import", "verify"])
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    if args.action == "export":
        report = export_mysql(args.snapshot)
    elif args.action == "preflight":
        _, report = preflight(args.snapshot)
    elif args.action == "import":
        report = import_snapshot(args.snapshot, engine())
    else:
        report = verify_snapshot(args.snapshot, engine())
    output = receipt_path(args.snapshot, args.action)
    if args.action != "export":
        if output.exists():
            output = output.with_name(output.stem + "." + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".json")
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"action": args.action, "tables": len(report["tables"]),
        "rows": sum(item["rows"] for item in report["tables"].values()), "report": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
