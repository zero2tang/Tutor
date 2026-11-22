"""只读经历投影；不把偏好提取版本与经历版本混为一谈。"""


def _memory(row):
    return {
        "session_id": row.session_id,
        "content": row.content,
        "source_refs": row.experience_source_refs,
        "version": row.experience_version,
        "covered_seq": row.experience_covered_seq,
        "status": row.experience_status,
        "updated_at": row.updated_at.isoformat(),
        "concept_tags": row.concept_tags,
    }
