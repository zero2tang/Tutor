"""只提供已下载的公开教学配图，不开放任意文件路径。"""

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.infrastructure.config import ROOT

router = APIRouter(prefix="/api/assets")
MATH_IMAGES = ROOT / "data/raw/xes3g5m/metadata/images"


@router.get("/math/{filename}")
def math_image(filename: str):
    if not re.fullmatch(r"(?:question|analysis)_\d+-image_\d+\.png", filename):
        raise HTTPException(status_code=404, detail="配图不存在")
    root = MATH_IMAGES.resolve()
    path = (root / filename).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=404, detail="配图不存在")
    return FileResponse(
        path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"}
    )
