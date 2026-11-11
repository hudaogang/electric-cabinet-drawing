"""元件库 / 器件库 / 模板 / 技术要求 的加载与解析."""
from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import Cabinet, Cutout, Job, SheetInfo, cutout_face


def app_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """数据目录查找顺序：环境变量 → EXE 同目录 data → 打包内置 data → 源码目录 data。

    EXE 同目录的 ``data`` 优先生效，用户可以直接修改元件库/模板而不必重新打包。
    """
    candidates: list[Path] = []
    env = os.environ.get("DKK_DATA_DIR", "").strip()
    if env:
        candidates.append(Path(env))
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "data")
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidates.append(Path(meipass) / "data")
    candidates.append(app_dir() / "data")
    for cand in candidates:
        if (cand / "components.json").exists():
            return cand
    return candidates[-1]


def _load_json(name: str) -> dict[str, Any]:
    path = data_dir() / name
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@dataclass
class ResolvedCutout:
    index: int
    raw: Cutout
    key: str
    name: str
    cutout: dict[str, Any]
    outline: dict[str, Any]
    spec: str
    label: str
    note: str

    @property
    def x(self) -> float:
        return self.raw.x

    @property
    def y(self) -> float:
        return self.raw.y

    @property
    def face(self) -> str:
        return cutout_face(self.raw)


def geom_bbox(geom: dict[str, Any], cx: float, cy: float) -> tuple[float, float, float, float]:
    """返回 (x0, y0, x1, y1)."""
    t = geom.get("type", "rect")
    if t == "circle":
        d = float(geom.get("d", 0))
        return cx - d / 2, cy - d / 2, cx + d / 2, cy + d / 2
    if t == "polygon":
        pts = geom.get("points", [])
        if not pts:
            return cx, cy, cx, cy
        xs = [cx + float(p[0]) for p in pts]
        ys = [cy + float(p[1]) for p in pts]
        return min(xs), min(ys), max(xs), max(ys)
    w = float(geom.get("w", 0))
    h = float(geom.get("h", 0))
    return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2


def geom_size(geom: dict[str, Any]) -> tuple[float, float]:
    if geom.get("type") == "circle":
        d = float(geom.get("d", 0))
        return d, d
    if geom.get("type") == "polygon":
        xs = [float(p[0]) for p in geom.get("points", [])] or [0]
        ys = [float(p[1]) for p in geom.get("points", [])] or [0]
        return max(xs) - min(xs), max(ys) - min(ys)
    return float(geom.get("w", 0)), float(geom.get("h", 0))


def spec_text(geom: dict[str, Any]) -> str:
    t = geom.get("type", "rect")
    if t == "circle":
        return f"Φ{_num(geom.get('d', 0))}"
    w, h = geom_size(geom)
    if t == "slot":
        return f"腰型 {_num(w)}×{_num(h)}"
    r = float(geom.get("r", 0) or 0)
    base = f"{_num(w)}×{_num(h)}"
    return f"{base} R{_num(r)}" if r > 0 else base


def _num(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f - round(f)) < 1e-6:
        return str(int(round(f)))
    return f"{f:g}"


class Library:
    def __init__(
        self,
        components: dict[str, Any],
        devices: dict[str, Any],
        templates: dict[str, Any],
        requirements: dict[str, Any],
    ):
        self.components = {k: v for k, v in components.items() if not k.startswith("_")}
        self.devices = {k: v for k, v in devices.items() if not k.startswith("_")}
        self.templates = {k: v for k, v in templates.items() if not k.startswith("_")}
        self.requirements = {k: v for k, v in requirements.items() if not k.startswith("_")}

    @classmethod
    def load(cls) -> "Library":
        return cls(
            _load_json("components.json"),
            _load_json("devices.json"),
            _load_json("templates.json"),
            _load_json("requirements.json"),
        )

    # ---------- 面板元件 ----------
    def resolve_cutout(self, cut: Cutout, index: int = 0) -> ResolvedCutout:
        item = self.components.get(cut.component, self.components.get("custom_rect", {}))
        cg = dict(item.get("cutout", {"type": "rect", "w": 100, "h": 100, "r": 0}))
        og = dict(item.get("outline", cg))
        # 任务级尺寸覆写（方孔与腰型孔用 W/H，圆孔用 D）
        if cut.d > 0 and cg.get("type") == "circle":
            cg["d"] = cut.d
        if cut.w > 0 and cg.get("type") in ("rect", "slot"):
            cg["w"] = cut.w
        if cut.h > 0 and cg.get("type") in ("rect", "slot"):
            cg["h"] = cut.h
        if cut.w > 0 and og.get("type") in ("rect", "slot"):
            og["w"] = max(float(og.get("w", 0)), cut.w)
        if cut.h > 0 and og.get("type") in ("rect", "slot"):
            og["h"] = max(float(og.get("h", 0)), cut.h)
        if cut.d > 0 and og.get("type") == "circle":
            og["d"] = max(float(og.get("d", 0)), cut.d)
        return ResolvedCutout(
            index=index,
            raw=cut,
            key=cut.component,
            name=str(item.get("name", cut.component)),
            cutout=cg,
            outline=og,
            spec=spec_text(cg),
            label=cut.label,
            note=cut.note,
        )

    def resolve_all(self, job: Job) -> list[ResolvedCutout]:
        return [self.resolve_cutout(c, i) for i, c in enumerate(job.cutouts)]

    # ---------- 柜内器件 ----------
    def device(self, model: str) -> dict[str, Any]:
        return self.devices.get(model, self.devices.get("custom", {"name": model, "width": 36, "height": 85, "prefix": "E"}))

    def template_name(self, key: str) -> str:
        return str(self.templates.get(key, {}).get("name", key))

    def template_defaults(self, key: str) -> dict[str, Any]:
        return dict(self.templates.get(key, {}).get("defaults", {}))

    # ---------- 技术要求 ----------
    def requirement_texts(self, job: Job) -> list[str]:
        tpl = self.requirements.get(job.req_set) or self.requirements.get("standard") or {"items": []}
        cab = job.cabinet
        mapping = {
            "W": _num(cab.W),
            "H": _num(cab.H),
            "D": _num(cab.D),
            "t": _num(cab.panel_thickness),
            "material": cab.material,
            "color": cab.color,
            "ip": cab.ip,
            "base": _num(cab.base_height),
            "plate_w": _num(cab.plate_w),
            "plate_h": _num(cab.plate_h),
            "door_type": (
                "双开门"
                if cab.double_door
                else ("壁挂单门" if cab.template == "wallbox" else "单开门")
            ),
            "cabinet_type": "琴式操作台" if cab.is_console else "控制柜",
            "hinge": (
                "左右对开"
                if cab.double_door
                else ("左开" if cab.hinge == "left" else "右开")
            ),
        }
        out: list[str] = []
        for raw in list(tpl.get("items", [])) + list(job.custom_requirements):
            text = str(raw).strip()
            if not text:
                continue
            try:
                out.append(text.format(**mapping))
            except (KeyError, ValueError, IndexError):
                out.append(text)
        return out


def make_default_job() -> Job:
    """新建项目的起点：立式双门柜，空开孔。"""
    return Job(cabinet=Cabinet(template="vertical_double"), sheet=SheetInfo())
