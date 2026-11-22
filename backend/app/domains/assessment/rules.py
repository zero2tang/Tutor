"""单题与整组评估共用的判分权威和证据层级边界。"""

from copy import deepcopy

LEVELS = ["beginner", "advanced", "proficient"]
COGNITIVE_LEVEL = dict(zip(["recall", "understanding", "application"], LEVELS))


def finalize_diagnosis(
    diagnosis, contract, cognitive_type, objective, run_id
):
    result = deepcopy(diagnosis)
    level = COGNITIVE_LEVEL[cognitive_type]
    result["level"] = (
        level
        if contract.get("cognitive_type") != "unknown"
        else LEVELS[min(LEVELS.index(result["level"]), LEVELS.index(level))]
    )
    if objective:
        if result["result"] != objective["result"]:
            result["feedback"] = objective["feedback"] + " " + result["feedback"]
        result.update(result=objective["result"], score=objective["score"])
        result["grading_confidence"] = objective["confidence"]
    elif result["confidence"] < 0.8:
        result.update(result="ungraded", score=None, next_action="clarify")
    result.update(
        answer_key=contract["answer"],
        source_ref=contract["source_ref"],
        run_id=run_id,
    )
    return result
