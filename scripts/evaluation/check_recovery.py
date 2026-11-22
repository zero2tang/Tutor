"""围绕真正的进程重启保存前后快照，不用新 HTTP 客户端冒充服务重启。"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "data/reports"


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def snapshot(client, artifacts) -> dict:
    sid = artifacts["recovery_session"]
    session = client.get(f"/sessions/{sid}").raise_for_status().json()
    profile = (
        client.get("/student", params={"student_id": artifacts["student_id"]})
        .raise_for_status()
        .json()
    )
    events = client.get(f"/sessions/{sid}/events").raise_for_status().json()["items"]
    # 启动脚本记录应用进程身份，用来确认前后确实发生了进程替换。
    process_file = ROOT / "data/runtime/processes.json"
    processes = {}
    if process_file.exists():
        for row in json.loads(process_file.read_text(encoding="utf-8-sig")):
            processes[row["kind"]] = row
    return {
        "session_id": sid,
        "question_id": session["pending_question"]["id"],
        "phase": session["phase"],
        "pending_question_sha256": digest(session["pending_question"]),
        "events_sha256": digest(events),
        "event_count": len(events),
        "student_states_sha256": digest(profile["states"]),
        "attempt_count": profile["attempt_count"],
        "application_processes": processes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["before", "after"])
    parser.add_argument("--base-url", default="http://127.0.0.1:8008/api")
    args = parser.parse_args()
    report = json.loads(
        (REPORTS / "mechanics_acceptance.json").read_text(encoding="utf-8")
    )
    assert report["complete_suite_run"], "先完成固定验收任务"
    with httpx.Client(base_url=args.base_url, timeout=30, trust_env=False) as client:
        current = snapshot(client, report["artifacts"])
    baseline = REPORTS / "restart_baseline.json"
    if args.stage == "before":
        baseline.write_text(
            json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print("已保存重启前原题、事件、学生状态快照")
        return
    before = json.loads(baseline.read_text(encoding="utf-8"))
    changes = {
        key: [before.get(key), value]
        for key, value in current.items()
        if key != "application_processes" and before.get(key) != value
    }
    process_replaced = all(
        before.get("application_processes", {}).get(kind)
        and current["application_processes"].get(kind)
        and before["application_processes"][kind]
        != current["application_processes"][kind]
        for kind in ["backend", "frontend"]
    )
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "passed" if not changes and process_replaced else "failed",
        "application_processes_replaced": bool(process_replaced),
        "before": before,
        "after": current,
        "differences": changes,
    }
    (REPORTS / "restart_recovery.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    assert (
        not changes and process_replaced
    ), "状态不一致或没有实际重启进程，见 restart_recovery.json"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
