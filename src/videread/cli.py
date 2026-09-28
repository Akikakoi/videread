"""命令行入口：参数解析、进度输出、退出码（对应开发文档 §6.1 / §9）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, failures
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
    parser.add_argument("url", help="Bilibili 视频链接或 BV 号，或本地音视频文件路径")
    parser.add_argument(
        "--mode", choices=_MODES, default="standard", help="阅读模式，默认 standard"
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT_ROOT), help="产物根目录，默认 ./runs")
    parser.add_argument("--no-cache", action="store_true", help="忽略已有产物，全部重跑")
    parser.add_argument("--keep-audio", action="store_true", help="保留 audio.m4a")
    parser.add_argument(
        "--open", dest="open_report", action="store_true", help="生成后自动用浏览器打开"
    )
    parser.add_argument(
        "--asr", dest="asr_backend", default=None, help="临时覆盖 ASR 后端（dashscope / openai）"
    )
    parser.add_argument("--version", action="version", version=f"videread {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = build_parser().parse_args(argv)
    try:
        path = run(
            args.url,
            mode=args.mode,
            out_root=Path(args.out),
            use_cache=not args.no_cache,
            keep_audio=args.keep_audio,
            asr_backend=args.asr_backend,
            open_report=args.open_report,
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


if __name__ == "__main__":
    sys.exit(main())