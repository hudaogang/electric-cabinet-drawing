"""设计校验：越界、跨门缝、孔间干涉、边距、导轨器件超长等."""
from __future__ import annotations

from dataclasses import dataclass

from .library import Library
from .model import FACE_LABELS, Duct, Job, face_dims
from .views import Box, box_from_geom, front_geometry, internal_layout


@dataclass
class Issue:
    level: str      # error / warn
    text: str

    def __str__(self) -> str:
        return f"[{'错误' if self.level == 'error' else '提示'}] {self.text}"


def _duct_overlap_kind(b1: Box, d1: Duct, b2: Box, d2: Duct) -> str:
    """区分线槽重叠类型，避免把正常的转角/T 形搭接误报为干涉。

    - touch：仅边界接触；
    - junction：转角或 T 形搭接（一条线槽的端部接到另一条上）；
    - parallel：同方向并排/重叠；
    - contained：几乎完全重合；
    - crossing：十字交叉且两条都继续延伸。
    """
    ix0, ix1 = max(b1.x0, b2.x0), min(b1.x1, b2.x1)
    iy0, iy1 = max(b1.y0, b2.y0), min(b1.y1, b2.y1)
    if ix1 <= ix0 + 1e-6 or iy1 <= iy0 + 1e-6:
        return "touch"
    area = (ix1 - ix0) * (iy1 - iy0)
    small_area = min(b1.w * b1.h, b2.w * b2.h)
    if small_area > 0 and area >= small_area * 0.9:
        return "contained"
    if d1.orient == d2.orient:
        return "parallel"

    def end_meets(duct: Duct, box: Box, other: Box) -> bool:
        if duct.orient == "v":
            lo, hi = other.y0 - 1.0, other.y1 + 1.0
            return lo <= box.y0 <= hi or lo <= box.y1 <= hi
        lo, hi = other.x0 - 1.0, other.x1 + 1.0
        return lo <= box.x0 <= hi or lo <= box.x1 <= hi

    if end_meets(d1, b1, b2) or end_meets(d2, b2, b1):
        return "junction"
    return "crossing"


def _console_side_top(cab, x: float) -> float:
    """琴柜侧板在距前沿 x 处的上边界高度：斜面下方为斜线，后部为平顶。"""
    run = cab.D - cab.console_top_depth
    if run <= 0:
        return cab.H
    if x <= 0:
        return cab.console_desk_h
    if x >= run:
        return cab.H
    return cab.console_desk_h + (cab.H - cab.console_desk_h) * x / run


def validate(job: Job, lib: Library) -> list[Issue]:
    issues: list[Issue] = []
    cab = job.cabinet

    if cab.W <= 100 or cab.H <= 100 or cab.D <= 50:
        issues.append(Issue("error", "柜体尺寸不合理：W、H 应大于 100mm，D 应大于 50mm。"))
    if cab.base_height < 0:
        issues.append(Issue("error", "底座高度不能为负数。"))
    if cab.base_height >= cab.H * 0.5:
        issues.append(Issue("error", "底座高度过大，应小于柜高的 50%。"))
    if cab.door_upper - cab.door_lower < 200:
        issues.append(Issue("error", "门板净高过小，请检查柜高、底座和门板边距。"))
    door_w = cab.door_right - cab.door_left
    if door_w < 100:
        issues.append(Issue("error", "门板净宽过小，请检查门边距与柜宽。"))
    if cab.door_gap < 0:
        issues.append(Issue("error", "门缝宽度不能为负数。"))
    if cab.template == "vertical_double":
        if cab.door_gap >= max(0.0, door_w - 100):
            issues.append(Issue("error", "门缝宽度过大，门板有效宽度不足。"))
        if not (0.1 <= cab.door_ratio <= 0.9):
            issues.append(Issue("error", "左门宽比应在 0.1～0.9 之间。"))
    if cab.plate_margin < 0 or cab.plate_bottom_gap < 0:
        issues.append(Issue("error", "安装板边距/下间隙不能为负数。"))
    if cab.plate_w < 100 or cab.plate_h < 100:
        issues.append(Issue("error", "安装板尺寸过小，请减小边距或增大柜体尺寸。"))

    if cab.is_console:
        if cab.console_desk_h <= cab.base_height + 150 or cab.console_desk_h >= cab.H - 100:
            issues.append(Issue("error", "琴柜前沿高应高于底座 150mm 以上，且低于总高 100mm 以上。"))
        if cab.console_top_depth < 50 or cab.console_top_depth >= cab.D - 100:
            issues.append(Issue("error", "琴柜后平顶深度应在 50mm 与柜深-100mm 之间。"))
        if cab.D - cab.console_top_depth < 150:
            issues.append(Issue("error", "琴柜斜面水平投影过小，请减小后平顶深度或增大柜深。"))
        if cab.H - cab.console_desk_h < 80:
            issues.append(Issue("error", "琴柜斜面高度过小，请增大总高或降低前沿高。"))

    fg = front_geometry(cab)
    door_box = fg.door_box

    resolved = lib.resolve_all(job)
    boxes: list[tuple[int, str, object]] = []
    for rc in resolved:
        b = box_from_geom(rc.cutout, rc.x, rc.y)
        boxes.append((rc.index + 1, rc.name, b))
        face_name = FACE_LABELS.get(rc.face, "正面")
        if rc.face == "front":
            if cab.is_console:
                # 琴柜正面 = 下部门板 + 上方沿斜面展开的操作面板。
                limit = Box(
                    cab.door_left, cab.door_lower,
                    cab.door_right, cab.front_dev_h,
                )
                limit_name = "正面展开（门板+斜面）"
            else:
                limit = door_box
                limit_name = "门板"
        else:
            fw, fh = face_dims(cab, rc.face)
            limit = box_from_geom({"type": "rect", "w": fw, "h": fh}, fw / 2, fh / 2)
            limit_name = face_name
        if not (
            b.x0 >= limit.x0
            and b.x1 <= limit.x1
            and b.y0 >= limit.y0
            and b.y1 <= limit.y1
        ):
            issues.append(
                Issue(
                    "error",
                    f"开孔 #{rc.index + 1}（{face_name}，{rc.name}，代号 {rc.label or '-'}）超出{limit_name}范围。",
                )
            )
        edge = min(b.x0 - limit.x0, limit.x1 - b.x1, b.y0 - limit.y0, limit.y1 - b.y1)
        if 0 <= edge < 15:
            issues.append(
                Issue(
                    "warn",
                    f"开孔 #{rc.index + 1}（{face_name}，{rc.label or rc.name}）距{limit_name}边缘仅 {edge:.0f}mm，建议 ≥15mm。",
                )
            )

        if (
            rc.face == "front"
            and cab.double_door
            and fg.gap_center is not None
            and (not cab.is_console or b.cy <= cab.console_desk_h + 0.5)
        ):
            gap = cab.door_gap / 2
            if b.x0 < fg.gap_center + gap and b.x1 > fg.gap_center - gap:
                issues.append(
                    Issue(
                        "error",
                        f"开孔 #{rc.index + 1}（{rc.label or rc.name}）跨越双开门中缝，必须移到单侧门板内。",
                    )
                )

        if cab.is_console and rc.face == "front":
            fold = cab.console_desk_h
            if b.y0 < fold - 0.5 and b.y1 > fold + 0.5:
                issues.append(
                    Issue(
                        "error",
                        f"开孔 #{rc.index + 1}（{rc.label or rc.name}）跨越门板与斜面的折弯线，请移到折弯线一侧。",
                    )
                )

        if cab.is_console and rc.face == "top":
            flat = Box(0, cab.D - cab.console_top_depth, cab.W, cab.D)
            if not (
                b.x0 >= flat.x0 - 0.5 and b.x1 <= flat.x1 + 0.5
                and b.y0 >= flat.y0 - 0.5 and b.y1 <= flat.y1 + 0.5
            ):
                issues.append(
                    Issue(
                        "error",
                        f"开孔 #{rc.index + 1}（{rc.label or rc.name}）不在琴柜后部平顶范围内；"
                        "斜面上的开孔请选“正面”，按展开坐标放置。",
                    )
                )

        if cab.is_console and rc.face in ("left", "right"):
            top_at_front = _console_side_top(cab, b.x0)
            if b.y1 > top_at_front + 0.5:
                issues.append(
                    Issue(
                        "error",
                        f"开孔 #{rc.index + 1}（{rc.label or rc.name}）超出琴柜侧板轮廓（斜面下方无侧板）。",
                    )
                )

    # 孔间干涉（同一面按外形轮廓 + 3mm 间隙；不同面互不干涉）
    outline_boxes = []
    for rc in resolved:
        ob = box_from_geom(rc.outline, rc.x, rc.y)
        outline_boxes.append((rc, ob))
    for i in range(len(outline_boxes)):
        for j in range(i + 1, len(outline_boxes)):
            rc1, b1 = outline_boxes[i]
            rc2, b2 = outline_boxes[j]
            if rc1.face != rc2.face:
                continue
            if b1.overlaps(b2, gap=3):
                issues.append(
                    Issue(
                        "error",
                        f"开孔 #{rc1.index + 1}（{rc1.label or rc1.name}）与 #{rc2.index + 1}（{rc2.label or rc2.name}）在{FACE_LABELS.get(rc1.face, '正面')}干涉。",
                    )
                )

    # 安装板 / 导轨 / 器件
    layout = internal_layout(cab, job.rails, job.ducts, lib)
    for rl in layout.rails:
        if rl.y < 20 or rl.y > layout.plate_h - 20:
            issues.append(Issue("error", f"导轨 {rl.label} 的 Y 位置超出安装板范围。"))
        if rl.x < 0 or rl.x + rl.length > layout.plate_w + 0.01:
            issues.append(
                Issue("error", f"导轨 {rl.label} 超出安装板宽度（X={rl.x:.0f}, L={rl.length:.0f}）。")
            )
        if rl.used_width > rl.length:
            issues.append(
                Issue(
                    "error",
                    f"导轨 {rl.label} 器件总宽 {rl.used_width:.0f}mm 超过导轨长度 {rl.length:.0f}mm，请换更长导轨或分排。",
                )
            )
        for dp in rl.device_list:
            top = dp.box.y1
            if top > layout.plate_h + 0.01:
                issues.append(
                    Issue("warn", f"导轨 {rl.label} 上器件 {dp.designation} 顶部超出安装板。")
                )
            if dp.box.x1 > rl.x + rl.length + 0.01:
                issues.append(
                    Issue("error", f"导轨 {rl.label} 上器件 {dp.designation} 超出导轨末端。")
                )

    for b, d in layout.ducts:
        if b.x0 < -0.01 or b.y0 < -0.01 or b.x1 > layout.plate_w + 0.01 or b.y1 > layout.plate_h + 0.01:
            issues.append(Issue("warn", "线槽超出安装板范围，请检查线槽坐标与长度。"))
            break

    # 安装板内导轨/线槽互相重叠（可在安装板布置图中直接拖动调整）
    rail_boxes = [
        (rl, Box(rl.x, rl.y - 17.5, rl.x + rl.length, rl.y + 17.5))
        for rl in layout.rails
    ]
    for rl, rb in rail_boxes:
        for db, duct in layout.ducts:
            if rb.overlaps(db):
                orient = "垂直" if duct.orient == "v" else "水平"
                issues.append(
                    Issue(
                        "error",
                        f"导轨 {rl.label} 与{orient}线槽（X={duct.x:.0f}, Y={duct.y:.0f}）重叠，请移动其中一个。",
                    )
                )
    for i in range(len(rail_boxes)):
        for j in range(i + 1, len(rail_boxes)):
            rl1, b1 = rail_boxes[i]
            rl2, b2 = rail_boxes[j]
            if b1.overlaps(b2):
                issues.append(
                    Issue("error", f"导轨 {rl1.label} 与 {rl2.label} 重叠，请调整 Y 或起点 X。")
                )
    for i in range(len(layout.ducts)):
        for j in range(i + 1, len(layout.ducts)):
            b1, d1 = layout.ducts[i]
            b2, d2 = layout.ducts[j]
            kind = _duct_overlap_kind(b1, d1, b2, d2)
            if kind in ("touch", "junction"):
                # 转角/T 形搭接属于正常工艺，不再重复提示。
                continue
            if kind == "parallel":
                text = f"线槽 {i + 1} 与线槽 {j + 1} 平行重叠，请错开或合并。"
            elif kind == "contained":
                text = f"线槽 {i + 1} 与线槽 {j + 1} 几乎完全重合，请检查坐标/长度。"
            else:
                text = f"线槽 {i + 1} 与线槽 {j + 1} 十字交叉且两端都继续延伸，请确认是否需要断开。"
            issues.append(Issue("warn", text))

    if not job.cutouts:
        issues.append(Issue("warn", "当前没有开孔，孔位表将为空。"))
    if not job.rails:
        issues.append(Issue("warn", "当前没有内部导轨布置，安装板图只有空板。"))

    # 图幅：比例过小提示
    if job.sheet.scale != "auto":
        try:
            s = 1.0 / float(job.sheet.scale.replace("：", ":").split(":")[1])
            if s < 1 / 50:
                issues.append(Issue("warn", "指定比例过小，打印后文字可能难以辨认。"))
        except (ValueError, IndexError):
            issues.append(Issue("warn", "比例格式应为 1:10 这类形式。"))

    return issues
