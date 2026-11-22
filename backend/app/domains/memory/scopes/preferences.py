"""显式偏好绑定到服务分配的原子ID及作用域，不能仅凭整条消息ID扩大范围。"""

from copy import deepcopy


def preference_index(preferences, scope_kind):
    return {p["claim_id"]: p for p in preferences if p.get("claim_id") and p.get("explicit") is True
        and p.get("scope_kind") == scope_kind and p.get("text") and p.get("source_refs")}


def valid_upper_claims(row):
    """旧版自由生成的显式偏好缺少原子绑定，读取即屏蔽，不能等下次提取才停止注入。"""
    claims = list(row.claims or [])
    if row.scope_kind not in {"student", "course"}:
        return claims
    return [claim for claim in claims if not claim.get("explicit_preference")
        or (claim.get("preference_id") and claim.get("preference_scope") == row.scope_kind)]


def projected_fields(row):
    claims = valid_upper_claims(row)
    if row.scope_kind not in {"student", "course"} or not row.claims:
        return row.content, list(row.source_refs or []), claims
    content = "\n".join("- " + claim["text"] for claim in claims)
    refs = sorted({ref for claim in claims for ref in [*claim.get("source_refs", []), *claim.get("counter_source_refs", [])]})
    return content, refs, claims


def bound_preference(preference, scope_kind):
    """上层模型只选择偏好，具体文本和支持子句从已校验的叶子候选复制。"""
    return {"text": preference["text"], "facet": "preference", "epistemic_status": "observed",
        "source_refs": list(dict.fromkeys(preference["source_refs"])), "counter_source_refs": [],
        "explicit_preference": True, "preference_id": preference["claim_id"],
        "preference_scope": scope_kind, "preference_quotes": deepcopy(preference.get("source_quotes", [])),
        "next_check": "后续交互核对偏好是否仍适用；本次明确要求优先。"}
