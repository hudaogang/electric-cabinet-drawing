"""基础回归测试：不依赖 GUI，覆盖库加载、出图、校验、JSON 往返与导出."""
from __future__ import annotations

import dataclasses
import json
import os
import random
import tempfile
import unittest
from pathlib import Path

from matplotlib.patches import Rectangle

from dkk.export import save_figure, save_tables_csv
from dkk.library import Library, data_dir
from dkk.model import Cabinet, Cutout, DeviceItem, Duct, Job, Rail
from dkk.sheet import bom_rows, build_drawing, choose_layout, rail_rows
from dkk.validate import validate
from dkk.views import default_rail_length, fit_rail_geometry

LIB = Library.load()
DEMO = data_dir() / "examples" / "demo_1700x1900x460.json"
CONSOLE_DEMO = data_dir() / "examples" / "demo_console_1200x1100x750.json"


def load_demo() -> Job:
    with open(DEMO, "r", encoding="utf-8") as f:
        return Job.from_dict(json.load(f))


class TestLibraryAndDemo(unittest.TestCase):
    def test_library_has_core_entries(self):
        self.assertIn("pushbutton_22", LIB.components)
        self.assertIn("hmi_10", LIB.components)
        self.assertIn("vertical_double", LIB.templates)
        self.assertIn("standard", LIB.requirements)

    def test_demo_builds_on_a2(self):
        job = load_demo()
        fig, meta = build_drawing(job, LIB)
        self.assertEqual(meta["page"], "A2")
        self.assertEqual(meta["scale"], "1:15")
        errors = [i for i in meta["issues"] if i.level == "error"]
        self.assertEqual(errors, [], [str(i) for i in errors])
        self.assertEqual(len(meta["cutouts"]), 29)
        self.assertEqual(sorted(n for n, _ in meta["cutouts"]), list(range(1, 30)))
        # 清理 figure，避免测试进程累积
        fig.clear()

    def test_json_roundtrip(self):
        job = load_demo()
        again = Job.from_dict(job.to_dict())
        self.assertEqual(dataclasses.asdict(job), dataclasses.asdict(again))

    def test_requirement_variables(self):
        job = load_demo()
        texts = LIB.requirement_texts(job)
        self.assertTrue(any("1700" in t and "1900" in t and "460" in t for t in texts))
        self.assertTrue(any("1640 × 1720" in t for t in texts))
        self.assertTrue(any("左右对开" in t for t in texts))

    def test_figure_reuse_keeps_one_axes(self):
        """GUI 预览刷新复用同一个 Figure，避免销毁画布造成闪屏。"""
        job = load_demo()
        fig, meta = build_drawing(job, LIB)
        self.assertEqual(len(fig.axes), 1)
        fig2, meta2 = build_drawing(job, LIB, fig=fig)
        self.assertIs(fig2, fig)
        self.assertEqual(len(fig.axes), 1)
        self.assertEqual(meta2["page"], meta["page"])
        self.assertEqual(len(meta2["cutouts"]), len(meta["cutouts"]))
        fig.clear()

    def test_cutout_sizes_are_annotated(self):
        """触摸屏/仪表/按钮指示灯等开孔旁边应有尺寸标注。"""
        job = load_demo()
        fig, meta = build_drawing(job, LIB)
        texts = {t.get_text() for t in fig.axes[0].texts}
        self.assertIn("260×202", texts)   # 10 寸触摸屏
        self.assertIn("192×138", texts)   # 7 寸触摸屏
        self.assertIn("92×92×2", texts)   # 两台 96 仪表
        self.assertTrue(any(t.startswith("Φ22.5×") for t in texts))
        fig.clear()

    def test_cutout_sizes_can_be_disabled(self):
        """孔位很密时可以关闭尺寸标注。"""
        job = load_demo()
        fig, meta = build_drawing(job, LIB, show_cutout_sizes=False)
        texts = {t.get_text() for t in fig.axes[0].texts}
        for spec in ("260×202", "192×138", "92×92×2"):
            self.assertNotIn(spec, texts)
        self.assertFalse(any(t.startswith("开孔尺寸：") for t in texts))
        fig.clear()

    def test_duct_fill_is_darker_gray(self):
        """线槽使用灰色填充 + 中粗轮廓，和导轨区分。"""
        job = load_demo()
        fig, meta = build_drawing(job, LIB)
        fills = [
            p.get_facecolor() for p in fig.axes[0].patches
            if isinstance(p, Rectangle) and p.get_facecolor()[3] > 0
        ]
        self.assertTrue(
            any(
                abs(c[0] - 0.776) < 0.02
                and abs(c[1] - 0.776) < 0.02
                and abs(c[2] - 0.776) < 0.02
                for c in fills
            ),
            "应存在 #c6c6c6 的线槽填充",
        )
        fig.clear()


class TestOtherCabinets(unittest.TestCase):
    def test_wallbox(self):
        job = Job(
            cabinet=Cabinet(
                template="wallbox", W=600, H=800, D=250, base_height=0,
                door_margin=10, plate_margin=25, plate_bottom_gap=40,
            ),
            cutouts=[Cutout("hmi_7", 300, 520, "HMI1")],
            rails=[Rail(y=100, x=50, length=450,
                        devices=[DeviceItem("2P", 4)])],
            req_set="wallbox",
        )
        fig, meta = build_drawing(job, LIB)
        self.assertEqual(meta["page"], "A3")
        self.assertEqual([i for i in meta["issues"] if i.level == "error"], [])
        fig.clear()

    def test_single_door(self):
        job = Job(
            cabinet=Cabinet(
                template="vertical_single", W=800, H=2000, D=400,
                base_height=100, hinge="right",
            ),
            cutouts=[Cutout("pushbutton_22", 400, 1200, "SB1")],
            rails=[Rail(y=200, devices=[DeviceItem("3P", 6)])],
        )
        fig, meta = build_drawing(job, LIB)
        self.assertIn(meta["page"], ("A2", "A1"))
        self.assertEqual([i for i in meta["issues"] if i.level == "error"], [])
        fig.clear()

    def test_empty_job_only_warns(self):
        fig, meta = build_drawing(Job(), LIB)
        self.assertTrue(all(i.level == "warn" for i in meta["issues"]))
        fig.clear()


class TestValidation(unittest.TestCase):
    def _codes(self, job: Job) -> list[str]:
        return [i.text for i in validate(job, LIB)]

    def test_crossing_double_door_gap(self):
        job = load_demo()
        job.cutouts = [Cutout("pushbutton_22", job.cabinet.W / 2, 1000, "SBX")]
        self.assertTrue(any("中缝" in t for t in self._codes(job)))

    def test_cutout_overlap(self):
        job = load_demo()
        job.cutouts = [
            Cutout("pushbutton_22", 300, 1000, "SB1"),
            Cutout("pushbutton_22", 300, 1000, "SB2"),
        ]
        self.assertTrue(any("干涉" in t for t in self._codes(job)))

    def test_rail_duct_overlap(self):
        job = Job(
            cabinet=Cabinet(template="vertical_double"),
            rails=[Rail(y=300, x=100, length=600,
                        devices=[DeviceItem("2P", 3)])],
            ducts=[Duct(x=150, y=260, length=200, width=60, orient="h")],
        )
        self.assertTrue(any("重叠" in i.text for i in validate(job, LIB)))

    def test_separated_rail_and_duct_ok(self):
        job = Job(
            cabinet=Cabinet(template="vertical_double"),
            rails=[Rail(y=500, x=100, length=600,
                        devices=[DeviceItem("2P", 3)])],
            ducts=[Duct(x=150, y=100, length=200, width=60, orient="h")],
        )
        self.assertFalse(any("重叠" in i.text for i in validate(job, LIB)))

    def test_rail_length_overflow(self):
        job = Job(
            cabinet=Cabinet(template="vertical_double"),
            rails=[Rail(y=200, x=100, length=100,
                        devices=[DeviceItem("3P", 10)])],
        )
        self.assertTrue(any("超过导轨长度" in t for t in self._codes(job)))

    def test_cutout_out_of_door(self):
        job = load_demo()
        job.cutouts = [Cutout("pushbutton_22", 0, 0, "SB1")]
        self.assertTrue(any("超出门板范围" in t for t in self._codes(job)))


class TestRailPlacement(unittest.TestCase):
    """导轨新建/拖动相关的纯几何回归测试（不依赖 Tkinter）。"""

    def test_default_length_leaves_room_to_drag(self):
        """默认长度不能铺满安装板，否则左右拖动几乎没有余量。"""
        for plate_w in (550.0, 780.0, 1640.0):
            length = default_rail_length(plate_w)
            self.assertLessEqual(length, 600.0)
            self.assertGreaterEqual(plate_w - length, 200.0 - 1e-6)

    def test_click_rail_always_fits_plate(self):
        """右击板边新建时，导轨应自动平移/缩短而不是越界。"""
        for click_x in (-50.0, 10.0, 390.0, 760.0, 1200.0):
            x, y, length = fit_rail_geometry(780.0, 620.0, click_x, 500.0)
            self.assertGreaterEqual(x, 0.0)
            self.assertGreaterEqual(y, 20.0)
            self.assertLessEqual(y, 600.0)
            self.assertGreaterEqual(length, 40.0)
            self.assertLessEqual(x + length, 780.0 + 1e-6)

    def test_click_rail_keeps_devices_valid(self):
        """按点击位置新建的导轨 + 常用器件应能直接通过校验。"""
        cab = Cabinet(template="vertical_double", W=800, H=2000, D=400)
        x, y, length = fit_rail_geometry(
            cab.plate_w, cab.plate_h, cab.plate_w * 0.8, 400.0
        )
        job = Job(
            cabinet=cab,
            rails=[Rail(y=y, x=x, length=length, devices=[DeviceItem("2P", 6)])],
        )
        errors = [i for i in validate(job, LIB) if i.level == "error"]
        self.assertEqual(errors, [])


class TestDuctJunctions(unittest.TestCase):
    def _codes(self, job: Job) -> list[str]:
        return [i.text for i in validate(job, LIB)]

    def test_corner_joint_is_not_reported(self):
        job = Job(
            cabinet=Cabinet(template="vertical_double"),
            rails=[Rail(y=300, x=100, length=600)],
            ducts=[
                Duct(x=500, y=200, length=300, width=60, orient="v"),
                Duct(x=500, y=440, length=300, width=60, orient="h"),
            ],
        )
        texts = self._codes(job)
        self.assertFalse(any("与线槽" in t for t in texts), texts)

    def test_crossing_ducts_are_reported(self):
        job = Job(
            cabinet=Cabinet(template="vertical_double"),
            ducts=[
                Duct(x=500, y=100, length=600, width=60, orient="v"),
                Duct(x=300, y=350, length=400, width=60, orient="h"),
            ],
        )
        self.assertTrue(any("十字交叉" in t for t in self._codes(job)))


class TestFacesAndSlots(unittest.TestCase):
    def test_slot_geometry_and_spec(self):
        rc = LIB.resolve_cutout(
            Cutout("custom_slot", 500, 200, "SL1", face="top", w=220, h=40)
        )
        self.assertEqual(rc.face, "top")
        self.assertEqual(rc.cutout["type"], "slot")
        self.assertEqual((rc.cutout["w"], rc.cutout["h"]), (220, 40))
        self.assertEqual(rc.spec, "腰型 220×40")

    def test_bottom_slot_builds_without_error(self):
        job = Job(
            cabinet=Cabinet(template="vertical_double"),
            cutouts=[Cutout("custom_slot", 850, 230, "SL1", face="bottom", w=220, h=40)],
        )
        fig, meta = build_drawing(job, LIB)
        self.assertEqual([i for i in meta["issues"] if i.level == "error"], [])
        self.assertEqual([n for n, _ in meta["cutouts"]], [1])
        fig.clear()

    def test_same_position_on_different_faces_is_not_interference(self):
        job = Job(
            cabinet=Cabinet(template="vertical_double"),
            cutouts=[
                Cutout("custom_slot", 500, 200, "SL1", face="top"),
                Cutout("custom_slot", 500, 200, "SL2", face="bottom"),
            ],
        )
        self.assertFalse(any("干涉" in i.text for i in validate(job, LIB)))

    def test_numbering_follows_entry_order(self):
        job = Job(
            cabinet=Cabinet(template="vertical_double"),
            cutouts=[
                Cutout("pushbutton_22", 200, 300, "SB1"),
                Cutout("pushbutton_22", 200, 1200, "SB2"),
            ],
        )
        fig, meta = build_drawing(job, LIB)
        self.assertEqual([(n, rc.label) for n, rc in meta["cutouts"]], [(1, "SB1"), (2, "SB2")])
        fig.clear()

    def test_side_cutout_out_of_range(self):
        job = load_demo()
        job.cutouts = [
            Cutout("custom_slot", job.cabinet.D + 100, 500, "SL1", face="left")
        ]
        self.assertTrue(any("左侧" in i.text and "超出" in i.text for i in validate(job, LIB)))


class TestConsoleCabinet(unittest.TestCase):
    def _cabinet(self) -> Cabinet:
        return Cabinet(
            template="console", W=1200, H=1100, D=750, base_height=100,
            console_desk_h=720, console_top_depth=250,
            plate_margin=30, plate_bottom_gap=50,
        )

    def _codes(self, job: Job) -> list[str]:
        return [i.text for i in validate(job, LIB)]

    def test_console_geometry_and_build(self):
        cab = self._cabinet()
        self.assertTrue(cab.double_door)
        self.assertAlmostEqual(cab.console_run, 500)
        self.assertAlmostEqual(cab.console_rise, 380)
        self.assertAlmostEqual(cab.console_panel_len, 628.01, places=1)
        self.assertAlmostEqual(cab.front_dev_h, 1348.01, places=1)
        self.assertAlmostEqual(cab.plate_h, 540)
        self.assertLessEqual(cab.door_upper, cab.console_desk_h)

        job = Job(
            cabinet=cab,
            cutouts=[
                Cutout("pushbutton_22", 300, 500, "SB1"),
                Cutout("hmi_7", 600, 920, "HMI1"),
                Cutout("fan_filter_120", 600, 650, "FN1", face="top"),
                Cutout("indicator_22", 300, 400, "HL1", face="left"),
            ],
            rails=[Rail(y=200, devices=[DeviceItem("2P", 6)])],
        )
        fig, meta = build_drawing(job, LIB)
        self.assertEqual([i for i in meta["issues"] if i.level == "error"], [])
        self.assertEqual(meta["page"], "A2")
        # 正视图的放置框高度按斜面展开计算，比总高更高
        front = meta["placements"]["front"]
        self.assertGreater(front.h, cab.H * meta["layout"].s)
        fig.clear()

    def test_slope_cutout_is_valid_but_fold_crossing_is_not(self):
        cab = self._cabinet()
        ok = Job(cabinet=cab, cutouts=[Cutout("hmi_7", 600, 900, "HMI1")])
        self.assertFalse(any("折弯线" in t for t in self._codes(ok)))
        bad = Job(
            cabinet=cab,
            cutouts=[Cutout("hmi_7", 600, cab.console_desk_h, "HMI1")],
        )
        self.assertTrue(any("折弯线" in t for t in self._codes(bad)))

    def test_console_top_hole_must_be_on_rear_flat(self):
        cab = self._cabinet()
        bad = Job(
            cabinet=cab,
            cutouts=[Cutout("fan_filter_120", 600, 400, "FN1", face="top")],
        )
        self.assertTrue(any("后部平顶" in t for t in self._codes(bad)))
        good = Job(
            cabinet=cab,
            cutouts=[Cutout("fan_filter_120", 600, 650, "FN1", face="top")],
        )
        self.assertFalse(any("后部平顶" in t for t in self._codes(good)))

    def test_console_side_hole_must_be_under_profile(self):
        cab = self._cabinet()
        bad = Job(
            cabinet=cab,
            cutouts=[Cutout("pushbutton_22", 100, 1000, "SB1", face="left")],
        )
        self.assertTrue(any("侧板轮廓" in t for t in self._codes(bad)))

    def test_console_demo_example_builds_cleanly(self):
        with open(CONSOLE_DEMO, "r", encoding="utf-8") as f:
            job = Job.from_dict(json.load(f))
        self.assertTrue(job.cabinet.is_console)
        self.assertEqual(len(job.cutouts), 17)
        self.assertEqual(len(job.rails), 2)
        fig, meta = build_drawing(job, LIB)
        self.assertEqual(meta["page"], "A2")
        self.assertEqual(meta["scale"], "1:15")
        self.assertEqual([i for i in meta["issues"] if i.level == "error"], [])
        fig.clear()


class TestDeviceMetadata(unittest.TestCase):
    def test_bom_and_rail_table_keep_brand_and_custom_spec(self):
        job = Job(
            cabinet=Cabinet(template="vertical_double"),
            rails=[
                Rail(y=200, devices=[
                    DeviceItem(
                        "2P", 6, width=36, height=85, prefix="QF",
                        brand="正泰", name="微型断路器 2P", spec="NXB-63 2P",
                    ),
                    DeviceItem(
                        "NXB-63 3P", 2, width=54, height=85, prefix="QF",
                        brand="正泰", name="微型断路器 3P", spec="NXB-63 3P",
                    ),
                ])
            ],
        )
        bom = bom_rows(job, LIB)
        self.assertTrue(any("正泰 NXB-63 2P" in row[2] for row in bom))
        self.assertTrue(any("正泰 NXB-63 3P" in row[2] for row in bom))
        rail = rail_rows(job, LIB)
        self.assertTrue(any(row[2] == "正泰 NXB-63 2P" for row in rail))
        self.assertTrue(any(row[4] in ("QF1~QF6", "QF7~QF8") for row in rail))


class TestLayoutAndExport(unittest.TestCase):
    def test_explicit_small_page_warns(self):
        job = Job(
            cabinet=Cabinet(template="vertical_double", W=1200, H=1600, D=400),
            sheet=dataclasses.replace(load_demo().sheet, size="A3", scale="1:20"),
        )
        layout, issues = choose_layout(job)
        self.assertEqual(layout.page, "A3")
        self.assertAlmostEqual(layout.den, 20.0)
        self.assertTrue(any("放不下" in i.text for i in issues))

    def test_pdf_svg_export(self):
        job = load_demo()
        fig, meta = build_drawing(job, LIB)
        with tempfile.TemporaryDirectory() as td:
            pdf = Path(td) / "a.pdf"
            svg = Path(td) / "a.svg"
            save_figure(fig, pdf, title="test")
            save_figure(fig, svg, title="test")
            self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
            text = svg.read_text(encoding="utf-8")
            self.assertIn("<svg", text)
            self.assertGreater(len(text), 10000)
        fig.clear()

    def test_dxf_export(self):
        job = load_demo()
        fig, meta = build_drawing(job, LIB)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.dxf"
            save_figure(fig, path)
            raw = path.read_bytes()
            self.assertGreater(len(raw), 50000)
            self.assertIn(b"AC1009", raw)
            self.assertIn(b"ANSI_936", raw)
            for entity in (b"LINE", b"TEXT", b"CIRCLE", b"POLYLINE", b"SOLID"):
                self.assertGreater(raw.count(("\r\n" + entity.decode() + "\r\n").encode()), 0, entity)
            text = raw.decode("gbk")
            self.assertIn("正视图", text)
            self.assertIn("开孔表", text)
        fig.clear()

    def test_csv_export(self):
        job = load_demo()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.csv"
            save_tables_csv(job, LIB, path)
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            text = raw.decode("utf-8-sig")
            self.assertIn("开孔表", text)
            self.assertIn("柜内元件材料表", text)
            self.assertIn("导轨元件表", text)
            self.assertIn("Φ22.5", text)

    def test_data_dir_can_be_overridden(self):
        """EXE 同目录 / DKK_DATA_DIR 的 data 优先于内置库。"""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "components.json").write_text("{}", encoding="utf-8")
            old = os.environ.get("DKK_DATA_DIR")
            os.environ["DKK_DATA_DIR"] = td
            try:
                self.assertEqual(data_dir(), Path(td))
            finally:
                if old is None:
                    os.environ.pop("DKK_DATA_DIR", None)
                else:
                    os.environ["DKK_DATA_DIR"] = old


class TestRandomJobs(unittest.TestCase):
    def test_random_jobs_do_not_crash(self):
        """随机柜型/开孔/导轨/线槽组合，确保排版与校验不会抛异常。"""
        rng = random.Random(20261111)
        templates = ["vertical_single", "vertical_double", "wallbox"]
        components = ["pushbutton_22", "indicator_22", "selector_22", "ammeter_96",
                      "hmi_7", "hmi_10", "fan_filter_120"]
        for _ in range(12):
            tpl = rng.choice(templates)
            W = rng.choice([600, 800, 1200, 1700, 2200])
            H = rng.choice([600, 1000, 1600, 1900, 2200])
            D = rng.choice([250, 400, 460, 600])
            cab = Cabinet(
                template=tpl, W=W, H=H, D=D,
                base_height=0 if tpl == "wallbox" else rng.choice([0, 100, 150]),
            )
            cuts = [
                Cutout(
                    component=rng.choice(components),
                    x=rng.uniform(50, max(60, W - 50)),
                    y=rng.uniform(50, max(60, H - 50)),
                    face="front",
                )
                for _ in range(rng.randint(0, 6))
            ]
            rails = [
                Rail(y=rng.uniform(80, max(100, cab.plate_h - 80)),
                     devices=[DeviceItem("2P", rng.randint(1, 8))])
                for _ in range(rng.randint(0, 3))
            ]
            ducts = [
                Duct(x=rng.uniform(0, max(10, cab.plate_w - 100)),
                     y=rng.uniform(0, max(10, cab.plate_h - 100)),
                     length=rng.uniform(100, max(120, cab.plate_w)),
                     width=rng.choice([40, 60, 80]),
                     orient=rng.choice(["h", "v"]))
                for _ in range(rng.randint(0, 3))
            ]
            job = Job(cabinet=cab, cutouts=cuts, rails=rails, ducts=ducts)
            fig, meta = build_drawing(job, LIB)
            self.assertIn(meta["page"], ("A3", "A2", "A1"))
            fig.clear()


if __name__ == "__main__":
    unittest.main(verbosity=2)
