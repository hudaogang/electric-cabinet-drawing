"""启动诊断：日志、未捕获异常提示、窗口模式下的标准流兜底.

打包为 --windowed 的 EXE 时，Python 的 stdout/stderr 为 None，任何库的
print/警告都可能让程序在启动阶段静默退出（表现为“双击闪退”）。这里统一
把输出接到用户日志，并把未捕获异常写入日志、弹窗提示。
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
import traceback
from pathlib import Path

APP_DIR_NAME = "电柜快画"


def log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) / APP_DIR_NAME if base else Path.home() / f".{APP_DIR_NAME}"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        root = Path.home()
    return root


def log_path() -> Path:
    return log_dir() / "app.log"


def _open_log_stream():
    try:
        return open(log_path(), "a", encoding="utf-8", errors="replace")
    except OSError:
        return open(os.devnull, "w", encoding="utf-8")


def ensure_std_streams() -> None:
    """--windowed 打包后 sys.stdout/sys.stderr 为 None，补成日志文件。"""
    if sys.stdout is None:
        sys.stdout = _open_log_stream()
    if sys.stderr is None:
        sys.stderr = _open_log_stream()


def log(message: str) -> None:
    try:
        with open(log_path(), "a", encoding="utf-8", errors="replace") as f:
            f.write(f"[{_dt.datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")
    except OSError:
        pass


def sanitize_environment() -> None:
    """避免外部 Python 环境变量干扰冻结程序自带的运行库。

    注意：不能删除 TCL_LIBRARY/TK_LIBRARY —— PyInstaller 的运行时钩子
    会把它们指向解压目录，删掉后 Tk 会找不到 init.tcl。
    """
    if getattr(sys, "frozen", False):
        for name in ("PYTHONHOME", "PYTHONPATH"):
            os.environ.pop(name, None)


def _format(exc_type, exc, tb) -> str:
    return "".join(traceback.format_exception(exc_type, exc, tb))


def show_error(text: str, parent=None) -> None:
    """无控制台时也要让用户看到失败原因。"""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    summary = lines[-1] if lines else "未知错误"
    message = f"程序遇到错误：\n{summary}\n\n详细信息已写入日志：\n{log_path()}"
    try:
        import tkinter as tk
        from tkinter import messagebox

        if parent is not None:
            try:
                messagebox.showerror("电柜快画", message, parent=parent)
                return
            except Exception:
                pass
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("电柜快画", message, parent=root)
        root.destroy()
        return
    except Exception:
        pass
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "电柜快画", 0x10)
    except Exception:
        pass


def report_exception(exc_type, exc, tb, parent=None) -> None:
    text = _format(exc_type, exc, tb)
    log("未捕获异常：\n" + text)
    try:
        if sys.stderr is not None:
            sys.stderr.write(text)
            sys.stderr.flush()
    except Exception:
        pass
    show_error(text, parent=parent)


def install_excepthook() -> None:
    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        report_exception(exc_type, exc, tb)

    sys.excepthook = hook


def install_tk_error_handler(root) -> None:
    """Tk 回调里的异常默认只打印到控制台，这里改为记录并提示。"""
    def handler(exc_type, exc, tb):
        report_exception(exc_type, exc, tb, parent=root)

    root.report_callback_exception = handler
