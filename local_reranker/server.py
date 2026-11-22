"""本地 GPU 精排服务；只接受调用方传来的题面文本和内嵌图片。"""

import argparse
import base64
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from threading import BoundedSemaphore
import time

from PIL import Image

from .scorer import INSTRUCTIONS, QwenScorer, TEXT_MODEL, VISION_MODEL


MAX_REQUEST_BYTES = 24 * 1024 * 1024
MEDIA = {"data:image/png;base64": "PNG", "data:image/jpeg;base64": "JPEG", "data:image/webp;base64": "WEBP"}


def decode_row(row):
    text, images = row["text"], row["images"]
    if not isinstance(text, str) or len(text) > 30000 or not isinstance(images, list) or len(images) > 10:
        raise ValueError("题面或图片数量不合法")
    decoded = []
    for uri in images:
        header, data = uri.split(",", 1)
        if header not in MEDIA or len(data) > 4 * ((2 * 1024 * 1024 + 2) // 3):
            raise ValueError("图片必须是受支持的内嵌图片")
        with Image.open(BytesIO(base64.b64decode(data, validate=True))) as image:
            if image.format != MEDIA[header] or image.width * image.height > 25_000_000:
                raise ValueError("图片内容或大小不合法")
            rgba = image.convert("RGBA")
            canvas = Image.new("RGB", rgba.size, "white")
            canvas.paste(rgba, mask=rgba.getchannel("A"))
            decoded.append(canvas)
    return {"text": text, "images": decoded}


def score_request(payload, models):
    intent, candidates = payload["intent"], payload["candidates"]
    if intent not in INSTRUCTIONS or not isinstance(candidates, list) or not 1 <= len(candidates) <= 30:
        raise ValueError("检索意图或候选数量不合法")
    ids = [item["id"] for item in candidates]
    if any(not isinstance(qid, str) or not 1 <= len(qid) <= 96 for qid in ids) or len(set(ids)) != len(ids):
        raise ValueError("候选编号必须唯一")
    query = decode_row(payload["query"])
    docs = [decode_row(item) for item in candidates]
    visual = bool(query["images"] or any(row["images"] for row in docs))
    model = VISION_MODEL if visual else TEXT_MODEL
    start = time.perf_counter()
    scores, tokens, images = models[model].score(query, docs, INSTRUCTIONS[intent], 2 if visual else 8)
    return {"model": model, "scores": [{"id": qid, "score": score} for qid, score in zip(ids, scores)],
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 2), "max_tokens": tokens, "images_scored": images}


class Handler(BaseHTTPRequestHandler):
    timeout = 5

    def reply(self, status, value):
        data = json.dumps(value, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path != "/health":
            return self.reply(404, {"error": "not_found"})
        self.reply(200, {"service": "tutor-qwen-reranker", "status": "ready", "models": list(self.server.models)})

    def do_POST(self):
        if self.path != "/rerank":
            return self.reply(404, {"error": "not_found"})
        if not self.server.slot.acquire(blocking=False):
            return self.reply(503, {"error": "busy"})
        try:
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= MAX_REQUEST_BYTES:
                    return self.reply(413, {"error": "request_size"})
                payload = json.loads(self.rfile.read(length))
                result = score_request(payload, self.server.models)
            except (ValueError, TypeError, KeyError, AttributeError, OSError, Image.DecompressionBombError):
                return self.reply(422, {"error": "invalid_input"})
            except Exception as exc:
                logging.error("精排失败：%s", type(exc).__name__)
                return self.reply(500, {"error": "inference_failed"})
            self.reply(200, result)
        finally:
            self.server.slot.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8019)
    port = parser.parse_args().port
    cache = Path(__file__).resolve().parents[1] / "data/cache/reranker_models"
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        server.models = {name: QwenScorer(cache / name, visual) for name, visual in
                         ((TEXT_MODEL, False), (VISION_MODEL, True))}
        server.slot = BoundedSemaphore(1)
        print(f"Qwen 精排已就绪：127.0.0.1:{port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
