"""电柜快画 — 电气设计壳体接口图自动生成工具."""

import os
import sys

__version__ = "0.5.1"
APP_NAME = "电柜快画"


def configure_console() -> None:
    """Git Bash / MSYS 默认按 GBK 输出中文会乱码；命令行输出统一 UTF-8。"""
    if os.environ.get("MSYSTEM") or os.environ.get("PYTHONUTF8"):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
