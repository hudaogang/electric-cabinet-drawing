"""视图几何计算：门板、五金、安装板、导轨器件排布、线槽."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .library import Library, ResolvedCutout, geom_bbox
from .model import Cabinet, Duct, Rail


@dataclass
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    def inflated(self, d: float) -> "Box":
        return Box(self.x0 - d, self.y0 - d, self.x1 + d, self.y1 + d)

    def overlaps(self, other: "Box", gap: float = 0.0) -> bool:
        return not (
            self.x1 + gap <= other.x0
            or other.x1 + gap <= self.x0
            or self.y1 + gap <= other.y0
            or other.y1 + gap <= self.y0
        )


@dataclass
class DoorLeaf:
    box: Box
    hinge: str  # left / right
    free_edge: float


@dataclass
class Hardware:
    kind: str            # rect / circle / line
    x: float
    y: float
    w: float = 0.0
    h: float = 0.0
    d: float = 0.0
    role: str = ""


@dataclass
class FrontGeometry:
    outer: Box
    door_l: float
    door_r: float
    door_b: float
    door_t: float
    leaves: list[DoorLeaf]
    hardware: list[Hardware]
    gap_center: float | None
    base_line: float

    @property
    def door_box(self) -> Box:
        return Box(self.door_l, self.door_b, self.door_r, self.door_t)


def box_from_geom(geom: dict[str, Any], cx: float, cy: float) -> Box:
    x0, y0, x1, y1 = geom_bbox(geom, cx, cy)
    return Box(x0, y0, x1, y1)


def front_geometry(cab: Cabinet) -> FrontGeometry:
    outer = Box(0, 0, cab.W, cab.front_dev_h if cab.is_console else cab.H)
    dl, dr = cab.door_left, cab.door_right
    db, dt = cab.door_lower, cab.door_upper
    leaves: list[DoorLeaf] = []
    gap_center = None

    if cab.double_door:
        inner = (dr - dl) - cab.door_gap
        left_w = max(50.0, inner * cab.door_ratio)
        right_w = max(50.0, inner - left_w)
        leaves.append(DoorLeaf(Box(dl, db, dl + left_w, dt), "left", dl + left_w))
        leaves.append(DoorLeaf(Box(dl + left_w + cab.door_gap, db, dr, dt), "right", dl + left_w + cab.door_gap))
        gap_center = dl + left_w + cab.door_gap / 2
    else:
        leaves.append(DoorLeaf(Box(dl, db, dr, dt), cab.hinge, dr if cab.hinge == "left" else dl))

    hardware: list[Hardware] = []
    for leaf in leaves:
        b = leaf.box
        hinge_x = b.x0 if leaf.hinge == "left" else b.x1
        hh = min(80.0, max(40.0, b.h * 0.05))
        hw = min(18.0, max(10.0, b.w * 0.015))
        for frac in (1 / 6, 5 / 6):
            cy = b.y0 + b.h * frac
            hardware.append(Hardware("rect", hinge_x - hw / 2, cy - hh / 2, hw, hh, role="hinge"))

        bar_w = min(22.0, max(14.0, b.w * 0.02))
        bar_h = min(180.0, max(90.0, b.h * 0.14))
        cy = b.y0 + b.h * 0.45
        if leaf.hinge == "left":
            hx = b.x1 - 22.0 - bar_w
        else:
            hx = b.x0 + 22.0
        hx = min(max(hx, b.x0 + 12.0), b.x1 - 12.0 - bar_w)
        hardware.append(Hardware("rect", hx, cy - bar_h / 2, bar_w, bar_h, role="handle"))
        # 锁孔示意
        hardware.append(Hardware("circle", hx + bar_w / 2, cy, d=min(18.0, bar_w * 0.7), role="lock"))

    return FrontGeometry(
        outer=outer,
        door_l=dl,
        door_r=dr,
        door_b=db,
        door_t=dt,
        leaves=leaves,
        hardware=hardware,
        gap_center=gap_center,
        base_line=cab.base_height,
    )


@dataclass
class DevicePos:
    model: str
    name: str
    designation: str
    box: Box


@dataclass
class DeviceGroup:
    model: str
    name: str
    qty: int
    first: str
    last: str
    brand: str = ""
    spec: str = ""


@dataclass
class RailLayout:
    index: int
    label: str
    x: float
    y: float
    length: float
    device_list: list[DevicePos] = field(default_factory=list)
    groups: list[DeviceGroup] = field(default_factory=list)
    designation_first: str = ""
    designation_last: str = ""
    used_width: float = 0.0

    @property
    def rail_top(self) -> float:
        return self.y + 35.0


@dataclass
class InternalLayout:
    plate_w: float
    plate_h: float
    rails: list[RailLayout]
    ducts: list[tuple[Box, Duct]]


RAIL_EDGE_MARGIN = 40.0
RAIL_DEFAULT_MAX = 600.0


def default_rail_length(plate_w: float) -> float:
    """新建导轨的默认长度。

    不再默认铺满整块安装板：至少给左右拖动留出 200mm 余量，最长按
    600mm 标准导轨段取，窄板则自动缩短。长度填回对话框后仍可改成“自动”。
    """
    plate_w = max(1.0, float(plate_w))
    return max(80.0, min(RAIL_DEFAULT_MAX, plate_w - 200.0))


def fit_rail_geometry(
    plate_w: float,
    plate_h: float,
    click_x: float,
    click_y: float,
    length: float | None = None,
) -> tuple[float, float, float]:
    """在安装板上放一根不越界的新导轨，返回显式的 (X, Y, 长度)。

    以点击位置为导轨中点，靠近板边时自动平移/缩短，保证 X + 长度 ≤ 板宽；
    这样右击空白处新建的导轨不会一拖就跳到左边，也不会超出安装板。
    """
    plate_w = max(1.0, float(plate_w))
    plate_h = max(1.0, float(plate_h))
    margin = min(RAIL_EDGE_MARGIN, plate_w / 4.0)
    max_len = max(20.0, plate_w - 2.0 * margin)
    raw_len = default_rail_length(plate_w) if length is None else float(length)
    length = min(max(40.0, raw_len), max_len)
    lo = min(margin, max(0.0, plate_w - length))
    hi = max(lo, plate_w - length - margin)
    x = min(max(float(click_x) - length / 2.0, lo), hi)
    y = min(max(float(click_y), 20.0), max(20.0, plate_h - 20.0))
    return float(x), float(y), float(length)


def rail_layouts(cab: Cabinet, rails: list[Rail], lib: Library) -> list[RailLayout]:
    counters: dict[str, int] = {}
    result: list[RailLayout] = []
    for idx, rail in enumerate(rails):
        x = rail.x if rail.x is not None else 60.0
        length = rail.length if rail.length is not None else max(80.0, cab.plate_w - 120.0)
        cur = x + 20.0
        positions: list[DevicePos] = []
        groups: list[DeviceGroup] = []
        first = ""
        last = ""
        for item in rail.devices:
            dev = lib.device(item.model)
            w = item.width if item.width > 0 else float(dev.get("width", 36))
            h = item.height if item.height > 0 else float(dev.get("height", 85))
            prefix = item.prefix or str(dev.get("prefix", "E"))
            name = item.name or str(dev.get("name", item.model))
            brand = item.brand or str(dev.get("brand", ""))
            spec = item.spec or str(dev.get("spec", item.model))
            gfirst = ""
            glast = ""
            for _ in range(max(1, int(item.qty))):
                n = counters.get(prefix, 0) + 1
                counters[prefix] = n
                desig = f"{prefix}{n}"
                first = first or desig
                last = desig
                gfirst = gfirst or desig
                glast = desig
                positions.append(
                    DevicePos(
                        model=item.model,
                        name=name,
                        designation=desig,
                        box=Box(cur, rail.y + 17.5, cur + w, rail.y + 17.5 + h),
                    )
                )
                cur += w
            if gfirst:
                groups.append(
                    DeviceGroup(
                        model=item.model,
                        name=name,
                        qty=max(1, int(item.qty)),
                        first=gfirst,
                        last=glast,
                        brand=brand,
                        spec=spec,
                    )
                )
        result.append(
            RailLayout(
                index=idx,
                label=f"L{idx + 1}",
                x=x,
                y=rail.y,
                length=length,
                device_list=positions,
                groups=groups,
                designation_first=first,
                designation_last=last,
                used_width=cur - x,
            )
        )
    return result


def internal_layout(cab: Cabinet, rails: list[Rail], ducts: list[Duct], lib: Library) -> InternalLayout:
    rls = rail_layouts(cab, rails, lib)
    dboxes = []
    for d in ducts:
        if d.orient == "v":
            dboxes.append((Box(d.x, d.y, d.x + d.width, d.y + d.length), d))
        else:
            dboxes.append((Box(d.x, d.y, d.x + d.length, d.y + d.width), d))
    return InternalLayout(plate_w=cab.plate_w, plate_h=cab.plate_h, rails=rls, ducts=dboxes)


def cutout_box(rc: ResolvedCutout) -> Box:
    return box_from_geom(rc.cutout, rc.x, rc.y)
