"""多模态图片：读盘、落盘引用、从用户输入里抽出路径（#110）。"""

from __future__ import annotations

import base64
import os
import re
import uuid

from .messages import image_block

# 单张上限：避免把几十 MB 图塞进会话 JSON / API
MAX_IMAGE_BYTES = 4 * 1024 * 1024
ALLOWED_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
# 用户输入里的 @/path/to.png 或 @path/to.png
_AT_PATH = re.compile(r"@((?:/[^\s@]+)|(?:\./[^\s@]+)|(?:[A-Za-z]:\\[^\s@]+)|(?:[^\s@]+\.(?:png|jpe?g|gif|webp)))",
                      re.IGNORECASE)


class ImageError(ValueError):
    pass


def media_type_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext not in ALLOWED_TYPES:
        raise ImageError(f"不支持的图片类型 {ext or '(无扩展名)'}（支持：{', '.join(sorted(ALLOWED_TYPES))}）")
    return ALLOWED_TYPES[ext]


def load_image_block(path: str, *, cwd: str = "") -> dict:
    """读本地图片 → image 内容块。路径相对 cwd。"""
    raw = path.strip().strip("\"'")
    if cwd and not os.path.isabs(raw):
        raw = os.path.join(cwd, raw)
    raw = os.path.realpath(raw)
    if not os.path.isfile(raw):
        raise ImageError(f"图片不存在：{path}")
    size = os.path.getsize(raw)
    if size > MAX_IMAGE_BYTES:
        raise ImageError(f"图片太大（{size} 字节，上限 {MAX_IMAGE_BYTES}）：{path}")
    if size == 0:
        raise ImageError(f"图片是空文件：{path}")
    media = media_type_for(raw)
    with open(raw, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return image_block(media, data, path=raw)


def extract_image_refs(text: str) -> tuple[str, list[str]]:
    """从用户输入抽出 @path 图片引用，返回 (去掉引用后的文本, 路径列表)。"""
    paths: list[str] = []

    def repl(m: re.Match) -> str:
        paths.append(m.group(1))
        return " "

    cleaned = _AT_PATH.sub(repl, text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return cleaned, paths


def save_attachment(data: bytes, media_type: str, attachments_dir: str) -> str:
    """把上传的图片落到 .nanocode/attachments/，返回路径。"""
    os.makedirs(attachments_dir, exist_ok=True)
    ext = {v: k for k, v in ALLOWED_TYPES.items()}.get(media_type, ".bin")
    # png 有两个 key 时取 .png
    if media_type == "image/jpeg":
        ext = ".jpg"
    elif media_type == "image/png":
        ext = ".png"
    name = f"{uuid.uuid4().hex[:12]}{ext}"
    path = os.path.join(attachments_dir, name)
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError(f"图片太大（{len(data)} 字节，上限 {MAX_IMAGE_BYTES}）")
    with open(path, "wb") as f:
        f.write(data)
    return path


__all__ = ["ImageError", "load_image_block", "extract_image_refs", "save_attachment",
           "media_type_for", "MAX_IMAGE_BYTES", "ALLOWED_TYPES"]
