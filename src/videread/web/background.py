"""控制台背景自定义：上传一张图片替换默认背景，可随时恢复默认。

图片存在项目根 `.ui/` 下（与 .env 同级的本地配置目录，不入库）；
只存一份，重复上传覆盖。校验走**魔数**而非 Content-Type——本机工具
不做鉴权，宁可多挡一道也不把任意二进制当图片存下来。

遮罩（overlay）独立于背景图保存（`.ui/overlay.txt`）：深色壁纸配深纱、
浅色壁纸配白纱或无纱，换图不丢遮罩选择。
"""

from __future__ import annotations

from pathlib import Path

from ..config import PROJECT_ROOT
from ..errors import UsageError

#: 背景图存放目录（模块级变量，测试可 monkeypatch 隔离）
BG_DIR = PROJECT_ROOT / ".ui"

#: 上限 15MB：足够 4K 壁纸，防止误传视频把内存/磁盘吃爆
MAX_BG_BYTES = 15 * 1024 * 1024

#: 可选遮罩风格：default=浅白渐变（默认）/ strong=加深白纱 / dark=深色纱 / none=无遮罩
OVERLAYS = ("default", "strong", "dark", "none")
_OVERLAY_FILE = "overlay.txt"

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpg"),            # JPEG
    (b"\x89PNG\r\n\x1a\n", "png"),       # PNG
)
_MIME = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def mime_for(ext: str) -> str:
    """扩展名 → MIME 类型；未知扩展按 JPEG 兜底。"""
    return _MIME.get(ext.lower(), "image/jpeg")


def _detect(data: bytes) -> str | None:
    """按魔数识别图片格式；WebP 的 RIFF 头需同时校验 'WEBP' 四字符。"""
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def background_path() -> Path | None:
    """当前自定义背景图的路径；未设置返回 None。"""
    if not BG_DIR.is_dir():
        return None
    for path in sorted(BG_DIR.glob("background.*")):
        if path.suffix.lstrip(".").lower() in _MIME:
            return path
    return None


def background_url() -> str | None:
    """背景图的服务地址（带 mtime 作缓存戳）；未设置返回 None（用默认背景）。"""
    path = background_path()
    if path is None:
        return None
    try:
        version = int(path.stat().st_mtime)
    except OSError:
        version = 0
    return f"/api/ui/background/image?v={version}"


def save_background(data: bytes) -> str:
    """校验并保存背景图，返回新的服务地址。"""
    if not data:
        raise UsageError("背景图内容为空")
    if len(data) > MAX_BG_BYTES:
        raise UsageError(f"背景图不能超过 {MAX_BG_BYTES // (1024 * 1024)}MB")
    ext = _detect(data)
    if ext is None:
        raise UsageError("只支持 JPG / PNG / WebP 图片")
    BG_DIR.mkdir(parents=True, exist_ok=True)
    # 只保留一份：清掉旧格式，避免「换了张 png 但还在读旧 jpg」
    for old in BG_DIR.glob("background.*"):
        old.unlink(missing_ok=True)
    path = BG_DIR / f"background.{ext}"
    path.write_bytes(data)
    return background_url() or "/api/ui/background/image"


def reset_background() -> None:
    """删除自定义背景，恢复默认（CSS 里的 bg.jpg）；遮罩选择独立保存、不受影响。"""
    if not BG_DIR.is_dir():
        return
    for old in BG_DIR.glob("background.*"):
        old.unlink(missing_ok=True)


# ------------------------------------------------------------------- 遮罩


def overlay_style() -> str:
    """当前遮罩风格；文件缺失 / 内容非法一律回落 default。"""
    path = BG_DIR / _OVERLAY_FILE
    try:
        name = path.read_text(encoding="utf-8").strip()
    except OSError:
        return "default"
    return name if name in OVERLAYS else "default"


def set_overlay(name: str) -> str:
    """保存遮罩风格；名字不在可选列表里报参数错误。"""
    name = (name or "").strip().lower()
    if name not in OVERLAYS:
        raise UsageError(
            f"未知遮罩风格：{name!r}（可选 {' / '.join(OVERLAYS)}）"
        )
    BG_DIR.mkdir(parents=True, exist_ok=True)
    (BG_DIR / _OVERLAY_FILE).write_text(name + "\n", encoding="utf-8", newline="\n")
    return name
