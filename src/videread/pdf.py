"""PDF 导出：用本机 Edge / Chrome 的无头打印能力把 report.html 转成 PDF。

不引入额外 Python 依赖：Windows 10/11 自带 Edge，Chromium 内核对本报告的
CSS（含 base64 内嵌图片）保真度最高。查找顺序：`PDF_BROWSER_BIN` 环境变量
→ 常见安装路径 → PATH 上的 msedge / chrome。产物 `report.pdf` 落在 run
目录，按文件新旧做缓存，重复导出不重复调用浏览器。
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from . import execution
from .config import PDF_TIMEOUT_SEC, Settings
from .errors import PdfError

# 常见安装位置（Windows 优先，兼顾 macOS / Linux 的默认路径）
_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/microsoft-edge",
    "/usr/bin/google-chrome",
)


def find_browser(settings: Settings | None = None) -> str | None:
    """定位可用的 Edge / Chrome 可执行文件；找不到返回 None。"""
    explicit = (settings.pdf_browser if settings else None) or ""
    explicit = explicit.strip()
    if explicit:
        return explicit if Path(explicit).is_file() else None

    for candidate in _CANDIDATES:
        if Path(candidate).is_file():
            return candidate

    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def expand_details(html: str) -> str:
    """给所有 `<details>` 补 `open` 属性（幂等）。

    PDF / 长图走无头浏览器静态渲染，没有点击交互，折叠块
    （视频简介 / 来源与处理说明）不展开内容就丢了。
    """
    return re.sub(r"<details(?![^>]*\bopen\b)", "<details open", html, flags=re.IGNORECASE)


def export_pdf(html_path: Path, out_pdf: Path, settings: Settings | None = None) -> Path:
    """把 `html_path` 打印为 `out_pdf`；产物已比 HTML 新时直接复用（缓存）。"""
    # Edge / Chrome 的无头打印对相对路径不可靠（可能解析到浏览器自身的工作
    # 目录），统一先转绝对路径再进命令行
    html_path = html_path.resolve()
    out_pdf = out_pdf.resolve()
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    if out_pdf.is_file() and out_pdf.stat().st_mtime >= html_path.stat().st_mtime:
        return out_pdf

    browser = find_browser(settings)
    if browser is None:
        raise PdfError(
            "未找到 Edge / Chrome，无法导出 PDF。"
            "请安装 Microsoft Edge，或用 PDF_BROWSER_BIN 指定浏览器路径"
        )

    # 独立的临时用户数据目录：避免与正在运行的浏览器实例冲突，用完即删
    profile = tempfile.mkdtemp(prefix="videread-pdf-")
    temp_dir = Path(tempfile.mkdtemp(prefix="videread-pdf-print-"))
    try:
        # 打印的是展开折叠块后的副本：report.html 本体保持默认收起的交互
        print_html = temp_dir / html_path.name
        print_html.write_text(
            expand_details(html_path.read_text(encoding="utf-8")), encoding="utf-8"
        )
        cmd = [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-pdf-header-footer",
            f"--user-data-dir={profile}",
            f"--print-to-pdf={out_pdf}",
            print_html.resolve().as_uri(),
        ]
        result = execution.run_command(
            cmd,
            timeout=PDF_TIMEOUT_SEC,
            error_cls=PdfError,
            missing_message=lambda program: f"浏览器启动失败（{program} 已不存在）",
            timeout_message=lambda seconds, _program: f"PDF 导出超时（{seconds:.0f}s）",
        )
        if result.returncode not in (0, None) and not _valid_pdf(out_pdf):
            raise PdfError(
                f"PDF 导出失败（退出码 {result.returncode}）："
                f"{result.stderr.strip()[-500:]}"
            )
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(temp_dir, ignore_errors=True)

    if not _valid_pdf(out_pdf):
        raise PdfError(f"PDF 导出失败：浏览器未产出有效文件 {out_pdf}")
    return out_pdf


def _valid_pdf(path: Path) -> bool:
    """PDF 有效性：存在、非空、以 %PDF 魔数开头。"""
    if not path.is_file() or path.stat().st_size < 100:
        return False
    try:
        return path.read_bytes()[:5] == b"%PDF-"
    except OSError:
        return False
