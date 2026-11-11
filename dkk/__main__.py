"""支持 python -m dkk 与 python -m dkk --cli ..."""
from __future__ import annotations

import sys

from . import APP_NAME, __version__, configure_console
from .diagnostics import (
    ensure_std_streams,
    install_excepthook,
    log,
    report_exception,
    sanitize_environment,
)


def _main() -> int:
    sanitize_environment()
    ensure_std_streams()
    configure_console()
    install_excepthook()
    args = list(sys.argv[1:])
    if "--version" in args or "-V" in args:
        print(f"{APP_NAME} {__version__}")
        return 0
    if "--cli" in args:
        args.remove("--cli")
        from .cli import main as cli_main

        return cli_main(args)
    from .gui import run

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
    raise SystemExit(_main())
