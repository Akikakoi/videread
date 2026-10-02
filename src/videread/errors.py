"""统一的退出码与异常分类（对应开发文档 §6.1 退出码约定 / §11.1 错误分类）。"""

from __future__ import annotations

# 退出码
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_DOWNLOAD = 2
EXIT_AUDIO_OR_ASR = 3
EXIT_LLM = 4
EXIT_RENDER = 5
EXIT_CANCELLED = 130  # 与 CLI 的 KeyboardInterrupt 返回值一致（128 + SIGINT）


class VidereadError(RuntimeError):
    """所有可归类的流水线错误基类。"""

    exit_code = EXIT_USAGE


class UsageError(VidereadError):
    """参数错误。"""

    exit_code = EXIT_USAGE


class DownloadError(VidereadError):
    """下载失败：反爬 / 地区限制 / 付费视频 / 链接失效。不做无意义重试。"""

    exit_code = EXIT_DOWNLOAD


class AudioError(VidereadError):
    """音频处理失败：ffmpeg 缺失、文件损坏。"""

    exit_code = EXIT_AUDIO_OR_ASR


class AsrError(VidereadError):
    """ASR 失败：密钥无效、余额不足、单段超时。"""

    exit_code = EXIT_AUDIO_OR_ASR


class FramesError(VidereadError):
    """截图失败：定点拉取被拒、ffmpeg 抽帧出错。流水线中降级为纯文字报告。"""

    exit_code = EXIT_AUDIO_OR_ASR


class LlmError(VidereadError):
    """LLM 调用失败：超时、限流、JSON 非法且重试耗尽。"""

    exit_code = EXIT_LLM


class RenderError(VidereadError):
    """渲染或写盘失败：占位符缺失、磁盘满。"""

    exit_code = EXIT_RENDER


class CancelledError(VidereadError):
    """任务被用户取消：在检查点抛出，已落盘产物照常保留、可据此续跑。"""

    exit_code = EXIT_CANCELLED


class PdfError(RenderError):
    """PDF 导出失败：找不到 Edge / Chrome、无头打印出错。"""


class PngError(RenderError):
    """长图导出失败：找不到 Edge / Chrome、无头截图出错。"""