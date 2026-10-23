"""长图导出：用本机 Edge / Chrome 的无头截图把 report.html 变成一张竖长 PNG。

与 pdf.py 共用浏览器查找逻辑（PDF_BROWSER_BIN → 常见安装路径 → PATH）。
无头截图只截取视口大小的画面，因此分两趟：先向临时副本注入测高脚本，
用 --dump-dom 拿到内容在同宽视口下的实际高度，再按「宽 PNG_WIDTH × 实际高」
开窗截一整张。产物 report.png 落在 run 目录，按文件新旧做缓存，重复导出
不重复调用浏览器。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import execution
from .config import PDF_TIMEOUT_SEC, Settings
from .errors import PngError
from .pdf import expand_details, find_browser

#: 长图宽度：报告正文在这个宽度下是舒适的手机阅读版式
PNG_WIDTH = 900
#: 测高失败时的兜底高度：宁可多留空白也不截断内容
_FALLBACK_HEIGHT = 16000
#: 浏览器窗口最大高度（再高会被合成器拒绝），超长报告截断到此处
_MAX_HEIGHT = 32000
_MEASURE_MARK = "VRH:"
#: 阅读页渲染预算（毫秒，虚拟时间）：覆盖字体 CDN + API 拉取 + 拼版
_READER_BUDGET_MS = 15000


def _inject_measure_script(html: str) -> str:
    """在 </body> 前注入测高脚本：把内容高度写进 document.title。"""
    script = (
        "<script>document.title='" + _MEASURE_MARK
        + "' + document.documentElement.scrollHeight;</script>"
    )
    lower = html.lower()
    idx = lower.rindex("</body>")
    return html[:idx] + script + html[idx:]


def _require_browser(settings: Settings | None) -> str:
    browser = find_browser(settings)
    if browser is None:
        raise PngError(
            "未找到 Edge / Chrome，无法导出长图。"
            "请安装 Microsoft Edge，或用 PDF_BROWSER_BIN 指定浏览器路径"
        )
    return browser


def _run_browser(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return execution.run_command(
        cmd,
        timeout=PDF_TIMEOUT_SEC,
        error_cls=PngError,
        missing_message=lambda program: f"浏览器启动失败（{program} 已不存在）",
        timeout_message=lambda seconds, _program: f"长图导出超时（{seconds:.0f}s）",
    )


def _browser_cmd(browser: str, profile: str, *extra: str) -> list[str]:
    return [
        browser,
        "--headless",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-scrollbars",
        f"--user-data-dir={profile}",
        *extra,
    ]


def _parse_height(stdout: str) -> int:
    """从 dump 出的 DOM 标题里读测高标记；缺失时退回兜底高度而不是失败。"""
    match = re.search(_MEASURE_MARK + r"(\d+)", stdout)
    if not match:
        return _FALLBACK_HEIGHT
    return min(max(int(match.group(1)), 400), _MAX_HEIGHT)


def _dump_height(
    target: str, width: int, browser: str, profile: str, *, extra: tuple[str, ...] = ()
) -> int:
    """第一趟：同宽视口下让页面把内容高度写进标题，再 dump 出来读。"""
    result = _run_browser(
        _browser_cmd(
            browser,
            profile,
            *extra,
            f"--window-size={width},800",
            "--dump-dom",
            target,
        )
    )
    return _parse_height(result.stdout or "")


def _screenshot(
    target: str,
    out_png: Path,
    width: int,
    height: int,
    browser: str,
    profile: str,
    *,
    extra: tuple[str, ...] = (),
) -> None:
    """第二趟：按测得高度开窗截图，并校验产物确为 PNG。"""
    result = _run_browser(
        _browser_cmd(
            browser,
            profile,
            *extra,
            f"--screenshot={out_png}",
            f"--window-size={width},{height}",
            target,
        )
    )
    if result.returncode not in (0, None) and not _valid_png(out_png):
        raise PngError(
            f"长图导出失败（退出码 {result.returncode}）："
            f"{result.stderr.strip()[-500:]}"
        )
    if not _valid_png(out_png):
        raise PngError(f"长图导出失败：浏览器未产出有效文件 {out_png}")


def _measure_height(html_path: Path, width: int, browser: str, profile: str) -> int:
    """第一趟：同宽视口下测出报告内容的实际像素高度。"""
    temp_dir = Path(tempfile.mkdtemp(prefix="videread-png-"))
    copy = temp_dir / "report.html"
    try:
        copy.write_text(
            expand_details(_inject_measure_script(html_path.read_text(encoding="utf-8"))),
            encoding="utf-8",
        )
        return _dump_height(copy.resolve().as_uri(), width, browser, profile)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def export_png(html_path: Path, out_png: Path, settings: Settings | None = None, width: int = PNG_WIDTH) -> Path:
    """把 `html_path` 截成竖长 PNG；产物已比 HTML 新时直接复用（缓存）。"""
    # 浏览器对相对路径不可靠（同 pdf.py 的坑），统一先转绝对路径
    html_path = html_path.resolve()
    out_png = out_png.resolve()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    if out_png.is_file() and out_png.stat().st_mtime >= html_path.stat().st_mtime:
        return out_png

    browser = _require_browser(settings)

    # 两趟共用一个独立临时用户数据目录，避免与正在运行的浏览器冲突
    profile = tempfile.mkdtemp(prefix="videread-png-")
    # 截图同样用展开折叠块后的副本（与测高、PDF 行为一致）
    temp_dir = Path(tempfile.mkdtemp(prefix="videread-png-shot-"))
    shot_html = temp_dir / html_path.name
    shot_html.write_text(
        expand_details(html_path.read_text(encoding="utf-8")), encoding="utf-8"
    )
    try:
        height = _measure_height(html_path, width, browser, profile)
        _screenshot(
            shot_html.resolve().as_uri(), out_png, width, height, browser, profile
        )
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(temp_dir, ignore_errors=True)
    return out_png


def export_reader_png(
    page_url: str,
    out_png: Path,
    settings: Settings | None = None,
    width: int = PNG_WIDTH,
    cache_key: Path | None = None,
) -> Path:
    """把在线编辑风阅读页（/read/{run_id}?png=1）截成竖长 PNG。

    与 export_png 同为两趟，但浏览的是活页面：?png=1 让 reader.js 在拼版
    完成后把内容高度写进 document.title，第一趟 --dump-dom 读标记测高，
    第二趟按高开窗整页截图（--virtual-time-budget 等字体 CDN / API 就绪）。
    `cache_key`（通常是 run 的 report.html）做缓存新旧判据，未提供则不缓存。
    """
    out_png = out_png.resolve()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    if (
        cache_key is not None
        and cache_key.is_file()
        and out_png.is_file()
        and out_png.stat().st_mtime >= cache_key.stat().st_mtime
    ):
        return out_png

    browser = _require_browser(settings)

    profile = tempfile.mkdtemp(prefix="videread-png-reader-")
    extra = (f"--virtual-time-budget={_READER_BUDGET_MS}",)
    try:
        # 第一趟：同宽视口下等拼版完成，从 dump 出的 <title> 里读测高标记
        height = _dump_height(page_url, width, browser, profile, extra=extra)
        # 第二趟：按测得高度开窗整页截图
        _screenshot(page_url, out_png, width, height, browser, profile, extra=extra)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    return out_png


def _valid_png(path: Path) -> bool:
    """PNG 有效性：存在、非空、以 PNG 魔数开头。"""
    if not path.is_file() or path.stat().st_size < 100:
        return False
    try:
        return path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False
