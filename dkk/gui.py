"""Tkinter 图形界面：参数表格 + 实时预览 + 校验 + 导出.

设计原则：
  * 左侧表单只维护数据，绘图完全复用 sheet.build_drawing，保证预览与 PDF 一致；
  * 表格类数据（开孔 / 导轨 / 线槽）用 Treeview 管理，弹窗编辑；
  * 所有尺寸单位 mm，坐标基准在界面中明确提示。
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.patches import Rectangle

from . import APP_NAME, __version__
from .export import default_filename, save_figure, save_tables_csv
from .library import Library, data_dir, geom_size, spec_text
from .model import (
    FACE_CHOICES,
    FACE_LABELS,
    Cabinet,
    Cutout,
    DeviceItem,
    Duct,
    Job,
    Rail,
    SheetInfo,
    cutout_face,
    face_dims,
    face_edge_labels,
    format_device_expr,
    parse_device_expr,
)
from .sheet import build_drawing
from .views import (
    Box,
    default_rail_length,
    fit_rail_geometry,
    front_geometry,
    internal_layout,
)

PAGE_CHOICES = {"自动": "auto", "A3": "A3", "A2": "A2", "A1": "A1"}
SCALE_CHOICES = {
    "自动": "auto",
    "1:5": "1:5",
    "1:10": "1:10",
    "1:15": "1:15",
    "1:20": "1:20",
    "1:25": "1:25",
    "1:30": "1:30",
    "1:40": "1:40",
    "1:50": "1:50",
}
VIEW_LABELS = {
    "front": "正视图",
    "side": "侧视图",
    "plan": "俯视图",
    "internal": "安装板图",
}


def num(v: float) -> str:
    return str(int(round(v))) if abs(v - round(v)) < 1e-6 else f"{v:g}"


def parse_num(
    text: str,
    label: str,
    default: float | None = None,
    positive: bool = False,
    minimum: float | None = None,
) -> float:
    s = str(text).strip().replace("，", ".").replace(",", ".")
    if not s:
        if default is not None:
            return default
        raise ValueError(f"{label}不能为空。")
    try:
        v = float(s)
    except ValueError:
        raise ValueError(f"{label}应为数字，当前为“{text}”。") from None
    if not math.isfinite(v):
        raise ValueError(f"{label}必须是有限数字。")
    if positive and v <= 0:
        raise ValueError(f"{label}必须大于 0。")
    if minimum is not None and v < minimum:
        raise ValueError(f"{label}不能小于 {num(minimum)}。")
    return v


def parse_optional(text: str, label: str) -> float | None:
    s = str(text).strip()
    if not s:
        return None
    return parse_num(s, label, positive=True)


# ---------------------------------------------------------------- 通用对话框
class _Dialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, title: str):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result: Any = None
        self.body = ttk.Frame(self, padding=14)
        self.body.pack(fill="both", expand=True)
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()

    def add_buttons(self, row: int, cols: int = 4) -> None:
        bar = ttk.Frame(self.body)
        bar.grid(row=row, column=0, columnspan=cols, sticky="e", pady=(16, 0))
        ttk.Button(bar, text="取消", width=9, command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="确定", width=9, command=self._ok).pack(side="right")

    def _ok(self) -> None:  # pragma: no cover - 子类实现
        self.destroy()

    def finish(self) -> None:
        self.update_idletasks()
        parent = self.master
        try:
            x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2)
            y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_height()) // 3)
            self.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass


class CutoutDialog(_Dialog):
    """新增 / 编辑门板开孔；新增时可直接生成等间距阵列。"""

    MAP_W = 520
    MAP_H = 210

    def __init__(
        self,
        parent: tk.Misc,
        lib: Library,
        cut: Cutout | None = None,
        *,
        cabinet: Cabinet | None = None,
        existing: list[Cutout] | None = None,
        initial: tuple[float, float] | None = None,
        face: str | None = None,
    ):
        super().__init__(parent, "编辑开孔" if cut else "添加开孔 / 阵列")
        self.lib = lib
        self.edit = cut is not None
        self.cabinet = cabinet or Cabinet()
        self.existing = list(existing or [])
        self._edit_ref = cut
        self.displays = [
            f"{item.get('name', key)} | {key}"
            for key, item in lib.components.items()
        ]
        self.display_to_key = {
            f"{item.get('name', key)} | {key}": key for key, item in lib.components.items()
        }
        self.face_to_key = {label: key for key, label in FACE_CHOICES}
        self.key_to_face = dict(FACE_CHOICES)
        face_key = cutout_face(cut) if cut is not None else (face or "front")
        if face_key not in FACE_LABELS:
            face_key = "front"
        fw, fh = face_dims(self.cabinet, face_key)
        if cut is not None:
            ix, iy = cut.x, cut.y
        elif initial is not None:
            ix, iy = initial
        else:
            ix, iy = min(200.0, fw / 2), min(600.0, fh / 2)
        c = cut or Cutout(component="pushbutton_22", x=ix, y=iy, face=face_key)
        current = self._display_for(c.component)

        self.comp_var = tk.StringVar(value=current)
        self.face_var = tk.StringVar(value=self.key_to_face[face_key])
        self.x_var = tk.StringVar(value=num(c.x))
        self.y_var = tk.StringVar(value=num(c.y))
        self.qty_var = tk.StringVar(value="1")
        self.dx_var = tk.StringVar(value="0")
        self.dy_var = tk.StringVar(value="0")
        self.label_var = tk.StringVar(value=c.label)
        self.note_var = tk.StringVar(value=c.note)
        self.ow_var = tk.StringVar(value=num(c.w) if c.w else "")
        self.oh_var = tk.StringVar(value=num(c.h) if c.h else "")
        self.od_var = tk.StringVar(value=num(c.d) if c.d else "")

        b = self.body
        b.columnconfigure(1, weight=1)
        b.columnconfigure(3, weight=1)

        ttk.Label(b, text="元件").grid(row=0, column=0, sticky="e", padx=(0, 5), pady=4)
        self.comp_combo = ttk.Combobox(
            b, textvariable=self.comp_var, values=self.displays,
            state="readonly", width=24,
        )
        self.comp_combo.grid(row=0, column=1, sticky="w", pady=4)
        self.comp_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_spec())
        ttk.Label(b, text="搜索").grid(row=0, column=2, sticky="e", padx=(10, 5), pady=4)
        self.comp_search_var = tk.StringVar()
        ttk.Entry(b, textvariable=self.comp_search_var, width=14).grid(
            row=0, column=3, sticky="w", pady=4
        )

        ttk.Label(b, text="所在面").grid(row=1, column=0, sticky="e", padx=(0, 5), pady=4)
        self.face_combo = ttk.Combobox(
            b,
            textvariable=self.face_var,
            values=[label for _key, label in FACE_CHOICES],
            state="readonly",
            width=18,
        )
        self.face_combo.grid(row=1, column=1, columnspan=2, sticky="w", pady=4)
        self.face_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_face_change())

        self.spec_label = ttk.Label(b, text="", foreground="#333333")
        self.spec_label.grid(row=2, column=0, columnspan=4, sticky="w", pady=(0, 6))

        ttk.Separator(b).grid(row=3, column=0, columnspan=4, sticky="ew", pady=4)

        self.x_label = ttk.Label(b, text="X（距左）")
        self.x_label.grid(row=4, column=0, sticky="e", padx=(0, 5), pady=4)
        ttk.Entry(b, textvariable=self.x_var, width=11).grid(row=4, column=1, sticky="w", pady=4)
        self.y_label = ttk.Label(b, text="Y（距下）")
        self.y_label.grid(row=4, column=2, sticky="e", padx=(10, 5), pady=4)
        ttk.Entry(b, textvariable=self.y_var, width=14).grid(row=4, column=3, sticky="w", pady=4)

        qty = ttk.Entry(b, textvariable=self.qty_var, width=11)
        self._pair_widget(b, 5, "数量", qty, "代号 / 前缀", self.label_var)
        self._pair(b, 6, "水平间距", self.dx_var, "垂直间距", self.dy_var)
        self._pair(b, 7, "开孔宽 W", self.ow_var, "开孔高 H", self.oh_var)
        ttk.Label(b, text="开孔直径 D").grid(row=8, column=0, sticky="e", padx=(0, 5), pady=4)
        dia = ttk.Entry(b, textvariable=self.od_var, width=11)
        dia.grid(row=8, column=1, sticky="w", pady=4)
        ttk.Label(b, text="备注").grid(row=8, column=2, sticky="e", padx=(10, 5), pady=4)
        ttk.Entry(b, textvariable=self.note_var, width=18).grid(row=8, column=3, sticky="w", pady=4)

        self.map_canvas = tk.Canvas(
            b,
            width=self.MAP_W,
            height=self.MAP_H,
            bg="#fbfcfd",
            highlightthickness=1,
            highlightbackground="#c8ced6",
            cursor="crosshair",
        )
        self.map_canvas.grid(row=9, column=0, columnspan=4, sticky="ew", pady=(8, 3))
        self.map_canvas.bind("<Button-1>", self._map_click)
        self.map_canvas.bind("<B1-Motion>", self._map_click)

        self.dist_label = ttk.Label(b, text="", style="Hint.TLabel")
        self.dist_label.grid(row=10, column=0, columnspan=4, sticky="w")

        hint = (
            "坐标基准随“所在面”变化：正面 X 距左、Y 距下；侧面 X 距前、Y 距下；"
            "顶/底面 X 距左、Y 距前；X/Y 均为孔心位置。\n"
            "示意图中可直接点击或拖动定位；离四边的距离已实时显示。\n"
            "阵列：数量 > 1 时按“水平/垂直间距”递增；前缀末位数字会自动递增（HL1 → HL2…）。\n"
            "覆写尺寸留空即使用元件库默认值；自定义方孔/圆孔/腰型孔需填写宽高或直径。\n"
            "琴式操作台：Y 在前沿高以下为门板，以上为斜面展开区（沿斜面量取真实尺寸）。"
        )
        ttk.Label(
            b, text=hint, foreground="#666666", justify="left", wraplength=540
        ).grid(row=11, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.add_buttons(12)
        self._update_spec()
        self._update_face_labels()

        if self.edit:
            for w in (qty,):
                w.configure(state="disabled")
            # 间距控件在 _pair 内部创建，这里按变量名禁用整行
            for child in b.grid_slaves(row=6):
                child.configure(state="disabled")
        self.finish()
        for var in (self.comp_var, self.x_var, self.y_var, self.qty_var, self.dx_var, self.dy_var):
            var.trace_add("write", self._map_redraw)
        self.comp_var.trace_add("write", lambda *_a: self._update_spec())
        self.comp_search_var.trace_add("write", lambda *_a: self._filter_components())
        self._map_redraw()

    def _filter_components(self) -> None:
        """按名称/型号/分类/尺寸关键字筛选元件下拉框。"""
        q = self.comp_search_var.get().strip().lower()
        vals: list[str] = []
        for display in self.displays:
            key = self.display_to_key.get(display, "")
            item = self.lib.components.get(key, {})
            hay = " ".join(
                str(x) for x in (
                    display, item.get("name", ""), item.get("category", ""),
                    item.get("brand", ""), item.get("spec", ""),
                )
            ).lower()
            if not q or q in hay:
                vals.append(display)
        if not vals:
            self.comp_combo.configure(values=["无匹配元件"])
            self.comp_var.set("无匹配元件")
            return
        self.comp_combo.configure(values=vals)
        if self.comp_var.get() not in vals:
            self.comp_var.set(vals[0])

    @staticmethod
    def _pair(parent, row, l1, v1, l2, v2):
        ttk.Label(parent, text=l1).grid(row=row, column=0, sticky="e", padx=(0, 5), pady=4)
        ttk.Entry(parent, textvariable=v1, width=11).grid(row=row, column=1, sticky="w", pady=4)
        ttk.Label(parent, text=l2).grid(row=row, column=2, sticky="e", padx=(10, 5), pady=4)
        ttk.Entry(parent, textvariable=v2, width=14).grid(row=row, column=3, sticky="w", pady=4)

    @staticmethod
    def _pair_widget(parent, row, l1, w1, l2, v2):
        ttk.Label(parent, text=l1).grid(row=row, column=0, sticky="e", padx=(0, 5), pady=4)
        w1.grid(row=row, column=1, sticky="w", pady=4)
        ttk.Label(parent, text=l2).grid(row=row, column=2, sticky="e", padx=(10, 5), pady=4)
        ttk.Entry(parent, textvariable=v2, width=18).grid(row=row, column=3, sticky="w", pady=4)

    def _display_for(self, key: str) -> str:
        for d in self.displays:
            if d.endswith(f"| {key}"):
                return d
        return self.displays[0] if self.displays else ""

    def _current_face(self) -> str:
        return self.face_to_key.get(self.face_var.get(), "front")

    def _update_face_labels(self) -> None:
        ax, ay, *_rest = face_edge_labels(self._current_face())
        self.x_label.configure(text=f"X（{ax[2:]}）")
        self.y_label.configure(text=f"Y（{ay[2:]}）")

    def _on_face_change(self) -> None:
        face = self._current_face()
        fw, fh = face_dims(self.cabinet, face)
        for var, limit in ((self.x_var, fw), (self.y_var, fh)):
            try:
                v = float(var.get())
            except ValueError:
                v = 0.0
            var.set(num(round(min(max(v, 0.0), limit))))
        self._update_face_labels()
        self._map_redraw()

    def _update_spec(self) -> None:
        key = self.display_to_key.get(self.comp_var.get(), "")
        item = self.lib.components.get(key)
        if not item:
            self.spec_label.configure(text="")
            return
        cut = spec_text(item.get("cutout", {}))
        out = spec_text(item.get("outline", item.get("cutout", {})))
        custom = "（自定义尺寸）" if item.get("custom") else ""
        self.spec_label.configure(text=f"开孔 {cut} / 外形 {out} {custom}")

    def _map_geom(self, item: Cutout) -> tuple[float, float]:
        try:
            return geom_size(self.lib.resolve_cutout(item, 0).cutout)
        except Exception:
            return 30.0, 30.0

    def _current_cutout(self) -> Cutout:
        """按对话框当前输入构造一个临时开孔，用于示意图显示实际尺寸。"""
        key = self.display_to_key.get(self.comp_var.get(), "pushbutton_22")

        def opt(text: str) -> float:
            s = str(text).strip().replace("，", ".").replace(",", ".")
            if not s:
                return 0.0
            try:
                return max(0.0, float(s))
            except ValueError:
                return 0.0

        return Cutout(
            component=key,
            x=0.0,
            y=0.0,
            w=opt(self.ow_var.get()),
            h=opt(self.oh_var.get()),
            d=opt(self.od_var.get()),
            face=self._current_face(),
        )

    def _map_click(self, event) -> None:
        if not hasattr(self, "_map_scale") or self._map_scale <= 0:
            return
        fw, fh = face_dims(self.cabinet, self._current_face())
        x = (event.x - self._map_ox) / self._map_scale
        y = (self.MAP_H - event.y - self._map_oy) / self._map_scale
        if x < -20 or x > fw + 20 or y < -20 or y > fh + 20:
            return
        x = min(max(x, 0.0), fw)
        y = min(max(y, 0.0), fh)
        self.x_var.set(num(round(x)))
        self.y_var.set(num(round(y)))

    def _map_redraw(self, *_args) -> None:
        """门板位置示意图：显示四周距离，可点击/拖动定位。"""
        if not hasattr(self, "map_canvas"):
            return
        cv = self.map_canvas
        cv.delete("all")
        cab = self.cabinet
        face = self._current_face()
        W, H = face_dims(cab, face)
        pad_x, pad_y = 20.0, 14.0
        scale = min((self.MAP_W - 2 * pad_x) / W, (self.MAP_H - 2 * pad_y) / H)
        ox = (self.MAP_W - W * scale) / 2
        oy = (self.MAP_H - H * scale) / 2
        self._map_scale, self._map_ox, self._map_oy = scale, ox, oy

        def P(x: float, y: float) -> tuple[float, float]:
            return ox + x * scale, self.MAP_H - oy - y * scale

        # 200 mm 辅助网格
        gx = 200.0
        while gx < W:
            cv.create_line(*P(gx, 0), *P(gx, H), fill="#edf0f4")
            gx += 200.0
        gy = 200.0
        while gy < H:
            cv.create_line(*P(0, gy), *P(W, gy), fill="#edf0f4")
            gy += 200.0

        bx0, by0 = P(0, H)
        bx1, by1 = P(W, 0)
        if face == "front" and cab.base_height > 0:
            _, bt = P(0, min(cab.base_height, H))
            cv.create_rectangle(bx0, bt, bx1, by1, fill="#f0f2f4", outline="")
            cv.create_line(bx0, bt, bx1, bt, fill="#9aa4b2")
        cv.create_rectangle(bx0, by0, bx1, by1, outline="#343a40", width=1.5)

        if face == "front":
            try:
                fg = front_geometry(cab)
            except Exception:
                fg = None
            if fg is not None:
                for leaf in fg.leaves:
                    lx0, ly0 = P(leaf.box.x0, leaf.box.y1)
                    lx1, ly1 = P(leaf.box.x1, leaf.box.y0)
                    cv.create_rectangle(lx0, ly0, lx1, ly1, outline="#868e96", dash=(4, 2))
                if fg.gap_center is not None:
                    gx, gy1 = P(fg.gap_center, 0)
                    _, gy2 = P(fg.gap_center, H)
                    cv.create_line(gx, gy2, gx, gy1, fill="#868e96", dash=(3, 2))
            if cab.is_console:
                _, fy = P(0, min(cab.console_desk_h, H))
                cv.create_line(bx0, fy, bx1, fy, fill="#d9480f", dash=(6, 2))
                cv.create_text(
                    (bx0 + bx1) / 2, (fy + by0) / 2,
                    text="斜面操作面板（沿斜面展开）",
                    fill="#d9480f", font=("Microsoft YaHei UI", 8),
                )
        elif face in ("left", "right"):
            if cab.base_height > 0:
                _, bt = P(0, min(cab.base_height, H))
                cv.create_line(bx0, bt, bx1, bt, fill="#9aa4b2", dash=(4, 2))
            dx = min(max(cab.door_thickness, 0.0), W)
            dxp, _ = P(dx, 0)
            _, dy1 = P(dx, min(cab.door_lower, H))
            _, dy2 = P(dx, min(cab.door_upper, H))
            cv.create_line(dxp, dy1, dxp, dy2, fill="#868e96", dash=(4, 2))
        else:  # top / bottom
            dy = min(max(cab.door_thickness, 0.0), H)
            cv.create_line(*P(0, dy), *P(W, dy), fill="#868e96", dash=(4, 2))
            if cab.template == "vertical_double":
                try:
                    fg = front_geometry(cab)
                except Exception:
                    fg = None
                if fg is not None and fg.gap_center is not None:
                    cv.create_line(*P(fg.gap_center, 0), *P(fg.gap_center, dy),
                                   fill="#868e96", dash=(3, 2))

        # 已有开孔用浅灰标记（只显示同一面上的孔）
        for item in self.existing:
            if item is self._edit_ref or cutout_face(item) != face:
                continue
            cw, ch = self._map_geom(item)
            cx, cy = P(item.x, item.y)
            rw, rh = max(3.0, cw * scale), max(3.0, ch * scale)
            if abs(cw - ch) < 0.01:
                cv.create_oval(
                    cx - rw / 2, cy - rh / 2, cx + rw / 2, cy + rh / 2,
                    outline="#9aa4b2", fill="#dde3e8",
                )
            else:
                cv.create_rectangle(
                    cx - rw / 2, cy - rh / 2, cx + rw / 2, cy + rh / 2,
                    outline="#9aa4b2", fill="#dde3e8",
                )

        try:
            x = float(parse_num(self.x_var.get(), "X", default=0.0))
            y = float(parse_num(self.y_var.get(), "Y", default=0.0))
            dx = float(parse_num(self.dx_var.get(), "水平间距", default=0.0))
            dy = float(parse_num(self.dy_var.get(), "垂直间距", default=0.0))
            qty = 1 if self.edit else int(
                parse_num(self.qty_var.get(), "数量", default=1, minimum=1)
            )
        except ValueError:
            x = y = dx = dy = 0.0
            qty = 1
        qty = max(1, min(qty, 200))

        def caption(x: float, y: float, text: str, anchor: str, color: str) -> None:
            item = cv.create_text(
                x, y, anchor=anchor, text=text,
                fill=color, font=("Microsoft YaHei UI", 8),
            )
            bb = cv.bbox(item)
            if bb:
                bg = cv.create_rectangle(
                    bb[0] - 3, bb[1] - 1, bb[2] + 3, bb[3] + 1,
                    fill="#fbfcfd", outline="",
                )
                cv.tag_lower(bg, item)

        # 当前元件按实际开孔尺寸绘制，并标上开孔规格（指示灯 Φ22.5、触摸屏 192×138 等）
        try:
            cur_cut = self._current_cutout()
            cw, ch = self._map_geom(cur_cut)
            cur_spec = self.lib.resolve_cutout(cur_cut, 0).spec
        except Exception:
            cw, ch, cur_spec = 30.0, 30.0, ""

        for i in range(qty):
            px, py = x + i * dx, y + i * dy
            cx, cy = P(px, py)
            if i == 0:
                # 引到四边的距离辅助线
                cv.create_line(*P(0, py), *P(W, py), fill="#f2b58c", dash=(2, 3))
                cv.create_line(*P(px, 0), *P(px, H), fill="#f2b58c", dash=(2, 3))
                rw, rh = max(6.0, cw * scale), max(6.0, ch * scale)
                if abs(cw - ch) < 0.01:
                    cv.create_oval(
                        cx - rw / 2, cy - rh / 2, cx + rw / 2, cy + rh / 2,
                        outline="#d9480f", width=2, fill="#ffe8d9",
                    )
                else:
                    cv.create_rectangle(
                        cx - rw / 2, cy - rh / 2, cx + rw / 2, cy + rh / 2,
                        outline="#d9480f", width=2, fill="#ffe8d9",
                    )
                cv.create_line(cx - 11, cy, cx + 11, cy, fill="#d9480f")
                cv.create_line(cx, cy - 11, cx, cy + 11, fill="#d9480f")
                if cur_spec:
                    caption(cx, cy - rh / 2 - 6, cur_spec, "s", "#d9480f")
            else:
                cv.create_oval(
                    cx - 3, cy - 3, cx + 3, cy + 3,
                    outline="#2f9e44", fill="#d3f9d8",
                )

        what = {
            "front": "门板", "left": "左侧板", "right": "右侧板",
            "top": "顶板", "bottom": "底板",
        }.get(face, "门板")
        caption(
            7, 7, f"{what} {W:.0f}×{H:.0f} mm · 网格 200 mm", "nw", "#8a94a3"
        )
        caption(self.MAP_W - 7, 7, "点击/拖动定位", "ne", "#d9480f")
        note = f"（阵列首孔，共 {qty} 个）" if qty > 1 else ""
        _ax, _ay, x0n, x1n, y0n, y1n = face_edge_labels(face)
        self.dist_label.configure(
            text=(
                f"孔心距边：{x0n} {x:.0f} · {x1n} {W - x:.0f} · "
                f"{y0n} {y:.0f} · {y1n} {H - y:.0f} mm{note}"
            )
        )

    def _ok(self) -> None:
        try:
            key = self.display_to_key.get(self.comp_var.get())
            if not key:
                raise ValueError("请选择元件。")
            x = parse_num(self.x_var.get(), "X 坐标")
            y = parse_num(self.y_var.get(), "Y 坐标")
            qty = int(parse_num(self.qty_var.get(), "数量", positive=True, minimum=1))
            qty = min(qty, 200)
            dx = parse_num(self.dx_var.get(), "水平间距", default=0.0)
            dy = parse_num(self.dy_var.get(), "垂直间距", default=0.0)
            ow = parse_num(self.ow_var.get(), "开孔宽", default=0.0)
            oh = parse_num(self.oh_var.get(), "开孔高", default=0.0)
            od = parse_num(self.od_var.get(), "开孔直径", default=0.0)
            prefix = self.label_var.get().strip()
            note = self.note_var.get().strip()
        except ValueError as e:
            messagebox.showerror("输入有误", str(e), parent=self)
            return

        if self.edit:
            labels = [prefix]
            qty = 1
        else:
            labels = self._make_labels(prefix, qty)

        result: list[Cutout] = []
        for i in range(qty):
            result.append(
                Cutout(
                    component=key,
                    x=x + i * dx,
                    y=y + i * dy,
                    label=labels[i] if i < len(labels) else "",
                    note=note,
                    w=ow,
                    h=oh,
                    d=od,
                    face=self._current_face(),
                )
            )
        self.result = result
        self.destroy()

    @staticmethod
    def _make_labels(prefix: str, qty: int) -> list[str]:
        if qty <= 1:
            return [prefix]
        if not prefix:
            return [""] * qty
        m = re.match(r"^(.*?)(\d+)$", prefix)
        if m:
            stem, start = m.group(1), int(m.group(2))
            return [f"{stem}{start + i}" for i in range(qty)]
        return [f"{prefix}{i + 1}" for i in range(qty)]


class RailDialog(_Dialog):
    def __init__(self, parent: tk.Misc, lib: Library, rail: Rail | None = None,
                 initial: tuple[float, float] | None = None,
                 plate: tuple[float, float] | None = None):
        super().__init__(parent, "编辑导轨" if rail else "添加导轨")
        self.lib = lib
        self.edit = rail is not None
        self.plate = plate
        self.old_items = list(rail.devices) if rail is not None else []
        if rail is not None:
            r = rail
        elif initial is not None and plate is not None:
            # 右击空白处新建：以点击点为中心，自动缩短/平移，保证不越界且能左右拖动。
            x, y, length = fit_rail_geometry(plate[0], plate[1], initial[0], initial[1])
            r = Rail(x=x, y=y, length=length)
        elif initial is not None:
            r = Rail(x=float(round(initial[0])), y=float(round(initial[1])))
        elif plate is not None:
            # 工具栏“添加导轨”：给一段默认长度，避免自动铺满整板后左右几乎拖不动。
            r = Rail(length=default_rail_length(plate[0]))
        else:
            r = Rail()
        self.y_var = tk.StringVar(value=num(r.y))
        self.x_var = tk.StringVar(value="" if r.x is None else num(r.x))
        self.len_var = tk.StringVar(value="" if r.length is None else num(r.length))
        self.expr_var = tk.StringVar(value=format_device_expr(r.devices))

        b = self.body
        b.columnconfigure(1, weight=1)
        ttk.Label(b, text="距安装板下沿 Y").grid(row=0, column=0, sticky="e", padx=(0, 6), pady=4)
        ttk.Entry(b, textvariable=self.y_var, width=14).grid(row=0, column=1, sticky="w", pady=4)
        ttk.Label(b, text="起点 X（空=自动）").grid(row=1, column=0, sticky="e", padx=(0, 6), pady=4)
        ttk.Entry(b, textvariable=self.x_var, width=14).grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(b, text="长度（空=自动）").grid(row=2, column=0, sticky="e", padx=(0, 6), pady=4)
        ttk.Entry(b, textvariable=self.len_var, width=14).grid(row=2, column=1, sticky="w", pady=4)
        ttk.Label(b, text="器件表达式").grid(row=3, column=0, sticky="e", padx=(0, 6), pady=4)
        ttk.Entry(b, textvariable=self.expr_var, width=32).grid(
            row=3, column=1, columnspan=2, sticky="w", pady=4
        )
        self.info = ttk.Label(b, text="", foreground="#555555")
        self.info.grid(row=4, column=0, columnspan=3, sticky="w", pady=(2, 4))

        models = "、".join(
            f"{k}({num(float(v.get('width', 36)))}mm)" for k, v in lib.devices.items()
        )
        plate_hint = ""
        if plate is not None:
            plate_hint = (
                f"安装板 {num(plate[0])}×{num(plate[1])} mm；"
                "X + 长度不得超过板宽。新建导轨默认留出左右移动余量，"
                "双击导轨可随时改长度。\n"
            )
        ttk.Label(
            b,
            text=f"{plate_hint}写法：2P×6, 3P×4；也可用 2P*6。\n可用型号：{models}",
            foreground="#666666",
            justify="left",
            wraplength=540,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.add_buttons(6, cols=3)
        self.expr_var.trace_add("write", lambda *_a: self._update_info())
        self._update_info()
        self.finish()

    def _update_info(self) -> None:
        items = parse_device_expr(self.expr_var.get())
        total_w = 0.0
        count = 0
        for it in items:
            dev = self.lib.device(it.model)
            total_w += float(dev.get("width", 36)) * it.qty
            count += it.qty
        self.info.configure(text=f"{len(items)} 种 / 共 {count} 件，占用宽度约 {num(total_w)} mm")

    def _ok(self) -> None:
        try:
            y = parse_num(self.y_var.get(), "Y 位置")
            x = parse_optional(self.x_var.get(), "起点 X")
            length = parse_optional(self.len_var.get(), "导轨长度")
        except ValueError as e:
            messagebox.showerror("输入有误", str(e), parent=self)
            return
        if self.plate is not None:
            err = self._check_plate_fit(y, x, length)
            if err:
                messagebox.showerror("导轨超出安装板", err, parent=self)
                return
        items = parse_device_expr(self.expr_var.get())
        self._merge_metadata(items)
        self.result = Rail(y=y, x=x, length=length, devices=items)
        self.destroy()

    def _check_plate_fit(self, y: float, x: float | None,
                         length: float | None) -> str:
        """在对话框阶段拦住越界导轨，避免建完才发现错误。

        长度留空时按自动长度（板宽-120）估算；起点 X 也留空时由布局自动
        放在 X=60，不会越界。

        这里只做输入阶段的快速反馈；最终仍以 ``validate.py`` 对整张
        安装板的全量校验为准，避免 GUI 与 CLI/导出使用两套规则。
        """
        plate_w, plate_h = self.plate
        if y < 20.0 or y > max(20.0, plate_h - 20.0):
            return (
                f"导轨中心 Y={num(y)} 超出安装板范围。\n"
                f"Y 应在 20 ～ {num(max(20.0, plate_h - 20.0))} mm 之间。"
            )
        if length is not None and length <= 0:
            return "导轨长度必须大于 0；留空表示按板宽自动计算。"
        if x is None:
            if length is not None and length > plate_w + 0.01:
                return f"导轨长度 {num(length)} mm 大于安装板宽度 {num(plate_w)} mm。"
            return ""
        if x < 0:
            return "起点 X 不能小于 0。"
        effective = length if length is not None else max(80.0, plate_w - 120.0)
        if x + effective > plate_w + 0.01:
            max_len = max(0.0, plate_w - x)
            return (
                f"导轨超出安装板右边：X {num(x)} + 长度 {num(effective)} "
                f"= {num(x + effective)} mm，板宽 {num(plate_w)} mm。\n\n"
                f"请把长度改为 ≤ {num(max_len)} mm，或减小起点 X。"
            )
        return ""

    def _merge_metadata(self, items: list[DeviceItem]) -> None:
        """表达式只写型号×数量；编辑时把快速选型填的品牌/规格/尺寸保留下来。"""
        used: set[int] = set()
        for i, item in enumerate(items):
            match = None
            if i < len(self.old_items) and i not in used and self.old_items[i].model == item.model:
                match = i
            else:
                for j, old in enumerate(self.old_items):
                    if j not in used and old.model == item.model:
                        match = j
                        break
            if match is None:
                continue
            old = self.old_items[match]
            used.add(match)
            item.brand = old.brand
            item.name = old.name
            item.spec = old.spec
            item.width = old.width
            item.height = old.height
            item.prefix = old.prefix
            item.note = old.note


class QuickDeviceDialog(_Dialog):
    """常用器件快速选型：搜索目录 + 品牌/型号/尺寸覆写，直接加到导轨。

    这是“折中”方案：不内置庞大的品牌数据库，只提供常用 DIN 器件目录；
    品牌、规格型号、宽度、高度都可以现场改，宽度按器件库默认值带出，
    特殊品牌只需改型号和宽度，几何和材料表同时生效。
    """

    def __init__(self, parent: tk.Misc, lib: Library, rails: list[Rail],
                 cab: Cabinet | None = None):
        super().__init__(parent, "快速放器件")
        self.lib = lib
        self.rails = rails
        self.cab = cab
        self.changed = False
        self.model_key = ""
        self._key_by_iid: dict[str, str] = {}

        b = self.body
        b.columnconfigure(1, weight=1)
        b.columnconfigure(3, weight=1)

        ttk.Label(b, text="搜索").grid(row=0, column=0, sticky="e", padx=(0, 6), pady=(0, 4))
        self.search_var = tk.StringVar()
        search = ttk.Entry(b, textvariable=self.search_var, width=34)
        search.grid(row=0, column=1, columnspan=3, sticky="w", pady=(0, 4))

        tree_wrap = ttk.Frame(b)
        tree_wrap.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(0, 6))
        tree_wrap.columnconfigure(0, weight=1)
        cols = ("category", "name", "brand", "spec", "size")
        self.tree = ttk.Treeview(
            tree_wrap, columns=cols, show="headings", selectmode="browse", height=9
        )
        heads = {
            "category": ("分类", 78), "name": ("名称", 132),
            "brand": ("品牌", 72), "spec": ("规格/型号", 150), "size": ("宽×高", 78),
        }
        for key, (text, width) in heads.items():
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor="w" if key != "size" else "center")
        self.tree.grid(row=0, column=0, sticky="ew")
        tsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        tsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        ttk.Separator(b).grid(row=2, column=0, columnspan=4, sticky="ew", pady=4)

        self.name_var = tk.StringVar()
        self.brand_var = tk.StringVar()
        self.spec_var = tk.StringVar()
        self.width_var = tk.StringVar()
        self.height_var = tk.StringVar()
        self.qty_var = tk.StringVar(value="1")
        self.prefix_var = tk.StringVar()
        self.target_var = tk.StringVar()
        self.new_y_var = tk.StringVar(value="200")

        ttk.Label(b, text="名称").grid(row=3, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Entry(b, textvariable=self.name_var, width=28).grid(
            row=3, column=1, columnspan=3, sticky="w", pady=3
        )
        ttk.Label(b, text="品牌").grid(row=4, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Combobox(
            b, textvariable=self.brand_var, width=18,
            values=["通用", "正泰", "德力西", "施耐德", "西门子", "ABB", "欧姆龙",
                    "台达", "汇川", "明纬", "菲尼克斯", "魏德米勒"],
        ).grid(row=4, column=1, sticky="w", pady=3)
        ttk.Label(b, text="规格/型号").grid(row=4, column=2, sticky="e", padx=(10, 5), pady=3)
        ttk.Entry(b, textvariable=self.spec_var, width=22).grid(
            row=4, column=3, sticky="w", pady=3
        )
        ttk.Label(b, text="宽度 mm").grid(row=5, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Entry(b, textvariable=self.width_var, width=10).grid(
            row=5, column=1, sticky="w", pady=3
        )
        ttk.Label(b, text="高度 mm").grid(row=5, column=2, sticky="e", padx=(10, 5), pady=3)
        ttk.Entry(b, textvariable=self.height_var, width=10).grid(
            row=5, column=3, sticky="w", pady=3
        )
        ttk.Label(b, text="数量").grid(row=6, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Entry(b, textvariable=self.qty_var, width=10).grid(
            row=6, column=1, sticky="w", pady=3
        )
        ttk.Label(b, text="编号前缀").grid(row=6, column=2, sticky="e", padx=(10, 5), pady=3)
        ttk.Entry(b, textvariable=self.prefix_var, width=10).grid(
            row=6, column=3, sticky="w", pady=3
        )

        ttk.Label(b, text="加到导轨").grid(row=7, column=0, sticky="e", padx=(0, 6), pady=3)
        self.target_combo = ttk.Combobox(
            b, textvariable=self.target_var, state="readonly", width=22
        )
        self.target_combo.grid(row=7, column=1, sticky="w", pady=3)
        ttk.Label(b, text="新建导轨 Y").grid(row=7, column=2, sticky="e", padx=(10, 5), pady=3)
        ttk.Entry(b, textvariable=self.new_y_var, width=10).grid(
            row=7, column=3, sticky="w", pady=3
        )

        self.info = ttk.Label(b, text="", style="Hint.TLabel")
        self.info.grid(row=8, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Label(
            b,
            text=(
                "选目录条目会自动带出默认宽度；实际品牌/型号不同时，直接改品牌、规格和宽度即可。"
                "宽度=极数×18mm 是微型断路器的常用算法，可手动覆盖。\n"
                "已有导轨时默认追加到最后一根，不会自动新建；只有明确选择“新建导轨”"
                "才会创建新导轨（自动居中并留出左右拖动余量）。"
            ),
            foreground="#666666", justify="left", wraplength=560,
        ).grid(row=9, column=0, columnspan=4, sticky="w", pady=(4, 0))

        bar = ttk.Frame(b)
        bar.grid(row=10, column=0, columnspan=4, sticky="e", pady=(12, 0))
        ttk.Button(bar, text="取消", width=9, command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="添加并继续", width=11,
                   command=lambda: self._add(False)).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="添加", width=9,
                   command=lambda: self._add(True)).pack(side="right")

        self._refresh_targets()
        self._refresh_catalog()
        self.search_var.trace_add("write", lambda *_a: self._refresh_catalog())
        self.finish()

    def _refresh_targets(self) -> None:
        """刷新“加到导轨”下拉框；已有导轨时默认追加到最后一根。

        这样普通添加器件不会再顺手多建一根导轨；确实需要新导轨时，
        用户可在下拉框中明确选择“新建导轨”。
        """
        current = self.target_var.get()
        self._targets = ["新建导轨"] + [
            f"L{i + 1}  (Y={num(r.y)})" for i, r in enumerate(self.rails)
        ]
        self.target_combo.configure(values=self._targets)
        if current in self._targets:
            self.target_var.set(current)
        elif len(self._targets) > 1:
            self.target_var.set(self._targets[-1])
        else:
            self.target_var.set(self._targets[0] if self._targets else "")

    def _refresh_catalog(self) -> None:
        query = self.search_var.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        self._key_by_iid.clear()
        for idx, (key, dev) in enumerate(self.lib.devices.items()):
            name = str(dev.get("name", key))
            brand = str(dev.get("brand", "通用"))
            spec = str(dev.get("spec", key))
            category = str(dev.get("category", "其他"))
            width = float(dev.get("width", 36))
            height = float(dev.get("height", 85))
            hay = " ".join((key, name, brand, spec, category)).lower()
            if query and query not in hay:
                continue
            iid = f"d{idx}"
            self._key_by_iid[iid] = key
            self.tree.insert(
                "", "end", iid=iid,
                values=(category, name, brand, spec, f"{num(width)}×{num(height)}"),
            )

    def _on_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        key = self._key_by_iid.get(sel[0], "")
        dev = self.lib.device(key)
        if not dev:
            return
        self.model_key = key
        self.name_var.set(str(dev.get("name", key)))
        self.brand_var.set(str(dev.get("brand", "通用")))
        self.spec_var.set(str(dev.get("spec", key)))
        self.width_var.set(num(float(dev.get("width", 36))))
        self.height_var.set(num(float(dev.get("height", 85))))
        self.prefix_var.set(str(dev.get("prefix", "E")))

    def _add(self, close: bool) -> None:
        try:
            qty = int(parse_num(self.qty_var.get(), "数量", positive=True, minimum=1))
            qty = min(qty, 999)
            width = parse_num(self.width_var.get(), "宽度", positive=True)
            height = parse_num(self.height_var.get(), "高度", positive=True)
            target = self.target_var.get()
            y = parse_num(self.new_y_var.get(), "新导轨 Y", default=200.0)
        except ValueError as e:
            messagebox.showerror("输入有误", str(e), parent=self)
            return

        name = self.name_var.get().strip()
        brand = self.brand_var.get().strip()
        spec = self.spec_var.get().strip()
        if not (name or spec):
            messagebox.showerror("输入有误", "请选择目录器件，或填写名称/规格型号。", parent=self)
            return
        prefix = self.prefix_var.get().strip() or "E"

        key = self.model_key or (spec or name or "custom")
        if key == "custom" and spec:
            # 自定义器件用型号作为显示键，项目保存后仍能看出来。
            key = spec
        item = DeviceItem(
            model=key, qty=qty, width=width, height=height,
            prefix=prefix, brand=brand, name=name, spec=spec,
        )
        if target == "新建导轨" or target not in getattr(self, "_targets", []):
            rail = self._new_rail_for_item(item, y)
            self.rails.append(rail)
            self._refresh_targets()
            # “添加并继续”通常是把同批器件接着放到刚建的这条导轨上。
            if len(self._targets) > 1:
                self.target_var.set(self._targets[-1])
        else:
            try:
                idx = int(target[1:].split()[0]) - 1
                self.rails[idx].devices.append(item)
            except (ValueError, IndexError):
                messagebox.showerror("输入有误", "目标导轨无效，请重新选择。", parent=self)
                return
        self.changed = True
        self.info.configure(
            text=f"已添加 {name or spec} ×{qty}（宽 {num(width)}mm，占 {num(width * qty)}mm）。"
        )
        if close:
            self.destroy()

    def _new_rail_for_item(self, item: DeviceItem, y: float) -> Rail:
        """为快速添加的器件创建一根长度合适、且能左右拖动的新导轨。

        导轨长度优先取默认段长；单次添加的器件更宽时自动加长，但始终
        限制在安装板宽度内。没有柜体信息时退回原来的“全自动”导轨。
        """
        if self.cab is None:
            return Rail(y=y, devices=[item])
        plate_w, plate_h = self.cab.plate_w, self.cab.plate_h
        need = 40.0 + max(1.0, float(item.width)) * max(1, int(item.qty))
        max_len = max(40.0, plate_w - 80.0)
        length = max(default_rail_length(plate_w), min(need, max_len))
        # 以板宽中点定位，新导轨默认居中，左右都留出可拖动余量。
        x, yy, length = fit_rail_geometry(plate_w, plate_h, plate_w / 2.0, y, length)
        return Rail(x=x, y=yy, length=length, devices=[item])

    def _ok(self) -> None:
        self._add(True)


class DuctDialog(_Dialog):
    def __init__(self, parent: tk.Misc, duct: Duct | None = None,
                 initial: tuple[float, float] | None = None):
        super().__init__(parent, "编辑线槽" if duct else "添加线槽")
        if duct is not None:
            d = duct
        elif initial is not None:
            d = Duct(x=float(round(initial[0])), y=float(round(initial[1])))
        else:
            d = Duct()
        self.orient_var = tk.StringVar(value="水平" if d.orient == "h" else "垂直")
        self.x_var = tk.StringVar(value=num(d.x))
        self.y_var = tk.StringVar(value=num(d.y))
        self.len_var = tk.StringVar(value=num(d.length))
        self.width_var = tk.StringVar(value=num(d.width))

        b = self.body
        ttk.Label(b, text="方向").grid(row=0, column=0, sticky="e", padx=(0, 6), pady=4)
        ttk.Combobox(
            b, textvariable=self.orient_var, values=["水平", "垂直"], state="readonly", width=12
        ).grid(row=0, column=1, sticky="w", pady=4)
        ttk.Label(b, text="X").grid(row=1, column=0, sticky="e", padx=(0, 6), pady=4)
        ttk.Entry(b, textvariable=self.x_var, width=14).grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(b, text="Y").grid(row=2, column=0, sticky="e", padx=(0, 6), pady=4)
        ttk.Entry(b, textvariable=self.y_var, width=14).grid(row=2, column=1, sticky="w", pady=4)
        ttk.Label(b, text="长度").grid(row=3, column=0, sticky="e", padx=(0, 6), pady=4)
        ttk.Entry(b, textvariable=self.len_var, width=14).grid(row=3, column=1, sticky="w", pady=4)
        ttk.Label(b, text="宽度").grid(row=4, column=0, sticky="e", padx=(0, 6), pady=4)
        ttk.Entry(b, textvariable=self.width_var, width=14).grid(row=4, column=1, sticky="w", pady=4)
        ttk.Label(
            b,
            text="线槽坐标基准：安装板左下角；垂直槽从 (X,Y) 向上延伸，水平槽向右延伸。",
            foreground="#666666",
            wraplength=420,
            justify="left",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.add_buttons(6, cols=2)
        self.finish()

    def _ok(self) -> None:
        try:
            x = parse_num(self.x_var.get(), "X")
            y = parse_num(self.y_var.get(), "Y")
            length = parse_num(self.len_var.get(), "长度", positive=True)
            width = parse_num(self.width_var.get(), "宽度", positive=True)
        except ValueError as e:
            messagebox.showerror("输入有误", str(e), parent=self)
            return
        self.result = Duct(
            x=x, y=y, length=length, width=width,
            orient="h" if self.orient_var.get() == "水平" else "v",
        )
        self.destroy()


# ---------------------------------------------------------------- 主应用
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.lib = Library.load()
        self._fig = None
        self._canvas = None
        self._last_job: Job | None = None
        self._meta: dict[str, Any] | None = None
        self._fit_job: str | None = None
        self._fig_inches: tuple[float, float] = (23.4, 16.5)
        self._drag: dict[str, Any] | None = None
        self._drag_artists: list[Any] = []
        self._hover_index: int | None = None
        self._internal_cache: Any = None
        self._last_selected: tuple[str, int] | None = None
        self._clipboard: dict[str, Any] | None = None
        self._paste_serial = 0
        self._nudge_after: str | None = None
        self._pos_indices: list[int] = []
        self._pos_sync = False
        self._pos_job: str | None = None
        self._pan: dict[str, float] | None = None
        self._preview_px: tuple[int, int] = (100, 100)

        # 撤销/重做与自动刷新
        self._undo_stack: list[dict[str, Any]] = []
        self._redo_stack: list[dict[str, Any]] = []
        self._snapshot_dict: dict[str, Any] | None = None
        self._snapshot_ts = 0.0
        self._auto_after: str | None = None
        self._populating = False
        self._restoring = False
        self._last_good_job: Job | None = None

        self.show_sizes_var = tk.BooleanVar(value=True)

        self.template_map = {
            str(v.get("name", k)): k
            for k, v in self.lib.templates.items()
            if v.get("enabled", True)
        }
        self.req_map = {
            str(v.get("name", k)): k for k, v in self.lib.requirements.items()
        }

        self.cutouts: list[Cutout] = []
        self.rails: list[Rail] = []
        self.ducts: list[Duct] = []
        self.vars: dict[str, tk.StringVar] = {}
        self.view_vars: dict[str, tk.BooleanVar] = {}

        self._setup_window()
        self._setup_style()
        self._build_ui()
        self._bind_shortcuts()
        self._bind_auto_refresh()

        demo = data_dir() / "examples" / "demo_1700x1900x460.json"
        try:
            self._load_path(demo, silent=True)
        except Exception:
            self.populate_job(Job())
        self.root.after(120, self.refresh_preview)

    # ---------------- 窗口与样式 ----------------
    def _setup_window(self) -> None:
        self.root.title(f"{APP_NAME} v{__version__} — 电柜壳体接口图")
        self.root.geometry("1400x900")
        self.root.minsize(1120, 720)
        self.root.columnconfigure(0, minsize=500)
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

    def _setup_style(self) -> None:
        family = "Microsoft YaHei UI"
        try:
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                tkfont.nametofont(name).configure(family=family, size=9)
        except tk.TclError:
            pass
        style = ttk.Style(self.root)
        for theme in ("vista", "clam"):
            if theme in style.theme_names():
                try:
                    style.theme_use(theme)
                    break
                except tk.TclError:
                    continue
        style.configure(".", font=(family, 9))
        style.configure("Treeview", font=(family, 9), rowheight=23)
        style.configure("Treeview.Heading", font=(family, 9, "bold"))
        style.configure("TLabelframe.Label", font=(family, 9, "bold"))
        style.configure("Section.TLabel", font=(family, 10, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Page.TLabel", font=(family, 10, "bold"))

    def _build_ui(self) -> None:
        left = ttk.Frame(self.root, padding=(8, 8, 4, 0))
        left.grid(row=0, column=0, sticky="nsew")
        right = ttk.Frame(self.root, padding=(4, 8, 8, 0))
        right.grid(row=0, column=1, sticky="nsew")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        self.notebook = ttk.Notebook(left)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        self._build_cabinet_tab()
        self._build_cutout_tab()
        self._build_internal_tab()
        self._build_requirements_tab()

        # 右侧：预览
        header = ttk.Frame(right)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        header.columnconfigure(0, weight=1)
        self.page_label = ttk.Label(header, text="正在生成预览…", style="Page.TLabel")
        self.page_label.grid(row=0, column=0, sticky="w")

        self.zoom_var = tk.StringVar(value="适合窗口")
        zoom = ttk.Combobox(
            header,
            textvariable=self.zoom_var,
            values=["适合窗口", "正视图", "侧视图", "俯视图", "安装板图", "50%", "75%", "100%", "150%"],
            state="readonly",
            width=8,
        )
        zoom.grid(row=0, column=1, padx=(8, 8))
        zoom.bind("<<ComboboxSelected>>", lambda _e: self._schedule_fit())

        self.issue_label = ttk.Label(header, text="", foreground="#555555")
        self.issue_label.grid(row=0, column=2, sticky="e")
        ttk.Label(
            header,
            text="预览：拖动/双击编辑 · 选中后方向键微调（Shift=10mm） · Ctrl+C/V 复制粘贴 · 中键拖动画布",
            style="Hint.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self.mouse_label = ttk.Label(header, text="", foreground="#555555")
        self.mouse_label.grid(row=1, column=2, sticky="e", pady=(2, 0))

        self.preview_frame = ttk.Frame(right, relief="solid", borderwidth=1)
        self.preview_frame.grid(row=1, column=0, sticky="nsew")
        # 不让 matplotlib 画布按整张 1:1 图纸的像素尺寸撑大窗口
        self.preview_frame.grid_propagate(False)
        self.preview_frame.rowconfigure(0, weight=1)
        self.preview_frame.columnconfigure(0, weight=1)

        self.preview_view = tk.Canvas(self.preview_frame, bg="#f7f7f7", highlightthickness=0)
        self.preview_view.grid(row=0, column=0, sticky="nsew")
        self.preview_vsb = ttk.Scrollbar(
            self.preview_frame, orient="vertical", command=self.preview_view.yview
        )
        self.preview_vsb.grid(row=0, column=1, sticky="ns")
        self.preview_hsb = ttk.Scrollbar(
            self.preview_frame, orient="horizontal", command=self.preview_view.xview
        )
        self.preview_hsb.grid(row=1, column=0, sticky="ew")
        self.preview_view.configure(
            yscrollcommand=self.preview_vsb.set, xscrollcommand=self.preview_hsb.set
        )
        self.preview_view.bind("<Configure>", self._schedule_fit)
        self.preview_view.bind("<MouseWheel>", self._on_preview_wheel)
        self._canvas_item: int | None = None

        issues_box = ttk.LabelFrame(right, text="设计校验", padding=4)
        issues_box.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        issues_box.columnconfigure(0, weight=1)
        self.issues_text = tk.Text(
            issues_box, height=5, wrap="word", relief="flat", state="disabled",
            font=("Microsoft YaHei UI", 9),
        )
        self.issues_text.grid(row=0, column=0, sticky="nsew")
        isb = ttk.Scrollbar(issues_box, orient="vertical", command=self.issues_text.yview)
        isb.grid(row=0, column=1, sticky="ns")
        self.issues_text.configure(yscrollcommand=isb.set)
        self.issues_text.tag_configure("error", foreground="#b00020")
        self.issues_text.tag_configure("warn", foreground="#9a5b00")
        self.issues_text.tag_configure("ok", foreground="#146c2e")

        self._build_toolbar()

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(8, 6, 8, 8))
        bar.grid(row=1, column=0, columnspan=2, sticky="ew")
        ttk.Button(bar, text="刷新预览  F5", command=self.refresh_preview).pack(side="left")
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(bar, text="导出 PDF", command=lambda: self.export(".pdf")).pack(side="left")
        ttk.Button(bar, text="导出 SVG", command=lambda: self.export(".svg")).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="导出 PNG", command=lambda: self.export(".png")).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="导出 DXF", command=lambda: self.export(".dxf")).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="导出表格 CSV", command=self.export_tables).pack(side="left", padx=(4, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(bar, text="新建", command=self.new_job).pack(side="left")
        ttk.Button(bar, text="打开项目", command=self.open_job).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="保存项目", command=self.save_job).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="打开示例", command=self.open_demo).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="琴柜示例", command=self.open_console_demo).pack(
            side="left", padx=(4, 0)
        )

        ttk.Button(bar, text="日志", command=self.open_log).pack(side="right", padx=(8, 0))
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.status_var, foreground="#555555").pack(side="right")

    def _bind_shortcuts(self) -> None:
        self.root.bind("<F5>", lambda _e: self.refresh_preview())
        self.root.bind("<Control-s>", lambda _e: self.save_job())
        self.root.bind("<Control-o>", lambda _e: self.open_job())
        self.root.bind("<Control-e>", lambda _e: self.export(".pdf"))
        self.root.bind("<Control-z>", self._undo_event)
        self.root.bind("<Control-y>", self._redo_event)
        self.root.bind("<Control-Z>", self._redo_event)

    def _bind_auto_refresh(self) -> None:
        """柜体/图框参数改动后自动刷新预览（防抖 700ms，输入非法时静默等待）。"""
        keys = (
            "template_display", "W", "H", "D", "base_height", "console_desk_h",
            "console_top_depth", "door_gap", "door_ratio",
            "hinge", "door_margin", "door_thickness", "panel_thickness", "material",
            "color", "ip", "plate_margin", "plate_bottom_gap", "size_display",
            "scale_display", "project", "drawing_no", "version", "designer", "checker",
            "date", "company", "remark",
        )
        for key in keys:
            var = self.vars.get(key)
            if var is not None:
                var.trace_add("write", lambda *_a: self._schedule_auto_refresh())

    def _schedule_auto_refresh(self) -> None:
        if self._populating or self._restoring:
            return
        if self._auto_after is not None:
            try:
                self.root.after_cancel(self._auto_after)
            except tk.TclError:
                pass
        self._auto_after = self.root.after(700, self._auto_refresh)

    def _auto_refresh(self) -> None:
        self._auto_after = None
        if self._populating or self._restoring:
            return
        self.refresh_preview(auto=True)

    # ---------------- 柜体 / 图框 tab ----------------
    def _build_cabinet_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="  柜体 / 图框  ")
        form = ttk.Frame(tab)
        form.pack(fill="both", expand=True)
        form.columnconfigure(1, minsize=110)
        form.columnconfigure(3, minsize=110)

        r = 0
        ttk.Label(form, text="柜体参数", style="Section.TLabel").grid(
            row=r, column=0, columnspan=4, sticky="w", pady=(0, 6)
        )
        r += 1
        ttk.Label(form, text="柜型模板").grid(row=r, column=0, sticky="e", padx=(0, 5), pady=3)
        tpl_combo = ttk.Combobox(
            form,
            textvariable=self._var("template_display"),
            values=list(self.template_map),
            state="readonly",
            width=27,
        )
        tpl_combo.grid(row=r, column=1, columnspan=2, sticky="w", pady=3)
        tpl_combo.bind("<<ComboboxSelected>>", self._on_template_selected)
        ttk.Button(form, text="载入推荐尺寸", width=12, command=self._apply_template_defaults).grid(
            row=r, column=3, sticky="w", pady=3
        )
        r += 1
        self._pair_row(form, r, "宽 W", "W", "高 H", "H"); r += 1
        self._pair_row(form, r, "深 D", "D", "底座高", "base_height"); r += 1
        self._pair_row(form, r, "前沿高（琴柜）", "console_desk_h",
                       "后平顶深（琴柜）", "console_top_depth"); r += 1
        self._pair_row(form, r, "门缝", "door_gap", "左门宽比", "door_ratio"); r += 1
        self._combo_row(form, r, "铰链侧(单门)", "hinge", ["左开", "右开"], "门边距", "door_margin"); r += 1
        self._pair_row(form, r, "门厚", "door_thickness", "板厚", "panel_thickness"); r += 1
        self._pair_row(form, r, "材质", "material", "颜色", "color", wide=True); r += 1
        self._combo_row(
            form, r, "防护等级", "ip", ["IP20", "IP30", "IP54", "IP55", "IP65", "IP66"],
            "安装板边距", "plate_margin", editable=True,
        ); r += 1
        self._pair_row(form, r, "安装板下间隙", "plate_bottom_gap", "", None); r += 1

        ttk.Separator(form).grid(row=r, column=0, columnspan=4, sticky="ew", pady=8); r += 1
        ttk.Label(form, text="图框信息", style="Section.TLabel").grid(
            row=r, column=0, columnspan=4, sticky="w", pady=(0, 6)
        ); r += 1
        ttk.Label(form, text="项目名称").grid(row=r, column=0, sticky="e", padx=(0, 5), pady=3)
        ttk.Entry(form, textvariable=self._var("project"), width=46).grid(
            row=r, column=1, columnspan=3, sticky="w", pady=3
        ); r += 1
        self._pair_row(form, r, "图号", "drawing_no", "版本", "version"); r += 1
        self._pair_row(form, r, "设计", "designer", "审核", "checker"); r += 1
        self._combo_row(form, r, "图幅", "size_display", list(PAGE_CHOICES),
                        "日期（空=今天）", "date"); r += 1
        self._combo_row(form, r, "比例", "scale_display", list(SCALE_CHOICES),
                        "单位名称", "company"); r += 1
        ttk.Label(form, text="备注").grid(row=r, column=0, sticky="e", padx=(0, 5), pady=3)
        ttk.Entry(form, textvariable=self._var("remark"), width=46).grid(
            row=r, column=1, columnspan=3, sticky="w", pady=3
        ); r += 1

        ttk.Separator(form).grid(row=r, column=0, columnspan=4, sticky="ew", pady=8); r += 1
        ttk.Label(form, text="输出视图").grid(row=r, column=0, sticky="e", padx=(0, 5), pady=3)
        vf = ttk.Frame(form)
        vf.grid(row=r, column=1, columnspan=3, sticky="w", pady=3)
        defaults = {"front": True, "side": True, "plan": True, "internal": True}
        for key, label in VIEW_LABELS.items():
            var = tk.BooleanVar(value=defaults[key])
            self.view_vars[key] = var
            ttk.Checkbutton(vf, text=label, variable=var).pack(side="left", padx=(0, 12))
        ttk.Separator(vf, orient="vertical").pack(side="left", fill="y", padx=(0, 12))
        ttk.Checkbutton(
            vf, text="标注开孔尺寸", variable=self.show_sizes_var,
            command=self.refresh_preview,
        ).pack(side="left")
        r += 1
        ttk.Label(
            form,
            text="提示：开孔坐标以柜体正面左下角为基准；安装板/导轨/线槽以安装板左下角为基准。",
            style="Hint.TLabel",
            wraplength=440,
            justify="left",
        ).grid(row=r, column=0, columnspan=4, sticky="w", pady=(10, 0))

    def _var(self, name: str) -> tk.StringVar:
        if name not in self.vars:
            self.vars[name] = tk.StringVar()
        return self.vars[name]

    def _pair_row(self, parent, row, l1, k1, l2, k2, wide=False):
        ttk.Label(parent, text=l1).grid(row=row, column=0, sticky="e", padx=(0, 5), pady=3)
        ttk.Entry(parent, textvariable=self._var(k1), width=20 if wide else 12).grid(
            row=row, column=1, sticky="w", pady=3
        )
        if k2:
            ttk.Label(parent, text=l2).grid(row=row, column=2, sticky="e", padx=(10, 5), pady=3)
            ttk.Entry(parent, textvariable=self._var(k2), width=20 if wide else 12).grid(
                row=row, column=3, sticky="w", pady=3
            )

    def _combo_row(self, parent, row, l1, k1, values, l2, k2, editable=False):
        ttk.Label(parent, text=l1).grid(row=row, column=0, sticky="e", padx=(0, 5), pady=3)
        ttk.Combobox(
            parent,
            textvariable=self._var(k1),
            values=values,
            state="normal" if editable else "readonly",
            width=10,
        ).grid(row=row, column=1, sticky="w", pady=3)
        ttk.Label(parent, text=l2).grid(row=row, column=2, sticky="e", padx=(10, 5), pady=3)
        ttk.Entry(parent, textvariable=self._var(k2), width=15).grid(
            row=row, column=3, sticky="w", pady=3
        )

    def _apply_template_defaults(self) -> None:
        key = self.template_map.get(self.vars["template_display"].get())
        if not key:
            return
        for name, value in self.lib.template_defaults(key).items():
            if name in self.vars:
                self.vars[name].set(num(float(value)))
        if key == "wallbox" and "base_height" in self.vars:
            self.vars["base_height"].set("0")
        self.status_var.set(f"已载入模板推荐尺寸：{self.vars['template_display'].get()}")

    def _on_template_selected(self, _event=None) -> None:
        """切换柜型时载入推荐尺寸；切到琴柜且已有数据时提示处理。"""
        self._apply_template_defaults()
        if self._auto_after is not None:
            try:
                self.root.after_cancel(self._auto_after)
            except tk.TclError:
                pass
            self._auto_after = None
        key = self.template_map.get(self.vars["template_display"].get())
        tname = self.vars["template_display"].get()
        if key == "console" and (self.cutouts or self.rails):
            clear = messagebox.askyesno(
                "切换到琴式操作台",
                "琴柜正面是“门板 + 斜面展开”的展开图，坐标基准与立式柜不同。\n"
                f"当前已有 {len(self.cutouts)} 个开孔、{len(self.rails)} 条导轨，"
                "直接沿用可能出现“超出正面展开范围”等校验提示。\n\n"
                "是否清空已有开孔和导轨，按琴柜重新布置？\n"
                "选择“否”将保留数据，可稍后手动删除或调整。",
                parent=self.root,
            )
            if clear:
                self.cutouts.clear()
                self.rails.clear()
                self._refresh_cutout_tree()
                self._refresh_rail_tree()
                self.status_var.set(f"已切换到“{tname}”，并清空原有开孔和导轨。")
            else:
                self.status_var.set(
                    f"已切换到“{tname}”；原开孔/导轨已保留，请检查是否超出斜面展开范围。"
                )
        else:
            self.status_var.set(f"已切换到“{tname}”，并载入推荐尺寸。")
        self.refresh_preview()

    # ---------------- 开孔 tab ----------------
    def _build_cutout_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="  门板开孔  ")
        tab.rowconfigure(4, weight=1)
        tab.columnconfigure(0, weight=1)

        bar = ttk.Frame(tab)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(bar, text="添加 / 阵列", command=self.add_cutout).pack(side="left")
        ttk.Button(bar, text="编辑", command=self.edit_cutout).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="删除", command=self.delete_cutout).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="清空", command=self.clear_cutouts).pack(side="left", padx=(4, 0))

        ttk.Label(
            tab,
            text="坐标基准随“面”变化：正面 X 距左/Y 距下，侧面 X 距前/Y 距下，顶底面 X 距左/Y 距前；双击一行可编辑，也可在右侧预览中直接拖动开孔、双击空白处添加。",
            style="Hint.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(0, 4))

        wrap = ttk.Frame(tab)
        wrap.grid(row=4, column=0, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        cols = ("no", "face", "name", "spec", "x", "y", "label", "note")
        self.cutout_tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="extended")
        heads = {
            "no": ("序号", 40), "face": ("面", 44), "name": ("元件", 105),
            "spec": ("开孔尺寸", 82), "x": ("X", 48), "y": ("Y", 48),
            "label": ("代号", 68), "note": ("备注", 90),
        }
        for key, (text, width) in heads.items():
            self.cutout_tree.heading(key, text=text)
            self.cutout_tree.column(key, width=width, anchor="center" if key != "name" else "w")
        self.cutout_tree.grid(row=0, column=0, sticky="nsew")
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=self.cutout_tree.yview)
        ysb.grid(row=0, column=1, sticky="ns")
        xsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.cutout_tree.xview)
        xsb.grid(row=1, column=0, sticky="ew")
        self.cutout_tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.cutout_tree.bind("<Double-1>", lambda _e: self.edit_cutout())
        self.cutout_tree.bind("<Delete>", lambda _e: self.delete_cutout())
        self.cutout_tree.bind("<Control-c>", self._copy_event)
        self.cutout_tree.bind("<Control-v>", self._paste_event)

        self.cutout_count = ttk.Label(tab, text="", style="Hint.TLabel")
        self.cutout_count.grid(row=3, column=0, sticky="w", pady=(0, 4))

        adj = ttk.LabelFrame(tab, text="位置微调（选中开孔后可用滑杆左右 / 上下移动）", padding=(8, 5))
        adj.grid(row=2, column=0, sticky="ew", pady=(4, 6))
        adj.columnconfigure(1, weight=1)
        self.pos_title = ttk.Label(adj, text="未选择开孔", foreground="#555555")
        self.pos_title.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 3))

        self.pos_x_val = tk.StringVar(value="—")
        self.pos_y_val = tk.StringVar(value="—")
        self.pos_x_label = ttk.Label(adj, text="X 距左")
        self.pos_x_label.grid(row=1, column=0, sticky="e", padx=(0, 6))
        self.pos_x_scale = ttk.Scale(
            adj, from_=0.0, to=1000.0, orient="horizontal",
            command=self._on_pos_x, state="disabled",
        )
        self.pos_x_scale.grid(row=1, column=1, sticky="ew", pady=1)
        self.pos_x_entry = ttk.Entry(
            adj, textvariable=self.pos_x_val, width=7, justify="right",
            state="disabled",
        )
        self.pos_x_entry.grid(row=1, column=2, sticky="w", padx=(6, 0))
        self.pos_x_entry.bind("<Return>", lambda _e: self._apply_entry("x"))
        self.pos_x_entry.bind("<FocusOut>", lambda _e: self._apply_entry("x"))
        self.pos_y_label = ttk.Label(adj, text="Y 距下")
        self.pos_y_label.grid(row=2, column=0, sticky="e", padx=(0, 6))
        self.pos_y_scale = ttk.Scale(
            adj, from_=0.0, to=1000.0, orient="horizontal",
            command=self._on_pos_y, state="disabled",
        )
        self.pos_y_scale.grid(row=2, column=1, sticky="ew", pady=1)
        self.pos_y_entry = ttk.Entry(
            adj, textvariable=self.pos_y_val, width=7, justify="right",
            state="disabled",
        )
        self.pos_y_entry.grid(row=2, column=2, sticky="w", padx=(6, 0))
        self.pos_y_entry.bind("<Return>", lambda _e: self._apply_entry("y"))
        self.pos_y_entry.bind("<FocusOut>", lambda _e: self._apply_entry("y"))
        self.pos_dist = ttk.Label(adj, text="", style="Hint.TLabel")
        self.pos_dist.grid(row=3, column=0, columnspan=3, sticky="w", pady=(2, 0))

        self.cutout_tree.bind("<<TreeviewSelect>>", self._on_cutout_select)

    def _refresh_cutout_tree(self) -> None:
        tree = self.cutout_tree
        tree.delete(*tree.get_children())
        for i, cut in enumerate(self.cutouts):
            rc = self.lib.resolve_cutout(cut, i)
            tree.insert(
                "", "end", iid=str(i),
                values=(
                    i + 1, FACE_LABELS.get(cutout_face(cut), "正面"),
                    rc.name, rc.spec, num(cut.x), num(cut.y), cut.label, cut.note,
                ),
            )
        self.cutout_count.configure(
            text=f"共 {len(self.cutouts)} 个开孔；阵列生成后可在表中逐条修改。"
        )
        self._sync_position_panel()

    def _on_cutout_select(self, _event=None) -> None:
        sel = self.cutout_tree.selection()
        if sel:
            try:
                self._last_selected = ("cutout", int(sel[-1]))
            except ValueError:
                pass
        self._sync_position_panel()

    def _sync_position_panel(self) -> None:
        sel: list[int] = []
        for item in self.cutout_tree.selection():
            try:
                idx = int(item)
            except ValueError:
                continue
            if 0 <= idx < len(self.cutouts):
                sel.append(idx)
        self._pos_indices = sel
        if not sel:
            self._pos_sync = True
            self.pos_x_scale.configure(state="disabled")
            self.pos_y_scale.configure(state="disabled")
            self.pos_x_entry.configure(state="disabled")
            self.pos_y_entry.configure(state="disabled")
            self._pos_sync = False
            self.pos_title.configure(text="未选择开孔")
            self.pos_x_val.set("—")
            self.pos_y_val.set("—")
            self.pos_dist.configure(text="")
            return
        idx = sel[0]
        cut = self.cutouts[idx]
        cab = self._current_cabinet()
        face = cutout_face(cut)
        fw, fh = face_dims(cab, face)
        self._pos_sync = True
        self.pos_x_scale.configure(
            state="normal", from_=min(0.0, cut.x), to=max(float(fw), cut.x)
        )
        self.pos_y_scale.configure(
            state="normal", from_=min(0.0, cut.y), to=max(float(fh), cut.y)
        )
        self.pos_x_scale.set(cut.x)
        self.pos_y_scale.set(cut.y)
        self.pos_x_entry.configure(state="normal")
        self.pos_y_entry.configure(state="normal")
        self._pos_sync = False
        ax, ay, _x0, _x1, _y0, _y1 = face_edge_labels(face)
        self.pos_x_label.configure(text=ax)
        self.pos_y_label.configure(text=ay)
        name = self.lib.resolve_cutout(cut, idx).name
        face_name = FACE_LABELS.get(face, "正面")
        extra = f"（共 {len(sel)} 个，将一起移动）" if len(sel) > 1 else ""
        self.pos_title.configure(text=f"{idx + 1}# [{face_name}] {name}{extra}")
        self._update_pos_labels(cut, cab)

    def _update_pos_labels(self, cut: Cutout, cab: Cabinet | None = None) -> None:
        cab = cab or self._current_cabinet()
        face = cutout_face(cut)
        fw, fh = face_dims(cab, face)
        self.pos_x_val.set(num(cut.x))
        self.pos_y_val.set(num(cut.y))
        _ax, _ay, x0n, x1n, y0n, y1n = face_edge_labels(face)
        self.pos_dist.configure(
            text=(
                f"{x0n} {cut.x:.0f} · {x1n} {fw - cut.x:.0f} · "
                f"{y0n} {cut.y:.0f} · {y1n} {fh - cut.y:.0f} mm"
            )
        )

    def _on_pos_x(self, value: str) -> None:
        self._apply_pos("x", value)

    def _on_pos_y(self, value: str) -> None:
        self._apply_pos("y", value)

    def _apply_entry(self, axis: str) -> None:
        if not self._pos_indices:
            return
        var = self.pos_x_val if axis == "x" else self.pos_y_val
        self._apply_pos(axis, var.get())
        idx = self._pos_indices[0]
        if 0 <= idx < len(self.cutouts):
            cut = self.cutouts[idx]
            var.set(num(cut.x if axis == "x" else cut.y))

    def _apply_pos(self, axis: str, value: str) -> None:
        if self._pos_sync or not self._pos_indices:
            return
        try:
            new = float(value)
        except ValueError:
            return
        idx = self._pos_indices[0]
        if not (0 <= idx < len(self.cutouts)):
            return
        cut = self.cutouts[idx]
        cab = self._current_cabinet()
        current = cut.x if axis == "x" else cut.y
        delta = new - current
        if abs(delta) < 1e-9:
            return
        for i in self._pos_indices:
            c = self.cutouts[i]
            fw, fh = face_dims(cab, cutout_face(c))
            limit = float(fw if axis == "x" else fh)
            if axis == "x":
                c.x = min(max(c.x + delta, 0.0), limit)
                self.cutout_tree.set(str(i), "x", num(c.x))
            else:
                c.y = min(max(c.y + delta, 0.0), limit)
                self.cutout_tree.set(str(i), "y", num(c.y))
        if (axis == "x" and abs(cut.x - new) > 0.01) or (
            axis == "y" and abs(cut.y - new) > 0.01
        ):
            self._pos_sync = True
            (self.pos_x_scale if axis == "x" else self.pos_y_scale).set(
                cut.x if axis == "x" else cut.y
            )
            self._pos_sync = False
        self._update_pos_labels(cut, cab)
        self.status_var.set(
            f"位置微调（{FACE_LABELS.get(cutout_face(cut), '正面')}）："
            f"X {cut.x:.0f} · Y {cut.y:.0f} mm"
        )
        self._schedule_pos_refresh()

    def _schedule_pos_refresh(self) -> None:
        if self._pos_job is not None:
            return
        self._pos_job = self.root.after(120, self._pos_refresh)

    def _pos_refresh(self) -> None:
        self._pos_job = None
        if self._pos_indices:
            self.refresh_preview()

    def _current_cabinet(self) -> Cabinet:
        try:
            return self.collect_job().cabinet
        except Exception:
            if self._last_job is not None:
                return self._last_job.cabinet
            return Cabinet()

    def add_cutout(self) -> None:
        self._add_cutout_dialog()

    def add_cutout_at(self, x: float, y: float, face: str = "front") -> None:
        self._add_cutout_dialog(initial=(x, y), face=face)

    def _add_cutout_dialog(self, initial: tuple[float, float] | None = None,
                           face: str = "front") -> None:
        dlg = CutoutDialog(
            self.root,
            self.lib,
            cabinet=self._current_cabinet(),
            existing=self.cutouts,
            initial=initial,
            face=face,
        )
        self.root.wait_window(dlg)
        if dlg.result:
            first = len(self.cutouts)
            self.cutouts.extend(dlg.result)
            self._refresh_cutout_tree()
            self._select_cutout(first)
            self.refresh_preview()

    def edit_cutout(self) -> None:
        sel = self.cutout_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在表格中选择一个开孔。", parent=self.root)
            return
        idx = int(sel[0])
        dlg = CutoutDialog(
            self.root,
            self.lib,
            self.cutouts[idx],
            cabinet=self._current_cabinet(),
            existing=self.cutouts,
        )
        self.root.wait_window(dlg)
        if dlg.result:
            self.cutouts[idx] = dlg.result[0]
            self._refresh_cutout_tree()
            self._select_cutout(idx)
            self.refresh_preview()

    def delete_cutout(self) -> None:
        sel = self.cutout_tree.selection()
        if not sel:
            return
        if not messagebox.askyesno("删除确认", f"确定删除选中的 {len(sel)} 个开孔？", parent=self.root):
            return
        for iid in sorted((int(s) for s in sel), reverse=True):
            self.cutouts.pop(iid)
        self._refresh_cutout_tree()
        self.refresh_preview()

    def clear_cutouts(self) -> None:
        if not self.cutouts:
            return
        if messagebox.askyesno("清空确认", "确定清空全部开孔？", parent=self.root):
            self.cutouts.clear()
            self._refresh_cutout_tree()
            self.refresh_preview()

    # ---------------- 内部布置 tab ----------------
    def _build_internal_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="  内部布置  ")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=3)
        tab.rowconfigure(1, weight=2)

        rail_box = ttk.LabelFrame(tab, text="导轨与器件", padding=6)
        rail_box.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        rail_box.columnconfigure(0, weight=1)
        rail_box.rowconfigure(1, weight=1)

        bar = ttk.Frame(rail_box)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Button(bar, text="添加导轨", command=self.add_rail).pack(side="left")
        ttk.Button(bar, text="快速放器件", command=self.quick_add_device).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="编辑", command=self.edit_rail).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="删除", command=self.delete_rail).pack(side="left", padx=(4, 0))
        self.plate_label = ttk.Label(bar, text="", style="Hint.TLabel")
        self.plate_label.pack(side="right")

        cols = ("no", "y", "x", "length", "devices")
        self.rail_tree = ttk.Treeview(rail_box, columns=cols, show="headings", selectmode="extended")
        heads = {
            "no": ("导轨", 50), "y": ("Y", 55), "x": ("起点 X", 60),
            "length": ("长度", 60), "devices": ("器件", 210),
        }
        for key, (text, width) in heads.items():
            self.rail_tree.heading(key, text=text)
            self.rail_tree.column(key, width=width, anchor="center" if key != "devices" else "w")
        self.rail_tree.grid(row=1, column=0, sticky="nsew")
        rsb = ttk.Scrollbar(rail_box, orient="vertical", command=self.rail_tree.yview)
        rsb.grid(row=1, column=1, sticky="ns")
        self.rail_tree.configure(yscrollcommand=rsb.set)
        self.rail_tree.bind("<Double-1>", lambda _e: self.edit_rail())
        self.rail_tree.bind("<Delete>", lambda _e: self.delete_rail())
        self.rail_tree.bind("<Control-c>", self._copy_event)
        self.rail_tree.bind("<Control-v>", self._paste_event)
        self.rail_tree.bind(
            "<<TreeviewSelect>>", lambda _e: self._on_internal_tree_select("rail")
        )

        duct_box = ttk.LabelFrame(tab, text="线槽", padding=6)
        duct_box.grid(row=1, column=0, sticky="nsew")
        duct_box.columnconfigure(0, weight=1)
        duct_box.rowconfigure(1, weight=1)
        dbar = ttk.Frame(duct_box)
        dbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Button(dbar, text="添加线槽", command=self.add_duct).pack(side="left")
        ttk.Button(dbar, text="编辑", command=self.edit_duct).pack(side="left", padx=(4, 0))
        ttk.Button(dbar, text="删除", command=self.delete_duct).pack(side="left", padx=(4, 0))
        ttk.Label(
            dbar,
            text="安装板左下角为原点；预览中单击选中后拖动或方向键微调（Shift=10mm），Ctrl+C/V 复制粘贴。",
            style="Hint.TLabel",
        ).pack(side="right")

        dcols = ("no", "orient", "x", "y", "length", "width")
        self.duct_tree = ttk.Treeview(duct_box, columns=dcols, show="headings", selectmode="extended", height=4)
        dheads = {
            "no": ("序号", 50), "orient": ("方向", 55), "x": ("X", 55),
            "y": ("Y", 55), "length": ("长度", 60), "width": ("宽度", 55),
        }
        for key, (text, width) in dheads.items():
            self.duct_tree.heading(key, text=text)
            self.duct_tree.column(key, width=width, anchor="center")
        self.duct_tree.grid(row=1, column=0, sticky="nsew")
        dsb = ttk.Scrollbar(duct_box, orient="vertical", command=self.duct_tree.yview)
        dsb.grid(row=1, column=1, sticky="ns")
        self.duct_tree.configure(yscrollcommand=dsb.set)
        self.duct_tree.bind("<Double-1>", lambda _e: self.edit_duct())
        self.duct_tree.bind("<Delete>", lambda _e: self.delete_duct())
        self.duct_tree.bind("<Control-c>", self._copy_event)
        self.duct_tree.bind("<Control-v>", self._paste_event)
        self.duct_tree.bind(
            "<<TreeviewSelect>>", lambda _e: self._on_internal_tree_select("duct")
        )

    def _refresh_rail_tree(self) -> None:
        tree = self.rail_tree
        tree.delete(*tree.get_children())
        for i, rail in enumerate(self.rails):
            tree.insert(
                "", "end", iid=str(i),
                values=(
                    f"L{i + 1}", num(rail.y),
                    "自动" if rail.x is None else num(rail.x),
                    "自动" if rail.length is None else num(rail.length),
                    format_device_expr(rail.devices),
                ),
            )
        self.plate_label.configure(text=f"导轨 {len(self.rails)} 条")

    def _refresh_duct_tree(self) -> None:
        tree = self.duct_tree
        tree.delete(*tree.get_children())
        for i, d in enumerate(self.ducts):
            tree.insert(
                "", "end", iid=str(i),
                values=(i + 1, "垂直" if d.orient == "v" else "水平",
                        num(d.x), num(d.y), num(d.length), num(d.width)),
            )

    def add_rail(self) -> None:
        cab = self._current_cabinet()
        dlg = RailDialog(self.root, self.lib, plate=(cab.plate_w, cab.plate_h))
        self.root.wait_window(dlg)
        if dlg.result:
            self.rails.append(dlg.result)
            self._refresh_rail_tree()
            self.refresh_preview()

    def quick_add_device(self) -> None:
        dlg = QuickDeviceDialog(
            self.root, self.lib, self.rails, cab=self._current_cabinet()
        )
        self.root.wait_window(dlg)
        if dlg.changed:
            self._refresh_rail_tree()
            self.refresh_preview()

    def edit_rail(self) -> None:
        sel = self.rail_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择一个导轨。", parent=self.root)
            return
        idx = int(sel[0])
        cab = self._current_cabinet()
        dlg = RailDialog(
            self.root, self.lib, self.rails[idx],
            plate=(cab.plate_w, cab.plate_h),
        )
        self.root.wait_window(dlg)
        if dlg.result:
            self.rails[idx] = dlg.result
            self._refresh_rail_tree()
            self.refresh_preview()

    def delete_rail(self) -> None:
        sel = self.rail_tree.selection()
        if not sel:
            return
        if not messagebox.askyesno("删除确认", f"确定删除选中的 {len(sel)} 条导轨？", parent=self.root):
            return
        for iid in sorted((int(s) for s in sel), reverse=True):
            self.rails.pop(iid)
        self._refresh_rail_tree()
        self.refresh_preview()

    def add_duct(self) -> None:
        dlg = DuctDialog(self.root)
        self.root.wait_window(dlg)
        if dlg.result:
            self.ducts.append(dlg.result)
            self._refresh_duct_tree()
            self.refresh_preview()

    def edit_duct(self) -> None:
        sel = self.duct_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择一条线槽。", parent=self.root)
            return
        idx = int(sel[0])
        dlg = DuctDialog(self.root, self.ducts[idx])
        self.root.wait_window(dlg)
        if dlg.result:
            self.ducts[idx] = dlg.result
            self._refresh_duct_tree()
            self.refresh_preview()

    def delete_duct(self) -> None:
        sel = self.duct_tree.selection()
        if not sel:
            return
        for iid in sorted((int(s) for s in sel), reverse=True):
            self.ducts.pop(iid)
        self._refresh_duct_tree()
        self.refresh_preview()

    # ---------------- 技术要求 tab ----------------
    def _build_requirements_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="  技术要求  ")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(3, weight=1)

        row = ttk.Frame(tab)
        row.grid(row=0, column=0, sticky="ew")
        ttk.Label(row, text="模板").pack(side="left")
        req_combo = ttk.Combobox(
            row, textvariable=self._var("req_display"), values=list(self.req_map),
            state="readonly", width=26,
        )
        req_combo.pack(side="left", padx=(6, 0))
        req_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_req_hint())
        self.req_hint = ttk.Label(tab, text="", style="Hint.TLabel")
        self.req_hint.grid(row=1, column=0, sticky="w", pady=(4, 6))

        ttk.Label(
            tab,
            text="追加条目（每行一条，排在模板条目之后；支持 {W} {H} {D} {t} {material} {color} {ip} 等变量）",
        ).grid(row=2, column=0, sticky="w")
        text_wrap = ttk.Frame(tab)
        text_wrap.grid(row=3, column=0, sticky="nsew", pady=(4, 0))
        tab.rowconfigure(3, weight=1)
        text_wrap.columnconfigure(0, weight=1)
        text_wrap.rowconfigure(0, weight=1)
        self.req_text = tk.Text(
            text_wrap, height=10, wrap="word", relief="solid", borderwidth=1,
            font=("Microsoft YaHei UI", 9), undo=True,
        )
        self.req_text.grid(row=0, column=0, sticky="nsew")
        tsb = ttk.Scrollbar(text_wrap, orient="vertical", command=self.req_text.yview)
        tsb.grid(row=0, column=1, sticky="ns")
        self.req_text.configure(yscrollcommand=tsb.set)
        ttk.Label(
            tab,
            text="示例：本图为壳体接口确认图，不含电气原理及钣金展开图；批量生产前请双方会签。",
            style="Hint.TLabel", wraplength=450, justify="left",
        ).grid(row=4, column=0, sticky="w", pady=(6, 0))

    # ---------------- 数据绑定 ----------------
    def populate_job(self, job: Job) -> None:
        self._populating = True
        if self._auto_after is not None:
            try:
                self.root.after_cancel(self._auto_after)
            except tk.TclError:
                pass
            self._auto_after = None
        try:
            self._populate_job(job)
        finally:
            self._populating = False

    def _populate_job(self, job: Job) -> None:
        cab = job.cabinet
        tname = self.lib.template_name(cab.template)
        if tname not in self.template_map:
            tname = self.lib.template_name("vertical_double")
        if tname not in self.template_map:
            tname = next(iter(self.template_map), "")
        self.vars["template_display"].set(tname)
        for key in (
            "W", "H", "D", "base_height", "console_desk_h", "console_top_depth",
            "door_gap", "door_ratio", "door_margin",
            "door_thickness", "panel_thickness", "plate_margin", "plate_bottom_gap",
        ):
            self.vars[key].set(num(float(getattr(cab, key))))
        for key in ("material", "color", "ip"):
            self.vars[key].set(str(getattr(cab, key)))
        self.vars["hinge"].set("左开" if cab.hinge == "left" else "右开")

        sh = job.sheet
        for key in ("project", "drawing_no", "version", "designer", "checker", "date", "company", "remark"):
            self.vars[key].set(str(getattr(sh, key)))
        self.vars["size_display"].set(next((k for k, v in PAGE_CHOICES.items() if v == sh.size), "自动"))
        self.vars["scale_display"].set(next((k for k, v in SCALE_CHOICES.items() if v == sh.scale), "自动"))

        rname = str(self.lib.requirements.get(job.req_set, {}).get("name", "通用控制柜"))
        if rname not in self.req_map:
            rname = str(self.lib.requirements.get("standard", {}).get("name", ""))
        if rname not in self.req_map:
            rname = next(iter(self.req_map), "")
        self.vars["req_display"].set(rname)
        self._update_req_hint()

        self.req_text.delete("1.0", "end")
        self.req_text.insert("1.0", "\n".join(job.custom_requirements))

        for key, var in self.view_vars.items():
            var.set(key in job.views)

        self.cutouts = list(job.cutouts)
        self.rails = list(job.rails)
        self.ducts = list(job.ducts)
        self._refresh_cutout_tree()
        self._refresh_rail_tree()
        self._refresh_duct_tree()

    def _update_req_hint(self) -> None:
        key = self.req_map.get(self.vars["req_display"].get(), "standard")
        items = self.lib.requirements.get(key, {}).get("items", [])
        self.req_hint.configure(text=f"当前模板 {len(items)} 条；变量会在生成时自动替换。")

    def collect_job(self) -> Job:
        template = self.template_map.get(self.vars["template_display"].get())
        if not template:
            raise ValueError("请选择柜型模板。")
        cab = Cabinet(
            template=template,
            W=parse_num(self.vars["W"].get(), "柜宽 W", positive=True),
            H=parse_num(self.vars["H"].get(), "柜高 H", positive=True),
            D=parse_num(self.vars["D"].get(), "柜深 D", positive=True),
            base_height=parse_num(self.vars["base_height"].get(), "底座高", default=0.0),
            console_desk_h=parse_num(
                self.vars["console_desk_h"].get(), "琴柜前沿高", default=720.0
            ),
            console_top_depth=parse_num(
                self.vars["console_top_depth"].get(), "琴柜后平顶深", default=250.0
            ),
            door_gap=parse_num(self.vars["door_gap"].get(), "门缝", default=2.0),
            door_ratio=parse_num(self.vars["door_ratio"].get(), "左门宽比", default=0.5),
            hinge="left" if self.vars["hinge"].get() == "左开" else "right",
            door_margin=parse_num(self.vars["door_margin"].get(), "门边距", default=12.0),
            door_thickness=parse_num(self.vars["door_thickness"].get(), "门厚", default=25.0),
            panel_thickness=parse_num(self.vars["panel_thickness"].get(), "板厚", default=1.5),
            material=self.vars["material"].get().strip() or "冷轧钢板",
            color=self.vars["color"].get().strip() or "RAL7035",
            ip=self.vars["ip"].get().strip() or "IP54",
            plate_margin=parse_num(self.vars["plate_margin"].get(), "安装板边距", default=30.0),
            plate_bottom_gap=parse_num(self.vars["plate_bottom_gap"].get(), "安装板下间隙", default=50.0),
        )
        if not (0 < cab.door_ratio < 1):
            raise ValueError("左门宽比应在 0～1 之间（双开门常用 0.5）。")

        sheet = SheetInfo(
            size=PAGE_CHOICES.get(self.vars["size_display"].get(), "auto"),
            scale=SCALE_CHOICES.get(self.vars["scale_display"].get(), "auto"),
            project=self.vars["project"].get().strip(),
            drawing_no=self.vars["drawing_no"].get().strip(),
            version=self.vars["version"].get().strip(),
            designer=self.vars["designer"].get().strip(),
            checker=self.vars["checker"].get().strip(),
            date=self.vars["date"].get().strip(),
            company=self.vars["company"].get().strip(),
            remark=self.vars["remark"].get().strip(),
        )
        views = [k for k, var in self.view_vars.items() if var.get()]
        if not views:
            raise ValueError("请至少选择一个输出视图。")
        custom = [
            line.strip() for line in self.req_text.get("1.0", "end-1c").splitlines()
            if line.strip()
        ]
        req_key = self.req_map.get(self.vars["req_display"].get(), "standard")
        return Job(
            cabinet=cab,
            cutouts=list(self.cutouts),
            rails=list(self.rails),
            ducts=list(self.ducts),
            sheet=sheet,
            req_set=req_key,
            custom_requirements=custom,
            views=views,
        )

    # ---------------- 预览与导出 ----------------
    def _build(self, job: Job, fig=None):
        return build_drawing(
            job,
            self.lib,
            fig=fig,
            show_cutout_sizes=self.show_sizes_var.get(),
        )

    def refresh_preview(self, auto: bool = False, source: str = "manual") -> None:
        try:
            job = self.collect_job()
        except ValueError as e:
            if auto:
                self.status_var.set(f"参数未完成，预览暂不更新：{e}")
            else:
                messagebox.showerror("参数有误", str(e), parent=self.root)
            return
        try:
            fig, meta = self._build(
                job, fig=self._fig if self._canvas is not None else None
            )
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._recover_preview()
            msg = f"预览更新失败：{e}"
            self.status_var.set(msg + "（已保留上一张图）")
            if not auto:
                messagebox.showerror(
                    "生成失败", msg + "\n\n详细信息已写入日志。", parent=self.root
                )
            return
        self._last_job = job
        self._last_good_job = Job.from_dict(job.to_dict())
        self._record_snapshot(job, source)
        self._internal_cache = internal_layout(job.cabinet, job.rails, job.ducts, self.lib)
        self._show_figure(fig, meta)
        self._show_issues(meta)
        self._update_plate_label(job)
        self.page_label.configure(
            text=f"图幅 {meta['page']}    比例 {meta['scale']}    {job.cabinet.W:.0f}×{job.cabinet.H:.0f}×{job.cabinet.D:.0f}"
        )

    def _recover_preview(self) -> None:
        """生成失败时把上一张成功的图重绘回同一 Figure，避免预览空白。"""
        if self._fig is None or self._canvas is None or self._last_good_job is None:
            return
        try:
            self._build(self._last_good_job, fig=self._fig)
            self._canvas.draw_idle()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._show_error_on_figure("上一张图也无法恢复，请检查参数或查看日志。")

    def _show_error_on_figure(self, message: str) -> None:
        if self._fig is None or self._canvas is None:
            return
        fig = self._fig
        fig.clear()
        fig.patch.set_facecolor("white")
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis("off")
        ax.text(0.5, 0.56, "图纸生成失败", fontsize=22, ha="center", va="center", color="#b00020")
        ax.text(0.5, 0.47, message, fontsize=10, ha="center", va="center",
                wrap=True, color="#555555")
        self._canvas.draw_idle()

    # ---------------- 撤销 / 重做 ----------------
    def _record_snapshot(self, job: Job, source: str = "manual") -> None:
        """每次成功刷新后记录状态；连续自动刷新会合并，避免撤销栈被逐字符填满。"""
        d = job.to_dict()
        now = time.monotonic()
        if self._restoring:
            self._snapshot_dict = d
            self._snapshot_ts = now
            return
        if self._snapshot_dict is None:
            self._snapshot_dict = d
            self._snapshot_ts = now
            return
        if d == self._snapshot_dict:
            return
        if source == "auto" and self._undo_stack and now - self._snapshot_ts < 2.0:
            self._snapshot_dict = d
            self._snapshot_ts = now
            return
        self._undo_stack.append(self._snapshot_dict)
        if len(self._undo_stack) > 100:
            self._undo_stack.pop(0)
        self._redo_stack.clear()
        self._snapshot_dict = d
        self._snapshot_ts = now

    def _undo_event(self, _event=None):
        self.undo()
        return "break"

    def _redo_event(self, _event=None):
        self.redo()
        return "break"

    def undo(self) -> None:
        if not self._undo_stack:
            self.status_var.set("没有可撤销的操作。")
            return
        target = self._undo_stack.pop()
        if self._snapshot_dict is not None:
            self._redo_stack.append(self._snapshot_dict)
        self._apply_state(target, "已撤销")

    def redo(self) -> None:
        if not self._redo_stack:
            self.status_var.set("没有可重做的操作。")
            return
        target = self._redo_stack.pop()
        if self._snapshot_dict is not None:
            self._undo_stack.append(self._snapshot_dict)
        self._apply_state(target, "已重做")

    def _apply_state(self, data: dict[str, Any], prefix: str) -> None:
        self._restoring = True
        try:
            self.populate_job(Job.from_dict(data))
            self.refresh_preview(source="restore")
        finally:
            self._restoring = False
        self.status_var.set(f"{prefix}；Ctrl+Y 可重做，Ctrl+Z 可继续撤销。")

    def _update_plate_label(self, job: Job) -> None:
        cab = job.cabinet
        self.plate_label.configure(
            text=f"安装板 {num(cab.plate_w)}×{num(cab.plate_h)} mm"
        )

    def _show_figure(self, fig, meta: dict[str, Any] | None = None) -> None:
        if meta is not None:
            self._meta = meta
        self._fig = fig
        self._fig_inches = tuple(fig.get_size_inches())

        # 关键：预览画布复用同一个 FigureCanvasTkAgg，仅重绘内容。
        # 之前每次刷新都 destroy + 新建 Tk 画布，窗口会闪一下。
        if self._canvas is not None and getattr(self._canvas, "figure", None) is fig:
            if self._fit_job is not None:
                try:
                    self.root.after_cancel(self._fit_job)
                except tk.TclError:
                    pass
                self._fit_job = None
            # 立即按当前缩放模式重排一次；窗口太小则等 Configure 事件再排。
            self._fit_preview()
            if self._preview_px[0] <= 100 or self._preview_px[1] <= 100:
                self._schedule_fit()
            return

        had_focus = False
        if self._canvas is not None:
            try:
                had_focus = self.root.focus_get() is self._canvas.get_tk_widget()
            except Exception:
                pass
        if self._canvas is not None:
            try:
                self._canvas.get_tk_widget().destroy()
            except tk.TclError:
                pass
            self._canvas = None
        self.preview_view.delete("all")
        self._canvas_item = None

        self._canvas = FigureCanvasTkAgg(fig, master=self.preview_view)
        self._canvas.mpl_connect("button_press_event", self._on_preview_press)
        self._canvas.mpl_connect("button_release_event", self._on_preview_release)
        self._canvas.mpl_connect("motion_notify_event", self._on_preview_motion)
        self._canvas.mpl_connect("figure_leave_event", self._on_preview_leave)
        widget = self._canvas.get_tk_widget()
        widget.configure(takefocus=True)
        widget.bind("<MouseWheel>", self._on_preview_wheel)
        widget.bind("<Shift-MouseWheel>", self._on_preview_wheel_h)
        widget.bind("<Button-2>", self._pan_start)
        widget.bind("<B2-Motion>", self._pan_move)
        widget.bind("<ButtonRelease-2>", self._pan_end)
        widget.bind("<KeyPress>", self._on_preview_key)
        widget.bind("<Control-c>", self._copy_event)
        widget.bind("<Control-v>", self._paste_event)
        if had_focus:
            widget.focus_set()
        self._canvas_item = self.preview_view.create_window(
            0, 0, window=widget, anchor="nw"
        )
        self._schedule_fit()

    def _schedule_fit(self, _event=None) -> None:
        """窗口/画布尺寸变化后，把 Figure 缩放到预览区实际像素大小。"""
        if self._fit_job is not None:
            try:
                self.root.after_cancel(self._fit_job)
            except tk.TclError:
                pass
        self._fit_job = self.root.after(60, self._fit_preview)

    def _fit_preview(self) -> None:
        self._fit_job = None
        if self._canvas is None or self._fig is None or self._canvas_item is None:
            return
        vw = self.preview_view.winfo_width() - 4
        vh = self.preview_view.winfo_height() - 4
        if vw < 80 or vh < 80:
            return
        fw, fh = self._fig_inches
        mode = self.zoom_var.get()
        focus = None
        focus_view = {"正视图": "front", "侧视图": "side", "俯视图": "plan",
                      "安装板图": "internal"}.get(mode)
        if focus_view is not None and self._meta is not None:
            focus = self._meta.get("placements", {}).get(focus_view)
        if mode == "适合窗口" or (focus_view is not None and focus is None):
            dpi = min(vw / fw, vh / fh)
        elif focus is not None:
            dpi = min((vw - 20.0) / (focus.w / 25.4), (vh - 20.0) / (focus.h / 25.4))
        else:
            try:
                dpi = 100.0 * float(mode.rstrip("%")) / 100.0
            except ValueError:
                dpi = 100.0
        dpi = max(18.0, min(dpi, 220.0))
        # 关键：只改 DPI，不改图幅物理尺寸；字体/线宽随整页等比缩放。
        self._fig.set_dpi(dpi)
        px_w = max(100, int(round(fw * dpi)))
        px_h = max(100, int(round(fh * dpi)))
        self._preview_px = (px_w, px_h)

        self._canvas.get_tk_widget().configure(width=px_w, height=px_h)
        self.preview_view.itemconfigure(self._canvas_item, width=px_w, height=px_h)
        self.preview_view.configure(scrollregion=(0, 0, px_w, px_h))
        x = max(0, (vw - px_w) // 2)
        y = max(0, (vh - px_h) // 2)
        self.preview_view.coords(self._canvas_item, x, y)
        try:
            self._canvas.draw()
        except tk.TclError:
            pass
        if focus is not None:
            # 把目标视图滚到预览区中央，方便直接拖动开孔
            px_per_mm = dpi / 25.4
            cx = x + focus.cx * px_per_mm
            cy = y + (self._fig_inches[1] * 25.4 - focus.cy) * px_per_mm
            if px_w > vw:
                self.preview_view.xview_moveto(
                    max(0.0, min(1.0, (cx - vw / 2.0) / px_w))
                )
            if px_h > vh:
                self.preview_view.yview_moveto(
                    max(0.0, min(1.0, (cy - vh / 2.0) / px_h))
                )

    def _on_preview_wheel(self, event) -> None:
        if self.preview_view.yview() == (0.0, 1.0):
            return
        self.preview_view.yview_scroll(int(-event.delta / 120), "units")

    def _on_preview_wheel_h(self, event) -> None:
        if self.preview_view.xview() == (0.0, 1.0):
            return
        self.preview_view.xview_scroll(int(-event.delta / 120), "units")

    def _pan_start(self, event) -> None:
        if self._canvas is None:
            return
        self._pan = {
            "x": float(event.x),
            "y": float(event.y),
            "vx": self.preview_view.xview()[0],
            "vy": self.preview_view.yview()[0],
        }
        self._canvas.get_tk_widget().configure(cursor="fleur")

    def _pan_move(self, event) -> None:
        if self._pan is None:
            return
        px_w, px_h = self._preview_px
        dx = event.x - self._pan["x"]
        dy = event.y - self._pan["y"]
        if px_w > 0:
            self.preview_view.xview_moveto(
                min(1.0, max(0.0, self._pan["vx"] - dx / px_w))
            )
        if px_h > 0:
            self.preview_view.yview_moveto(
                min(1.0, max(0.0, self._pan["vy"] - dy / px_h))
            )

    def _pan_end(self, event) -> None:
        self._pan = None
        if self._canvas is not None:
            self._canvas.get_tk_widget().configure(cursor="")

    # ---------------- 预览内直接拖动开孔（正/侧/俯视图通用） ----------------
    @staticmethod
    def _view_faces(view: str) -> tuple[str, ...]:
        return {
            "front": ("front",),
            "side": ("left", "right"),
            "plan": ("top", "bottom"),
        }.get(view, ())

    @staticmethod
    def _view_dims(view: str, cab: Cabinet) -> tuple[float, float]:
        if view == "side":
            return max(1.0, cab.D), max(1.0, cab.H)
        if view == "plan":
            return max(1.0, cab.W), max(1.0, cab.D)
        if view == "internal":
            return max(1.0, cab.plate_w), max(1.0, cab.plate_h)
        return max(1.0, cab.W), max(1.0, cab.front_dev_h if cab.is_console else cab.H)

    def _event_view(self, event) -> dict[str, Any] | None:
        if self._meta is None or self._last_job is None:
            return None
        if event.xdata is None or event.ydata is None:
            return None
        s = float(self._meta["layout"].s)
        cab = self._last_job.cabinet
        for view in ("front", "side", "plan", "internal"):
            box = self._meta.get("placements", {}).get(view)
            if box is None:
                continue
            if not (
                box.x0 - 0.5 <= event.xdata <= box.x1 + 0.5
                and box.y0 - 0.5 <= event.ydata <= box.y1 + 0.5
            ):
                continue
            vw, vh = self._view_dims(view, cab)
            x = (event.xdata - box.x0) / s
            y = (event.ydata - box.y0) / s
            if -1.0 <= x <= vw + 1.0 and -1.0 <= y <= vh + 1.0:
                return {
                    "view": view, "box": box, "s": s,
                    "x": x, "y": y, "w": vw, "h": vh,
                    "faces": self._view_faces(view),
                }
        return None

    def _cutout_size(self, index: int) -> tuple[float, float]:
        try:
            w, h = geom_size(self.lib.resolve_cutout(self.cutouts[index], index).cutout)
        except Exception:
            w, h = 30.0, 30.0
        return max(w, 1.0), max(h, 1.0)

    def _selected_index(self) -> int | None:
        sel = self.cutout_tree.selection()
        if not sel:
            return None
        try:
            idx = int(sel[0])
        except ValueError:
            return None
        return idx if 0 <= idx < len(self.cutouts) else None

    def _cutout_at(self, x: float, y: float, faces: tuple[str, ...] | None = None,
                   prefer: int | None = None) -> int | None:
        if self._meta is None:
            return None
        s = float(self._meta["layout"].s)
        dpi = self._fig.dpi if self._fig is not None else 100.0
        tol = max(2.0, 7.0 * 25.4 / max(dpi * s, 1e-6))
        hits: list[tuple[tuple[float, float], int]] = []
        for i in range(len(self.cutouts)):
            cut = self.cutouts[i]
            if faces is not None and cutout_face(cut) not in faces:
                continue
            w, h = self._cutout_size(i)
            if abs(x - cut.x) <= w / 2 + tol and abs(y - cut.y) <= h / 2 + tol:
                key = ((x - cut.x) ** 2 + (y - cut.y) ** 2, w * h)
                hits.append((key, i))
        if not hits:
            return None
        if prefer is not None and any(i == prefer for _k, i in hits):
            return prefer
        return min(hits, key=lambda t: t[0])[1]

    def _select_cutout(self, index: int) -> None:
        item = str(index)
        if not self.cutout_tree.exists(item):
            return
        self.cutout_tree.selection_set(item)
        self.cutout_tree.focus(item)
        self.cutout_tree.see(item)
        self._last_selected = ("cutout", index)

    def _cutout_status(self, index: int, x: float | None = None, y: float | None = None,
                       prefix: str = "") -> None:
        if self._last_job is None or not (0 <= index < len(self.cutouts)):
            return
        cut = self.cutouts[index]
        x = cut.x if x is None else x
        y = cut.y if y is None else y
        face = cutout_face(cut)
        fw, fh = face_dims(self._last_job.cabinet, face)
        _ax, _ay, x0n, x1n, y0n, y1n = face_edge_labels(face)
        name = self.lib.resolve_cutout(cut, index).name
        self.status_var.set(
            f"{prefix}{index + 1}# [{FACE_LABELS.get(face, '正面')}] {name}："
            f"{x0n} {x:.0f} · {x1n} {fw - x:.0f} · {y0n} {y:.0f} · {y1n} {fh - y:.0f} mm"
        )

    def _default_face_for_view(self, view: str) -> str:
        sel = self._selected_index()
        if sel is not None:
            face = cutout_face(self.cutouts[sel])
            if face in self._view_faces(view):
                return face
        return {"side": "left", "plan": "top"}.get(view, "front")

    # ---------------- 安装板图内直接拖动导轨/线槽 ----------------
    def _internal_layout(self):
        if self._last_job is None:
            return None
        if self._internal_cache is None:
            self._internal_cache = internal_layout(
                self._last_job.cabinet, self.rails, self.ducts, self.lib
            )
        return self._internal_cache

    @staticmethod
    def _rail_hit_box(rl) -> Box:
        return Box(rl.x, rl.y - 22.5, rl.x + rl.length, rl.y + 22.5)

    def _on_internal_tree_select(self, kind: str) -> None:
        tree = self.rail_tree if kind == "rail" else self.duct_tree
        sel = tree.selection()
        if not sel:
            return  # 程序化清空选择时不要反向清掉另一张表
        try:
            self._last_selected = (kind, int(sel[-1]))
        except ValueError:
            pass
        other = self.duct_tree if kind == "rail" else self.rail_tree
        if other.selection():
            other.selection_remove(*other.selection())

    def _selected_internal(self) -> tuple[str, int] | None:
        for kind, tree in (("rail", self.rail_tree), ("duct", self.duct_tree)):
            sel = tree.selection()
            if sel:
                try:
                    return kind, int(sel[0])
                except ValueError:
                    continue
        return None

    def _internal_hit(self, x: float, y: float) -> tuple[str, int, float, float, float, float] | None:
        """返回 (kind, index, w, h, anchor_x, anchor_y)。"""
        layout = self._internal_layout()
        if layout is None:
            return None
        s = float(self._meta["layout"].s) if self._meta else 1.0
        dpi = self._fig.dpi if self._fig is not None else 100.0
        tol = max(2.0, 7.0 * 25.4 / max(dpi * s, 1e-6))
        hits: list[tuple[str, int, float, float, float, float]] = []
        for i, rl in enumerate(layout.rails):
            b = self._rail_hit_box(rl)
            on_rail = b.x0 - tol <= x <= b.x1 + tol and b.y0 - tol <= y <= b.y1 + tol
            if not on_rail:
                # 器件固定在导轨上，点器件本体也应能选中并整体拖动导轨；
                # 否则用户抓住露在导轨上方的断路器时没有反应。
                on_rail = any(
                    dp.box.x0 - tol <= x <= dp.box.x1 + tol
                    and dp.box.y0 - tol <= y <= dp.box.y1 + tol
                    for dp in rl.device_list
                )
            if on_rail:
                hits.append(("rail", i, max(rl.length, 1.0), 35.0, rl.x, rl.y))
        for i, (b, duct) in enumerate(layout.ducts):
            if b.x0 - tol <= x <= b.x1 + tol and b.y0 - tol <= y <= b.y1 + tol:
                hits.append(("duct", i, max(b.w, 1.0), max(b.h, 1.0), duct.x, duct.y))
        if not hits:
            return None
        pref = self._selected_internal()
        if pref is not None:
            for item in hits:
                if item[0] == pref[0] and item[1] == pref[1]:
                    return item
        # 导轨绘制在线槽之后，重叠时默认优先选中导轨；线槽可先在表格选中再拖动。
        for item in hits:
            if item[0] == "rail":
                return item
        return hits[0]

    def _select_internal(self, kind: str, index: int) -> None:
        tree, other = (
            (self.rail_tree, self.duct_tree)
            if kind == "rail"
            else (self.duct_tree, self.rail_tree)
        )
        item = str(index)
        if not tree.exists(item):
            return
        if other.selection():
            other.selection_remove(*other.selection())
        tree.selection_set(item)
        tree.focus(item)
        tree.see(item)
        self._last_selected = (kind, index)

    def _internal_status(self, kind: str, index: int, x: float | None = None,
                         y: float | None = None, prefix: str = "") -> None:
        layout = self._internal_layout()
        if layout is None:
            return
        if kind == "rail" and 0 <= index < len(layout.rails):
            rl = layout.rails[index]
            x = rl.x if x is None else x
            y = rl.y if y is None else y
            right = layout.plate_w - (x + rl.length)
            hint = ""
            if right < -0.5:
                hint = " · 导轨超出板宽，请双击缩短长度"
            elif x <= 0.5:
                hint = " · 已到左边界（双击缩短导轨可继续移动）"
            elif right <= 0.5:
                hint = " · 已到右边界（双击缩短导轨可继续移动）"
            count = len(rl.device_list)
            device_hint = f" · {count} 件随导轨整体移动" if count else ""
            self.status_var.set(
                f"{prefix}导轨 {rl.label}：X {x:.0f} · 右 {right:.0f} · "
                f"Y {y:.0f} · 上 {layout.plate_h - y:.0f} mm"
                f"{device_hint}{hint}"
            )
        elif kind == "duct" and 0 <= index < len(layout.ducts):
            b, duct = layout.ducts[index]
            x = duct.x if x is None else x
            y = duct.y if y is None else y
            orient = "垂直" if duct.orient == "v" else "水平"
            self.status_var.set(
                f"{prefix}线槽 {index + 1}（{orient}）：X {x:.0f} · "
                f"右 {layout.plate_w - (x + b.w):.0f} · Y {y:.0f} · "
                f"上 {layout.plate_h - (y + b.h):.0f} mm"
            )

    def add_duct_at(self, x: float, y: float) -> None:
        dlg = DuctDialog(self.root, initial=(x, y))
        self.root.wait_window(dlg)
        if dlg.result:
            self.ducts.append(dlg.result)
            self._refresh_duct_tree()
            self.refresh_preview()
            self._select_internal("duct", len(self.ducts) - 1)

    def add_rail_at(self, x: float, y: float) -> None:
        cab = self._current_cabinet()
        dlg = RailDialog(
            self.root, self.lib, initial=(x, y),
            plate=(cab.plate_w, cab.plate_h),
        )
        self.root.wait_window(dlg)
        if dlg.result:
            self.rails.append(dlg.result)
            self._refresh_rail_tree()
            self.refresh_preview()
            self._select_internal("rail", len(self.rails) - 1)

    def _internal_menu(self, event, item, pos: tuple[float, float]) -> None:
        menu = tk.Menu(self.root, tearoff=0)
        if item is None:
            menu.add_command(
                label="在此添加线槽…", command=lambda: self.add_duct_at(*pos)
            )
            menu.add_command(
                label="在此添加导轨…", command=lambda: self.add_rail_at(*pos)
            )
            if self._clipboard:
                menu.add_separator()
                menu.add_command(label="粘贴（Ctrl+V）", command=self.paste_clipboard)
        else:
            kind, idx = item[0], item[1]
            self._select_internal(kind, idx)
            if kind == "rail":
                menu.add_command(label=f"编辑导轨 L{idx + 1}…", command=self.edit_rail)
                menu.add_command(
                    label="复制选中导轨（Ctrl+C）",
                    command=lambda: self.copy_selected("rail"),
                )
                if self._clipboard:
                    menu.add_command(label="粘贴（Ctrl+V）", command=self.paste_clipboard)
                menu.add_separator()
                menu.add_command(label="删除此导轨", command=self.delete_rail)
            else:
                menu.add_command(label=f"编辑线槽 {idx + 1}…", command=self.edit_duct)
                menu.add_command(
                    label="复制选中线槽（Ctrl+C）",
                    command=lambda: self.copy_selected("duct"),
                )
                if self._clipboard:
                    menu.add_command(label="粘贴（Ctrl+V）", command=self.paste_clipboard)
                menu.add_separator()
                menu.add_command(label="删除此线槽", command=self.delete_duct)
        try:
            menu.tk_popup(event.guiEvent.x_root, event.guiEvent.y_root)
        finally:
            menu.grab_release()

    def _on_internal_press(self, event, hit: dict[str, Any]) -> None:
        pos = (hit["x"], hit["y"])
        item = self._internal_hit(*pos)
        if event.button == 3:
            self._internal_menu(event, item, pos)
            return
        if event.dblclick:
            self._cancel_drag()
            if item is not None:
                kind, idx = item[0], item[1]
                self._select_internal(kind, idx)
                (self.edit_rail if kind == "rail" else self.edit_duct)()
            else:
                self.add_duct_at(*pos)
            return
        if item is None:
            return
        kind, idx, w, h, ax, ay = item
        self._select_internal(kind, idx)
        dpi = self._fig.dpi if self._fig is not None else 100.0
        tol = max(1.0, 4.0 * 25.4 / max(dpi * hit["s"], 1e-6))
        if kind == "rail":
            xmax = max(0.0, hit["w"] - w)
            # 兼容旧项目里已经超出板宽的导轨：不要一按就把它吸到左边；
            # 至少保持原位置，允许向下/向左拖回板内，再双击缩短长度。
            if ax > xmax + 0.01:
                xmax = min(float(ax), max(float(hit["w"]), 0.0))
            limits = (0.0, xmax, 20.0, max(20.0, hit["h"] - 20.0))
        else:
            limits = (0.0, max(0.0, hit["w"] - w), 0.0, max(0.0, hit["h"] - h))
        self._drag = {
            "kind": kind,
            "index": idx,
            "ox": ax - pos[0],
            "oy": ay - pos[1],
            "orig": (ax, ay),
            "pos": (ax, ay),
            "w": w,
            "h": h,
            "tol": tol,
            "moved": False,
            "view": "internal",
            "box": hit["box"],
            "s": hit["s"],
            "view_w": hit["w"],
            "view_h": hit["h"],
            "limits": limits,
        }
        if self._canvas is not None:
            self._canvas.get_tk_widget().configure(cursor="fleur")
        self._internal_status(kind, idx, prefix="拖动中：")

    def _on_preview_press(self, event) -> None:
        if event.button not in (1, 3):
            return
        if self._canvas is not None:
            try:
                self._canvas.get_tk_widget().focus_set()
            except tk.TclError:
                pass
        hit = self._event_view(event)
        if hit is None:
            return
        if hit["view"] == "internal":
            self._on_internal_press(event, hit)
            return
        pos = (hit["x"], hit["y"])
        idx = self._cutout_at(*pos, faces=hit["faces"], prefer=self._selected_index())
        if event.button == 3:
            face = self._default_face_for_view(hit["view"]) if idx is None else None
            self._cutout_menu(event, idx, pos, face=face)
            return
        if event.dblclick:
            self._cancel_drag()
            if idx is not None:
                self._select_cutout(idx)
                self.edit_cutout()
            else:
                self.add_cutout_at(*pos, face=self._default_face_for_view(hit["view"]))
            return
        if idx is None:
            return
        cut = self.cutouts[idx]
        w, h = self._cutout_size(idx)
        dpi = self._fig.dpi if self._fig is not None else 100.0
        tol = max(1.0, 4.0 * 25.4 / max(dpi * hit["s"], 1e-6))
        self._select_cutout(idx)
        self._drag = {
            "kind": "cutout",
            "index": idx,
            "ox": cut.x - pos[0],
            "oy": cut.y - pos[1],
            "orig": (cut.x, cut.y),
            "pos": (cut.x, cut.y),
            "w": w,
            "h": h,
            "tol": tol,
            "moved": False,
            "view": hit["view"],
            "box": hit["box"],
            "s": hit["s"],
            "view_w": hit["w"],
            "view_h": hit["h"],
            "face": cutout_face(cut),
        }
        if self._canvas is not None:
            self._canvas.get_tk_widget().configure(cursor="fleur")
        self._cutout_status(idx, prefix="拖动中：")

    def _on_preview_motion(self, event) -> None:
        hit = self._event_view(event)
        if self._drag is not None:
            if hit is None or self._last_job is None or hit["view"] != self._drag["view"]:
                return
            drag = self._drag
            if drag.get("kind") in ("rail", "duct"):
                xmin, xmax, ymin, ymax = drag["limits"]
                nx = min(max(hit["x"] + drag["ox"], xmin), xmax)
                ny = min(max(hit["y"] + drag["oy"], ymin), ymax)
                drag["pos"] = (nx, ny)
                ox, oy = drag["orig"]
                drag["moved"] = math.hypot(nx - ox, ny - oy) > drag["tol"]
                self._draw_internal_drag_ghost(drag, nx, ny)
                self.mouse_label.configure(text=f"安装板 X {nx:.0f}   Y {ny:.0f}")
                self._internal_status(drag["kind"], drag["index"], nx, ny, prefix="拖动中：")
                return
            nx = min(max(hit["x"] + drag["ox"], 0.0), drag["view_w"])
            ny = min(max(hit["y"] + drag["oy"], 0.0), drag["view_h"])
            drag["pos"] = (nx, ny)
            ox, oy = drag["orig"]
            drag["moved"] = math.hypot(nx - ox, ny - oy) > drag["tol"]
            self._draw_drag_ghost(drag, nx, ny)
            self.mouse_label.configure(text=f"X {nx:.0f}   Y {ny:.0f}")
            self._cutout_status(drag["index"], nx, ny, prefix="拖动中：")
            return
        if self._canvas is None:
            return
        if hit is not None:
            self.mouse_label.configure(
                text=f"{VIEW_LABELS.get(hit['view'], '')}  X {hit['x']:.0f}   Y {hit['y']:.0f}"
            )
        else:
            self.mouse_label.configure(text="")
        if hit is not None and hit["view"] == "internal":
            item = self._internal_hit(hit["x"], hit["y"])
            self._hover_index = None
            if self._canvas is not None:
                self._canvas.get_tk_widget().configure(cursor="fleur" if item else "")
            if item is not None:
                self._internal_status(item[0], item[1], prefix="可拖动：")
            return
        idx = (
            self._cutout_at(hit["x"], hit["y"], faces=hit["faces"],
                            prefer=self._selected_index())
            if hit is not None else None
        )
        if idx != self._hover_index:
            self._hover_index = idx
            self._canvas.get_tk_widget().configure(
                cursor="fleur" if idx is not None else ""
            )
            if idx is not None:
                self._cutout_status(idx, prefix="可拖动：")

    def _on_preview_release(self, event) -> None:
        if self._drag is None:
            return
        drag = self._drag
        self._drag = None
        self._clear_drag_artists()
        if self._canvas is not None:
            self._canvas.get_tk_widget().configure(cursor="")
        if drag.get("kind") in ("rail", "duct"):
            kind, idx = drag["kind"], drag["index"]
            if not drag["moved"]:
                self._select_internal(kind, idx)
                self._internal_status(kind, idx)
                return
            self._apply_internal_drag(drag)
            return
        idx = drag["index"]
        if not drag["moved"]:
            self._select_cutout(idx)
            self._cutout_status(idx)
            return
        nx, ny = drag["pos"]
        cut = self.cutouts[idx]
        cut.x, cut.y = float(round(nx)), float(round(ny))
        self._refresh_cutout_tree()
        self._select_cutout(idx)

        def after_refresh() -> None:
            self.refresh_preview()
            self._cutout_status(idx, prefix="已移动：")

        self.root.after(1, after_refresh)

    def _apply_internal_drag(self, drag: dict[str, Any]) -> None:
        kind, idx = drag["kind"], drag["index"]
        nx, ny = (float(round(v)) for v in drag["pos"])
        if kind == "rail":
            if not (0 <= idx < len(self.rails)):
                return
            # 原起点为“自动”时，仅上下拖动不改 X，避免轻微的横向抖动把自动值固化成具体数字。
            if self.rails[idx].x is not None or abs(nx - drag["orig"][0]) >= 1.0:
                self.rails[idx].x = nx
            self.rails[idx].y = ny
            self._refresh_rail_tree()
        else:
            if not (0 <= idx < len(self.ducts)):
                return
            self.ducts[idx].x = nx
            self.ducts[idx].y = ny
            self._refresh_duct_tree()
        self._select_internal(kind, idx)

        def after_refresh() -> None:
            self.refresh_preview()
            self._internal_status(kind, idx, prefix="已移动：")

        self.root.after(1, after_refresh)

    def _draw_internal_drag_ghost(self, drag: dict[str, Any], nx: float, ny: float) -> None:
        if self._fig is None or self._canvas is None or self._last_job is None:
            return
        box, s = drag["box"], drag["s"]
        vw, vh = drag["view_w"], drag["view_h"]
        self._clear_drag_artists()
        ax = self._fig.axes[0]
        color = "#00875a"
        w = drag["w"] * s
        h = drag["h"] * s
        px = box.x0 + nx * s
        py = box.y0 + ny * s
        if drag["kind"] == "rail":
            rect_y = py - h / 2
            center_y = py
        else:
            rect_y = py
            center_y = py + h / 2
        artists: list[Any] = []
        line, = ax.plot(
            [box.x0, box.x0 + vw * s], [center_y, center_y],
            color=color, lw=0.7, ls=(0, (2, 2)), zorder=40,
        )
        artists.append(line)
        line, = ax.plot(
            [px, px], [box.y0, box.y0 + vh * s],
            color=color, lw=0.7, ls=(0, (2, 2)), zorder=40,
        )
        artists.append(line)
        rect = Rectangle(
            (px, rect_y), w, h, fill=False,
            edgecolor=color, lw=1.8, linestyle=(0, (4, 2)), zorder=41,
        )
        ax.add_patch(rect)
        artists.append(rect)
        line, = ax.plot(
            [px, px + w], [center_y, center_y], color=color, lw=0.8, zorder=42
        )
        artists.append(line)
        fs_pt = max(6.0, min(40.0, 13.0 * 72.0 / max(self._fig.dpi, 1.0)))
        right = vw - (nx + drag["w"])
        top = vh - ny if drag["kind"] == "rail" else vh - (ny + drag["h"])
        label = f"X {nx:.0f}   Y {ny:.0f}\n右 {right:.0f} · 上 {top:.0f}"
        text = ax.text(
            px + w + 2.0, center_y + h / 2 + 2.0, label,
            fontsize=fs_pt, color=color, ha="left", va="bottom",
            zorder=43, linespacing=1.25,
            bbox=dict(facecolor="white", edgecolor=color, linewidth=0.6, pad=1.5),
        )
        artists.append(text)
        self._drag_artists = artists
        try:
            self._canvas.draw_idle()
        except Exception:
            pass

    def _on_preview_leave(self, event) -> None:
        if self._drag is not None:
            return
        self._hover_index = None
        self.mouse_label.configure(text="")
        if self._canvas is not None:
            self._canvas.get_tk_widget().configure(cursor="")

    def _cancel_drag(self) -> None:
        if self._drag is None:
            return
        self._drag = None
        self._clear_drag_artists()
        if self._canvas is not None:
            self._canvas.get_tk_widget().configure(cursor="")

    def _clear_drag_artists(self) -> None:
        if not self._drag_artists:
            return
        for artist in self._drag_artists:
            try:
                artist.remove()
            except Exception:
                pass
        self._drag_artists.clear()
        if self._canvas is not None:
            try:
                self._canvas.draw_idle()
            except Exception:
                pass

    def _draw_drag_ghost(self, drag: dict[str, Any], nx: float, ny: float) -> None:
        if self._fig is None or self._canvas is None or self._last_job is None:
            return
        box, s = drag["box"], drag["s"]
        vw, vh = drag["view_w"], drag["view_h"]
        face = drag["face"]
        self._clear_drag_artists()
        ax = self._fig.axes[0]
        px = box.x0 + nx * s
        py = box.y0 + ny * s
        w = drag["w"] * s
        h = drag["h"] * s
        color = "#00875a"
        artists: list[Any] = []
        # 到四边的辅助线
        line, = ax.plot(
            [box.x0, box.x0 + vw * s], [py, py],
            color=color, lw=0.7, ls=(0, (2, 2)), zorder=40,
        )
        artists.append(line)
        line, = ax.plot(
            [px, px], [box.y0, box.y0 + vh * s],
            color=color, lw=0.7, ls=(0, (2, 2)), zorder=40,
        )
        artists.append(line)
        rect = Rectangle(
            (px - w / 2, py - h / 2), w, h, fill=False,
            edgecolor=color, lw=1.8, linestyle=(0, (4, 2)), zorder=41,
        )
        ax.add_patch(rect)
        artists.append(rect)
        arm = max(w, h, 8.0)
        line, = ax.plot([px - arm, px + arm], [py, py], color=color, lw=0.8, zorder=42)
        artists.append(line)
        line, = ax.plot([px, px], [py - arm, py + arm], color=color, lw=0.8, zorder=42)
        artists.append(line)
        fs_pt = max(6.0, min(40.0, 13.0 * 72.0 / max(self._fig.dpi, 1.0)))
        _ax, _ay, x0n, x1n, y0n, y1n = face_edge_labels(face)
        label = (
            f"X {nx:.0f}   Y {ny:.0f}\n"
            f"{x0n} {nx:.0f} · {x1n} {vw - nx:.0f}\n"
            f"{y0n} {ny:.0f} · {y1n} {vh - ny:.0f}"
        )
        text = ax.text(
            px + w / 2 + 2.0, py + h / 2 + 2.0, label,
            fontsize=fs_pt, color=color, ha="left", va="bottom",
            zorder=43, linespacing=1.25,
            bbox=dict(facecolor="white", edgecolor=color, linewidth=0.6, pad=1.5),
        )
        artists.append(text)
        self._drag_artists = artists
        try:
            self._canvas.draw_idle()
        except Exception:
            pass

    def _cutout_menu(self, event, idx: int | None, pos: tuple[float, float],
                     face: str | None = None) -> None:
        menu = tk.Menu(self.root, tearoff=0)
        if idx is None:
            face = face or "front"
            menu.add_command(
                label=f"在此添加{FACE_LABELS.get(face, '正面')}开孔…",
                command=lambda: self.add_cutout_at(*pos, face=face),
            )
            if self._clipboard and self._clipboard["kind"] == "cutout":
                menu.add_command(label="粘贴开孔（Ctrl+V）", command=self.paste_clipboard)
        else:
            self._select_cutout(idx)
            face_name = FACE_LABELS.get(cutout_face(self.cutouts[idx]), "正面")
            menu.add_command(label=f"编辑此开孔（{face_name}）…", command=self.edit_cutout)
            menu.add_command(
                label="复制此开孔", command=lambda i=idx: self._duplicate_cutout(i)
            )
            menu.add_command(
                label="复制到剪贴板（Ctrl+C）",
                command=lambda: self.copy_selected("cutout"),
            )
            if self._clipboard and self._clipboard["kind"] == "cutout":
                menu.add_command(label="粘贴开孔（Ctrl+V）", command=self.paste_clipboard)
            menu.add_separator()
            menu.add_command(label="删除此开孔", command=self.delete_cutout)
        try:
            menu.tk_popup(event.guiEvent.x_root, event.guiEvent.y_root)
        finally:
            menu.grab_release()

    def _duplicate_cutout(self, idx: int) -> None:
        if not (0 <= idx < len(self.cutouts)) or self._last_job is None:
            return
        src = self.cutouts[idx]
        cab = self._last_job.cabinet
        face = cutout_face(src)
        fw, fh = face_dims(cab, face)
        dup = Cutout(
            component=src.component,
            x=min(src.x + 20.0, fw),
            y=min(src.y + 20.0, fh),
            label="",
            note=src.note,
            w=src.w,
            h=src.h,
            d=src.d,
            face=face,
        )
        self.cutouts.insert(idx + 1, dup)
        self._refresh_cutout_tree()
        self._select_cutout(idx + 1)
        self.refresh_preview()
        self.status_var.set(f"已复制开孔 {idx + 1}#，位置偏移 +20/+20 mm")

    # ---------------- 复制 / 粘贴 / 方向键微调 ----------------
    def _copy_event(self, _event=None):
        self.copy_selected()
        return "break"

    def _paste_event(self, _event=None):
        self.paste_clipboard()
        return "break"

    def _selection_kind(self) -> str | None:
        focused = self.root.focus_get()
        for widget, kind in (
            (self.cutout_tree, "cutout"),
            (self.rail_tree, "rail"),
            (self.duct_tree, "duct"),
        ):
            if focused is widget:
                return kind
        if self._last_selected is not None:
            return self._last_selected[0]
        internal = self._selected_internal()
        if internal is not None:
            return internal[0]
        return "cutout" if self._selected_index() is not None else None

    def copy_selected(self, kind: str | None = None) -> None:
        kind = kind or self._selection_kind()
        items: list[Any] = []
        if kind == "cutout":
            idxs = sorted(int(i) for i in self.cutout_tree.selection())
            if not idxs and self._last_selected and self._last_selected[0] == "cutout":
                idxs = [self._last_selected[1]]
            items = [copy.deepcopy(self.cutouts[i]) for i in idxs
                     if 0 <= i < len(self.cutouts)]
        elif kind in ("rail", "duct"):
            tree = self.rail_tree if kind == "rail" else self.duct_tree
            source = self.rails if kind == "rail" else self.ducts
            idxs = sorted(int(i) for i in tree.selection())
            if not idxs and self._last_selected and self._last_selected[0] == kind:
                idxs = [self._last_selected[1]]
            items = [copy.deepcopy(source[i]) for i in idxs if 0 <= i < len(source)]
        if not items:
            self.status_var.set("请先选中要复制的开孔/导轨/线槽。")
            return
        self._clipboard = {"kind": kind, "items": items}
        self._paste_serial = 0
        label = {"cutout": "开孔", "rail": "导轨", "duct": "线槽"}[kind]
        self.status_var.set(f"已复制 {len(items)} 个{label}，按 Ctrl+V 粘贴。")

    @staticmethod
    def _shift_axis(value: float, delta: float, lo: float, hi: float) -> float:
        if hi < lo:
            hi = lo
        target = value + delta
        if target > hi + 1e-9:
            target = value - delta
        return float(min(max(target, lo), hi))

    def paste_clipboard(self) -> None:
        if not self._clipboard:
            self.status_var.set("剪贴板为空：先选中开孔/导轨/线槽，按 Ctrl+C 复制。")
            return
        kind = self._clipboard["kind"]
        items = copy.deepcopy(self._clipboard["items"])
        if not items:
            return
        cab = self._last_job.cabinet if self._last_job is not None else self._current_cabinet()
        self._paste_serial += 1
        serial = self._paste_serial
        new_indices: list[int] = []

        if kind == "cutout":
            for src in items:
                face = cutout_face(src)
                fw, fh = face_dims(cab, face)
                src.x = self._shift_axis(src.x, 20.0 * serial, 0.0, fw)
                src.y = self._shift_axis(src.y, 20.0 * serial, 0.0, fh)
                src.label = ""
                self.cutouts.append(src)
                new_indices.append(len(self.cutouts) - 1)
            self._refresh_cutout_tree()
            self._select_cutout_many(new_indices)
        elif kind == "rail":
            for src in items:
                length = src.length if src.length is not None else max(80.0, cab.plate_w - 120.0)
                if src.x is not None:
                    src.x = float(min(max(src.x, 0.0), max(0.0, cab.plate_w - length)))
                src.y = self._shift_axis(
                    src.y, 45.0 * serial, 20.0, max(20.0, cab.plate_h - 20.0)
                )
                self.rails.append(src)
                new_indices.append(len(self.rails) - 1)
            self._refresh_rail_tree()
            self._select_internal_many("rail", new_indices)
        else:
            for src in items:
                if src.orient == "v":
                    w, h = src.width, src.length
                else:
                    w, h = src.length, src.width
                src.x = self._shift_axis(
                    src.x, 40.0 * serial, 0.0, max(0.0, cab.plate_w - w)
                )
                src.y = self._shift_axis(
                    src.y, 40.0 * serial, 0.0, max(0.0, cab.plate_h - h)
                )
                self.ducts.append(src)
                new_indices.append(len(self.ducts) - 1)
            self._refresh_duct_tree()
            self._select_internal_many("duct", new_indices)

        self.refresh_preview()
        label = {"cutout": "开孔", "rail": "导轨", "duct": "线槽"}[kind]
        self.status_var.set(f"已粘贴 {len(new_indices)} 个{label}（偏移 {20 if kind == 'cutout' else 40}mm 级）。")

    def _select_cutout_many(self, indices: list[int]) -> None:
        items = [str(i) for i in indices if self.cutout_tree.exists(str(i))]
        if not items:
            return
        self.cutout_tree.selection_set(*items)
        self.cutout_tree.focus(items[-1])
        self.cutout_tree.see(items[-1])
        try:
            self._last_selected = ("cutout", int(items[-1]))
        except ValueError:
            pass

    def _select_internal_many(self, kind: str, indices: list[int]) -> None:
        tree, other = (
            (self.rail_tree, self.duct_tree)
            if kind == "rail"
            else (self.duct_tree, self.rail_tree)
        )
        items = [str(i) for i in indices if tree.exists(str(i))]
        if not items:
            return
        if other.selection():
            other.selection_remove(*other.selection())
        tree.selection_set(*items)
        tree.focus(items[-1])
        tree.see(items[-1])
        try:
            self._last_selected = (kind, int(items[-1]))
        except ValueError:
            pass

    def _nudge_target(self) -> tuple[str, int] | None:
        internal = self._selected_internal()
        cut = self._selected_index()
        if self._last_selected is not None:
            kind, idx = self._last_selected
            if kind == "cutout" and cut == idx:
                return kind, idx
            if kind in ("rail", "duct") and internal == (kind, idx):
                return kind, idx
        if internal is not None:
            return internal
        if cut is not None:
            return "cutout", cut
        return None

    def _on_preview_key(self, event):
        keys = {"Left": (-1, 0), "Right": (1, 0), "Up": (0, 1), "Down": (0, -1)}
        delta = keys.get(event.keysym)
        if delta is None:
            return None
        try:
            step = 10.0 if int(event.state) & 0x0001 else 1.0
        except (TypeError, ValueError):
            step = 1.0
        self._nudge_selected(delta[0] * step, delta[1] * step)
        return "break"

    def _nudge_selected(self, dx: float, dy: float) -> None:
        target = self._nudge_target()
        if target is None:
            self.status_var.set("请先选中开孔/导轨/线槽，再按方向键微调。")
            return
        kind, idx = target
        if kind == "cutout":
            self._nudge_cutout(idx, dx, dy)
        else:
            self._nudge_internal(kind, idx, dx, dy)
        self._schedule_nudge_refresh()

    def _nudge_cutout(self, idx: int, dx: float, dy: float) -> None:
        if not (0 <= idx < len(self.cutouts)):
            return
        cab = self._last_job.cabinet if self._last_job is not None else self._current_cabinet()
        cut = self.cutouts[idx]
        fw, fh = face_dims(cab, cutout_face(cut))
        cut.x = float(min(max(cut.x + dx, 0.0), fw))
        cut.y = float(min(max(cut.y + dy, 0.0), fh))
        self._refresh_cutout_tree()
        self._select_cutout(idx)
        self._cutout_status(idx, cut.x, cut.y, prefix="微调：")

    def _nudge_internal(self, kind: str, idx: int, dx: float, dy: float) -> None:
        layout = self._internal_layout()
        if layout is None:
            return
        if kind == "rail":
            if not (0 <= idx < len(self.rails) and 0 <= idx < len(layout.rails)):
                return
            rail, rl = self.rails[idx], layout.rails[idx]
            xmax = max(0.0, layout.plate_w - rl.length)
            # 旧项目里导轨本身超宽时，保持原位，允许先往左拖回板内。
            if rail.x is not None and rail.x > xmax + 0.01:
                xmax = rail.x
            if rail.x is None:
                if dx:
                    rail.x = float(min(max(rl.x + dx, 0.0), xmax))
            else:
                rail.x = float(min(max(rail.x + dx, 0.0), xmax))
            rail.y = float(min(max(rail.y + dy, 20.0), max(20.0, layout.plate_h - 20.0)))
            self._refresh_rail_tree()
            self._select_internal("rail", idx)
            self._internal_status(
                "rail", idx, x=rail.x if rail.x is not None else rl.x, y=rail.y,
                prefix="微调：",
            )
        elif kind == "duct" and 0 <= idx < len(layout.ducts):
            duct = self.ducts[idx]
            b, _ = layout.ducts[idx]
            duct.x = float(min(max(duct.x + dx, 0.0), max(0.0, layout.plate_w - b.w)))
            duct.y = float(min(max(duct.y + dy, 0.0), max(0.0, layout.plate_h - b.h)))
            self._refresh_duct_tree()
            self._select_internal("duct", idx)
            self._internal_status("duct", idx, prefix="微调：")

    def _schedule_nudge_refresh(self) -> None:
        if self._nudge_after is not None:
            try:
                self.root.after_cancel(self._nudge_after)
            except tk.TclError:
                pass
        self._nudge_after = self.root.after(130, self._do_nudge_refresh)

    def _do_nudge_refresh(self) -> None:
        self._nudge_after = None
        self.refresh_preview()

    def _show_issues(self, meta: dict[str, Any]) -> None:
        issues = meta["issues"]
        errors = sum(1 for i in issues if i.level == "error")
        warns = sum(1 for i in issues if i.level == "warn")
        if issues:
            self.issue_label.configure(
                text=f"错误 {errors}    提示 {warns}",
                foreground="#b00020" if errors else "#9a5b00",
            )
        else:
            self.issue_label.configure(text="校验通过", foreground="#146c2e")

        text = self.issues_text
        text.configure(state="normal")
        text.delete("1.0", "end")
        if not issues:
            text.insert("end", "未发现尺寸/干涉问题。导出前请人工复核开孔与器件布置。\n", "ok")
        else:
            for issue in issues:
                text.insert("end", f"{issue}\n", issue.level)
        text.configure(state="disabled")
        text.see("1.0")
        self.status_var.set(
            f"已生成 {meta['page']} / {meta['scale']}" + (f"；错误 {errors} 项" if errors else "")
        )

    def export(self, suffix: str) -> None:
        try:
            job = self.collect_job()
        except ValueError as e:
            messagebox.showerror("参数有误", str(e), parent=self.root)
            return
        outdir = Path.cwd() / "output"
        outdir.mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title=f"导出 {suffix.upper()}",
            initialdir=str(outdir),
            initialfile=default_filename(job.sheet.project, job.sheet.drawing_no, suffix),
            defaultextension=suffix,
            filetypes=[(suffix.upper().lstrip("."), f"*{suffix}"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            fig, meta = self._build(
                job, fig=self._fig if self._canvas is not None else None
            )
            save_figure(fig, path, title=job.sheet.project)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            messagebox.showerror("导出失败", str(e), parent=self.root)
            return
        self._last_job = job
        self._last_good_job = Job.from_dict(job.to_dict())
        self._record_snapshot(job)
        self._internal_cache = internal_layout(job.cabinet, job.rails, job.ducts, self.lib)
        self._show_figure(fig, meta)
        self._show_issues(meta)
        self.status_var.set(f"已导出：{path}")
        messagebox.showinfo("导出成功", f"文件已保存：\n{path}", parent=self.root)

    def export_tables(self) -> None:
        try:
            job = self.collect_job()
        except ValueError as e:
            messagebox.showerror("参数有误", str(e), parent=self.root)
            return
        outdir = Path.cwd() / "output"
        outdir.mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="导出表格 CSV",
            initialdir=str(outdir),
            initialfile=default_filename(job.sheet.project, job.sheet.drawing_no, ".csv"),
            defaultextension=".csv",
            filetypes=[("CSV 表格", "*.csv"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            save_tables_csv(job, self.lib, path)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            messagebox.showerror("导出失败", str(e), parent=self.root)
            return
        self.status_var.set(f"表格已导出：{path}")
        messagebox.showinfo("导出成功", f"表格已保存：\n{path}", parent=self.root)

    # ---------------- 项目文件 ----------------
    def _load_path(self, path: Path, silent: bool = False) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                job = Job.from_dict(json.load(f))
        except Exception as e:  # noqa: BLE001
            if silent:
                raise
            messagebox.showerror("打开失败", f"无法读取项目文件：\n{e}", parent=self.root)
            return
        self.populate_job(job)
        self.refresh_preview()
        self.status_var.set(f"已打开：{path}")

    def new_job(self) -> None:
        if not messagebox.askyesno("新建项目", "确定清空当前内容并新建？", parent=self.root):
            return
        self.populate_job(Job())
        self.refresh_preview()

    def open_job(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="打开项目",
            initialdir=str(data_dir() / "examples"),
            filetypes=[("电柜快画项目", "*.json"), ("所有文件", "*.*")],
        )
        if path:
            self._load_path(Path(path))

    def save_job(self) -> None:
        try:
            job = self.collect_job()
        except ValueError as e:
            messagebox.showerror("参数有误", str(e), parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="保存项目",
            initialdir=str(data_dir() / "examples"),
            initialfile=default_filename(job.sheet.project, job.sheet.drawing_no, ".json"),
            defaultextension=".json",
            filetypes=[("电柜快画项目", "*.json")],
        )
        if not path:
            return
        try:
            Path(path).write_text(
                json.dumps(job.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:
            messagebox.showerror("保存失败", str(e), parent=self.root)
            return
        self.status_var.set(f"项目已保存：{path}")

    def open_demo(self) -> None:
        self._load_path(data_dir() / "examples" / "demo_1700x1900x460.json")

    def open_console_demo(self) -> None:
        self._load_path(data_dir() / "examples" / "demo_console_1200x1100x750.json")

    def open_log(self) -> None:
        from .diagnostics import log_dir

        try:
            os.startfile(str(log_dir()))
        except OSError as e:
            messagebox.showerror("无法打开日志目录", str(e), parent=self.root)


def run() -> None:
    """启动 GUI。"""
    from .diagnostics import install_tk_error_handler, log

    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    install_tk_error_handler(root)
    App(root)
    log("GUI 初始化完成")
    root.mainloop()


if __name__ == "__main__":  # pragma: no cover
    run()
