"""将作答帮助条件的叙述收回服务端；模型只评价具体推理。"""

from copy import deepcopy
import re

# 这是保守的发布约束，覆盖常见中英文归因表达，不把关键词匹配当作语义证明。
_QUOTED = re.compile(r'“[^”\n]*”|「[^」\n]*」|『[^』\n]*』|"[^"\n]*"')
_REPORT = re.compile(
    r"(?:你说|学生说|学生原话|原话是|引用|写道|不能说|不应(?:该)?说|不应(?:该)?写|不要写|不能据此说)\s*[:：]?\s*$"
)
_ENDORSEMENT = re.compile(
    r"^[，,。；;：:\s]*(?:这(?:句话|个说法|一说法|个判断)?|你说的)?(?:是)?"
    r"(?:确实|属实|没错|正确|说明|证明|可以认定|可见)"
)
_SENTENCE = re.compile(r"[^。！？；\n.!?;]+[。！？；.!?;]?")
_FUTURE = re.compile(
    r"^(?:[-*\d.、\s]*)(?:请|试着|接下来(?:请|可以|试着)|下面(?:请|可以)|"
    r"下一步(?:请|可以)|之后(?:请|可以)|下次(?:请|可以)|这次请|现在先|建议你尝试)"
)
_PAST = re.compile(
    r"已经|刚才|上次|刚刚|完成了|解出了|答对了|做对了|走通了|本次属于|这次属于|本次是|这次是"
)
_INDEPENDENT = re.compile(
    r"独立|自主|自行|自己|靠(?:你)?自己|未借助|没借助|不借助|无需(?:任何)?帮助|无须(?:任何)?帮助"
)
_PERFORMANCE = re.compile(
    r"作答|答题|完成|解答|解决|解出|答对|做对|推导|列式|求解|走通|做出|得出|写出|想出|推出|证明|掌握"
)
_SUBJECT = re.compile(
    r"你|学生|本次|这次|本轮|这轮|此次|刚才|上次|刚刚|该生|当前回答|当前作答|答案"
)
_HELP_FACT = re.compile(
    r"(?:没有|没(?:有)?给|未(?:给|获|收|得)|不曾|从未|无需|无须|无)[^。！？；\n]{0,18}(?:提示|帮助|辅助|指导|示范|点拨)"
    r"|(?:只|仅)(?:得|获|收|接受)[^。！？；\n]{0,35}(?:提示|帮助|辅助|指导|示范)"
    r"|(?:在|经过|接受|获得|收到|得到|已有|已给|已获|提供了)[^。！？；\n]{0,35}(?:提示|帮助|辅助|指导|示范)[^。！？；\n]{0,25}(?:下|后|完成|作答|解出|答对|走通|列式)"
    r"|(?:属于|算作|记为|记录为|是|为)(?:一次|本次|这次|有|无|已|受)?(?:辅助|受助|独立)(?:作答|完成|解答|回答)"
    r"|(?:辅助|受助)作答"
    r"|(?:依赖|依靠|借助|用到)[^。！？；\n]{0,20}(?:提示|帮助|辅助|指导|示范)"
    r"|(?:没人|无人)(?:提示|帮助|指导|提醒|点拨)"
)
_MATH_TERMS = re.compile(r"线性独立|独立(?:事件|方程|变量|样本|试验|随机变量)")
_CONDITIONAL = re.compile(r"^(?:[-*\d.、\s]*)(?:若|如果|假如|倘若|只有|即使)")
_ACTUAL_TURN = re.compile(
    r"(?:但|然而|实际上|事实上)[，,：:\s]*(?:你|学生|本次|这次|刚才)|(?:本次|这次|刚才|上次|刚刚)(?:已经|已|是|属于)|你(?:是|已经|确实)"
)
_GENERAL_RULE = re.compile(
    r"不等于|不代表|不能证明|不应|应当|应记|应该|必须|通常|需要|能力|练习|目标|训练|时要|时应"
)
_TARGETED = re.compile(
    r"你|本次|这次|本轮|这轮|此次|刚才|上次|刚刚|该生|当前回答|当前作答"
)
_BARE_INDEPENDENT = re.compile(
    r"^(?:已|已能|成功|未能|尚未|并未)?(?:独立|自主|自行)(?:地)?(?:完成|解答|求解|列式|作答)(?:本题|了|，|,|$)"
)
_ENGLISH_FACT = re.compile(
    r"\b(?:you|student|this (?:answer|response|attempt))\b[^.!?\n]{0,80}"
    r"\b(?:independently|unassisted|unaided|without (?:any )?(?:help|hints?|assistance)|with (?:my |a |the )?(?:help|hints?|assistance))\b",
    re.I,
)


def _reported_quotes(text):
    """只屏蔽明确引述或否定示例；单给判断加引号不能绕过校验。"""
    pieces = []
    end = 0
    for match in _QUOTED.finditer(text):
        prefix = text[max(end, match.start() - 24) : match.start()]
        tail = text[match.end() :]
        pieces.append(text[end : match.start()])
        if _REPORT.search(prefix) and not _ENDORSEMENT.search(tail):
            pieces.append("引述内容")
        else:
            pieces.append(match.group())
        end = match.end()
    pieces.append(text[end:])
    return "".join(pieces)


def _claim_sentences(text):
    masked = _reported_quotes(text)
    for match in _SENTENCE.finditer(masked):
        sentence = match.group().strip()
        # 允许安排接下来的独立尝试；已经发生的表现不能借未来语气放行。
        if (
            _FUTURE.search(sentence)
            and not _PAST.search(sentence)
            and not _ACTUAL_TURN.search(sentence)
        ):
            continue
        if _CONDITIONAL.search(sentence) and not _ACTUAL_TURN.search(sentence):
            continue
        if _GENERAL_RULE.search(sentence) and not _TARGETED.search(sentence):
            continue
        checked = _MATH_TERMS.sub("数学概念", sentence)
        has_independent = bool(_INDEPENDENT.search(checked))
        has_performance = bool(_PERFORMANCE.search(checked))
        is_attribution = bool(
            _SUBJECT.search(checked)
            or _PAST.search(checked)
            or _BARE_INDEPENDENT.search(checked)
        )
        if (
            (has_independent and has_performance and is_attribution)
            or _HELP_FACT.search(sentence)
            or _ENGLISH_FACT.search(sentence)
        ):
            yield sentence


def model_feedback_claims(output):
    """检查公开正文和持久化观察，避免错误身份判断进入下一轮模型上下文。"""
    claims = []

    def walk(value, path):
        if isinstance(value, str):
            for sentence in _claim_sentences(value):
                claims.append({"path": path, "text": sentence})
        elif isinstance(value, dict):
            for key, child in value.items():
                if key != "source_ids":
                    walk(child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(output, "")
    return claims


def validate_model_feedback(output):
    """在发布前拒绝模型自由生成的独立性或帮助身份结论，让模型重写教学内容。"""
    claims = model_feedback_claims(output)
    if claims:
        fields = ", ".join(dict.fromkeys(item["path"] for item in claims))
        raise ValueError(
            "模型不得描述学生已经独立、自主、自己完成，或判定其是否得到提示、属于辅助作答；"
            "历史作答的此类归因也不能凭模型自由重述。请删除相关结论，只评价可见的推理与步骤。"
            "服务端会根据提交前帮助快照生成统一作答记录。相关字段：" + fields
        )


def response_record(request, output, context):
    """沿用证据层的同一条件，描述记录范围，避免把无帮助记录当作掌握证明。"""
    action = request.action if hasattr(request, "action") else request["action"]
    is_attempt = action == "reply" and output.get("student_input_kind") == "attempt"
    assisted = bool(context["assisted"])
    independent = bool(
        is_attempt and context["problem_kind"] == "transfer" and not assisted
    )
    if not is_attempt:
        statement = ""
        condition = "not_attempt"
    elif assisted:
        statement = (
            "作答记录：旧版历史记录无法确认本题提示隔离，本次保守按辅助条件记录。"
            if context.get("assistance_basis") == "legacy_unverified"
            else "作答记录：提交前已有本题帮助记录，本次按辅助条件记录。"
        ) + "答后补讲不改写本次记录；作答正确性与掌握情况另行判断。"
        condition = "assisted"
    elif independent:
        statement = (
            "作答记录：提交前未记录本题的提示或示范，本次按独立作答条件记录。"
            "这只描述系统已记录的帮助条件，不证明没有外部帮助，也不等同于已经掌握。"
        )
        condition = "independent_recorded_conditions"
    else:
        statement = (
            "作答记录：提交前未记录本题的提示或示范。"
            "这只描述系统已记录的帮助条件，不据此认定已经掌握。"
        )
        condition = "no_recorded_help"
    return {
        "method": "submission_snapshot_and_model_input_kind",
        "student_input_kind": output.get("student_input_kind", "other"),
        "is_attempt": is_attempt,
        "assisted_before_response": assisted,
        "independent_response": independent,
        "condition": condition,
        "statement": statement,
        "classification_limit": "student_input_kind_is_model_observation",
    }


def finalize_feedback(output, request, context):
    """只返回通过校验的模型文本及服务端说明，教学帮助分析不读取该说明。"""
    validate_model_feedback(output)
    value = deepcopy(output)
    return value, response_record(request, value, context)
