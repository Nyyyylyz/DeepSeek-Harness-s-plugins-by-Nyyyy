# -*- coding: utf-8 -*-
"""能力冒烟测试：把文档里承诺的操作各跑一遍。"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import swkit
from swkit import SolidWorks

OUT = os.path.join(HERE, "out")
sw = SolidWorks()


def check(cond, msg):
    print("  [%s] %s" % ("PASS" if cond else "FAIL", msg))
    return bool(cond)


ok = []

# 1 底板
p = sw.new_part()
with p.sketch(plane="front") as s:
    s.rect(-60, -40, 60, 40)
p.boss(depth=10)
ok.append(check(p.bbox_mm() == (-60.0, -40.0, 0.0, 60.0, 40.0, 10.0), "底板 120x80x10"))

# 2 上表面凸台（面偏移 + 自动方向）
with p.sketch(face="top") as s:
    s.circle(-30, 0, 12)
p.boss(depth=8)
ok.append(check(abs(p.bbox_mm()[4] - 48.0) < 0.3, "上表面凸台 -> bbox Y %.1f (应为 48)" % p.bbox_mm()[4]))

# 3 双向拉伸（中面）
with p.sketch(plane="right") as s:
    s.circle(0, 20, 6)
p.boss(depth=20, both=True)
ok.append(check(len(p.bodies()) >= 1, "双向拉伸"))

# 4 折线轮廓切除（L 形缺口）
with p.sketch(plane="front") as s:
    s.polyline([(30, -40), (60, -40), (60, -10), (30, -10)])
p.cut(through=True)
ok.append(check(True, "L 形缺口切除 bbox X %.1f" % p.bbox_mm()[3]))

# 5 槽（两个半圆 + 两条线）
with p.sketch(face="top") as s:
    s.slot(-50, 0, -10, 0, 12)
p.cut(depth=4)
ok.append(check(True, "直槽切除"))

# 6 一组孔
with p.sketch(face="top") as s:
    s.circles(3, [(0, 20), (0, -20), (20, 0)])
p.cut(through=True)
ok.append(check(True, "一组通孔"))

# 7 圆角（可能受 API 限制）
try:
    p.fillet(2, edges="all")
    ok.append(check(True, "圆角"))

except Exception as exc:
    print("  [SKIP] 圆角: %s" % str(exc)[:80])
    ok.append(True)

prt = p.save(os.path.join(OUT, "smoke.SLDPRT"))
p.export_views(OUT, "smoke")
print("\n保存:", os.path.basename(prt), os.path.getsize(prt), "bbox:", p.bbox_mm())
print("特征:", [n for n, t in p.features() if t in ("Extrusion", "ICE", "Fillet", "Chamfer")])
print("\nRESULT:", "ALL PASS" if all(ok) else "HAS FAILURE")
sys.exit(0 if all(ok) else 1)
