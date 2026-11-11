"""数据模型：柜体、开孔、导轨、线槽、图框与完整任务.

所有长度单位均为 mm。坐标约定：
  * 门板开孔坐标：相对柜体正面左下角，X 向右、Y 向上（柜体坐标）。
  * 安装板坐标：相对安装板左下角，X 向右、Y 向上（板坐标）。
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class Cabinet:
    template: str = "vertical_double"
    W: float = 1700.0
    H: float = 1900.0
    D: float = 460.0
    base_height: float = 100.0        # 底座高度；壁挂箱为 0
    door_gap: float = 2.0             # 双开门中缝宽度
    door_ratio: float = 0.5           # 左门占内宽比例
    hinge: str = "left"               # 单门铰链侧：left / right
    door_margin: float = 12.0         # 门板四周与柜体外沿的间距
    door_bottom_gap: float = 3.0      # 门板下沿与底座上沿的间隙
    door_thickness: float = 25.0      # 门厚（侧视/俯视示意）
    panel_thickness: float = 1.5      # 柜体板厚
    material: str = "冷轧钢板"
    color: str = "RAL7035"
    ip: str = "IP54"
    plate_margin: float = 30.0        # 安装板与柜体左/右/上内壁的间隙
    plate_bottom_gap: float = 50.0    # 安装板下沿距底座上沿的高度
    # 琴式操作台专用：前沿高（地面到斜面下沿）、后部平顶深度
    console_desk_h: float = 720.0
    console_top_depth: float = 250.0

    @property
    def is_console(self) -> bool:
        return self.template == "console"

    @property
    def double_door(self) -> bool:
        """双开门柜型：立式双门柜，以及宽度较大的琴式操作台。"""
        return self.template == "vertical_double" or (
            self.template == "console" and self.W >= 900
        )

    @property
    def door_lower(self) -> float:
        return self.base_height + (self.door_bottom_gap if self.base_height > 0 else 0.0)

    @property
    def door_upper(self) -> float:
        if self.is_console:
            # 琴柜门板只到前沿高，上面是斜面操作面板。
            return max(self.door_lower + 150.0, self.console_desk_h - 4.0)
        return self.H - self.door_margin

    # ---------- 琴式操作台几何 ----------
    @property
    def console_run(self) -> float:
        """斜面水平投影：从柜体前沿到后部平顶前缘。"""
        return max(1.0, self.D - self.console_top_depth)

    @property
    def console_rise(self) -> float:
        return max(1.0, self.H - self.console_desk_h)

    @property
    def console_panel_len(self) -> float:
        """斜面操作面板沿斜面的真实长度。"""
        return math.hypot(self.console_run, self.console_rise)

    @property
    def front_dev_h(self) -> float:
        """正面展开高度：门板到前沿高，再沿斜面展开真实长度。"""
        if self.is_console:
            return self.console_desk_h + self.console_panel_len
        return self.H

    @property
    def door_left(self) -> float:
        return self.door_margin

    @property
    def door_right(self) -> float:
        return self.W - self.door_margin

    @property
    def plate_w(self) -> float:
        return max(50.0, self.W - 2.0 * self.plate_margin)

    @property
    def plate_h(self) -> float:
        if self.is_console:
            # 安装板藏在操作台下柜内，不能按总高计算。
            return max(
                50.0,
                self.console_desk_h - self.base_height
                - self.plate_bottom_gap - self.plate_margin,
            )
        return max(50.0, self.H - self.base_height - self.plate_bottom_gap - self.plate_margin)


# 开孔所在面：front 正面门板；left/right 左/右侧板；top/bottom 顶/底板
FACE_LABELS = {
    "front": "正面",
    "left": "左侧",
    "right": "右侧",
    "top": "顶板",
    "bottom": "底板",
}
FACE_CHOICES = [
    ("front", "正面（门板）"),
    ("left", "左侧板"),
    ("right", "右侧板"),
    ("top", "顶板"),
    ("bottom", "底板"),
]


def cutout_face(cut: "Cutout") -> str:
    """返回规范化后的面代码，兼容旧项目中缺失 face 字段的数据。"""
    face = getattr(cut, "face", "front") or "front"
    return face if face in FACE_LABELS else "front"


def face_dims(cab: "Cabinet", face: str) -> tuple[float, float]:
    """开孔面的展开尺寸：正面 W×H（琴柜含斜面展开），侧面 D×H，顶/底面 W×D。"""
    if face in ("left", "right"):
        return max(1.0, cab.D), max(1.0, cab.H)
    if face in ("top", "bottom"):
        return max(1.0, cab.W), max(1.0, cab.D)
    return max(1.0, cab.W), max(1.0, cab.front_dev_h)


def face_edge_labels(face: str) -> tuple[str, str, str, str, str, str]:
    """返回 (X 轴说明, Y 轴说明, X 小边, X 大边, Y 小边, Y 大边) 的中文名称。

    正面：X 距左、Y 距下；侧面：X 距前、Y 距下；顶/底面：X 距左、Y 距前。
    """
    if face in ("left", "right"):
        return "X 距前", "Y 距下", "距前", "距后", "距下", "距上"
    if face in ("top", "bottom"):
        return "X 距左", "Y 距前", "距左", "距右", "距前", "距后"
    return "X 距左", "Y 距下", "距左", "距右", "距下", "距上"


@dataclass
class Cutout:
    component: str = "pushbutton_22"
    x: float = 0.0
    y: float = 0.0
    label: str = ""       # 代号，如 SB1 / HL1
    note: str = ""
    w: float = 0.0        # 自定义覆写（0 = 用元件库值）
    h: float = 0.0
    d: float = 0.0
    face: str = "front"   # front / left / right / top / bottom


@dataclass
class DeviceItem:
    model: str = "2P"
    qty: int = 1
    width: float = 0.0     # 覆写宽度，0 = 用器件库
    height: float = 0.0    # 覆写高度，0 = 用器件库
    prefix: str = ""       # 覆写编号前缀
    note: str = ""
    # 品牌/名称/规格覆写：不改变几何，只影响材料表显示；
    # 为空时回退到器件库同名条目。
    brand: str = ""
    name: str = ""
    spec: str = ""


@dataclass
class Rail:
    y: float = 200.0              # 导轨中心距安装板下沿
    x: float | None = None        # 起点，None = 自动
    length: float | None = None   # 长度，None = 自动
    devices: list[DeviceItem] = field(default_factory=list)


@dataclass
class Duct:
    x: float = 0.0
    y: float = 0.0
    length: float = 1000.0
    width: float = 60.0
    orient: str = "h"             # h 水平 / v 垂直


@dataclass
class SheetInfo:
    size: str = "auto"            # auto / A3 / A2 / A1
    scale: str = "auto"           # auto / 1:5 / 1:10 ...
    project: str = "非标控制柜壳体接口图"
    drawing_no: str = "DG-2026-001"
    version: str = "A"
    designer: str = ""
    checker: str = ""
    date: str = ""
    company: str = ""
    remark: str = ""

    def effective_date(self) -> str:
        return self.date or date.today().isoformat()


@dataclass
class Job:
    cabinet: Cabinet = field(default_factory=Cabinet)
    cutouts: list[Cutout] = field(default_factory=list)
    rails: list[Rail] = field(default_factory=list)
    ducts: list[Duct] = field(default_factory=list)
    sheet: SheetInfo = field(default_factory=SheetInfo)
    req_set: str = "standard"
    custom_requirements: list[str] = field(default_factory=list)
    views: list[str] = field(default_factory=lambda: ["front", "side", "plan", "internal"])

    # ---------- 序列化 ----------
    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        cab = _build(Cabinet, data.get("cabinet", {}))
        cutouts = [_build(Cutout, c) for c in data.get("cutouts", [])]
        rails: list[Rail] = []
        for r in data.get("rails", []):
            rail = _build(Rail, r)
            rail.devices = [_build(DeviceItem, d) for d in r.get("devices", [])]
            rails.append(rail)
        ducts = [_build(Duct, d) for d in data.get("ducts", [])]
        sheet = _build(SheetInfo, data.get("sheet", {}))
        views = data.get("views") or ["front", "side", "plan", "internal"]
        return cls(
            cabinet=cab,
            cutouts=cutouts,
            rails=rails,
            ducts=ducts,
            sheet=sheet,
            req_set=data.get("req_set", "standard"),
            custom_requirements=list(data.get("custom_requirements", [])),
            views=list(views),
        )


def _build(cls, data: dict[str, Any] | None):
    """只取数据类已声明的字段，容忍旧版/多余键。"""
    data = data or {}
    names = {f.name for f in dataclasses.fields(cls)}
    kwargs = {k: v for k, v in data.items() if k in names}
    return cls(**kwargs)


# 便携解析表达式：如 "2P×6, 3P×4" / "2P*6;3P*4"
def parse_device_expr(text: str) -> list[DeviceItem]:
    items: list[DeviceItem] = []
    for chunk in text.replace("；", ";").replace("，", ",").split(","):
        for part in chunk.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            sep = None
            for cand in ("×", "*", "x", "X"):
                if cand in part:
                    sep = cand
                    break
            if sep:
                model, _, qty_s = part.partition(sep)
                model = model.strip()
                try:
                    qty = max(1, int(float(qty_s.strip())))
                except ValueError:
                    qty = 1
            else:
                model, qty = part, 1
            if model:
                items.append(DeviceItem(model=model, qty=qty))
    return items


def format_device_expr(items: list[DeviceItem]) -> str:
    return ", ".join(f"{d.model}×{d.qty}" for d in items)
