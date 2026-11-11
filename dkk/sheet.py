"""图纸排版与绘制（纯 matplotlib Figure，无 pyplot）."""
from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from matplotlib.figure import Figure
from matplotlib.patches import Circle, FancyBboxPatch, Polygon, Rectangle

from . import APP_NAME, __version__
from .fonts import setup_fonts
from .library import Library, ResolvedCutout, geom_bbox, geom_size
from .model import FACE_LABELS, Cabinet, Job
from .validate import Issue, validate
from .views import (
    Box,
    FrontGeometry,
    InternalLayout,
    RailLayout,
    box_from_geom,
    front_geometry,
    internal_layout,
)

PT = 72.0 / 25.4  # 1 mm -> pt

PAGE_SIZES: dict[str, tuple[float, float]] = {
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
}
SCALES = [1, 2, 2.5, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 40, 50]

LW_THIN = 0.15
LW_MID = 0.30
LW_THICK = 0.55


def fs(mm: float) -> float:
    return mm * PT


def fmt(v: float) -> str:
    if abs(v - round(v)) < 1e-6:
        return str(int(round(v)))
    return f"{v:.1f}".rstrip("0").rstrip(".")


def text_width_mm(text: str, font_mm: float) -> float:
    w = 0.0
    for ch in text:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            w += font_mm * 1.0
        elif ch == " ":
            w += font_mm * 0.35
        elif ch in ".,:;!|'`":
            w += font_mm * 0.30
        else:
            w += font_mm * 0.56
    return w


def wrap_text(text: str, max_mm: float, font_mm: float) -> list[str]:
    lines: list[str] = []
    cur = ""
    for ch in text:
        trial = cur + ch
        if cur and text_width_mm(trial, font_mm) > max_mm:
            lines.append(cur.rstrip())
            cur = ch
        else:
            cur = trial
    if cur:
        lines.append(cur.rstrip())
    return lines or [""]


@dataclass
class Layout:
    page: str
    pw: float
    ph: float
    s: float
    den: float
    content: Box
    right_x: float
    right_w: float
    tech: Box
    title: Box
    inner: Box


def parse_scale(value: str) -> float | None:
    if not value or value.strip().lower() == "auto":
        return None
    t = value.replace("：", ":").replace("／", "/").strip()
    try:
        if ":" in t:
            a, b = t.split(":", 1)
            return float(a) / float(b)
        return float(t)
    except (ValueError, ZeroDivisionError):
        return None


def view_model_dims(cab: Cabinet, view: str) -> tuple[float, float]:
    """各视图在模型坐标下的宽×高（琴柜正视图为斜面展开高度）。"""
    if view == "front":
        return cab.W, cab.front_dev_h
    if view == "side":
        return cab.D, cab.H
    if view == "plan":
        return cab.W, cab.D
    if view == "internal":
        return cab.plate_w, cab.plate_h
    return 0.0, 0.0


def row1_model_height(cab: Cabinet, views: set[str]) -> float:
    heights = [view_model_dims(cab, v)[1] for v in ("front", "side", "plan") if v in views]
    return max(heights, default=0.0)


def _best_scale(pw: float, ph: float, cab: Cabinet, views: set[str]) -> tuple[float, float, bool]:
    """返回 (比例, 分母, 是否放得下)."""
    # 内容区尺寸（与 _layout_for_page 保持一致）
    right_w = {"A3": 165.0, "A2": 175.0, "A1": 200.0}.get(_page_name(pw, ph), 175.0)
    right_x = pw - 12.0 - right_w
    content_x1 = right_x - 8.0
    content_x0 = 32.0
    content_y0 = 91.0
    content_y1 = ph - 16.0
    cw = content_x1 - content_x0
    ch = content_y1 - content_y0

    gap = 22.0
    widths: list[float] = []
    if "front" in views:
        widths.append(cab.W)
    if "side" in views:
        widths.append(cab.D)
    if "plan" in views:
        widths.append(cab.W)
    row1_w_model = sum(widths)
    row1_extra = gap * max(0, len(widths) - 1)
    # 左侧要给孔编号留 30mm 带，排版时也必须计入，否则视图会压到右侧表格。
    left_band = 30.0 if widths else 0.0

    row1_h_model = row1_model_height(cab, views)
    row2_h_model = cab.plate_h if "internal" in views else 0.0
    extra_h = 64.0 if row2_h_model else 30.0
    if not widths and not row2_h_model:
        return 1.0, 1.0, True

    def max_s(avail_w: float, avail_h: float) -> float:
        sw = (avail_w - row1_extra - left_band) / row1_w_model if row1_w_model else 1.0
        sh = (avail_h - extra_h) / (row1_h_model + row2_h_model) if (row1_h_model + row2_h_model) else 1.0
        return min(sw, sh)

    s_max = max_s(cw, ch)
    if s_max <= 0:
        return 1.0 / 50.0, 50.0, False
    chosen = 1.0 / 50.0
    for den in SCALES:
        if 1.0 / den <= s_max + 1e-9:
            chosen = 1.0 / den
            break
    fits = (row1_w_model * chosen + row1_extra + left_band <= cw + 0.01) and (
        (row1_h_model + row2_h_model) * chosen + extra_h <= ch + 0.01
    )
    return chosen, 1.0 / chosen, fits


def _page_name(pw: float, ph: float) -> str:
    for name, (w, h) in PAGE_SIZES.items():
        if abs(pw - w) < 0.01 and abs(ph - h) < 0.01:
            return name
    return "A2"


def choose_layout(job: Job) -> tuple[Layout, list[Issue]]:
    issues: list[Issue] = []
    views = set(job.views)
    requested = job.sheet.size.upper().strip()
    explicit_scale = parse_scale(job.sheet.scale)

    candidates = [requested] if requested in PAGE_SIZES else ["A3", "A2", "A1"]
    page_name = None
    s = 1.0
    fits = True
    for name in candidates:
        pw, ph = PAGE_SIZES[name]
        best, den, ok = _best_scale(pw, ph, job.cabinet, views)
        if explicit_scale:
            s = explicit_scale
            fits = _fit_check(pw, ph, job.cabinet, views, s)
        else:
            s = best
            fits = ok
        if requested in PAGE_SIZES:
            page_name = name
            break
        if fits and 1.0 / s <= 20.0 + 1e-6:
            page_name = name
            break
        page_name = name
        if name == "A1":
            break

    if page_name is None:
        page_name = "A2"
    if explicit_scale:
        s = explicit_scale
        if not _fit_check(*PAGE_SIZES[page_name], job.cabinet, views, s):
            issues.append(Issue("warn", f"指定比例 1:{fmt(1/s)} 在图幅 {page_name} 上放不下，图形会超出内容区。"))
    else:
        best, den, ok = _best_scale(*PAGE_SIZES[page_name], job.cabinet, views)
        s = best
        if not ok:
            issues.append(Issue("warn", "当前柜体尺寸过大，已使用最小比例，仍可能超出图幅。"))
        if 1.0 / s > 20.0 + 1e-6:
            issues.append(Issue("warn", f"自动比例 1:{fmt(1/s)} 偏小，建议改用更大图幅。"))

    pw, ph = PAGE_SIZES[page_name]
    right_w = {"A3": 165.0, "A2": 175.0, "A1": 200.0}[page_name]
    right_x = pw - 12.0 - right_w
    inner = Box(25.0, 15.0, pw - 12.0, ph - 12.0)
    title_w = {"A3": 150.0, "A2": 170.0, "A1": 190.0}[page_name]
    title_h = {"A3": 44.0, "A2": 48.0, "A1": 52.0}[page_name]
    title = Box(inner.x1 - title_w, inner.y0, inner.x1, inner.y0 + title_h)
    tech = Box(32.0, inner.y0, right_x - 8.0, inner.y0 + 68.0)
    content = Box(32.0, tech.y1 + 8.0, right_x - 8.0, ph - 16.0)
    return (
        Layout(
            page=page_name,
            pw=pw,
            ph=ph,
            s=s,
            den=1.0 / s,
            content=content,
            right_x=right_x,
            right_w=right_w,
            tech=tech,
            title=title,
            inner=inner,
        ),
        issues,
    )


def _fit_check(pw: float, ph: float, cab: Cabinet, views: set[str], s: float) -> bool:
    right_w = {"A3": 165.0, "A2": 175.0, "A1": 200.0}.get(_page_name(pw, ph), 175.0)
    cw = (pw - 12.0 - right_w - 8.0) - 32.0
    ch = (ph - 16.0) - 91.0
    widths = []
    if "front" in views:
        widths.append(cab.W)
    if "side" in views:
        widths.append(cab.D)
    if "plan" in views:
        widths.append(cab.W)
    row1_w = sum(widths) * s + 22.0 * max(0, len(widths) - 1)
    left_band = 30.0 if widths else 0.0
    row1_h = row1_model_height(cab, views) * s
    row2_h = cab.plate_h * s if "internal" in views else 0.0
    extra_h = 64.0 if row2_h else 30.0
    return row1_w + left_band <= cw + 0.01 and row1_h + row2_h + extra_h <= ch + 0.01


class Drawing:
    def __init__(self, fig: Figure, ax):
        self.fig = fig
        self.ax = ax

    def line(self, x1, y1, x2, y2, lw=LW_THIN, dash=None, color="black", z=2):
        kw: dict[str, Any] = dict(color=color, lw=lw * PT, solid_capstyle="butt", zorder=z)
        if dash:
            kw["linestyle"] = (0, dash)
        self.ax.plot([x1, x2], [y1, y2], **kw)

    def rect(self, x, y, w, h, lw=LW_THIN, dash=None, color="black", z=2, fill=None):
        patch = Rectangle(
            (x, y),
            w,
            h,
            fill=fill is not None,
            facecolor=fill if fill is not None else "none",
            edgecolor=color,
            lw=lw * PT,
            linestyle=(0, dash) if dash else "solid",
            zorder=z,
        )
        self.ax.add_patch(patch)

    def round_rect(self, cx, cy, w, h, r=0.0, lw=LW_MID, dash=None, color="black", z=3):
        if r <= 0 or w <= 0 or h <= 0:
            self.rect(cx - w / 2, cy - h / 2, w, h, lw=lw, dash=dash, color=color, z=z)
            return
        r = min(r, w / 2, h / 2)
        patch = FancyBboxPatch(
            (cx - w / 2, cy - h / 2),
            w,
            h,
            boxstyle=f"round,pad=0,rounding_size={r}",
            fill=False,
            edgecolor=color,
            lw=lw * PT,
            linestyle=(0, dash) if dash else "solid",
            zorder=z,
        )
        self.ax.add_patch(patch)

    def circle(self, cx, cy, d, lw=LW_MID, dash=None, color="black", z=3):
        patch = Circle(
            (cx, cy),
            d / 2,
            fill=False,
            edgecolor=color,
            lw=lw * PT,
            linestyle=(0, dash) if dash else "solid",
            zorder=z,
        )
        self.ax.add_patch(patch)

    def poly(self, pts, lw=LW_THIN, fill=False, color="black", z=3):
        patch = Polygon(
            pts,
            closed=True,
            fill=fill,
            facecolor=color if fill else "none",
            edgecolor=color,
            lw=lw * PT,
            zorder=z,
        )
        self.ax.add_patch(patch)

    def text(
        self,
        x,
        y,
        s,
        size=2.5,
        ha="left",
        va="center",
        rotation=0,
        color="black",
        white=False,
        z=6,
        style="normal",
    ):
        kw: dict[str, Any] = dict(
            fontsize=fs(size),
            ha=ha,
            va=va,
            rotation=rotation,
            color=color,
            zorder=z,
            fontstyle=style,
            rotation_mode="anchor",
        )
        if white:
            kw["bbox"] = dict(facecolor="white", edgecolor="none", pad=0.3)
        return self.ax.text(x, y, s, **kw)


class VP:
    """模型坐标 -> 图纸坐标 (mm)."""

    def __init__(self, d: Drawing, s: float, ox: float, oy: float):
        self.d = d
        self.s = s
        self.ox = ox
        self.oy = oy

    def X(self, x: float) -> float:
        return self.ox + x * self.s

    def Y(self, y: float) -> float:
        return self.oy + y * self.s

    def line(self, x1, y1, x2, y2, lw=LW_THIN, dash=None, z=2):
        self.d.line(self.X(x1), self.Y(y1), self.X(x2), self.Y(y2), lw=lw, dash=dash, z=z)

    def rect(self, x, y, w, h, lw=LW_THIN, dash=None, z=2, fill=None):
        self.d.rect(
            self.X(x), self.Y(y), w * self.s, h * self.s,
            lw=lw, dash=dash, z=z, fill=fill,
        )

    def circle(self, cx, cy, dia, lw=LW_MID, dash=None, z=3):
        self.d.circle(self.X(cx), self.Y(cy), dia * self.s, lw=lw, dash=dash, z=z)

    def round_rect(self, cx, cy, w, h, r=0.0, lw=LW_MID, dash=None, z=3):
        self.d.round_rect(self.X(cx), self.Y(cy), w * self.s, h * self.s, r * self.s, lw=lw, dash=dash, z=z)

    def text(self, x, y, s, size=2.5, ha="center", va="center", **kw):
        # 视图内文字尺寸按图面 mm 处理，不随比例缩放
        return self.d.text(self.X(x), self.Y(y), s, size=size, ha=ha, va=va, **kw)


@dataclass
class _Dim:
    axis: str
    a1: float
    a2: float
    feature: float
    side: int
    text: str


class DimEngine:
    """尺寸标注：自动分层避让。"""

    def __init__(self, d: Drawing, vp: VP):
        self.d = d
        self.vp = vp
        self.items: list[_Dim] = []

    def add_h(self, x1: float, x2: float, feature_y: float, side: int, text: str | None = None):
        p1, p2 = self.vp.X(x1), self.vp.X(x2)
        self.items.append(_Dim("h", p1, p2, self.vp.Y(feature_y), side, text or fmt(abs(x2 - x1))))

    def add_v(self, y1: float, y2: float, feature_x: float, side: int, text: str | None = None):
        p1, p2 = self.vp.Y(y1), self.vp.Y(y2)
        self.items.append(_Dim("v", p1, p2, self.vp.X(feature_x), side, text or fmt(abs(y2 - y1))))

    def render(self):
        groups: dict[tuple[str, int], list[_Dim]] = {}
        for it in self.items:
            groups.setdefault((it.axis, it.side), []).append(it)

        for (axis, side), dims in groups.items():
            dims.sort(key=lambda dd: abs(dd.a2 - dd.a1))
            occupied: list[list[tuple[float, float]]] = []
            for dim in dims:
                span = abs(dim.a2 - dim.a1)
                tw = text_width_mm(dim.text, 2.6)
                mid = (dim.a1 + dim.a2) / 2
                half = max(span / 2, tw / 2) + 2.2
                interval = (mid - half, mid + half)
                level = 0
                while level < len(occupied) and any(
                    not (interval[1] <= a or b <= interval[0]) for a, b in occupied[level]
                ):
                    level += 1
                if level == len(occupied):
                    occupied.append([])
                occupied[level].append(interval)
                if axis == "h":
                    self._draw_h(dim, level, side)
                else:
                    self._draw_v(dim, level, side)

    def _draw_h(self, dim: _Dim, level: int, side: int):
        d = self.d
        offset = 7.0 + level * 6.5
        y = dim.feature + side * offset
        x1, x2 = dim.a1, dim.a2
        # 尺寸界线
        d.line(x1, dim.feature + side * 1.5, x1, y + side * 1.8, lw=LW_THIN)
        d.line(x2, dim.feature + side * 1.5, x2, y + side * 1.8, lw=LW_THIN)
        # 尺寸线
        d.line(x1, y, x2, y, lw=LW_THIN)
        # 箭头
        self._arrow_h(x1, y, 1)
        self._arrow_h(x2, y, -1)
        # 文字
        tx = (x1 + x2) / 2
        d.text(tx, y + 0.9 * (1 if side > 0 else 1), dim.text, size=2.6, ha="center", va="bottom", white=True)

    def _draw_v(self, dim: _Dim, level: int, side: int):
        d = self.d
        offset = 7.0 + level * 6.5
        x = dim.feature + side * offset
        y1, y2 = dim.a1, dim.a2
        d.line(dim.feature + side * 1.5, y1, x + side * 1.8, y1, lw=LW_THIN)
        d.line(dim.feature + side * 1.5, y2, x + side * 1.8, y2, lw=LW_THIN)
        d.line(x, y1, x, y2, lw=LW_THIN)
        self._arrow_v(x, y1, -1)
        self._arrow_v(x, y2, 1)
        tx = x + side * 1.0
        d.text(tx, (y1 + y2) / 2, dim.text, size=2.6, ha="center", va="center", rotation=90, white=True)

    def _arrow_h(self, x: float, y: float, direction: int):
        # direction=-1: 箭头指向右；1: 指向左
        self.d.poly(
            [(x, y), (x + direction * 2.2, y + 0.38), (x + direction * 2.2, y - 0.38)],
            lw=0.05,
            fill=True,
        )

    def _arrow_v(self, x: float, y: float, direction: int):
        self.d.poly(
            [(x, y), (x + 0.38, y + direction * 2.2), (x - 0.38, y + direction * 2.2)],
            lw=0.05,
            fill=True,
        )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def build_drawing(
    job: Job,
    lib: Library,
    fig: Figure | None = None,
    show_cutout_sizes: bool = True,
) -> tuple[Figure, dict[str, Any]]:
    """生成图纸。

    ``fig`` 传入已有 Figure 时会复用该对象（清空后重新绘制），供 GUI 预览
    避免每次刷新都销毁/重建 Tk 画布控件（那样会闪屏）。
    ``show_cutout_sizes=False`` 时不绘制开孔尺寸标注，适合孔位很密时临时关闭。
    """
    setup_fonts()
    issues = validate(job, lib)
    layout, lissues = choose_layout(job)
    issues.extend(lissues)

    if fig is None:
        fig = Figure(figsize=(layout.pw / 25.4, layout.ph / 25.4), dpi=100)
    else:
        fig.clear()
        fig.set_size_inches(layout.pw / 25.4, layout.ph / 25.4, forward=False)
    fig.patch.set_facecolor("white")
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, layout.pw)
    ax.set_ylim(0, layout.ph)
    ax.set_aspect("equal")
    ax.axis("off")
    d = Drawing(fig, ax)

    _draw_frame(d, layout)
    _draw_title_block(d, layout, job)
    _draw_tech_req(d, layout, job, lib, issues)

    resolved = lib.resolve_all(job)
    # 编号必须与 GUI 表格行号、校验信息一致，统一按用户录入顺序编号。
    numbered = list(enumerate(resolved, 1))

    placements = _draw_views(
        d, layout, job, lib, numbered, issues, show_cutout_sizes=show_cutout_sizes
    )
    _draw_hole_table(d, layout, numbered, issues)
    _draw_bom(d, layout, job, lib, issues)
    _draw_rail_table(d, layout, job, lib, issues)

    meta = {
        "layout": layout,
        "issues": issues,
        "page": layout.page,
        "scale": f"1:{fmt(layout.den)}",
        "placements": placements,
        "cutouts": numbered,
        "callouts_debug": d.__dict__.get("_callout_debug", []),
    }
    return fig, meta


def _draw_frame(d: Drawing, layout: Layout):
    d.rect(10, 10, layout.pw - 20, layout.ph - 20, lw=LW_MID)
    d.rect(layout.inner.x0, layout.inner.y0, layout.inner.w, layout.inner.h, lw=LW_THICK)
    # 对中标记（画在内外图框之间）
    d.line(layout.pw / 2, 10, layout.pw / 2, 15, lw=LW_THIN)
    d.line(layout.pw / 2, layout.ph - 10, layout.pw / 2, layout.ph - 12, lw=LW_THIN)
    d.line(10, layout.ph / 2, 25, layout.ph / 2, lw=LW_THIN)
    d.line(layout.pw - 10, layout.ph / 2, layout.pw - 12, layout.ph / 2, lw=LW_THIN)


def _draw_title_block(d: Drawing, layout: Layout, job: Job):
    b = layout.title
    sh = job.sheet
    d.rect(b.x0, b.y0, b.w, b.h, lw=LW_THICK)
    # 行分割
    y_company = b.y1 - b.h * 0.21
    y_project = b.y1 - b.h * 0.47
    y_info = b.y1 - b.h * 0.73
    d.line(b.x0, y_company, b.x1, y_company, lw=LW_MID)
    d.line(b.x0, y_project, b.x1, y_project, lw=LW_MID)
    d.line(b.x0, y_info, b.x1, y_info, lw=LW_MID)

    company = sh.company or ""
    d.text(b.cx, (b.y1 + y_company) / 2, company, size=4.0, ha="center", va="center")

    # 项目名称 / 图号
    split = b.x0 + b.w * 0.62
    d.line(split, y_company, split, y_project, lw=LW_MID)
    _cell(d, b.x0, y_project, split - b.x0, y_company - y_project, "项目名称", sh.project, 2.9)
    _cell(d, split, y_project, b.x1 - split, y_company - y_project, "图号", sh.drawing_no, 2.9)

    # 比例 / 版本 / 日期 / 图幅
    cells = [
        ("比例", f"1:{fmt(layout.den)}"),
        ("版本", sh.version),
        ("日期", sh.effective_date()),
        ("图幅", layout.page),
    ]
    x = b.x0
    cw = b.w / 4
    for i, (label, value) in enumerate(cells):
        if i:
            d.line(x, y_info, x, y_project, lw=LW_MID)
        _cell(d, x, y_info, cw, y_project - y_info, label, value, 2.8)
        x += cw

    # 设计 / 审核 / 张数
    cells2 = [("设计", sh.designer or ""), ("审核", sh.checker or ""), ("张数", "第1张 共1张")]
    x = b.x0
    cw2 = b.w / 3
    for i, (label, value) in enumerate(cells2):
        if i:
            d.line(x, b.y0, x, y_info, lw=LW_MID)
        _cell(d, x, b.y0, cw2, y_info - b.y0, label, value, 2.8)
        x += cw2


def _cell(d: Drawing, x, y, w, h, label, value, value_size=2.8):
    d.text(x + 1.5, y + h - 1.2, label, size=2.0, ha="left", va="top")
    text, sz = _fit_cell(str(value), w - 3.0, value_size, min_size=2.0)
    d.text(x + 2.2, y + h * 0.32, text, size=sz, ha="left", va="center")


def _draw_tech_req(d: Drawing, layout: Layout, job: Job, lib: Library, issues: list[Issue]):
    b = layout.tech
    items = lib.requirement_texts(job)
    d.text(b.x0, b.y1 - 1.0, "技术要求", size=4.2, ha="left", va="top")
    if not items:
        return

    width = b.w
    if width >= 300:
        ncols = max(1, min(4, int(width // 105)))
    else:
        ncols = 2 if width >= 180 else 1
    col_gap = 6.0
    col_w = (width - col_gap * (ncols - 1)) / ncols
    avail_h = b.h - 9.0
    top = b.y1 - 8.0
    max_w = col_w - 2.0

    def wrap_item(n: int, text: str, font: float) -> list[str]:
        prefix = f"{n}. "
        indent = "   "
        lines: list[str] = []
        remaining = text
        first = True
        while remaining:
            head = prefix if first else indent
            avail = max_w - text_width_mm(head, font)
            line = ""
            for ch in remaining:
                if line and text_width_mm(line + ch, font) > avail:
                    break
                line += ch
            if not line:
                line = remaining[0]
            lines.append(head + line)
            remaining = remaining[len(line):]
            first = False
        return lines

    def build(font: float, line_h: float):
        wrapped = [wrap_item(n, item, font) for n, item in enumerate(items, 1)]
        total = sum(len(x) for x in wrapped)
        target = max(1, math.ceil(total / ncols))
        cols: list[list[str]] = [[] for _ in range(ncols)]
        ci = 0
        for lines in wrapped:
            if ci < ncols - 1 and cols[ci] and len(cols[ci]) + len(lines) > target:
                ci += 1
            cols[ci].extend(lines)
        return cols, max((len(c) for c in cols), default=0) * line_h

    font_mm, line_h = 2.85, 4.75
    cols, used = build(font_mm, line_h)
    for font_mm, line_h in ((2.6, 4.35), (2.35, 4.0), (2.1, 3.7)):
        if used <= avail_h:
            break
        cols, used = build(font_mm, line_h)
    if used > avail_h:
        issues.append(Issue("warn", "技术要求文字较多，已压缩排版；建议改用“精简版”或减少自定义条目。"))

    for ci, col in enumerate(cols):
        cx = b.x0 + ci * (col_w + col_gap)
        for li, text in enumerate(col):
            d.text(cx, top - (li + 0.5) * line_h, text, size=font_mm, ha="left", va="center")


def _draw_views(
    d: Drawing,
    layout: Layout,
    job: Job,
    lib: Library,
    numbered: list[tuple[int, ResolvedCutout]],
    issues: list[Issue],
    show_cutout_sizes: bool = True,
) -> dict[str, Box]:
    cab = job.cabinet
    views = set(job.views)
    s = layout.s
    c = layout.content
    gap = 22.0
    placements: dict[str, Box] = {}

    row2_h = cab.plate_h * s if "internal" in views else 0.0
    row2_bottom = c.y0 + (28.0 if row2_h else 0.0)
    row2_top = row2_bottom + row2_h
    row1_bottom = row2_top + (32.0 if row2_h else 28.0)

    widths = []
    if "front" in views:
        widths.append(cab.W)
    if "side" in views:
        widths.append(cab.D)
    if "plan" in views:
        widths.append(cab.W)
    row1_w = sum(widths) * s + gap * max(0, len(widths) - 1)
    row1_h = row1_model_height(cab, views) * s
    row1_top = row1_bottom + row1_h
    if row1_top > c.y1 + 0.1:
        issues.append(Issue("warn", "视图高度超出内容区，请增大图幅或减小比例。"))
    if row1_w + (30.0 if widths else 0.0) > c.w + 0.1:
        issues.append(Issue("warn", "视图宽度超出内容区，请增大图幅或减小比例。"))

    # 左侧要给孔编号留 26mm 带
    x_start = c.x0 + (c.w - row1_w) / 2
    x_start = max(x_start, c.x0 + 30.0)
    y0 = row1_bottom
    x = x_start

    if "front" in views:
        fg = front_geometry(cab)
        vp = VP(d, s, x, y0)
        fh = cab.front_dev_h
        placements["front"] = Box(x, y0, x + cab.W * s, y0 + fh * s)
        _draw_front(d, vp, cab, fg, numbered, show_cutout_sizes)
        title = "正视图（斜面展开）" if cab.is_console else "正视图"
        _view_title(d, vp, cab.W / 2, -24.0, title)
        x += cab.W * s + gap
    if "side" in views:
        vp = VP(d, s, x, y0)
        placements["side"] = Box(x, y0, x + cab.D * s, y0 + cab.H * s)
        _draw_side(d, vp, cab, numbered, show_cutout_sizes)
        _view_title(d, vp, cab.D / 2, -24.0, "侧视图")
        x += cab.D * s + gap
    if "plan" in views:
        vp = VP(d, s, x, y0)
        placements["plan"] = Box(x, y0, x + cab.W * s, y0 + cab.D * s)
        _draw_plan(d, vp, cab, numbered, show_cutout_sizes)
        _view_title(d, vp, cab.W / 2, -24.0, "俯视图")

    if "internal" in views:
        lay = internal_layout(cab, job.rails, job.ducts, lib)
        ix = x_start if widths else c.x0 + (c.w - lay.plate_w * s) / 2
        ivp = VP(d, s, ix, row2_bottom)
        placements["internal"] = Box(ix, row2_bottom, ix + lay.plate_w * s, row2_bottom + lay.plate_h * s)
        _draw_internal(d, ivp, cab, lay, issues)
        _view_title(d, ivp, lay.plate_w / 2, -24.0, "安装板布置图")

    return placements


def _view_title(d: Drawing, vp: VP, x_model: float, dy: float, title: str):
    d.text(vp.X(x_model), vp.Y(0) + dy, f"{title}  1:{fmt(1 / vp.s)}", size=4.0, ha="center", va="top")


# ---------------------------------------------------------------------------
# 各视图
# ---------------------------------------------------------------------------

def _draw_front(
    d: Drawing, vp: VP, cab: Cabinet, fg: FrontGeometry, numbered,
    show_cutout_sizes: bool = True,
):
    # 外框与底座：琴柜正面按“门板 + 斜面展开”绘制，高度用 front_dev_h。
    vp.rect(0, 0, cab.W, cab.front_dev_h, lw=LW_THICK)
    if cab.base_height > 0:
        vp.line(0, cab.base_height, cab.W, cab.base_height, lw=LW_MID)
        vp.line(80, 0, 80, cab.base_height, lw=LW_THIN)
        vp.line(cab.W - 80, 0, cab.W - 80, cab.base_height, lw=LW_THIN)

    # 门板
    for leaf in fg.leaves:
        b = leaf.box
        vp.rect(b.x0, b.y0, b.w, b.h, lw=LW_MID)
    if fg.gap_center is not None:
        vp.line(
            fg.gap_center,
            fg.door_b,
            fg.gap_center,
            fg.door_t,
            lw=LW_THIN,
            dash=(6, 1.5),
        )

    if cab.is_console:
        fold = cab.console_desk_h
        # 折弯线：下面是门板，上面是沿斜面展开的操作面板。
        vp.line(cab.door_left, fold, cab.door_right, fold, lw=LW_THIN, dash=(9, 2, 2, 2))
        vp.line(cab.door_left, fold, cab.door_left, cab.front_dev_h, lw=LW_THIN)
        vp.line(cab.door_right, fold, cab.door_right, cab.front_dev_h, lw=LW_THIN)
        vp.text(
            cab.W / 2, fold + (cab.front_dev_h - fold) / 2,
            "斜面操作面板（展开）", size=2.3,
        )
        vp.text(
            cab.W / 2, fold - 9.0,
            f"折弯线  斜面展开长 {fmt(cab.console_panel_len)}", size=2.0,
        )

    # 五金
    for hw in fg.hardware:
        if hw.kind == "rect":
            vp.rect(hw.x, hw.y, hw.w, hw.h, lw=LW_THIN)
            if hw.role == "handle":
                for k in range(1, 4):
                    yy = hw.y + hw.h * k / 4
                    vp.line(hw.x + 2, yy, hw.x + hw.w - 2, yy, lw=LW_THIN)
        elif hw.kind == "circle":
            vp.circle(hw.x, hw.y, hw.d, lw=LW_THIN)

    # 开孔（正视图只画正面门板上的孔）
    callouts = []
    for num, rc in numbered:
        if rc.face != "front":
            continue
        ob = box_from_geom(rc.outline, rc.x, rc.y)
        cb = box_from_geom(rc.cutout, rc.x, rc.y)
        ow, oh = geom_size(rc.outline)
        cw, ch = geom_size(rc.cutout)
        if abs(ow - cw) > 0.01 or abs(oh - ch) > 0.01:
            _draw_geom(d, vp, rc.outline, rc.x, rc.y, lw=LW_THIN, dash=(4, 1.8))
        _draw_geom(d, vp, rc.cutout, rc.x, rc.y, lw=LW_MID)
        # 中心十字
        half_w, half_h = cb.w / 2, cb.h / 2
        ext = 4.0
        vp.line(cb.cx - half_w - ext, cb.cy, cb.cx + half_w + ext, cb.cy,
                lw=0.10, dash=(4, 1.5))
        vp.line(cb.cx, cb.cy - half_h - ext, cb.cx, cb.cy + half_h + ext,
                lw=0.10, dash=(4, 1.5))
        callouts.append((num, rc, cb))

    # 孔编号引线
    callout_pos = _draw_callouts(
        d, vp, cab, callouts, numbered,
        view_w=cab.W, view_h=cab.front_dev_h,
    )
    # 开孔尺寸（Φ22.5 / 192×138 等），放不下的在视图下方汇总
    if show_cutout_sizes:
        unplaced = _draw_cutout_sizes(
            d, vp, callouts, callout_pos,
            view_w=cab.W, view_h=cab.front_dev_h, fg=fg,
        )
        _draw_view_note(d, vp, cab.W / 2, -30.0, unplaced, max_width=cab.W * vp.s)

    # 尺寸
    dim = DimEngine(d, vp)
    if cab.double_door and len(fg.leaves) == 2:
        l1, l2 = fg.leaves
        dim.add_h(l1.box.x0, l1.box.x1, 0, -1, fmt(l1.box.w))
        dim.add_h(l2.box.x0, l2.box.x1, 0, -1, fmt(l2.box.w))
        dim.add_h(0, cab.W, 0, -1, fmt(cab.W))
    else:
        dim.add_h(0, cab.W, 0, -1, fmt(cab.W))
    dim.add_v(0, cab.base_height, 0, -1, fmt(cab.base_height)) if cab.base_height > 0 else None
    if cab.is_console:
        dim.add_v(0, cab.console_desk_h, 0, -1, fmt(cab.console_desk_h))
        dim.add_v(cab.console_desk_h, cab.front_dev_h, 0, -1, fmt(cab.console_panel_len))
        dim.add_v(0, cab.front_dev_h, 0, -1, fmt(cab.front_dev_h))
    else:
        dim.add_v(0, cab.H, 0, -1, fmt(cab.H))
    dim.render()


def _draw_geom(d: Drawing, vp: VP, geom: dict, cx: float, cy: float, lw=LW_MID, dash=None):
    t = geom.get("type", "rect")
    if t == "circle":
        vp.circle(cx, cy, float(geom.get("d", 0)), lw=lw, dash=dash)
    elif t == "rect":
        vp.round_rect(
            cx,
            cy,
            float(geom.get("w", 0)),
            float(geom.get("h", 0)),
            float(geom.get("r", 0) or 0),
            lw=lw,
            dash=dash,
        )
    elif t == "slot":
        w = float(geom.get("w", 0))
        h = float(geom.get("h", 0))
        # 腰型孔 = 两端半圆的跑道形，等价于圆角半径 min(w,h)/2 的圆角矩形
        vp.round_rect(cx, cy, w, h, min(w, h) / 2, lw=lw, dash=dash)
    elif t == "polygon":
        pts = [(vp.X(cx + float(p[0])), vp.Y(cy + float(p[1]))) for p in geom.get("points", [])]
        d.poly(pts, lw=lw)


def _rect_hit(a, b, margin=0.0) -> bool:
    return not (
        a[2] + margin <= b[0]
        or b[2] + margin <= a[0]
        or a[3] + margin <= b[1]
        or b[3] + margin <= a[1]
    )


def _seg_hits_rects(p1, p2, rects, samples=18) -> bool:
    for i in range(1, samples + 1):
        t = i / samples
        x = p1[0] + (p2[0] - p1[0]) * t
        y = p1[1] + (p2[1] - p1[1]) * t
        for r in rects:
            if r[0] <= x <= r[2] and r[1] <= y <= r[3]:
                return True
    return False


def _draw_callouts(d: Drawing, vp: VP, cab: Cabinet, callouts, numbered,
                   *, view_w: float | None = None, view_h: float | None = None,
                   hardware: bool = True) -> list[tuple[int, float, float]]:
    """孔编号：成排孔优先在排的上/下方成排放置，其余在孔附近找空位，最后才贴边兜底.

    返回已放置的编号圈位置 ``[(编号, 圆心 x, 圆心 y), ...]``，供尺寸标注避让。
    """
    placed_callouts: list[tuple[int, float, float]] = []
    r = 2.55
    view_w = cab.W if view_w is None else view_w
    view_h = cab.H if view_h is None else view_h
    fg = front_geometry(cab) if hardware else None

    items: list[tuple[int, ResolvedCutout, tuple[float, float, float, float]]] = []
    hole_boxes: list[tuple[float, float, float, float]] = []
    for num, rc, _cb in callouts:
        bx0, by0, bx1, by1 = geom_bbox(rc.outline, rc.x, rc.y)
        pb = (vp.X(bx0), vp.Y(by0), vp.X(bx1), vp.Y(by1))
        items.append((num, rc, pb))
        hole_boxes.append(pb)

    hardware_boxes: list[tuple[float, float, float, float]] = []
    if fg is not None:
        for hw in fg.hardware:
            if hw.kind == "rect":
                hardware_boxes.append((vp.X(hw.x), vp.Y(hw.y), vp.X(hw.x + hw.w), vp.Y(hw.y + hw.h)))
            elif hw.kind == "circle":
                hardware_boxes.append(
                    (vp.X(hw.x - hw.d / 2), vp.Y(hw.y - hw.d / 2), vp.X(hw.x + hw.d / 2), vp.Y(hw.y + hw.d / 2))
                )
    gap_boxes: list[tuple[float, float, float, float]] = []
    if fg is not None and fg.gap_center is not None:
        gx = vp.X(fg.gap_center)
        gap_boxes.append((gx - 1.0, vp.Y(fg.door_b), gx + 1.0, vp.Y(fg.door_t)))

    bounds = (vp.X(0) + r + 0.5, vp.Y(0) + r + 0.5, vp.X(view_w) - r - 0.5, vp.Y(view_h) - r - 0.5)
    static_obstacles = hardware_boxes + gap_boxes
    placed_labels: list[tuple[float, float]] = []
    done: set[int] = set()

    def inside(lb) -> bool:
        return bounds[0] <= lb[0] and lb[2] <= bounds[2] and bounds[1] <= lb[1] and lb[3] <= bounds[3]

    def free(lb, local_labels) -> bool:
        if not inside(lb):
            return False
        for ob in hole_boxes:
            if _rect_hit(lb, ob, 0.35):
                return False
        for ob in static_obstacles:
            if _rect_hit(lb, ob, 0.5):
                return False
        for px, py in placed_labels:
            if _rect_hit(lb, (px - r, py - r, px + r, py + r), 0.25):
                return False
        for px, py in local_labels:
            if _rect_hit(lb, (px - r, py - r, px + r, py + r), 0.25):
                return False
        return True

    # 1) 成排孔：在整排的上方或下方成排放编号
    by_y = sorted(items, key=lambda t: -((t[2][1] + t[2][3]) / 2))
    groups: list[tuple[float, list]] = []
    for it in by_y:
        cy = (it[2][1] + it[2][3]) / 2
        if groups and abs(cy - groups[-1][0]) <= 0.8:
            groups[-1][1].append(it)
        else:
            groups.append((cy, [it]))

    for _cy, group in groups:
        if len(group) < 3:
            continue
        row_bottom = min(t[2][1] for t in group)
        row_top = max(t[2][3] for t in group)
        sides = []
        if row_top + 1.2 + 2 * r <= bounds[3]:
            sides.append(1)
        if row_bottom - 1.2 - 2 * r >= bounds[1]:
            sides.append(-1)
        sides.sort(
            key=lambda sd: (bounds[3] - row_top) if sd > 0 else (row_bottom - bounds[1]),
            reverse=True,
        )
        best: list[tuple[int, tuple[float, float, float, float]]] = []
        for sd in sides:
            band = row_top + 1.2 + r if sd > 0 else row_bottom - 1.2 - r
            local: list[tuple[float, float]] = []
            result = []
            for num, rc, pb in sorted(group, key=lambda t: (t[2][0] + t[2][2]) / 2):
                cx = min(max((pb[0] + pb[2]) / 2, bounds[0]), bounds[2])
                for dy in (0.0, 3.4, -3.4, 6.8, -6.8):
                    ly = band + dy
                    lb = (cx - r, ly - r, cx + r, ly + r)
                    if not free(lb, local):
                        continue
                    ty = pb[3] if sd > 0 else pb[1]
                    tx = min(max(cx, pb[0]), pb[2])
                    result.append((num, (cx, ly, tx, ty)))
                    local.append((cx, ly))
                    break
            if len(result) > len(best):
                best = result
        for num, pos in best:
            _draw_one_callout(d, num, r, pos)
            placed_callouts.append((num, pos[0], pos[1]))
            placed_labels.append((pos[0], pos[1]))
            done.add(num)

    # 2) 其余孔：在孔附近（距离不超过约 7mm）找空位
    rest = sorted(
        [t for t in items if t[0] not in done],
        key=lambda t: -((t[2][2] - t[2][0]) * (t[2][3] - t[2][1])),
    )
    for num, rc, pb in rest:
        cx = (pb[0] + pb[2]) / 2
        cy = (pb[1] + pb[3]) / 2
        candidates: list[tuple[float, float]] = []
        for g in (2.5, 4.5, 6.5):
            candidates.extend(
                [
                    (pb[2] + g + r, cy),
                    (pb[0] - g - r, cy),
                    (cx, pb[3] + g + r),
                    (cx, pb[1] - g - r),
                    (pb[2] + g + r, pb[3] + g + r),
                    (pb[0] - g - r, pb[3] + g + r),
                    (pb[2] + g + r, pb[1] - g - r),
                    (pb[0] - g - r, pb[1] - g - r),
                ]
            )
        chosen = None
        for lx, ly in candidates:
            lb = (lx - r, ly - r, lx + r, ly + r)
            if not free(lb, []):
                continue
            tx = min(max(lx, pb[0]), pb[2])
            ty = min(max(ly, pb[1]), pb[3])
            dx, dy = tx - lx, ty - ly
            dist = math.hypot(dx, dy) or 1.0
            start = (lx + dx / dist * r, ly + dy / dist * r)
            others = [ob for ob, it in zip(hole_boxes, items) if it[0] != num]
            if _seg_hits_rects(start, (tx, ty), [(o[0] - 0.3, o[1] - 0.3, o[2] + 0.3, o[3] + 0.3) for o in others]):
                continue
            chosen = (lx, ly, tx, ty)
            break
        if chosen:
            _draw_one_callout(d, num, r, chosen)
            placed_callouts.append((num, chosen[0], chosen[1]))
            placed_labels.append((chosen[0], chosen[1]))
            done.add(num)

    # 3) 兜底：贴视图左右两侧排布
    left = [t for t in items if t[0] not in done and t[1].x < view_w / 2]
    right = [t for t in items if t[0] not in done and t[1].x >= view_w / 2]
    lo = vp.Y(0) + 3.0
    hi = vp.Y(view_h) - 3.0

    def side_place(rows, label_x):
        if not rows:
            return
        rows = sorted(rows, key=lambda t: (t[2][1] + t[2][3]) / 2)
        desired = [(t[2][1] + t[2][3]) / 2 for t in rows]
        gap = 6.6
        if len(rows) > 1 and (hi - lo) / (len(rows) - 1) < gap:
            gap = max(3.8, (hi - lo) / (len(rows) - 1))
        ys = [min(max(y, lo), hi) for y in desired]
        for i in range(1, len(ys)):
            ys[i] = max(ys[i], ys[i - 1] + gap)
        if ys[-1] > hi:
            ys[-1] = hi
            for i in range(len(ys) - 2, -1, -1):
                ys[i] = min(ys[i], ys[i + 1] - gap)
        if ys[0] < lo:
            ys[0] = lo
            for i in range(1, len(ys)):
                ys[i] = max(ys[i], ys[i - 1] + gap)
        for (num, rc, pb), ylab in zip(rows, ys):
            tx = pb[0] if label_x < pb[0] else pb[2]
            ty = min(max(ylab, pb[1]), pb[3])
            _draw_one_callout(d, num, r, (label_x, ylab, tx, ty))
            placed_callouts.append((num, label_x, ylab))

    side_place(left, vp.X(0) - 20.0)
    side_place(right, vp.X(view_w) + 12.0)
    return placed_callouts


def _draw_one_callout(d: Drawing, num: int, r: float, pos):
    lx, ly, tx, ty = pos
    d.__dict__.setdefault("_callout_debug", []).append((num, lx, ly, tx, ty))
    dx, dy = tx - lx, ty - ly
    dist = math.hypot(dx, dy) or 1.0
    d.line(lx + dx / dist * r, ly + dy / dist * r, tx, ty, lw=0.13)
    d.circle(tx, ty, 0.7, lw=0.05)
    d.circle(lx, ly, 2 * r, lw=LW_THIN)
    d.text(lx, ly, str(num), size=2.35, ha="center", va="center")


def _cutout_size_text(rc: ResolvedCutout) -> str:
    """开孔尺寸标注文字：圆孔 Φ22.5，方孔 192×138，腰型孔 220×40。"""
    spec = rc.spec
    if spec.startswith("腰型 "):
        spec = spec[3:]
    if " R" in spec:
        spec = spec.split(" R", 1)[0]
    return spec


def _draw_cutout_sizes(
    d: Drawing,
    vp: VP,
    items,
    callout_positions: list[tuple[int, float, float]],
    *,
    view_w: float,
    view_h: float,
    fg=None,
) -> list[tuple[int, ResolvedCutout, Any]]:
    """在每个开孔旁标开孔尺寸；放不下的孔返回给视图下方汇总标注。

    小孔（Φ22/Φ30 等）用竖排文字，避免在密集孔排里互相压字；
    大孔（触摸屏/仪表/过滤网/腰型孔）用横排文字。
    """
    font = 1.75
    margin = 0.5
    obstacles: list[tuple[float, float, float, float]] = []
    # 所有孔的外形轮廓
    for _num, rc, cb in items:
        ox0, oy0, ox1, oy1 = geom_bbox(rc.outline, rc.x, rc.y)
        obstacles.append(
            (vp.X(ox0) - 0.35, vp.Y(oy0) - 0.35, vp.X(ox1) + 0.35, vp.Y(oy1) + 0.35)
        )
    # 编号圈（编号引线本身不再避让，否则小孔处几乎没有空位）
    for _num, lx, ly in callout_positions:
        obstacles.append((lx - 2.75, ly - 2.75, lx + 2.75, ly + 2.75))
    # 门板五金
    if fg is not None:
        for hw in fg.hardware:
            if hw.kind == "rect":
                obstacles.append(
                    (vp.X(hw.x), vp.Y(hw.y), vp.X(hw.x + hw.w), vp.Y(hw.y + hw.h))
                )
            elif hw.kind == "circle":
                obstacles.append(
                    (
                        vp.X(hw.x - hw.d / 2), vp.Y(hw.y - hw.d / 2),
                        vp.X(hw.x + hw.d / 2), vp.Y(hw.y + hw.d / 2),
                    )
                )

    bounds = (vp.X(0) + margin, vp.Y(0) + margin,
              vp.X(view_w) - margin, vp.Y(view_h) - margin)
    placed: list[tuple[float, float, float, float]] = []

    def free(box) -> bool:
        if box[0] < bounds[0] or box[1] < bounds[1] or box[2] > bounds[2] or box[3] > bounds[3]:
            return False
        for ob in obstacles:
            if _rect_hit(box, ob, 0.25):
                return False
        for ob in placed:
            if _rect_hit(box, ob, 0.35):
                return False
        return True

    def place(text: str, dx0: float, dy0: float, dx1: float, dy1: float,
              rotated: bool) -> bool:
        tw = text_width_mm(text, font)
        if rotated:
            bw, bh = font * 0.95 + 0.35, tw + 0.55
        else:
            bw, bh = tw + 0.7, font * 1.3 + 0.35
        dcx, dcy = (dx0 + dx1) / 2.0, (dy0 + dy1) / 2.0
        candidates = [
            (dcx, dy1 + 1.5 + bh / 2),
            (dcx, dy0 - 1.5 - bh / 2),
            (dx1 + 1.5 + bw / 2, dcy),
            (dx0 - 1.5 - bw / 2, dcy),
        ]
        for cx, cy in candidates:
            box = (cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2)
            if not free(box):
                continue
            d.text(
                cx, cy, text, size=font, ha="center", va="center",
                rotation=90 if rotated else 0, white=True,
            )
            placed.append(box)
            return True
        return False

    unplaced: list[tuple[int, ResolvedCutout, Any]] = []

    # 大孔（触摸屏/仪表/过滤网/腰型孔）：逐个横排标注
    large, small = [], []
    for item in items:
        (small if max(item[2].w, item[2].h) * vp.s < 14.0 else large).append(item)
    for num, rc, cb in sorted(large, key=lambda t: -(t[2].w * t[2].h)):
        text = _cutout_size_text(rc)
        if not text:
            continue
        if not place(text, vp.X(cb.x0), vp.Y(cb.y0), vp.X(cb.x1), vp.Y(cb.y1), False):
            unplaced.append((num, rc, cb))

    # 小孔（Φ22/Φ30 按钮、指示灯、开关）：同一排同规格只标一次，如 Φ22.5×6，
    # 避免密集孔排里每个孔都挤一段文字。落单的孔仍逐个标注。
    grouped: dict[tuple[int, str], list] = {}
    for item in small:
        spec = _cutout_size_text(item[1])
        if not spec:
            continue
        key = (round(item[2].cy / 10.0), spec)
        grouped.setdefault(key, []).append(item)
    groups: list[list] = []
    for _key, row_items in grouped.items():
        row_items.sort(key=lambda t: t[2].cx)
        cur = [row_items[0]]
        for item in row_items[1:]:
            # 同一排但跨越双开门/距离很远时不要合并成一组
            if item[2].cx - cur[-1][2].cx <= 300.0:
                cur.append(item)
            else:
                groups.append(cur)
                cur = [item]
        groups.append(cur)
    for group in groups:
        spec = _cutout_size_text(group[0][1])
        if len(group) == 1:
            num, rc, cb = group[0]
            ok = place(spec, vp.X(cb.x0), vp.Y(cb.y0), vp.X(cb.x1), vp.Y(cb.y1), True)
        else:
            xs, ys = [], []
            for _num, _rc, cb in group:
                xs.extend((vp.X(cb.x0), vp.X(cb.x1)))
                ys.extend((vp.Y(cb.y0), vp.Y(cb.y1)))
            ok = place(
                f"{spec}×{len(group)}",
                min(xs), min(ys), max(xs), max(ys), False,
            )
        if not ok:
            unplaced.extend(group)
    return unplaced


def _size_summary(items) -> list[str]:
    """按尺寸归并，保持首次出现顺序：Φ22.5×13、192×138。"""
    counts: dict[str, int] = {}
    for _num, rc, _cb in items:
        text = _cutout_size_text(rc)
        if text:
            counts[text] = counts.get(text, 0) + 1
    return [f"{spec}×{n}" if n > 1 else spec for spec, n in counts.items()]


def _draw_view_note(d: Drawing, vp: VP, x_model: float, dy: float,
                    items, max_width: float) -> None:
    """视图标题下方的开孔尺寸汇总（只用于个别放不下尺寸文字的开孔）。"""
    if not items:
        return
    prefix = "开孔尺寸："
    tokens = _size_summary(items)
    if not tokens:
        return
    lines: list[str] = []
    cur = prefix
    for tok in tokens:
        sep = "" if cur == prefix else "  "
        if max_width > 0 and text_width_mm(cur + sep + tok, 1.95) > max_width and cur != prefix:
            lines.append(cur)
            cur = tok
        else:
            cur += sep + tok
    lines.append(cur)
    for i, line in enumerate(lines):
        d.text(
            vp.X(x_model), vp.Y(0) + dy - i * 2.6, line,
            size=1.95, ha="center", va="top",
        )


def _draw_face_hole(d: Drawing, vp: VP, rc: ResolvedCutout, dash=None):
    """在侧视图/俯视图上画一个开孔及中心十字，返回孔的外包框。"""
    cb = box_from_geom(rc.cutout, rc.x, rc.y)
    _draw_geom(d, vp, rc.cutout, rc.x, rc.y, lw=LW_MID, dash=dash)
    ext = 4.0
    vp.line(cb.x0 - ext, cb.cy, cb.x1 + ext, cb.cy, lw=0.10, dash=(4, 1.5))
    vp.line(cb.cx, cb.y0 - ext, cb.cx, cb.y1 + ext, lw=0.10, dash=(4, 1.5))
    return cb


def _draw_side(
    d: Drawing, vp: VP, cab: Cabinet, numbered, show_cutout_sizes: bool = True
):
    if cab.is_console:
        # 琴柜侧视轮廓：下柜 + 斜面操作面板 + 后部平顶。
        run = cab.D - cab.console_top_depth
        pts = [
            (vp.X(0), vp.Y(0)),
            (vp.X(0), vp.Y(cab.console_desk_h)),
            (vp.X(run), vp.Y(cab.H)),
            (vp.X(cab.D), vp.Y(cab.H)),
            (vp.X(cab.D), vp.Y(0)),
        ]
        d.poly(pts, lw=LW_THICK)
        vp.line(run, cab.H, cab.D, cab.H, lw=LW_MID)
        vp.line(0, cab.console_desk_h, run, cab.H, lw=LW_THICK)
        vp.text(
            run * 0.55, cab.console_desk_h + (cab.H - cab.console_desk_h) * 0.62,
            "斜面", size=2.2,
        )
        vp.text((run + cab.D) / 2, cab.H - 7, "平顶", size=2.0)
    else:
        vp.rect(0, 0, cab.D, cab.H, lw=LW_THICK)
        if cab.base_height > 0:
            vp.line(0, cab.base_height, cab.D, cab.base_height, lw=LW_MID)
    vp.line(cab.door_thickness, cab.door_lower, cab.door_thickness, cab.door_upper, lw=LW_MID)
    vp.text(cab.door_thickness + 6, (cab.door_lower + cab.door_upper) / 2, "门", size=2.2, ha="left")

    # 左侧板开孔实线，右侧板开孔虚线（侧视图上右侧被遮挡）
    callouts = []
    for num, rc in numbered:
        if rc.face not in ("left", "right"):
            continue
        cb = _draw_face_hole(d, vp, rc, dash=(4, 1.8) if rc.face == "right" else None)
        callouts.append((num, rc, cb))
    if callouts:
        callout_pos = _draw_callouts(d, vp, cab, callouts, numbered,
                                     view_w=cab.D, view_h=cab.H, hardware=False)
        if show_cutout_sizes:
            unplaced = _draw_cutout_sizes(
                d, vp, callouts, callout_pos,
                view_w=cab.D, view_h=cab.H,
            )
            _draw_view_note(d, vp, cab.D / 2, -30.0, unplaced, max_width=cab.D * vp.s)

    dim = DimEngine(d, vp)
    dim.add_h(0, cab.D, 0, -1, fmt(cab.D))
    dim.add_v(0, cab.H, cab.D, 1, fmt(cab.H))
    if cab.base_height > 0:
        dim.add_v(0, cab.base_height, 0, -1, fmt(cab.base_height))
    if cab.is_console:
        run = cab.D - cab.console_top_depth
        dim.add_v(0, cab.console_desk_h, 0, -1, fmt(cab.console_desk_h))
        dim.add_v(cab.console_desk_h, cab.H, run, 1, fmt(cab.console_rise))
        dim.add_h(cab.D - cab.console_top_depth, cab.D, cab.H, 1, fmt(cab.console_top_depth))
        dim.add_h(0, run, cab.console_desk_h, -1, fmt(run))
    dim.render()


def _draw_plan(
    d: Drawing, vp: VP, cab: Cabinet, numbered, show_cutout_sizes: bool = True
):
    vp.rect(0, 0, cab.W, cab.D, lw=LW_THICK)
    vp.line(0, cab.door_thickness, cab.W, cab.door_thickness, lw=LW_MID)
    if cab.double_door:
        fg = front_geometry(cab)
        if fg.gap_center is not None:
            vp.line(fg.gap_center, 0, fg.gap_center, cab.door_thickness, lw=LW_THIN)
    if cab.is_console:
        run = cab.D - cab.console_top_depth
        vp.line(0, run, cab.W, run, lw=LW_MID, dash=(8, 2))
        vp.text(cab.W / 2, run * 0.55, "斜面投影", size=2.2)
        vp.text(cab.W / 2, (run + cab.D) / 2, "后部平顶", size=2.2)
    # 底座轮廓
    if cab.base_height > 0:
        vp.line(40, cab.D - 40, cab.W - 40, cab.D - 40, lw=LW_THIN, dash=(4, 1.5))
        vp.line(40, 40, 40, cab.D - 40, lw=LW_THIN, dash=(4, 1.5))
        vp.line(cab.W - 40, 40, cab.W - 40, cab.D - 40, lw=LW_THIN, dash=(4, 1.5))

    # 顶板开孔实线，底板开孔虚线（俯视时底板被顶板遮挡）
    callouts = []
    for num, rc in numbered:
        if rc.face not in ("top", "bottom"):
            continue
        cb = _draw_face_hole(d, vp, rc, dash=(4, 1.8) if rc.face == "bottom" else None)
        callouts.append((num, rc, cb))
    if callouts:
        callout_pos = _draw_callouts(d, vp, cab, callouts, numbered,
                                     view_w=cab.W, view_h=cab.D, hardware=False)
        if show_cutout_sizes:
            unplaced = _draw_cutout_sizes(
                d, vp, callouts, callout_pos,
                view_w=cab.W, view_h=cab.D,
            )
            _draw_view_note(d, vp, cab.W / 2, -30.0, unplaced, max_width=cab.W * vp.s)

    dim = DimEngine(d, vp)
    dim.add_h(0, cab.W, 0, -1, fmt(cab.W))
    dim.add_v(0, cab.D, cab.W, 1, fmt(cab.D))
    dim.render()


def _draw_internal(d: Drawing, vp: VP, cab: Cabinet, lay: InternalLayout, issues: list[Issue]):
    vp.rect(0, 0, lay.plate_w, lay.plate_h, lw=LW_THICK)

    # 线槽：浅灰填充 + 加粗轮廓，和导轨、安装板区分开（原来细线太淡）
    duct_fill = "#c6c6c6"
    for box, duct in lay.ducts:
        vp.rect(box.x0, box.y0, box.w, box.h, lw=LW_MID, fill=duct_fill)
        if duct.orient == "h":
            ymid = box.cy
            vp.line(box.x0, ymid, box.x1, ymid, lw=0.18, dash=(5, 2))
            if box.w * vp.s >= 22 and box.h * vp.s >= 4:
                vp.text(box.cx, ymid, "线槽", size=1.9, white=True)
        else:
            xmid = box.cx
            vp.line(xmid, box.y0, xmid, box.y1, lw=0.18, dash=(5, 2))
            if box.h * vp.s >= 22 and box.w * vp.s >= 4:
                vp.text(xmid, box.cy, "线槽", size=1.9, rotation=90, white=True)

    # 导轨与器件
    for rl in lay.rails:
        vp.rect(rl.x, rl.y - 17.5, rl.length, 35, lw=LW_MID)
        vp.line(rl.x, rl.y, rl.x + rl.length, rl.y, lw=0.10, dash=(6, 1.5))
        # 端挡
        vp.rect(rl.x, rl.y - 22.5, 10, 45, lw=LW_THIN)
        vp.rect(rl.x + rl.length - 10, rl.y - 22.5, 10, 45, lw=LW_THIN)
        # 导轨编号（放在导轨内，避免与线槽/尺寸打架）
        vp.text(rl.x + 12, rl.y, rl.label, size=1.9, ha="left", white=True)
        for dp in rl.device_list:
            vp.rect(dp.box.x0, dp.box.y0, dp.box.w, dp.box.h, lw=LW_THIN)
            if dp.box.w * vp.s >= 5.0 and dp.box.h * vp.s >= 12.0:
                vp.text(dp.box.cx, dp.box.cy, dp.designation, size=1.7, rotation=90)

    dim = DimEngine(d, vp)
    dim.add_h(0, lay.plate_w, 0, -1, fmt(lay.plate_w))
    dim.add_v(0, lay.plate_h, 0, -1, fmt(lay.plate_h))
    ys = [0.0] + sorted(rl.y for rl in lay.rails) + [lay.plate_h]
    for a, b in zip(ys[:-1], ys[1:]):
        if b - a > 0.5:
            dim.add_v(a, b, lay.plate_w, 1, fmt(b - a))
    dim.render()


# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------

def _fit_cell(text: str, width: float, size: float, min_size: float = 1.85) -> tuple[str, float]:
    if text is None:
        text = ""
    text = str(text)
    while text_width_mm(text, size) > width - 1.6 and size > min_size:
        size *= 0.92
    if text_width_mm(text, size) <= width - 1.6:
        return text, size
    out = ""
    for ch in text:
        if text_width_mm(out + ch + "…", size) > width - 1.6:
            break
        out += ch
    return (out + "…") if out else "", size


def _draw_table(
    d: Drawing,
    x: float,
    y_top: float,
    widths: list[float],
    headers: list[str],
    rows: list[list[str]],
    title: str,
    note: str = "",
    y_limit: float = 70.0,
    aligns: list[str] | None = None,
    size: float = 2.35,
    row_h: float | None = None,
) -> float:
    total_w = sum(widths)
    d.text(x, y_top, title, size=3.0, ha="left", va="top")
    y = y_top - 5.2
    if note:
        d.text(x, y, note, size=1.95, ha="left", va="top")
        y -= 4.0
    header_h = 5.0
    n = len(rows)
    avail = y - y_limit - header_h
    if row_h is None:
        row_h = 5.0 if n == 0 else min(5.0, max(3.6, avail / n))
    table_h = header_h + n * row_h
    bottom = y - table_h
    d.rect(x, bottom, total_w, table_h, lw=LW_MID)
    d.line(x, y - header_h, x + total_w, y - header_h, lw=LW_THIN)
    # 列线
    xx = x
    for w in widths[:-1]:
        xx += w
        d.line(xx, bottom, xx, y, lw=LW_THIN)
    # 行线
    for i in range(1, n):
        yy = y - header_h - i * row_h
        d.line(x, yy, x + total_w, yy, lw=0.10)

    aligns = aligns or ["center"] * len(headers)
    xx = x
    for w, h in zip(widths, headers):
        text, sz = _fit_cell(h, w, 2.25)
        d.text(xx + w / 2, y - header_h / 2, text, size=sz, ha="center", va="center")
        xx += w

    for ri, row in enumerate(rows):
        yy = y - header_h - (ri + 0.5) * row_h
        xx = x
        for ci, (w, val) in enumerate(zip(widths, row)):
            al = aligns[ci]
            text, sz = _fit_cell(val, w, size)
            tx = xx + 1.2 if al == "left" else (xx + w - 1.2 if al == "right" else xx + w / 2)
            d.text(tx, yy, text, size=sz, ha=al, va="center")
            xx += w
    return bottom


def hole_rows(numbered) -> list[list[str]]:
    """开孔表行数据，供图纸表格和 CSV 导出共用。"""
    rows = []
    for num, rc in numbered:
        if rc.label and rc.note:
            code = f"{rc.label} / {rc.note}"
        else:
            code = rc.label or rc.note or ""
        rows.append([
            str(num), FACE_LABELS.get(rc.face, "正面"), rc.name, rc.spec,
            fmt(rc.x), fmt(rc.y), code,
        ])
    return rows


def _draw_hole_table(d: Drawing, layout: Layout, numbered, issues: list[Issue]):
    rows = hole_rows(numbered)
    widths = [9, 12, 48, 36, 18, 18, 27]
    # 适配右栏宽度
    scale_w = layout.right_w / sum(widths)
    widths = [w * scale_w for w in widths]
    bottom = _draw_table(
        d,
        layout.right_x,
        layout.inner.y1 - 2,
        widths,
        ["编号", "面", "名称", "开孔尺寸", "X", "Y", "代号/备注"],
        rows,
        f"开孔表（共 {len(rows)} 项）",
        "正面 X 距左/Y 距下；侧面 X 距前/Y 距下；顶底面 X 距左/Y 距前；单位 mm。",
        y_limit=70.0,
        aligns=["center", "center", "left", "center", "right", "right", "left"],
    )
    if len(rows) > 0 and bottom < 70:
        issues.append(Issue("warn", "开孔表行数过多，已压缩行高；建议拆图或减少开孔。"))
    layout_hole_bottom = bottom
    # 暂存，供 BOM 定位
    setattr(d, "_hole_bottom", layout_hole_bottom)


def bom_rows(job: Job, lib: Library) -> list[list[str]]:
    rows: list[list[str]] = []
    agg: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str, str]] = []
    for rail in job.rails:
        for item in rail.devices:
            dev = lib.device(item.model)
            name = item.name or str(dev.get("name", item.model))
            brand = item.brand or str(dev.get("brand", ""))
            spec = item.spec or str(dev.get("spec", item.model))
            key = (item.model, name, brand, spec)
            if key not in agg:
                agg[key] = {"name": name, "brand": brand, "spec": spec, "qty": 0}
                order.append(key)
            agg[key]["qty"] += max(0, int(item.qty))
    for key in order:
        a = agg[key]
        spec_text = f"{a['brand']} {a['spec']}".strip()
        rows.append([str(len(rows) + 1), str(a["name"]), spec_text, str(a["qty"]), ""])

    # 导轨按长度汇总
    rail_groups: dict[float, int] = {}
    for rail in job.rails:
        length = rail.length if rail.length is not None else max(80.0, job.cabinet.plate_w - 120.0)
        rail_groups[round(length, 1)] = rail_groups.get(round(length, 1), 0) + 1
    for length, qty in sorted(rail_groups.items()):
        rows.append([str(len(rows) + 1), "35mm 标准导轨", f"L={fmt(length)}", str(qty), ""])

    # 线槽按规格汇总
    duct_groups: dict[tuple[float, float], int] = {}
    for duct in job.ducts:
        key = (round(duct.width, 1), round(duct.length, 1))
        duct_groups[key] = duct_groups.get(key, 0) + 1
    for (width, length), qty in sorted(duct_groups.items()):
        rows.append([str(len(rows) + 1), "线槽", f"{fmt(width)}×{fmt(length)}", str(qty), ""])
    return rows


def _draw_bom(d: Drawing, layout: Layout, job: Job, lib: Library, issues: list[Issue]):
    rows = bom_rows(job, lib)
    if not rows:
        return
    hole_bottom = getattr(d, "_hole_bottom", layout.inner.y1 - 150)
    y_top = hole_bottom - 12
    widths = [10, 55, 45, 15, 37]
    scale_w = layout.right_w / sum(widths)
    widths = [w * scale_w for w in widths]
    bottom = _draw_table(
        d,
        layout.right_x,
        y_top,
        widths,
        ["序号", "名称", "规格/型号", "数量", "备注"],
        rows,
        f"柜内元件材料表（共 {len(rows)} 项）",
        "",
        y_limit=60.0,
        aligns=["center", "left", "left", "center", "left"],
    )
    setattr(d, "_bom_bottom", bottom)
    if bottom < 60:
        issues.append(Issue("warn", "材料表空间不足，已压缩行高；建议增大图幅。"))


def rail_rows(job: Job, lib: Library) -> list[list[str]]:
    """导轨元件表行数据，供图纸表格和 CSV 导出共用。"""
    if not job.rails:
        return []
    lay = internal_layout(job.cabinet, job.rails, job.ducts, lib)
    rows: list[list[str]] = []
    for rl in lay.rails:
        for g in rl.groups:
            nums = g.first if g.first == g.last else f"{g.first}~{g.last}"
            spec = f"{g.brand} {g.spec}".strip() or str(lib.device(g.model).get("spec", g.model))
            rows.append([rl.label, g.name, spec, str(g.qty), nums])
    return rows


def _draw_rail_table(d: Drawing, layout: Layout, job: Job, lib: Library, issues: list[Issue]):
    rows = rail_rows(job, lib)
    if not rows:
        return
    y_top = getattr(d, "_bom_bottom", layout.inner.y1 - 150) - 12
    widths = [12, 55, 35, 14, 51]
    scale_w = layout.right_w / sum(widths)
    widths = [w * scale_w for w in widths]
    bottom = _draw_table(
        d,
        layout.right_x,
        y_top,
        widths,
        ["导轨", "元件名称", "规格/型号", "数量", "自动编号"],
        rows,
        f"导轨元件表（共 {len(rows)} 组）",
        "L 编号与安装板图中的导轨编号一一对应。",
        y_limit=65.0,
        aligns=["center", "left", "left", "center", "center"],
    )
    if bottom < 65:
        issues.append(Issue("warn", "导轨元件表空间不足，已压缩行高；建议增大图幅。"))
