"""教材导入只接收内容，不接受服务端路径或任意远程下载指令。"""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class TextbookImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=8, max_length=80)
    course_id: str = Field(default="junior_algebra", max_length=60)
    title: str = Field(min_length=1, max_length=250)
    edition: str = Field(default="", max_length=120)
    source_uri: str = Field(default="", max_length=2000)
    source_kind: Literal[
        "open_textbook", "curated_excerpt", "self_authored", "uploaded"
    ] = "uploaded"
    format: Literal["html", "markdown", "pdf"]
    content: str | None = Field(default=None, max_length=2000000)
    content_base64: str | None = Field(default=None, max_length=12000000)
    license: str = Field(default="unspecified", max_length=300)
    attribution: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def content_matches_format(self):
        if (self.content is None) == (self.content_base64 is None):
            raise ValueError("教材内容和二进制编码必须且只能提供一种。")
        if self.format == "pdf" and self.content_base64 is None:
            raise ValueError("PDF 必须提供 content_base64。")
        if self.format != "pdf" and self.content is None:
            raise ValueError("HTML 和 Markdown 必须提供文本 content。")
        return self
