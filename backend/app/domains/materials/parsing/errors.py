"""各文档格式共享的失败契约。"""


class UnsupportedDocument(ValueError):
    """缺少适配器或文档不能可靠读取时的明确失败。"""
