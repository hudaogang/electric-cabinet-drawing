"""电柜快画入口：默认启动图形界面，--cli 走命令行批处理."""
from __future__ import annotations

import sys

from dkk import APP_NAME, __version__, configure_console


def _prepare() -> None:
    """打包成无控制台 EXE 时，先兜住 stdout/stderr 和异常。"""
    from dkk.diagnostics import (
        ensure_std_streams,
        install_excepthook,
        sanitize_environment,
    )

    sanitize_environment()
    ensure_std_streams()
    configure_console()
    install_excepthook()


def main(argv: list[str] | None = None) -> int:
    _prepare()
    args = list(sys.argv[1:] if argv is None else argv)
    if "--version" in args or "-V" in args:
        print(f"{APP_NAME} {__version__}")
        return 0
    if "--cli" in args:
        args.remove("--cli")
        from dkk.cli import main as cli_main

        return cli_main(args)
    from dkk.diagnostics import log, report_exception
    from dkk.gui import run

    log(f"启动 {APP_NAME} v{__version__} (frozen={getattr(sys, 'frozen', False)})")
    try:
        run()
    except SystemExit:
        raise
    except BaseException:
        report_exception(*sys.exc_info())
        return 1
    log("正常退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
