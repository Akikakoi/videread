"""失败分类：退出码 → 类别 / 用户文案 的纯函数映射（对应开发文档 §6.1 退出码约定）。

参考 video-report-agent 的 failures.py：把「失败怎么呈现」收在一处。异常类仍然各自
带 `exit_code`（那是稳定契约），但类别与文案不再散落在 CLI 与 Web 里各写一遍；
本模块只吃退出码这个整数，不 import 任何异常类型。

纯函数、无 I/O，可直接单测。
"""

from __future__ import annotations

from .errors import (
    EXIT_AUDIO_OR_ASR,
    EXIT_CANCELLED,
    EXIT_DOWNLOAD,
    EXIT_LLM,
    EXIT_OK,
    EXIT_RENDER,
    EXIT_USAGE,
)

#: 退出码 → 类别（input=用户输入 / external=外部服务 / processing=本地处理 / runtime=运行时）
GROUPS: dict[int, str] = {
    EXIT_OK: "ok",
    EXIT_USAGE: "input",
    EXIT_DOWNLOAD: "external",
    EXIT_AUDIO_OR_ASR: "processing",
    EXIT_LLM: "external",
    EXIT_RENDER: "processing",
    EXIT_CANCELLED: "runtime",
}

#: 退出码 → 用户文案（CLI 与 Web 提示共用，新增码必须在此登记）
HINTS: dict[int, str] = {
    EXIT_USAGE: "参数 / 用法错误（含缺少密钥）",
    EXIT_DOWNLOAD: "下载失败（反爬、地域限制、链接失效）",
    EXIT_AUDIO_OR_ASR: "音频处理或 ASR 失败",
    EXIT_LLM: "LLM 调用失败（超时、限流、JSON 非法）",
    EXIT_RENDER: "渲染或写盘失败",
    EXIT_CANCELLED: "任务已被用户取消（已生成产物保留，可续跑）",
}

#: 未登记码的兜底类别 / 文案
_UNKNOWN_GROUP = "unknown"
_UNKNOWN_HINT = "未知错误"


def group(exit_code: int) -> str:
    """退出码 → 失败类别；未登记一律视为 unknown。"""
    return GROUPS.get(exit_code, _UNKNOWN_GROUP)


def hint(exit_code: int) -> str:
    """退出码 → 用户文案；未登记一律返回通用文案。"""
    return HINTS.get(exit_code, _UNKNOWN_HINT)