"""中文字体配置：优先使用系统黑体，保证 PDF 字体嵌入."""
from __future__ import annotations

import sys
from pathlib import Path

from matplotlib import font_manager, rcParams

_CANDIDATES = [
    "SimHei",
    "Microsoft YaHei",
    "Noto Sans SC",
    "Source Han Sans SC",
    "DengXian",
    "SimSun",
]

_configured: str | None = None


def setup_fonts() -> str:
    """设置全局字体并返回实际使用的字体名。"""
    global _configured
    if _configured:
        return _configured

    # 允许把字体放在 data/fonts 下随程序分发
    font_dir = Path(__file__).resolve().parent.parent / "data" / "fonts"
    if font_dir.exists():
        for f in font_dir.glob("*.tt[fc]"):
            try:
                font_manager.fontManager.addfont(str(f))
            except Exception:
                pass

    available = {f.name for f in font_manager.fontManager.ttflist}
    chosen = next((name for name in _CANDIDATES if name in available), "DejaVu Sans")

    rcParams["font.family"] = "sans-serif"
    rcParams["font.sans-serif"] = [chosen, "DejaVu Sans"]
    rcParams["axes.unicode_minus"] = False
    rcParams["pdf.fonttype"] = 42       # TrueType 子集嵌入，PDF 可移植
    rcParams["ps.fonttype"] = 42
    rcParams["svg.fonttype"] = "path"   # SVG 文字转路径，避免缺字体
    rcParams["figure.facecolor"] = "white"
    rcParams["savefig.facecolor"] = "white"
    _configured = chosen
    return chosen


def is_chinese_font_available() -> bool:
    name = setup_fonts()
    return name != "DejaVu Sans"
