# -*- coding: utf-8 -*-
"""标定三个基准面的 草图坐标 -> 模型坐标 映射。"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import win32com.client as wc


def make(sw, plane_index):
    doc = sw.NewDocument(sw.GetUserPreferenceStringValue(8), 0, 0, 0)
    f = doc.FirstFeature
    k = 0
    while f:
        if f.GetTypeName2 == "RefPlane":
            if k == plane_index:
                break
            k += 1
        f = f.GetNextFeature
    name = f.Name
    f.Select2(False, 0)
    sm = doc.SketchManager
    sm.InsertSketch(True)
    # 原始草图坐标：x 0..20mm, y 0..10mm
    sm.CreateCornerRectangle(0.0, 0.0, 0, 0.020, 0.010, 0)
    sm.InsertSketch(True)
    skf = None
    g = doc.FirstFeature
    while g:
        if g.GetTypeName2 == "ProfileFeature":
            skf = g
        g = g.GetNextFeature
    skf = doc.FeatureByName(skf.Name)
    skf.Select2(False, 0)
    feat = doc.FeatureManager.FeatureExtrusion2(
        True, False, False, 0, 0, 0.005, 0.0,
        False, False, False, False, 0.0, 0.0,
        False, False, False, False, True, True, True, 0, 0.0, False)
    if not feat:
        return name, None
    body = doc.GetBodies2(0, True)[0]
    box = [round(float(v) * 1000, 2) for v in body.GetBodyBox()]
    return name, box


def main():
    sw = wc.Dispatch("SldWorks.Application")
    for idx, label in ((0, "front"), (1, "top"), (2, "right")):
        name, box = make(sw, idx)
        print("%-6s plane=%-8s bbox_mm=%s" % (label, name, box))
    print("\n判读：bbox = [xmin,ymin,zmin,xmax,ymax,zmax]")


main()
