"""从会话事实构建题目上下文，不依赖聊天正文保留题干。"""

from copy import deepcopy

from sqlalchemy import and_, func, or_, select

from app.domains.assessment.models import Attempt
from app.domains.sessions.models import LearningSession, Message
from app.domains.courses.models import Question
from app.domains.courses.catalog import question_contract

PUBLIC_FIELDS = (
    "id",
    "source_id",
    "course_id",
    "stem",
    "stem_format",
    "options",
    "images",
    "question_type",
    "cognitive_type",
    "concept_ids",
    "source_ref",
    "version",
    "generated",
    "blank_count",
    "availability",
    "availability_note",
    "practice_type",
    "difficulty",
    "difficulty_value",
    "difficulty_source",
    "difficulty_target",
)


def public_exercise(contract: dict, course_id: str) -> dict:
    """只投影题干所需字段，排除内部答案、解析和未审查的元数据。"""
    if (
        not isinstance(contract, dict)
        or not str(contract.get("id", "")).startswith(course_id + ":")
        or contract.get("course_id", course_id) != course_id
        or not isinstance(contract.get("stem"), str)
    ):
        raise ValueError("题目快照不存在或不属于当前课程")
    result = {key: deepcopy(contract[key]) for key in PUBLIC_FIELDS if key in contract}
    result["course_id"] = course_id
    return result


def same_exercise(left: dict, right: dict) -> bool:
    """核对冻结内容，不能只凭相同题号就混用另一版本的答案。"""
    fields = (
        "id",
        "version",
        "stem",
        "options",
        "concept_ids",
        "images",
        "blank_count",
    )
    return all(left.get(key) == right.get(key) for key in fields)


class SessionExerciseService:
    """借用调用方短事务，只读取指定学生、课程和会话中的练习。"""

    def __init__(self, db, session_id: str, course_id: str, student_id: str):
        self.db = db
        self.session = db.get(LearningSession, session_id)
        if (
            self.session is None
            or self.session.course_id != course_id
            or self.session.student_id != student_id
        ):
            raise ValueError("无法读取当前范围内的练习会话")
        self.course_id = course_id
        self.pending = self.session.pending_context or {}

    def _messages(self):
        return select(Message).where(
            Message.session_id == self.session.id,
            Message.role == "assistant",
            Message.metadata_json["kind"].as_string() == "question",
        )

    def _question_message(self, question_id: str):
        return self.db.scalar(
            self._messages()
            .where(Message.metadata_json["question_id"].as_string() == question_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )

    def _attempt(self, question_id: str, message: Message | None):
        statement = select(Attempt).where(
            Attempt.session_id == self.session.id, Attempt.question_id == question_id
        )
        if message:
            # 同题重复出现时，答案只能关联本次出题到下一次出题之间的记录。
            statement = statement.where(Attempt.created_at >= message.created_at)
            following = self.db.scalar(
                self._messages()
                .where(
                    Message.metadata_json["question_id"].as_string() == question_id,
                    or_(
                        Message.created_at > message.created_at,
                        and_(
                            Message.created_at == message.created_at,
                            Message.id > message.id,
                        ),
                    ),
                )
                .order_by(Message.created_at, Message.id)
                .limit(1)
            )
            if following:
                statement = statement.where(Attempt.created_at < following.created_at)
        return self.db.scalar(statement.order_by(Attempt.created_at.desc()).limit(1))

    def _view(self, question: dict, message: Message | None) -> dict:
        question = public_exercise(question, self.course_id)
        batch_item = next(
            (
                item
                for item in self.pending.get("exercise_batch", {}).get("items", [])
                if item["contract"]["id"] == question["id"]
                and same_exercise(question, item["contract"])
                and (message is None or message.id == item["exercise_id"])
            ),
            None,
        )
        current_message = (
            self._question_message(question["id"])
            if self.session.pending_question_id == question["id"]
            else None
        )
        is_pending = (
            self.session.pending_question_id == question["id"]
            and same_exercise(question, self.pending.get("contract") or {})
            and (
                message is None
                or current_message is None
                or message.id == current_message.id
            )
        )
        is_pending = is_pending or batch_item is not None
        attempt = self._attempt(question["id"], message)
        result = {
            "exercise_id": message.id if message else None,
            "question": question,
            "status": (
                "awaiting_answer"
                if is_pending
                else "answered" if attempt else "previous"
            ),
            "assisted": (
                bool(
                    batch_item.get("assisted")
                    if batch_item
                    else self.pending.get("assisted")
                )
                if is_pending
                else bool(attempt and attempt.assisted)
            ),
            "student_answer": attempt.answer_text if attempt else None,
            "assessment_summary": None,
        }
        if attempt:
            diagnosis = attempt.assessment or {}
            result["assessment_summary"] = {
                "result": attempt.result,
                "score": attempt.score,
                "level": diagnosis.get("level"),
                "confidence": diagnosis.get("confidence"),
            }
        return result

    def get(self, exercise_id: str | None = None) -> dict | None:
        """返回指定出题记录，缺省优先当前讨论题，再取待答题或最近一道题。"""
        if exercise_id:
            message = self.db.scalar(self._messages().where(Message.id == exercise_id))
            if message is None:
                raise ValueError("练习记录不存在或不属于当前会话")
            return self._view(
                (message.metadata_json or {}).get("question") or {}, message
            )
        discussion = self.pending.get("discussion_exercise_id")
        if discussion:
            return self.get(discussion)
        contract = self.pending.get("contract") or self.pending.get("last_contract")
        batch = self.pending.get("exercise_batch") or self.pending.get(
            "last_exercise_batch"
        )
        if batch:
            return self.get(batch["items"][0]["exercise_id"])
        if contract:
            question = public_exercise(contract, self.course_id)
            message = self._question_message(question["id"])
            if message and not same_exercise(
                question, (message.metadata_json or {}).get("question") or {}
            ):
                message = None
            return self._view(question, message)
        message = self.db.scalar(
            self._messages()
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
        return self.get(message.id) if message else None

    def list(self, *, query: str = "", limit: int = 5, offset: int = 0) -> dict:
        """按出题时间倒序分页返回摘要，不加载整门题库或完整对话。"""
        if not 1 <= limit <= 10 or not 0 <= offset <= 10000:
            raise ValueError("练习分页范围无效")
        statement = self._messages()
        if query:
            statement = statement.where(
                or_(
                    Message.metadata_json["question"]["stem"]
                    .as_string()
                    .contains(query),
                    Message.metadata_json["question_id"].as_string().contains(query),
                )
            )
        total = self.db.scalar(select(func.count()).select_from(statement.subquery()))
        messages = self.db.scalars(
            statement.order_by(Message.created_at.desc(), Message.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        items = []
        for message in messages:
            view = self.get(message.id)
            question = view["question"]
            items.append(
                {
                    "exercise_id": message.id,
                    "question_id": question["id"],
                    "version": question.get("version"),
                    "title": question["stem"][:160],
                    "concept_ids": question.get("concept_ids", []),
                    "status": view["status"],
                    "created_at": message.created_at.isoformat(),
                }
            )
        return {
            "items": items,
            "total": total,
            "offset": offset,
            "returned_count": len(items),
            "has_more": offset + len(items) < total,
        }

    def context(self) -> dict:
        """当前题保留完整公开内容，历史仅提供少量摘要并支持工具继续查询。"""
        active = self.get()
        pending = None
        if self.session.pending_question_id and self.pending.get("contract"):
            question = public_exercise(self.pending["contract"], self.course_id)
            if active is None or not same_exercise(active["question"], question):
                pending = self._view(question, self._question_message(question["id"]))
        batch = self.pending.get("exercise_batch") or self.pending.get(
            "last_exercise_batch"
        )
        batch_context = None
        if batch:
            batch_context = {
                "id": batch["id"],
                "title": batch["title"],
                "status": (
                    "awaiting_answer"
                    if self.pending.get("exercise_batch")
                    else "completed"
                ),
                "items": [self.get(item["exercise_id"]) for item in batch["items"]],
            }
        return {
            "active_exercise": active,
            "pending_exercise": pending,
            "recent_exercises": self.list(limit=3),
            "exercise_batch": batch_context,
        }

    def internal_contract(self, exercise_id: str | None = None) -> dict:
        """仅供题解服务读取答案；旧快照与现行题库不一致时拒绝混用。"""
        view = self.get(exercise_id)
        if view is None:
            raise ValueError("当前没有可讲解的习题")
        question = view["question"]
        if view["exercise_id"]:
            message = self.db.scalar(
                self._messages().where(Message.id == view["exercise_id"])
            )
            archived = (message.metadata_json or {}).get("_exercise_contract")
            if archived:
                public_exercise(archived, self.course_id)
                if not same_exercise(question, archived):
                    raise ValueError("出题记录的公开与内部快照不一致")
                return deepcopy(archived)
        for key in ("contract", "last_contract"):
            contract = self.pending.get(key)
            if contract and same_exercise(question, contract):
                return deepcopy(contract)
        row = self.db.get(Question, question["id"])
        if row is None or row.course_id != self.course_id:
            raise ValueError("历史题目内部答案不可用，可基于公开题干回应或澄清")
        contract = question_contract(row)
        if not same_exercise(question, contract):
            raise ValueError(
                "历史题目版本已变化，不能混用现行答案；请基于历史公开题干回应或澄清"
            )
        return contract
