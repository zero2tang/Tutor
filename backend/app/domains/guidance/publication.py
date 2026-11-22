"""讲题正文先校验再发布；实时进度不携带未经核对的学生表现断言。"""

from uuid import uuid4

from app.domains.guidance.tools import GuidanceTools


class PublicationEvents:
    def __init__(self, emit):
        self.emit = emit
        self.suppressed_deltas = 0

    async def model_event(self, name, data):
        """上游仍可流式接收，但原始正文不可绕过发布边界直接到浏览器。"""
        if name == "text_delta":
            self.suppressed_deltas += 1
            return
        if name == "text_reset":
            return
        if name == "tool_completed":
            # 检索词和其他自由参数也可能直接透露建模方法，只发送中性的运行进度。
            data = {
                key: value
                for key, value in data.items()
                if key
                in {
                    "agent",
                    "name",
                    "status",
                    "duration_ms",
                    "item_count",
                    "tool_call_id",
                    "call_index",
                    "cached",
                    "error",
                }
            }
            if data.get("name") not in GuidanceTools.ALLOWED:
                data["name"] = "teaching_resource_tool"
        await self.emit(name, data)

    async def publish(self, content, run_id):
        """仅分段发送已提交的正文，明确这不是上游模型即时生成的分片。"""
        stream_id = str(uuid4())
        for offset in range(0, len(content), 120):
            await self.emit(
                "text_delta",
                {
                    "stream_id": stream_id,
                    "agent": "exercise",
                    "field": "content",
                    "delta": content[offset : offset + 120],
                    "run_id": run_id,
                    "delivery_mode": "validated_publication",
                },
            )
