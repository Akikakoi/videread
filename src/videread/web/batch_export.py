"""批量导出：把多份报告按 Markdown / PDF / PNG 打包成一个 ZIP。

单份导出的规则在这里原样复用：Markdown 只保留文字层次，带视频截图的
报告无法把图片带出 run 目录，整批拒绝；PDF / PNG 走本机浏览器，run
目录里已有较新的文件时直接打包，不重复调用。
"""

from __future__ import annotations

import re
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote

from .. import frames as frames_mod
from .. import markdown
from .. import pdf as pdf_mod
from .. import png as png_mod
from ..config import Settings
from ..errors import PdfError, PngError, UsageError, VidereadError
from . import library

#: 批量导出支持的格式
EXPORT_FORMATS = ("md", "pdf", "png")
#: 单次导出的期数上限：再多更适合分批，避免一次请求长时间占用浏览器
MAX_BATCH = 200
#: Windows / macOS / Linux 都不适合出现在文件名里的字符
_ILLEGAL_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_MAX_NAME_CHARS = 80


def safe_filename(title: str, run_id: str, ext: str, used: set[str]) -> str:
    """标题 → ZIP 内文件名：清掉非法字符、限长，重名时补 run_id 区分。"""
    base = _ILLEGAL_NAME.sub(" ", title or "").strip(" .")
    base = re.sub(r"\s+", " ", base).strip(" .")[:_MAX_NAME_CHARS].rstrip(" .")
    if not base:
        base = run_id
    name = f"{base}.{ext}"
    if name.lower() in used:
        name = f"{base} ({run_id[:8]}).{ext}"
    serial = 2
    while name.lower() in used:
        name = f"{base} ({run_id[:8]}-{serial}).{ext}"
        serial += 1
    used.add(name.lower())
    return name


def _export_png(
    run_id: str,
    run_dir: Path,
    html_path: Path,
    settings: Settings | None,
    self_base: str | None,
) -> Path:
    """与单份 PNG 导出同口径：有 self_base 截编辑风阅读页，否则截 report.html。"""
    if self_base:
        return png_mod.export_reader_png(
            f"{self_base}/read/{quote(run_id)}?png=1",
            run_dir / "report.png",
            settings,
            cache_key=html_path,
        )
    return png_mod.export_png(html_path, run_dir / "report.png", settings)


def build_archive(
    out_root: Path,
    run_ids: list[str],
    fmt: str,
    settings: Settings | None = None,
    self_base: str | None = None,
) -> tuple[Path, list[str]]:
    """把 run_ids 按 fmt 打包为临时 ZIP，返回 `(zip 路径, 已导出标题)`。

    临时文件由调用方删除。任意一期不可导出或导出失败都中止整批（已写的
    临时文件一并清掉），错误信息带上报告标题，前端可直接展示。
    """
    if fmt not in EXPORT_FORMATS:
        raise UsageError(f"不支持的批量导出格式：{fmt}")

    used: set[str] = set()
    exported: list[str] = []
    tmp = tempfile.NamedTemporaryFile(
        prefix="videread-export-", suffix=".zip", delete=False
    )
    tmp.close()
    archive = Path(tmp.name)
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for run_id in run_ids:
                run_dir = library.safe_run_dir(out_root, run_id)
                if run_dir is None:
                    raise UsageError(f"未找到该 run：{run_id}")
                title = library.run_title(out_root, run_id) or run_id
                html_path = run_dir / "report.html"
                if not html_path.is_file():
                    raise UsageError(f"《{title}》还没有 report.html")

                if fmt == "md":
                    if frames_mod.has_frames(run_dir):
                        raise UsageError(
                            f"《{title}》带视频截图，Markdown 导出不含图片；请改用 PDF / PNG"
                        )
                    content = markdown.html_to_markdown(
                        html_path.read_text(encoding="utf-8")
                    )
                    zf.writestr(safe_filename(title, run_id, "md", used), content)
                elif fmt == "pdf":
                    try:
                        pdf_path = pdf_mod.export_pdf(
                            html_path, run_dir / "report.pdf", settings
                        )
                    except VidereadError as exc:
                        raise PdfError(f"《{title}》导出 PDF 失败：{exc}") from exc
                    zf.write(pdf_path, arcname=safe_filename(title, run_id, "pdf", used))
                else:
                    try:
                        png_path = _export_png(
                            run_id, run_dir, html_path, settings, self_base
                        )
                    except VidereadError as exc:
                        raise PngError(f"《{title}》导出 PNG 失败：{exc}") from exc
                    zf.write(png_path, arcname=safe_filename(title, run_id, "png", used))
                exported.append(title)
    except BaseException:
        archive.unlink(missing_ok=True)
        raise
    return archive, exported
