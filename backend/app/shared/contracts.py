"""业务输入输出协议；校验器和字段约束与迁移前一致。"""

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    # 模型多输出的字段也视为契约错误，交由运行循环反馈修正，避免悄悄忽略意外决策。
    model_config = ConfigDict(extra="forbid")
