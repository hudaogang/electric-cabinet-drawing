"""把已排版的 matplotlib Figure 导出为 DXF（R12 / AC1009，单位 mm）。

只转换图纸实际用到的图元：直线、矩形/圆角矩形、圆、多边形、文字，
因此不依赖 ezdxf。中文按 Windows 中文 CAD 常用的 GBK（ANSI_936）写入，
AutoCAD / 浩辰 / 中望等可直接打开；填充的灰色线槽用 SOLID 实体表达。
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable

from matplotlib.colors import to_rgb
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Circle, FancyBboxPatch, Patch, Polygon, Rectangle
from matplotlib.text import Text

# AutoCAD 认可的线宽档位（单位 0.01mm）
_LW_STEPS = [0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70,
             80, 90, 100, 106, 120, 140, 158, 200, 211]


def _num(v: float) -> str:
    return f"{v:.4f}"


def _lw(value_pt: float) -> int:
    mm = max(0.0, float(value_pt) * 25.4 / 72.0)
    target = round(mm * 100)
    return min(_LW_STEPS, key=lambda s: abs(s - target))


def _color(artist) -> int:
    """黑/深灰 → 7，浅灰填充 → 9（AutoCAD 索引色）。"""
    try:
        try:
            raw = artist.get_edgecolor()
        except AttributeError:
            raw = artist.get_color()
        rgb = to_rgb(raw)
    except (ValueError, TypeError):
        return 7
    lum = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    if lum >= 0.55:
        return 9
    return 7


def _fill_color(patch: Patch) -> int | None:
    fc = patch.get_facecolor()
    if fc is None or len(fc) < 4 or fc[3] <= 0.01:
        return None
    rgb = fc[:3]
    if min(rgb) > 0.97:  # 白色不填充
        return None
    lum = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    return 9 if lum >= 0.55 else 7


def _dashed(artist) -> bool:
    ls = artist.get_linestyle()
    if ls in (None, "-", "solid", "None", "none"):
        return False
    if isinstance(ls, (tuple, list)) and len(ls) == 2:
        seq = ls[1]
        return bool(seq)
    return True


class _Writer:
    def __init__(self):
        self.lines: list[str] = []

    def tag(self, code: int, value: Any) -> None:
        self.lines.append(str(code))
        if isinstance(value, float):
            self.lines.append(_num(value))
        else:
            self.lines.append(str(value))

    def sections(self) -> str:
        return "\r\n".join(self.lines) + "\r\n"


def _emit_line(w: _Writer, pts: Iterable[tuple[float, float]], color: int,
               lw: float, dashed: bool, closed: bool = False) -> None:
    pts = list(pts)
    if len(pts) < 2:
        return
    ltype = "DASHED" if dashed else "CONTINUOUS"
    if len(pts) == 2:
        (x1, y1), (x2, y2) = pts
        w.tag(0, "LINE")
        w.tag(8, "0")
        w.tag(6, ltype)
        w.tag(62, color)
        w.tag(370, _lw(lw))
        w.tag(10, float(x1)); w.tag(20, float(y1)); w.tag(30, 0.0)
        w.tag(11, float(x2)); w.tag(21, float(y2)); w.tag(31, 0.0)
        return
    if closed and _same(pts[0], pts[-1]):
        pts = pts[:-1]
    w.tag(0, "POLYLINE")
    w.tag(8, "0")
    w.tag(6, ltype)
    w.tag(62, color)
    w.tag(370, _lw(lw))
    w.tag(66, 1)
    w.tag(70, 1 if closed else 0)
    w.tag(10, 0.0); w.tag(20, 0.0); w.tag(30, 0.0)
    for x, y in pts:
        w.tag(0, "VERTEX")
        w.tag(8, "0")
        w.tag(10, float(x)); w.tag(20, float(y)); w.tag(30, 0.0)
    w.tag(0, "SEQEND")
    w.tag(8, "0")


def _same(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6


def _emit_solid(w: _Writer, pts: list[tuple[float, float]], color: int) -> None:
    """用 SOLID 实体表达填充（多点多边形按扇形三角化）。"""
    if len(pts) < 3:
        return
    if len(pts) == 3:
        tri = [pts[0], pts[1], pts[2]]
        w.tag(0, "SOLID"); w.tag(8, "0"); w.tag(62, color)
        for code, (x, y) in zip((10, 11, 12), tri):
            w.tag(code, float(x)); w.tag(code + 10, float(y)); w.tag(code + 20, 0.0)
        return
    if len(pts) == 4:
        p0, p1, p2, p3 = pts
        w.tag(0, "SOLID"); w.tag(8, "0"); w.tag(62, color)
        for code, (x, y) in zip((10, 11, 12, 13), (p0, p1, p3, p2)):
            w.tag(code, float(x)); w.tag(code + 10, float(y)); w.tag(code + 20, 0.0)
        return
    for i in range(1, len(pts) - 1):
        _emit_solid(w, [pts[0], pts[i], pts[i + 1]], color)


def _emit_text(w: _Writer, t: Text, color: int) -> None:
    raw = str(t.get_text() or "").replace("\r", "")
    if not raw.strip():
        return
    height = float(t.get_fontsize()) / (72.0 / 25.4)  # pt -> mm
    if height <= 0:
        return
    ha = str(t.get_ha()).lower()
    va = str(t.get_va()).lower()
    hcode = {"left": 0, "center": 1, "right": 2}.get(ha, 0)
    vcode = {"baseline": 0, "bottom": 1, "center": 2, "center_baseline": 2,
             "top": 3}.get(va, 2)
    x, y = t.get_position()
    rotation = float(t.get_rotation() or 0.0) % 360.0
    lines = raw.split("\n")
    lh = height * 1.3
    n = len(lines)
    if vcode == 3:
        y0 = float(y)
    elif vcode == 1:
        y0 = float(y) + (n - 1) * lh
    elif vcode == 0:
        y0 = float(y) + (n - 1) * lh
    else:
        y0 = float(y) + (n - 1) * lh / 2
    for i, line in enumerate(lines):
        yy = y0 - i * lh
        w.tag(0, "TEXT")
        w.tag(8, "0")
        w.tag(62, color)
        w.tag(10, float(x)); w.tag(20, float(yy)); w.tag(30, 0.0)
        w.tag(40, height)
        w.tag(1, line)
        if abs(rotation) > 1e-6:
            w.tag(50, rotation)
        w.tag(72, hcode)
        w.tag(73, vcode)
        w.tag(11, float(x)); w.tag(21, float(yy)); w.tag(31, 0.0)


def _patch_points(patch: Patch) -> list[tuple[float, float]] | None:
    if isinstance(patch, Rectangle):
        x, y, wid, hei = patch.get_x(), patch.get_y(), patch.get_width(), patch.get_height()
        angle = float(patch.get_angle() or 0.0)
        corners = [(x, y), (x + wid, y), (x + wid, y + hei), (x, y + hei)]
        if abs(angle) > 1e-9:
            cx, cy = x, y
            rad = math.radians(angle)
            cos, sin = math.cos(rad), math.sin(rad)
            corners = [
                (cx + (px - cx) * cos - (py - cy) * sin,
                 cy + (px - cx) * sin + (py - cy) * cos)
                for px, py in corners
            ]
        return corners
    if isinstance(patch, FancyBboxPatch):
        path = patch.get_path()
        verts = path.transformed(patch.get_patch_transform()).vertices
        return [(float(px), float(py)) for px, py in verts]
    if isinstance(patch, Polygon):
        return [(float(px), float(py)) for px, py in patch.get_xy()]
    return None


def _emit_artist(w: _Writer, art: Any) -> None:
    if isinstance(art, Line2D):
        color = _color(art)
        if art.get_alpha() == 0:
            return
        pts = [(float(x), float(y)) for x, y in art.get_xydata()]
        _emit_line(w, pts, color, float(art.get_linewidth()), _dashed(art))
        return
    if isinstance(art, Text):
        _emit_text(w, art, _color(art))
        return
    if isinstance(art, Circle):
        edge = art.get_edgecolor()
        if edge is not None and not (len(edge) >= 4 and edge[3] <= 0.01):
            cx, cy = art.center
            w.tag(0, "CIRCLE")
            w.tag(8, "0")
            w.tag(6, "DASHED" if _dashed(art) else "CONTINUOUS")
            w.tag(62, _color(art))
            w.tag(370, _lw(float(art.get_linewidth())))
            w.tag(10, float(cx)); w.tag(20, float(cy)); w.tag(30, 0.0)
            w.tag(40, float(art.radius))
        fill = _fill_color(art)
        if fill is not None:
            # 圆填充用内接正多边形近似（当前图纸未使用，留作扩展）。
            n = 48
            cx, cy = art.center
            r = art.radius
            pts = [(cx + r * math.cos(2 * math.pi * i / n),
                    cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]
            _emit_solid(w, pts, fill)
        return
    if isinstance(art, Patch):
        pts = _patch_points(art)
        if pts is None:
            return
        fill = _fill_color(art)
        if fill is not None:
            _emit_solid(w, pts, fill)
        edge = art.get_edgecolor()
        if edge is None or (len(edge) >= 4 and edge[3] <= 0.01):
            return
        _emit_line(w, pts, _color(art), float(art.get_linewidth()),
                   _dashed(art), closed=True)


def _tables(w: _Writer) -> None:
    w.tag(0, "SECTION"); w.tag(2, "TABLES")

    w.tag(0, "TABLE"); w.tag(2, "LTYPE"); w.tag(70, 2)
    w.tag(0, "LTYPE"); w.tag(2, "CONTINUOUS"); w.tag(70, 64)
    w.tag(3, "Solid line"); w.tag(72, 65); w.tag(73, 0); w.tag(40, 0.0)
    w.tag(0, "LTYPE"); w.tag(2, "DASHED"); w.tag(70, 64)
    w.tag(3, "Dashed __ __ __"); w.tag(72, 65); w.tag(73, 2)
    w.tag(40, 6.0); w.tag(49, 4.0); w.tag(74, 0)
    w.tag(49, -2.0); w.tag(74, 0)
    w.tag(0, "ENDTAB")

    w.tag(0, "TABLE"); w.tag(2, "LAYER"); w.tag(70, 1)
    w.tag(0, "LAYER"); w.tag(2, "0"); w.tag(70, 0); w.tag(62, 7)
    w.tag(6, "CONTINUOUS")
    w.tag(0, "ENDTAB")

    w.tag(0, "TABLE"); w.tag(2, "STYLE"); w.tag(70, 1)
    w.tag(0, "STYLE"); w.tag(2, "STANDARD"); w.tag(70, 0)
    w.tag(40, 0.0); w.tag(41, 1.0); w.tag(50, 0.0); w.tag(71, 0)
    w.tag(42, 2.5); w.tag(3, "txt"); w.tag(4, "gbcbig.shx")
    w.tag(0, "ENDTAB")

    w.tag(0, "ENDSEC")


def save_dxf(fig: Figure, path: str | Path) -> Path:
    """把 Figure 中第一个坐标系的图元写成 DXF。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not fig.axes:
        raise ValueError("图纸为空，无法导出 DXF。")

    w = _Writer()
    w.tag(0, "SECTION"); w.tag(2, "HEADER")
    w.tag(9, "$ACADVER"); w.tag(1, "AC1009")
    w.tag(9, "$DWGCODEPAGE"); w.tag(3, "ANSI_936")
    w.tag(9, "$INSUNITS"); w.tag(70, 4)      # 4 = 毫米
    w.tag(9, "$LTSCALE"); w.tag(40, 1.0)
    w.tag(0, "ENDSEC")
    _tables(w)

    w.tag(0, "SECTION"); w.tag(2, "ENTITIES")
    ax = fig.axes[0]
    items: list[Any] = []
    items.extend(ax.lines)
    items.extend(ax.patches)
    items.extend(ax.texts)
    order = {id(o): i for i, o in enumerate(items)}
    items.sort(key=lambda o: (float(o.get_zorder()), order.get(id(o), 0)))
    for art in items:
        try:
            _emit_artist(w, art)
        except Exception:  # noqa: BLE001 - 单个图元异常不应中断导出
            continue
    w.tag(0, "ENDSEC")
    w.tag(0, "EOF")

    path.write_bytes(w.sections().encode("gbk", errors="replace"))
    return path
