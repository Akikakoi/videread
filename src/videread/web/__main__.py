"""`python -m videread.web`：启动本地 Web 控制台（默认 http://127.0.0.1:8765）。"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

from ..config import get_settings
from ..errors import EXIT_OK, EXIT_USAGE
from .app import create_app

_DEFAULT_PORT = 8765
_OPEN_DELAY_SEC = 1.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="videread-web",
        description="videread 本地 Web 控制台：填链接、看进度、读报告",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT, help="监听端口，默认 8765")
    parser.add_argument("--out", default=None, help="产物根目录，默认 ./runs")
    parser.add_argument(
        "--open",
        dest="open_browser",
        action="store_true",
        help="启动后用浏览器打开控制台",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        print(
            "缺少 fastapi / uvicorn，请先执行：pip install -e .",
            file=sys.stderr,
        )
        return EXIT_USAGE

    # 不调用 configure_console()：它会 reconfigure 全局 stdout，干扰 uvicorn 日志
    settings = get_settings()
    out_root = Path(args.out).expanduser() if args.out else settings.out_root
    out_root.mkdir(parents=True, exist_ok=True)

    url = f"http://{args.host}:{args.port}/"
    print(f"videread 控制台 → {url}")
    print(f"产物目录        → {out_root}")
    if args.open_browser:
        # 等服务起来再开浏览器，否则首屏会因连接被拒而显示错误页
        threading.Timer(_OPEN_DELAY_SEC, webbrowser.open, args=(url,)).start()

    uvicorn.run(create_app(out_root), host=args.host, port=args.port, log_level="info")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())