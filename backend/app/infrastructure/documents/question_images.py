"""受控本地图像读取；链接文字不能代替图像像素。"""

import base64
import hashlib
from io import BytesIO
from pathlib import Path
from dataclasses import dataclass
from PIL import Image
from app.infrastructure.config import ROOT


class ImageReadError(ValueError):
    """调用方可以区分缺图、格式错误、变更和预算限制。"""


@dataclass
class ImageBudget:
    max_images: int
    max_bytes: int
    count: int = 0
    size: int = 0

    def check(self):
        if self.count >= self.max_images:
            raise ImageReadError("图片超出本轮读取数量限制，不能假定已经读图。")

    def add(self, image):
        self.check()
        if self.size + image["bytes"] > self.max_bytes:
            raise ImageReadError("图片超出本轮总大小限制，不能假定已经读图。")
        self.count += 1
        self.size += image["bytes"]


class QuestionImageReader:
    def __init__(self, roots=None, *, max_bytes=2 * 1024 * 1024):
        self.roots = [p.resolve() for p in (roots or [ROOT / "data/raw/xes3g5m/metadata/images", ROOT / "data/raw/textbook_extraction"])]
        self.max_bytes = max_bytes

    def read(self, location, expected_hash=None, *, required_format=None, with_identity=True):
        path = Path(location).resolve()
        if not any(path.is_relative_to(root) for root in self.roots):
            raise ImageReadError("图片不属于受控资产目录。")
        if not path.is_file():
            raise ImageReadError("本地图片缺失，不能根据链接猜测图意。")
        if path.stat().st_size > self.max_bytes:
            raise ImageReadError("图片超出本轮单图大小限制，不能假定已经读图。")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ImageReadError("本地图片暂时无法读取，不能根据链接猜测图意。") from exc
        if len(content) > self.max_bytes:
            raise ImageReadError("图片超出本轮单图大小限制，不能假定已经读图。")
        value = hashlib.sha256(content).hexdigest()
        if expected_hash and value != expected_hash:
            raise ImageReadError("资产内容已变化，必须重新提取和构建。")
        identity = None
        try:
            with Image.open(BytesIO(content)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"} or (required_format and image.format != required_format):
                    raise ImageReadError("图片内容不是受支持的格式，不能假定已经读图。")
                if image.width * image.height > 25_000_000:
                    raise ImageReadError("图片像素数量超过限制。")
                mime = Image.MIME[image.format]
                image.verify()
            if with_identity:
                with Image.open(BytesIO(content)) as image:
                    # 最近邻归一化让无损整倍缩放共享身份；来源组仍是近重复审查的依据。
                    rgba = image.convert("RGBA")
                    canvas = Image.new("RGBA", rgba.size, "white")
                    canvas.alpha_composite(rgba)
                    pixels = canvas.convert("RGB").resize((1024, 1024), Image.Resampling.NEAREST).tobytes()
                    identity = hashlib.sha256(str(round(image.width / image.height, 5)).encode() + pixels).hexdigest()
        except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
            raise ImageReadError("图片内容不是受支持的格式，不能假定已经读图。") from exc
        return {"hash": value, "identity": identity, "bytes": len(content), "url": f"data:{mime};base64," + base64.b64encode(content).decode("ascii")}
