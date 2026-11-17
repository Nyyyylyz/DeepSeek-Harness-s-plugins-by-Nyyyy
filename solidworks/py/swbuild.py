# -*- coding: utf-8 -*-
"""
swbuild —— 用 JSON 规格驱动 SolidWorks 建模的命令行工具

    python swbuild.py build spec.json -o out
    python swbuild.py shot out/part.SLDPRT --views iso,front
    python swbuild.py drawing out/part.SLDPRT -o out/drawing.png

规格（单位一律 mm，坐标系：X 右、Y 上、Z 朝向观察者）
------------------------------------------------------
{
  "name": "bracket",
  "steps": [
    {"op": "boss", "plane": "front", "rect": [-50,-30,50,30], "depth": 12},
    {"op": "cut",  "face": "top",   "rect": [-40,2,-20,10],  "depth": 5},
    {"op": "cut",  "face": "top",   "circle": [30,6,4],      "through": true},
    {"op": "boss", "face": "top",   "circle": [0,0,10],      "depth": 8}
  ]
}

形状字段（四选一）
    "rect":   [u1, v1, u2, v2]          矩形
    "circle": [u, v, r]                 圆
    "circles":{"r": r, "at": [[u,v]...]} 一组等半径圆（多孔一次切除）
    "polyline": [[u,v], ...]            闭合折线
    "slot":   [u1,v1,u2,v2,width]       直槽

平面字段
    "plane": "front" | "top" | "right"   基准面（主/俯/侧视图对应）
    "face":  "top" | "bottom" | "front" | "back" | "right" | "left"
            实体极值面 → 自动建偏移基准面，坐标仍按该视图的 2D 系给出

其余
    "depth": 拉伸/切除深度；"through": true 为通切
    "both": true 双向对称；“reverse”: true 反向
    "draft": 拔模角(度)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import swkit
from swkit import SolidWorks


def draw_shape(sk: "swkit.Sketch", step):
    if "rect" in step:
        sk.rect(*step["rect"])
    elif "circle" in step:
        sk.circle(*step["circle"])
    elif "circles" in step:
        spec = step["circles"]
        sk.circles(spec["r"], spec["at"])
    elif "polyline" in step:
        sk.polyline(step["polyline"], close=step.get("close", True))
    elif "slot" in step:
        sk.slot(*step["slot"])
    elif "polygon" in step:
        sk.polygon(*step["polygon"])
    else:
        raise swkit.SwError("步骤缺形状: %r" % (step,))


def build(part, steps):
    report = []
    for i, step in enumerate(steps, 1):
        op = step.get("op", "boss")
        try:
            if op in ("boss", "cut"):
                with part.sketch(plane=step.get("plane"), face=step.get("face"),
                                 offset=step.get("offset")) as sk:
                    draw_shape(sk, step)
                if op == "boss":
                    feat = part.boss(depth=step["depth"],
                                     both=step.get("both", False),
                                     reverse=step.get("reverse"),
                                     draft=step.get("draft", 0.0))
                else:
                    feat = part.cut(depth=step.get("depth"),
                                    through=step.get("through", False),
                                    both=step.get("both", False),
                                    reverse=step.get("reverse"))
            elif op == "fillet":
                feat = part.fillet(step["radius"], edges=step.get("edges", "all"))
            elif op == "chamfer":
                feat = part.chamfer(step.get("size", 1.0), step.get("angle", 45.0),
                                    edges=step.get("edges", "all"))
            else:
                raise swkit.SwError("未知操作 %r" % op)
            report.append({"step": i, "op": op, "feature": feat.Name, "ok": True})
        except Exception as exc:
            report.append({"step": i, "op": op, "ok": False, "error": str(exc)[:200]})
            if not step.get("optional"):
                raise
    return report


def cmd_build(args):
    spec = json.load(open(args.spec, encoding="utf-8"))
    name = spec.get("name") or os.path.splitext(os.path.basename(args.spec))[0]
    outdir = os.path.abspath(args.out or "out")
    os.makedirs(outdir, exist_ok=True)

    sw = SolidWorks(visible=not args.headless)
    part = sw.new_part()
    steps = spec.get("steps", [])
    try:
        report = build(part, steps)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    prt = part.save(os.path.join(outdir, name + ".SLDPRT"))
    views = part.export_views(outdir, name)
    summary = {
        "ok": True,
        "name": name,
        "part": prt,
        "bbox_mm": part.bbox_mm(),
        "features": [{"name": n, "type": t} for n, t in part.features()
                     if t not in ("FavoriteFolder", "HistoryFolder", "SelectionSetFolder",
                                  "SensorFolder", "DocsFolder", "DetailCabinet",
                                  "InkMarkupFolder", "EnvFolder", "SolidBodyFolder",
                                  "SurfaceBodyFolder", "CommentsFolder", "EqnFolder",
                                  "MaterialFolder", "ConfigTableFolder")],
        "views": {k: v for k, v in views.items()},
        "steps": report,
    }
    if args.drawing:
        try:
            drw = sw.new_drawing()
            drw.create_first_angle_views(prt)
            dpath = drw.save(os.path.join(outdir, name + ".SLDDRW"))
            dpg = drw.screenshot(os.path.join(outdir, name + "_drawing.png"))
            summary["drawing"] = dpath
            summary["drawing_png"] = dpg
        except Exception as exc:
            summary["drawing_error"] = str(exc)[:200]

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_shot(args):
    sw = SolidWorks()
    part = sw.open(args.part)
    views = [v.strip() for v in args.views.split(",") if v.strip()]
    out = {}
    for v in views:
        out[v] = part.screenshot(
            os.path.join(args.out or os.path.dirname(os.path.abspath(args.part)),
                         "%s_%s.png" % (os.path.splitext(os.path.basename(args.part))[0], v)),
            view=v)
    print(json.dumps({"ok": True, "views": out}, ensure_ascii=False, indent=2))
    return 0


def cmd_drawing(args):
    sw = SolidWorks()
    part = sw.open(args.part)
    drw = sw.new_drawing()
    drw.create_first_angle_views(os.path.abspath(args.part))
    png = args.out or (os.path.splitext(args.part)[0] + "_drawing.png")
    drw.screenshot(png)
    print(json.dumps({"ok": True, "drawing_png": os.path.abspath(png)}, ensure_ascii=False))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="SolidWorks 三视图建模工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="按 JSON 规格建模")
    b.add_argument("spec")
    b.add_argument("-o", "--out", default="out")
    b.add_argument("--drawing", action="store_true", help="同时生成第一角三视图工程图")
    b.add_argument("--headless", action="store_true", help="不显示 SolidWorks 窗口")
    b.set_defaults(func=cmd_build)

    s = sub.add_parser("shot", help="导出指定视图截图")
    s.add_argument("part")
    s.add_argument("--views", default="front,top,right,iso")
    s.add_argument("-o", "--out")
    s.set_defaults(func=cmd_shot)

    d = sub.add_parser("drawing", help="由零件生成三视图工程图 PNG")
    d.add_argument("part")
    d.add_argument("-o", "--out")
    d.set_defaults(func=cmd_drawing)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
