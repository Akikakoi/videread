"""videread 本地 Web 控制台（FastAPI 后端 + 原生前端，零构建）。"""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    """延迟导出 `main`（PEP 562）。

    直接 `from .__main__ import main` 会让 `python -m videread.web` 在 runpy 执行
    `__main__` 之前就把该模块拉进 sys.modules，触发 RuntimeWarning；改成按需导入
    即可让控制台命令 `videread.web:main` 与 `python -m videread.web` 两条入口共存。
    """
    if name == "main":
        from .__main__ import main

        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["main"]