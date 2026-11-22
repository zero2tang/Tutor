"""将安全切点与必要程度分开：每次模型调用前都是最终检查点。"""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class CompressionPolicy:
    soft: float = 0.70
    preventive: float = 0.90
    keep_recent_turns: int = 4
    enabled: bool = True

    @classmethod
    def from_settings(cls, settings):
        value = cls(
            soft=float(getattr(settings, "context_soft_ratio", 0.70)),
            preventive=float(getattr(settings, "context_preventive_ratio", 0.90)),
            keep_recent_turns=int(getattr(settings, "context_keep_recent_turns", 4)),
            enabled=bool(getattr(settings, "context_compression_enabled", True)),
        )
        if not 0 < value.soft < value.preventive < 1:
            raise ValueError("压缩水位需要满足 0<soft<preventive<1")
        if value.keep_recent_turns < 1:
            raise ValueError("至少保留一个最近业务轮次")
        return value

    def reason(self, tokens, available, growth, *, safe=True, recoverable=0):
        if tokens > available:
            return "hard_limit"
        if tokens + growth >= self.preventive * available:
            return "preventive_growth"
        if (
            safe and tokens >= self.soft * available
            and recoverable >= max(1024, math.ceil(0.15 * tokens))
        ):
            return "soft_boundary"
        return "within_budget"
