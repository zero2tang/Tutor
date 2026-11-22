"""统一的请求输入预算与保守估算，不依据模型名称猜测上下文窗口。"""

from dataclasses import dataclass
import json
import math


class ContextBudgetExceeded(ValueError):
    """完整当前任务无法安全发送时，交由业务层返回可理解的缺口。"""


class ProviderContextLimitError(ContextBudgetExceeded):
    """服务商计数与本地估算不一致时触发一次重建，禁止原载荷盲重试。"""


@dataclass(frozen=True)
class RequestBudget:
    window: int
    request_limit: int
    input_limit: int
    output_reserve: int
    safety: int
    image_tokens: int

    @classmethod
    def from_settings(cls, settings, output_tokens: int):
        def setting(name, default):
            return getattr(settings, name, default)

        budget = cls(
            window=int(setting("context_window_tokens", 32768)),
            request_limit=int(setting("context_request_tokens", 32768)),
            input_limit=int(setting("context_input_limit_tokens", 0)),
            output_reserve=output_tokens,
            safety=int(setting("context_safety_tokens", 2048)),
            image_tokens=int(setting("context_image_tokens", 4096)),
        )
        if (
            budget.window <= 0
            or budget.request_limit <= 0
            or budget.request_limit > budget.window
            or isinstance(output_tokens, bool)
            or not isinstance(output_tokens, int)
            or output_tokens <= 0
            or budget.safety < 0
            or budget.image_tokens <= 0
            or budget.input_limit < 0
            or budget.available <= 0
        ):
            raise ContextBudgetExceeded("上下文预算配置无效：需要 C≤W 且 C-O-S>0。")
        return budget

    @property
    def available(self):
        remaining = self.request_limit - self.output_reserve - self.safety
        return min(self.input_limit, remaining) if self.input_limit else remaining


@dataclass(frozen=True)
class TokenEstimate:
    tokens: int
    text_tokens: int
    image_tokens: int
    image_count: int
    # 中英文按不同权重估算并另留协议安全量，不冒充部署模型的精确分词。
    method: str = "weighted_unicode_v1+configured_vision_allowance"

    def as_dict(self):
        return {
            "estimated_input_tokens": self.tokens,
            "estimated_text_tokens": self.text_tokens,
            "estimated_image_tokens": self.image_tokens,
            "image_count": self.image_count,
            "estimator": self.method,
            "estimator_notice": "ASCII按每2字符1Token，非ASCII按每字符2Tokens保守估算；视觉采用配置预留。误差无保证，需按实际usage校准。",
        }


def estimate_request(messages, tools=None, *, json_output=False, image_tokens=4096):
    """计入系统消息、工具 Schema、格式契约和题图；不把 Base64 当视觉 Tokens。"""
    images = 0

    def without_pixels(value):
        nonlocal images
        if isinstance(value, dict):
            if value.get("type") in {"image_url", "input_image"}:
                images += 1
                # URL长度和像素token没有可靠线性关系；计入每张图独立配置的预留。
                return {"type": value["type"], "image": "[按视觉预留计数]"}
            return {key: without_pixels(item) for key, item in value.items()}
        if isinstance(value, list):
            return [without_pixels(item) for item in value]
        return value

    body = {"messages": without_pixels(messages)}
    if tools:
        body.update(tools=without_pixels(tools), tool_choice="auto")
    if json_output and not tools:
        body["response_format"] = {"type": "json_object"}
    # 比常见中英文分词偏大的冷启动代理；代码、罕见字符与视觉误差仍依赖安全量校准。
    serialized = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    text = math.ceil(sum(0.5 if ord(char) < 128 else 2 for char in serialized))
    text += 32 + 12 * len(messages)
    vision = images * image_tokens
    return TokenEstimate(text + vision, text, vision, images)


def assert_request_budget(settings, messages, tools=None, *, max_tokens=3000, json_output=False):
    """最终共享出口再次守卫；超限请求不发给服务商，也不原样重试。"""
    budget = RequestBudget.from_settings(settings, max_tokens)
    estimate = estimate_request(
        messages, tools, json_output=json_output, image_tokens=budget.image_tokens
    )
    if estimate.tokens > budget.available:
        raise ContextBudgetExceeded(
            f"当前完整任务需要约 {estimate.tokens} 输入 Tokens，超过预算 {budget.available}；"
            "请分段处理或缩小材料范围，当前题目和答案未被截断。"
        )
    return budget, estimate
