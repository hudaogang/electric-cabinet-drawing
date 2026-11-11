"""导出 PDF / SVG / PNG / DXF 及表格 CSV."""
from __future__ import annotations

import csv
from pathlib import Path

from matplotlib.figure import Figure


def save_figure(fig: Figure, path: str | Path, title: str = "") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    metadata = {"Title": title, "Creator": "电柜快画"}
    if suffix == ".pdf":
        fig.savefig(path, format="pdf", facecolor="white", metadata=metadata)
    elif suffix == ".svg":
        fig.savefig(path, format="svg", facecolor="white", metadata=metadata)
    elif suffix in (".png", ".jpg", ".jpeg"):
        fig.savefig(path, format=suffix.lstrip("."), dpi=200, facecolor="white")
    elif suffix == ".dxf":
        from .dxf import save_dxf

        save_dxf(fig, path)
    else:
        raise ValueError(f"不支持的输出格式：{path.suffix}")
    return path


def save_tables_csv(job, lib, path: str | Path) -> Path:
    """把开孔表、材料表、导轨元件表导出为 CSV（Excel 可直接打开）。"""
    from .sheet import bom_rows, choose_layout, hole_rows, rail_rows

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    layout, _issues = choose_layout(job)
    resolved = lib.resolve_all(job)
    numbered = list(enumerate(resolved, 1))

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["电柜快画 表格导出"])
        w.writerow(["项目", job.sheet.project, "图号", job.sheet.drawing_no])
        w.writerow(["图幅", layout.page, "比例", f"1:{layout.den:g}"])
        w.writerow(["柜体 W×H×D", f"{job.cabinet.W:g}×{job.cabinet.H:g}×{job.cabinet.D:g}"])
        w.writerow([])
        w.writerow(["开孔表"])
        w.writerow(["编号", "面", "名称", "开孔尺寸", "X", "Y", "代号/备注"])
        w.writerows(hole_rows(numbered))
        w.writerow([])
        w.writerow(["柜内元件材料表"])
        w.writerow(["序号", "名称", "规格/型号", "数量", "备注"])
        w.writerows(bom_rows(job, lib))
        w.writerow([])
        w.writerow(["导轨元件表"])
        w.writerow(["导轨", "元件名称", "规格/型号", "数量", "自动编号"])
        w.writerows(rail_rows(job, lib))
    return path


def default_filename(project: str, drawing_no: str, suffix: str) -> str:
    base = drawing_no or project or "壳体接口图"
    base = "".join(ch for ch in base if ch not in r'\/:*?"<>|').strip() or "壳体接口图"
    return f"{base}{suffix}"
