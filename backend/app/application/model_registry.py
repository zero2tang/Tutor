"""集中发现全部表定义，注册元数据时不连接数据库。"""


def register_models():
    """建表和离线验证前调用；Python 模块缓存使重复注册幂等。"""
    import app.infrastructure.persistence.model_calls  # noqa: F401，共享模型请求审计。
    import app.infrastructure.persistence.execution_models  # noqa: F401，登记模型运行记录表。
    import app.domains.assessment.models  # noqa: F401，登记该功能的表定义。
    import app.domains.courses.models  # noqa: F401，登记该功能的表定义。
    import app.domains.materials.models  # noqa: F401，登记该功能的表定义。
    import app.domains.materials.extraction.models  # noqa: F401，登记教材提取任务与资源表。
    import app.domains.courses.questions.search.models  # noqa: F401，登记含图习题检索版本与回执。
    import app.domains.memory.models  # noqa: F401，登记该功能的表定义。
    import app.domains.memory.scopes.models  # noqa: F401，登记该功能的表定义。
    import app.domains.sessions.models  # noqa: F401，登记该功能的表定义。
    import app.domains.students.models  # noqa: F401，登记该功能的表定义。
    from app.infrastructure.persistence.database import Base

    return Base.metadata
