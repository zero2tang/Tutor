"""记忆提取与任务调度共用的冲突、延后协议。"""

class MemoryConflict(RuntimeError):
    """候选读取后事实或记忆已变化，必须用最新版重新提取。"""


class MemoryDeferred(RuntimeError):
    """正常教学仍占用会话，稍后处理，不计入模型失败重试。"""
