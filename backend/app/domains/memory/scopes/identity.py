"""固定三级记忆作用域的稳定身份，独立于存储和撤回逻辑。"""

import hashlib
import json


def scope_key(
    student_id: str, scope_kind: str, course_id: str = "", concept_id: str = ""
) -> str:
    if scope_kind == "global":
        scope_kind = "student"
    if scope_kind not in {"student", "course", "concept"}:
        raise ValueError("未知业务记忆作用域")
    if scope_kind == "student":
        course_id, concept_id = "", ""
    elif scope_kind == "course":
        if not course_id:
            raise ValueError("课程作用域缺少课程")
        concept_id = ""
    elif not course_id or not concept_id:
        raise ValueError("知识点作用域缺少课程或知识点")
    raw = json.dumps(
        [student_id, scope_kind, course_id, concept_id], ensure_ascii=False
    )
    return scope_kind + ":" + hashlib.sha256(raw.encode()).hexdigest()
