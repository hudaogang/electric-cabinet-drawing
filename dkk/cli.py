"""命令行入口：便于批处理和验证."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import configure_console
from .export import default_filename, save_figure, save_tables_csv
from .library import Library, data_dir
from .model import Job
from .sheet import build_drawing


def main(argv: list[str] | None = None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(prog="电柜快画", description="电气壳体接口图自动生成")
    parser.add_argument("job", nargs="?", help="项目 JSON 文件；省略时使用内置示例")
    parser.add_argument(
        "-o", "--output",
        help="输出文件（.pdf/.svg/.png/.dxf/.csv），默认 output/图号.pdf",
    )
    parser.add_argument("--png", action="store_true", help="同时输出 PNG 预览")
    parser.add_argument("--csv", action="store_true", help="同时输出开孔/材料/导轨表格 CSV")
    parser.add_argument("--no-sizes", action="store_true", help="不在图纸上标注开孔尺寸")
    args = parser.parse_args(argv)

    if args.job:
        job_path = Path(args.job)
    else:
        job_path = data_dir() / "examples" / "demo_1700x1900x460.json"
    with open(job_path, "r", encoding="utf-8") as f:
        job = Job.from_dict(json.load(f))

    lib = Library.load()
    fig, meta = build_drawing(job, lib, show_cutout_sizes=not args.no_sizes)

    if args.output:
        out = Path(args.output)
    else:
        out = Path("output") / default_filename(job.sheet.project, job.sheet.drawing_no, ".pdf")
    if out.suffix.lower() == ".csv":
        save_tables_csv(job, lib, out)
    else:
        save_figure(fig, out, title=job.sheet.project)

    for issue in meta["issues"]:
        print(issue)
    print(f"图幅 {meta['page']}，比例 {meta['scale']}，输出：{out.resolve()}")
    if args.png and out.suffix.lower() != ".png":
        png = out.with_suffix(".png")
        save_figure(fig, png, title=job.sheet.project)
        print(f"PNG 预览：{png.resolve()}")
    if args.csv and out.suffix.lower() != ".csv":
        csv_path = out.with_suffix(".csv")
        save_tables_csv(job, lib, csv_path)
        print(f"表格 CSV：{csv_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
