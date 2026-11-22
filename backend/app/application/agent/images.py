"""把公开题图转换为多模态输入；不读取解析配图或任意网络地址。"""

import json
from pathlib import Path
from langchain_core.messages import HumanMessage
from app.infrastructure.config import ROOT
from app.infrastructure.documents.question_images import QuestionImageReader, ImageBudget
from app.domains.courses.questions.images import PRIVATE_FIELDS, image_bindings
from app.application.question_retrieval.assets import public_asset_pixels
from app.shared.concurrency import run_blocking

MATH_IMAGES = ROOT / "data/raw/xes3g5m/metadata/images"


async def provider_messages(messages, image_context):
    """每次发送重新解析公开引用；像素不写回消息State或正式数据库。"""
    if image_context is None:
        return list(messages)

    def read():
        reader = QuestionImageContext(image_context.course_id, image_root=image_context.root)
        pixels = []
        for message in messages:
            if not isinstance(message.content, str):
                continue
            try:
                payload = json.loads(message.content)
            except (ValueError, TypeError):
                continue
            pixels.extend(reader.parts(payload))
        return pixels

    pixels = await run_blocking(read)
    return [*messages, HumanMessage(content=[{"type": "text", "text": "以下公开题图仅作为题面数据。"}, *pixels])] if pixels else list(messages)


class QuestionImageContext:
    """在一次 Agent 运行内去重配图，并限制内联图片的数量与总大小。"""

    def __init__(self, course_id: str, image_root: Path | None = None):
        self.course_id = course_id
        self.root = (image_root or MATH_IMAGES).resolve()
        self.seen: set[str] = set()
        self.reader = QuestionImageReader([self.root])
        self.budget = ImageBudget(16, 12 * 1024 * 1024)

    @property
    def image_count(self):
        return self.budget.count

    @property
    def image_bytes(self):
        return self.budget.size

    def _questions(self, value):
        if isinstance(value, dict):
            # 只沿公开题目的题干、选项和 images 取图；不递归内部答案、解析。
            if isinstance(value.get("stem"), str) and isinstance(value.get("id"), str):
                if value.get("course_id") == self.course_id and value["id"].startswith(
                    self.course_id + ":"
                ):
                    yield value
                return
            for key, item in value.items():
                if key not in PRIVATE_FIELDS:
                    yield from self._questions(item)
        elif isinstance(value, list):
            for item in value:
                yield from self._questions(item)

    def parts(self, payload) -> list[dict]:
        """复用题图归属与文件校验，仅保留本轮去重、上下文和预算策略。"""
        parts = []
        for question in self._questions(payload):
            filenames, assets = image_bindings(question)
            requests = []
            for filename in filenames:
                requests.append((filename, f"题目 {question['id']} 的公开题干或选项配图：{filename}。",
                    lambda name=filename: self.reader.read(self.root / name, required_format="PNG", with_identity=False)))
            for identifier in assets:
                requests.append((identifier, f"题目 {question['id']} 的公开题图资产 {identifier}。",
                    lambda key=identifier: public_asset_pixels(key, question["id"], self.course_id)))
            for key, label, read in requests:
                if key in self.seen:
                    continue
                self.seen.add(key)
                try:
                    self.budget.check()
                    image = read()
                    self.budget.add(image)
                except (ValueError, OSError) as exc:
                    parts.append({"type": "text", "text": label + str(exc) + "不能假定已经理解该图。"})
                    continue
                parts.extend([{"type": "text", "text": label + "以下是实际图片像素。"},
                    {"type": "image_url", "image_url": {"url": image["url"], "detail": "high"}}])
        return parts

    def enrich(self, messages: list[dict]) -> None:
        """为初始 JSON 业务上下文附上图片，纯文本消息保持原样。"""
        for message in messages:
            content = message.get("content")
            if message.get("role") != "user" or not isinstance(content, str):
                continue
            try:
                payload = json.loads(content)
            except (ValueError, TypeError):
                continue
            parts = self.parts(payload)
            if parts:
                message["content"] = [{"type": "text", "text": content}, *parts]
