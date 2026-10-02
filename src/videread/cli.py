"""命令行入口：参数解析、进度输出、退出码（对应开发文档 §6.1 / §9）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, download, failures
from .config import DEFAULT_OUT_ROOT, configure_console
from .errors import EXIT_OK, EXIT_USAGE, VidereadError
from .pipeline import run

_MODES = ("standard", "brief")


class _Parser(argparse.ArgumentParser):
    """参数错误统一返回退出码 1（argparse 默认是 2，与 §6.1 不符）。"""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: 参数错误：{message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="videread",
        description="把 Bilibili 视频 / 本地音视频的转写稿重组为自包含 HTML 阅读报告",
    )
    parser.add_argument(
        "url",
        help="Bilibili 视频链接或 BV 号、本地音视频文件路径；"
        "也可以是 .txt 批量列表（每行一个来源，# 为注释，逐个顺序解析）",
    )
    parser.add_argument(
        "--mode", choices=_MODES, default="standard", help="阅读模式，默认 standard"
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT_ROOT), help="产物根目录，默认 ./runs")
    parser.add_argument("--no-cache", action="store_true", help="忽略已有产物，全部重跑")
    parser.add_argument("--keep-audio", action="store_true", help="跑完保留音频中间文件（默认自动清理）")
    parser.add_argument(
        "--open", dest="open_report", action="store_true", help="生成后自动用浏览器打开"
    )
    parser.add_argument(
        "--asr",
        dest="asr_backend",
        default=None,
        help="临时覆盖 ASR 后端（dashscope / dashscope-realtime / openai / local）",
    )
    parser.add_argument(
        "--force-asr",
        dest="force_asr",
        action="store_true",
        help="忽略平台字幕，坚持下载音频走 ASR（字幕优先的兜底开关）",
    )
    parser.add_argument(
        "--page",
        dest="page",
        type=int,
        default=1,
        metavar="N",
        help="多 P 视频指定解析第 N 个分P（默认 1；本地文件无效）",
    )
    parser.add_argument(
        "--frames",
        dest="frames",
        action="store_true",
        default=None,
        help="按大纲节截取视频画面并内嵌进报告（默认跟随 .env 的 FRAMES，默认关）",
    )
    parser.add_argument(
        "--pdf",
        dest="pdf",
        action="store_true",
        help="报告生成后用本机 Edge / Chrome 无头打印导出 report.pdf",
    )
    parser.add_argument(
        "--png",
        dest="png",
        action="store_true",
        help="报告生成后用本机 Edge / Chrome 无头截图导出竖长 report.png",
    )
    parser.add_argument(
        "--md",
        dest="md",
        action="store_true",
        help="报告生成后转换导出 report.md（文字层次，适合粘贴进笔记软件）",
    )
    parser.add_argument("--version", action="version", version=f"videread {__version__}")
    return parser


def _normalize_entry(raw: str) -> str:
    """批量列表的单行条目：BV 号补全、空白清理（与单任务入口同一套规则）。"""
    url = download.normalize_source(raw)
    return url


def _run_single(args: argparse.Namespace) -> int:
    """跑一个来源；返回退出码（不直接退出进程，批量模式要逐个收集）。"""
    try:
        url = _normalize_entry(args.url)
        if args.page > 1 and download.extract_bvid(url):
            # 只有确认为 B 站视频才改写链接；裸 BV 号先补全成标准链接
            url = download.page_url(url, args.page)
        path = run(
            url,
            mode=args.mode,
            out_root=Path(args.out),
            use_cache=not args.no_cache,
            keep_audio=args.keep_audio,
            asr_backend=args.asr_backend,
            force_asr=args.force_asr,
            open_report=args.open_report,
            frames=args.frames,
            export_pdf=args.pdf,
            export_png=args.png,
            export_md=args.md,
        )
    except VidereadError as exc:
        code = int(exc.exit_code)
        print(f"错误：{exc}", file=sys.stderr)
        print(f"  [{failures.group(code)}] {failures.hint(code)}", file=sys.stderr)
        return code
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return 130
    print(f"完成 → {path}")
    return EXIT_OK


def _run_batch(list_path: Path, args: argparse.Namespace) -> int:
    """批量模式：逐行顺序解析列表文件，失败不中断，最后汇总并给出退出码。"""
    try:
        lines = list_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        print(f"错误：无法读取批量列表 {list_path}：{exc}", file=sys.stderr)
        return EXIT_USAGE
    entries = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    if not entries:
        print(f"错误：批量列表是空的：{list_path}（每行一个链接 / BV 号 / 本地文件，# 为注释）", file=sys.stderr)
        return EXIT_USAGE

    print(f"批量模式：共 {len(entries)} 个任务（失败不中断，逐个继续）")
    results: list[tuple[str, int]] = []
    for index, entry in enumerate(entries, 1):
        print(f"\n—— [{index}/{len(entries)}] {entry} ——")
        task = argparse.Namespace(**{**vars(args), "url": entry, "page": 1, "open_report": False})
        try:
            code = _run_single(task)
        except KeyboardInterrupt:
            # 批量中途 Ctrl+C：停止后续任务，按 130 收场
            print(f"\n批量已中断（完成 {index - 1}/{len(entries)}）。", file=sys.stderr)
            return 130
        results.append((entry, code))

    failed = [(entry, code) for entry, code in results if code != EXIT_OK]
    print(f"\n批量完成：成功 {len(results) - len(failed)}/{len(results)}")
    for entry, code in failed:
        print(f"  失败 [{code}] {entry}")
    return failed[0][1] if failed else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = build_parser().parse_args(argv)
    # 位置参数指向存在的 .txt 文件即进入批量模式（一行一个来源，# 为注释）
    candidate = Path(args.url)
    if candidate.suffix.lower() == ".txt" and candidate.is_file():
        return _run_batch(candidate, args)
    return _run_single(args)


if __name__ == "__main__":
    sys.exit(main())