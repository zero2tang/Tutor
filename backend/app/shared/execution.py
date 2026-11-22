"""任务归属错误必须停止旧执行，不能当作可由模型修正的业务输入。"""


class LearningTurnLost(RuntimeError):
    pass


class LearningConflict(ValueError):
    """输入与已经接受的请求、等待点或业务版本冲突。"""
