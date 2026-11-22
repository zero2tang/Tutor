"""HTTP 入口共享的应用服务依赖。"""

from fastapi import Request


def services(request: Request):
    return request.app.state.services
