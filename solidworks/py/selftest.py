# -*- coding: utf-8 -*-
"""swkit 自检：非对称件验证 特征/坐标/截图/三视图工程图 全链路。

零件：100(X) x 60(Y) x 12(Z) 底板，带
   * 上表面(Y=30) 矩形槽 X:-40..-20  Z:2..10  深 5
   * 上表面 Ø8 通孔 @ X=30 Z=6
   * 前表面(Z=12) 矩形槽 X:-10..10  Y:-20..-10 深 4
"""
import os
import sys
import traceback

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import swkit
from swkit import SolidWorks, MM

OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)


def section(t):
    print("\n" + "=" * 72)
    print("## " + t)
    print("=" * 72, flush=True)


def planar_faces_at(part, axis_idx, level_m, tol=3e-4):
    hits = []
    for face in part.faces():
        try:
            n = list(swkit._v(face, "Normal"))
            if len(n) != 3 or abs(abs(n[axis_idx]) - 1.0) > 1e-3:
                continue
            b = list(swkit._v(face, "GetBox"))
            if len(b) != 6:
                continue
            lv = b[3 + axis_idx] if n[axis_idx] > 0 else b[axis_idx]
            if abs(lv - level_m) > tol:
                continue
            center = [round((b[i] + b[i + 3]) / 2 / MM, 2) for i in range(3)]
            hits.append(center)
        except Exception:
            continue
    return hits


def count_cylinders(part):
    n = 0
    for face in part.faces():
        try:
            surf = swkit._v(face, "GetSurface")
            if surf is not None and swkit._v(surf, "IsCylinder"):
                n += 1
        except Exception:
            pass
    return n


def main():
    sw = SolidWorks()
    print("SW revision:", sw.revision)
    checks = []

    section("底板 100 x 60 x 12")
    part = sw.new_part()
    with part.sketch(plane="front") as s:
        s.rect(-50, -30, 50, 30)
    part.boss(depth=12)
    box = part.bbox_mm()
    print("bbox(mm):", box)
    checks.append(("底板 100x60x12", box == (-50.0, -30.0, 0.0, 50.0, 30.0, 12.0)))

    section("上表面矩形槽 X:-40..-20 Z:2..10 深 5")
    with part.sketch(face="top") as s:
        s.rect(-40, 2, -20, 10)
    part.cut(depth=5)
    hits = planar_faces_at(part, 1, 25 * MM)
    print("Y=25 面片:", hits)
    ok = any(abs(h[0] + 30) < 0.8 and abs(h[2] - 6) < 0.8 for h in hits)
    print("槽底中心应为 (-30, 25, 6):", "PASS" if ok else "FAIL")
    checks.append(("上表面槽位置", ok))

    section("上表面通孔 Ø8 @ X=30 Z=6")
    n0 = count_cylinders(part)
    with part.sketch(face="top") as s:
        s.circle(30, 6, 4)
    part.cut(through=True)
    n1 = count_cylinders(part)
    print("圆柱面数: %d -> %d" % (n0, n1))
    checks.append(("通孔生成", n1 > n0))

    section("前表面矩形槽 X:-10..10 Y:-20..-10 深 4")
    with part.sketch(face="front") as s:
        s.rect(-10, -20, 10, -10)
    part.cut(depth=4)
    hits2 = planar_faces_at(part, 2, 8 * MM)
    print("Z=8 面片:", hits2)
    ok2 = any(abs(h[0]) < 0.8 and abs(h[1] + 15) < 0.8 for h in hits2)
    print("槽底中心应为 (0, -15, 8):", "PASS" if ok2 else "FAIL")
    checks.append(("前表面槽位置", ok2))

    section("圆角 R3（可选）")
    try:
        part.fillet(3, edges="all")
        print("圆角: OK")
    except Exception as exc:
        print("圆角: 跳过 (%s)" % str(exc)[:100])
    print("特征树:")
    part.dump_tree()

    section("保存 + 三视图截图")
    prt = part.save(os.path.join(OUT, "selftest.SLDPRT"))
    print("part:", prt, os.path.getsize(prt))
    shots = part.export_views(OUT, "selftest")
    for k, v in shots.items():
        ok3 = os.path.exists(v) and os.path.getsize(v) > 5000
        print("  %-6s %-26s %7d bytes %s" % (k, os.path.basename(v),
                                             os.path.getsize(v) if os.path.exists(v) else 0,
                                             "OK" if ok3 else "!!"))
        checks.append(("截图 %s" % k, ok3))

    section("三视图工程图（第一角投影）")
    try:
        drw = sw.new_drawing()
        drw.create_first_angle_views(os.path.abspath(prt))
        dpath = drw.save(os.path.join(OUT, "selftest.SLDDRW"))
        png = drw.screenshot(os.path.join(OUT, "selftest_drawing.png"))
        print("drawing:", os.path.basename(dpath), os.path.getsize(dpath))
        print("png:", os.path.basename(png), os.path.getsize(png))
        checks.append(("三视图工程图", os.path.getsize(png) > 5000))
    except Exception:
        traceback.print_exc()
        checks.append(("三视图工程图", False))

    section("结果")
    for name, ok in checks:
        print("  %-24s %s" % (name, "PASS" if ok else "FAIL"))
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
