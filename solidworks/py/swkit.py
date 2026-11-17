# -*- coding: utf-8 -*-
"""
swkit —— SolidWorks 自动化建模内核（三视图 -> 三维模型）
SolidWorks 2026 SP4 / Python 3.12 / pywin32 实测通过

坐标与单位
----------
* 对外一律毫米；COM 内部米。
* 模型空间右手系：X 向右、Y 向上、Z 朝向观察者（前视基准面法向）。
* 三视图 -> 模型 的对应：
      主视图(前视)  u=X  v=Y   normal=+Z
      俯视图(上视)  u=X  v=Z   normal=+Y
      左/右视图(右视) u=Z v=Y   normal=+X
* 草图坐标映射经实测标定（不是猜的）：
      front: (u,v) -> sketch( u,  v)
      top  : (u,v) -> sketch( u, -v)
      right: (u,v) -> sketch(-u,  v)
  正在标定脚本 calibrate.py 里，三个基准面各画 20x10 矩形拉伸 5mm，
  由包围盒反推得到上述系数。

实现要点
--------
* SelectByID2 在 SW2026+pywin32 下会报"类型不匹配"，全部改用 Feature.Select2。
* SolidWorks 的 IDispatch 没有类型库，零参方法在 getattr 时即被执行，
  统一用 _v() 兼容。
* 表面特征（挖槽/凸台）不用"在实体面上开草图"（面草图坐标系不可控），
  而是插入一个**偏移基准面**，坐标系与父基准面一致，可精确下料。
"""
from __future__ import annotations

import math
import os

import win32com.client as win32
from win32com.client import CDispatch

MM = 0.001

# --- swEndConditions_e（实测常量表 swconst.tlb） ----------------------------
END_BLIND = 0
END_THROUGH_ALL = 1
END_THROUGH_NEXT = 2
END_UP_TO_VERTEX = 3
END_UP_TO_SURFACE = 4
END_OFFSET_FROM_SURFACE = 5
END_MID_PLANE = 6
END_UP_TO_BODY = 7

# --- swStartConditions_e -----------------------------------------------------
START_SKETCH_PLANE = 0
START_SURFACE = 1
START_VERTEX = 2
START_OFFSET = 3

# --- swRefPlaneReferenceConstraint_* ----------------------------------------
REF_DISTANCE = 8
REF_PARALLEL = 1
REF_PERPENDICULAR = 2
REF_COINCIDENT = 4

# --- swChamferType_e --------------------------------------------------------
CHAMFER_ANGLE_DISTANCE = 1
CHAMFER_DISTANCE_DISTANCE = 2
CHAMFER_EQUAL_DISTANCE = 16

VIEW_NAMES = {
    "front": ["*前视", "*Front", "*前视图", "*主视图"],
    "back": ["*后视", "*Back", "*后视图"],
    "top": ["*上视", "*Top", "*俯视图"],
    "bottom": ["*下视", "*Bottom"],
    "left": ["*左视", "*Left"],
    "right": ["*右视", "*Right", "*右视图"],
    "iso": ["*等轴测", "*Isometric", "*等角"],
}

AXIS_INDEX = {"x": 0, "y": 1, "z": 2}
DEFAULT_TEMPLATE_PREF = {"part": 8, "assembly": 9, "drawing": 10}

# 基准面 -> (原点, u 轴, v 轴, 法向)，模型空间；原点都在原点
PLANE_FRAMES = {
    "front": ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    "top":   ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
    "right": ((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (1.0, 0.0, 0.0)),
}

# 草图坐标实测映射：(u,v) -> (sx, sy) 的符号
SKETCH_SIGN = {
    "front": (1.0, 1.0),
    "top": (1.0, -1.0),
    "right": (-1.0, 1.0),
}


class SwError(RuntimeError):
    pass


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# COM 兼容层
# ---------------------------------------------------------------------------
def _v(obj, name):
    """读成员；pywin32 对零参方法在 getattr 时就调用，这里两种都兼容。"""
    val = getattr(obj, name)
    if isinstance(val, CDispatch):
        return val
    if callable(val):
        try:
            return val()
        except TypeError:
            return val
    return val


def _arr(v):
    if v is None:
        return []
    try:
        return list(v)
    except TypeError:
        return []


def _try_call(obj, name, candidates, label=""):
    """按候选参数表依次试调用（不同 SW 版本签名长度不同）。"""
    errors = []
    for args in candidates:
        try:
            return getattr(obj, name)(*args)
        except Exception as exc:
            errors.append("n=%d %s" % (len(args), str(exc).strip()[:110]))
    raise SwError("%s%s 调用失败: %s" % (label, name, " | ".join(errors)))


class SolidWorks:
    def __init__(self, visible=True):
        self.app = win32.Dispatch("SldWorks.Application")
        try:
            self.app.Visible = bool(visible)
        except Exception:
            pass
        self._view_ok = {}

    @property
    def revision(self):
        return self.app.RevisionNumber

    def template(self, kind="part"):
        path = self.app.GetUserPreferenceStringValue(DEFAULT_TEMPLATE_PREF[kind])
        if not path or not os.path.exists(path):
            raise SwError("找不到 %s 模板: %r" % (kind, path))
        return path

    def new_part(self, name=None):
        doc = self.app.NewDocument(self.template("part"), 0, 0, 0)
        if not doc:
            raise SwError("新建零件失败")
        part = Part(self, doc)
        if name:
            try:
                doc.SetTitle(name, "")
            except Exception:
                pass
        return part

    def new_drawing(self):
        doc = self.app.NewDocument(self.template("drawing"), 0, 0, 0)
        if not doc:
            raise SwError("新建工程图失败")
        return Drawing(self, doc)

    def open(self, path):
        path = os.path.abspath(path)
        # 文件已经开在 SolidWorks 里就直接复用，避免"打开失败"
        try:
            for opened in _arr(self.app.GetDocuments()):
                if opened is None:
                    continue
                try:
                    if os.path.abspath(_v(opened, "GetPathName") or "") == path:
                        return Part(self, opened)
                except Exception:
                    continue
        except Exception:
            pass
        doc = None
        for args in ((path, 1), (path, 1, 1, "", 0, 0), (path, 3, 1, "", 0, 0)):
            try:
                doc = self.app.OpenDoc(*args)
            except Exception:
                doc = None
            if doc:
                break
        if not doc:
            raise SwError("打开 %s 失败" % path)
        return Part(self, doc)

    def quit(self):
        try:
            self.app.ExitApp()
        except Exception:
            pass


class Part:
    def __init__(self, sw: SolidWorks, doc):
        self.sw = sw
        self.doc = doc
        self._planes = self._detect_planes()
        self.steps = []

    # ---------------------------------------------------------------- 基础
    @property
    def ext(self):
        return self.doc.Extension

    @property
    def fm(self):
        return self.doc.FeatureManager

    @property
    def sm(self):
        return self.doc.SketchManager

    def title(self):
        return _v(self.doc, "GetTitle")

    def rebuild(self):
        try:
            _v(self.doc, "EditRebuild3")
        except Exception:
            pass

    def _iter_features(self):
        feat = _v(self.doc, "FirstFeature")
        guard = 0
        while feat is not None and guard < 800:
            guard += 1
            yield feat
            try:
                feat = _v(feat, "GetNextFeature")
            except Exception:
                return

    def features(self):
        out = []
        for feat in self._iter_features():
            try:
                out.append((feat.Name, _v(feat, "GetTypeName2")))
            except Exception:
                out.append(("?", "?"))
        return out

    def dump_tree(self):
        for name, typ in self.features():
            log("   %-26s | %s" % (name, typ))

    def _detect_planes(self):
        feats = [f for f in self._iter_features()
                 if _v(f, "GetTypeName2") == "RefPlane"]
        if len(feats) < 3:
            raise SwError("基准面识别失败（找到 %d 个）" % len(feats))
        self._plane_feats = {k: feats[i] for i, k in enumerate(("front", "top", "right"))}
        return {k: v.Name for k, v in self._plane_feats.items()}

    # ---------------------------------------------------------------- 选择
    def clear(self):
        self.doc.ClearSelection2(True)

    def select_feature(self, name):
        self.clear()
        for feat in self._iter_features():
            if feat.Name == name:
                return bool(feat.Select2(False, 0))
        return False

    # ---------------------------------------------------------------- 实体
    def bodies(self):
        out = []
        for args in ((0, True), (0,)):
            try:
                out = [b for b in _arr(self.doc.GetBodies2(*args)) if b is not None]
                if out:
                    return out
            except Exception:
                continue
        return out

    def edges(self):
        out = []
        for body in self.bodies():
            try:
                out.extend([e for e in _arr(_v(body, "GetEdges")) if e is not None])
            except Exception:
                continue
        return out

    def faces(self):
        out = []
        for body in self.bodies():
            try:
                out.extend([f for f in _arr(_v(body, "GetFaces")) if f is not None])
            except Exception:
                continue
        return out

    def bbox(self):
        for body in self.bodies():
            try:
                box = _arr(_v(body, "GetBodyBox"))
                if len(box) == 6:
                    return tuple(float(v) for v in box)
            except Exception:
                continue
        raise SwError("取不到包围盒（模型为空？）")

    def bbox_mm(self):
        return tuple(round(v / MM, 3) for v in self.bbox())

    # ---------------------------------------------------------------- 基准面
    def _offset_plane(self, base_key, offset_mm):
        """基于基准面做一个偏移基准面，返回 IFeature。"""
        base = self._plane_feats[base_key]
        self.clear()
        if not base.Select2(False, 0):
            raise SwError("偏移面：选中 %s 失败" % base_key)
        d = offset_mm * MM
        feat = None
        for args in ((REF_DISTANCE, d, 0, 0, 0, 0),
                     (REF_DISTANCE, d, -1, 0, -1, 0)):
            try:
                feat = self.fm.InsertRefPlane(*args)
                if feat:
                    break
            except Exception:
                continue
        if not feat:
            raise SwError("创建偏移基准面失败 (%s by %s mm)" % (base_key, offset_mm))
        return feat

    def _level_of(self, spec):
        """面 -> (基准面 key, 偏移 mm, 内法向符号)"""
        box = self.bbox()
        # box = xmin,ymin,zmin,xmax,ymax,zmax
        table = {
            "top":    ("top",   box[4] / MM, -1),
            "bottom": ("top",   box[1] / MM, +1),
            "front":  ("front", box[5] / MM, -1),
            "back":   ("front", box[2] / MM, +1),
            "right":  ("right", box[3] / MM, -1),
            "left":   ("right", box[0] / MM, +1),
        }
        if spec not in table:
            raise SwError("未知面 %r（可用 top/bottom/front/back/right/left）" % (spec,))
        return table[spec]

    # ---------------------------------------------------------------- 草图
    def sketch(self, plane=None, face=None, offset=None):
        return SketchCtx(self, plane=plane, face=face, offset=offset)

    def _open_sketch(self, plane=None, face=None, offset=None):
        self.clear()
        inward = None
        if face is not None:
            key, level, inward = self._level_of(face)
            off = level
            feat = self._offset_plane(key, off)
            if not feat.Select2(False, 0):
                raise SwError("选中偏移基准面失败")
        else:
            key = plane or "front"
            if offset:
                feat = self._offset_plane(key, float(offset))
                if not feat.Select2(False, 0):
                    raise SwError("选中偏移基准面失败")
            else:
                if not self._plane_feats[key].Select2(False, 0):
                    raise SwError("选中基准面 %s 失败" % key)
        self.sm.InsertSketch(True)
        sk = self.sm.ActiveSketch
        if sk is None:
            raise SwError("进入草图失败")
        self._active_sketch_feat = None
        for feat in self._iter_features():
            if _v(feat, "GetTypeName2") == "ProfileFeature":
                self._active_sketch_feat = feat
        return Sketch(self, sk, key, inward)

    def _close_sketch(self):
        self.sm.InsertSketch(True)
        self.clear()
        feat = getattr(self, "_active_sketch_feat", None)
        if feat is None or not feat.Select2(False, 0):
            raise SwError("关闭草图后无法选中草图")
        self._last_sketch = feat
        return feat.Name

    # ---------------------------------------------------------------- 特征
    def boss(self, depth, both=False, reverse=None, draft=0.0, merge=True):
        d1 = abs(depth) * MM
        d2 = d1 if both else 0.0
        draft_r = math.radians(draft)

        def cands(rev):
            a23 = [not both, False, rev, END_BLIND, END_BLIND, d1, d2,
                   False, False, False, False, draft_r, draft_r,
                   False, False, False, False, bool(merge), True, True, 0, 0.0, False]
            a16 = [not both, False, rev, END_BLIND, END_BLIND, d1, d2,
                   False, False, False, False, draft_r, draft_r, False, False, bool(merge)]
            return [a23, a16]

        feat = self._make("FeatureExtrusion2", cands, reverse, "拉伸 ")
        self.steps.append(("boss", depth, feat.Name))
        return feat

    def cut(self, depth=None, through=False, both=False, reverse=None):
        t1 = END_THROUGH_ALL if through else END_BLIND
        d1 = 0.0 if through else abs(depth or 0) * MM

        def cands(rev):
            # 第2参 Flip 是"反向切除(保留轮廓外侧)"，必须为 False
            # 第3参 Dir 才是切除方向
            a17 = [not both, False, rev, t1, END_BLIND, d1, 0.0,
                   False, False, False, False, 0.0, 0.0, False, False, 0, False]
            a27 = [not both, False, rev, t1, END_BLIND, d1, 0.0,
                   False, False, False, False, 0.0, 0.0,
                   False, False, False, False, False,
                   True, True, False, False, False, 0, 0.0, False, False]
            return [a27, a17]

        feat = self._make("FeatureCut4", cands, reverse, "切除 ")
        self.steps.append(("cut", depth, feat.Name))
        return feat

    def _make(self, api, cands, reverse, label):
        """调用特征 API：签名多版本 + 方向不确定时两个方向都试。"""
        revs = [bool(reverse)] if reverse is not None else [False, True]
        errors = []
        for rev in revs:
            for args in cands(rev):
                try:
                    feat = getattr(self.fm, api)(*args)
                except Exception as exc:
                    errors.append("rev=%s n=%d %s" % (rev, len(args), str(exc).strip()[:90]))
                    continue
                if feat:
                    self.rebuild()
                    return feat
                errors.append("rev=%s n=%d -> None" % (rev, len(args)))
        raise SwError("%s失败: %s" % (label, " | ".join(errors[:6])))

    def revolve(self, angle_deg, merge=True):
        rad = math.radians(abs(angle_deg))
        args20 = [True, True, False, False, False, False, 0, 0, rad, 0.0,
                  False, False, 0.0, 0.0, 0, 0.0, 0.0, bool(merge), True, True]
        args5 = [rad, False, 0.0, 0, 0]
        feat = _try_call(self.fm, "FeatureRevolve2", [args20, args5], "旋转 ")
        if not feat:
            raise SwError("旋转失败")
        self.rebuild()
        self.steps.append(("revolve", angle_deg, feat.Name))
        return feat

    # ---------------------------------------------------------------- 边
    def select_edges(self, which="all"):
        self.clear()
        picked = 0
        for edge in self.edges():
            if not self._edge_match(edge, which):
                continue
            try:
                if edge.Select2(True, 0):
                    picked += 1
            except Exception:
                continue
        return picked

    @staticmethod
    def _edge_match(edge, which):
        if which in ("all", None, True):
            return True
        if isinstance(which, str):
            which = {"axis": which}
        if not isinstance(which, dict):
            return True
        axis = which.get("axis")
        idx = AXIS_INDEX.get(axis) if axis else None
        if idx is None:
            return True
        try:
            curve = _v(edge, "GetCurve")
            if curve is None or not _v(curve, "IsLine"):
                return False
            p = _arr(curve.LineParams)
            if len(p) != 6:
                return False
            d = [p[3] - p[0], p[4] - p[1], p[5] - p[2]]
        except Exception:
            return False
        norm = math.sqrt(sum(x * x for x in d))
        if norm < 1e-9:
            return False
        return abs(d[idx]) / norm > 0.99

    def fillet(self, radius, edges="all"):
        n = self.select_edges(edges)
        if n == 0:
            raise SwError("圆角未选中任何边 (%s)" % (edges,))
        r = abs(radius) * MM
        args9 = [r, True, 0, 0, 0, 1, [r], False, False]
        args17 = [r, True, 0, 0, 0, 1, [r], False, False, 0, 0.0, 0.0, 0.0,
                  False, 0.0, False, False]
        feat = _try_call(self.fm, "FeatureFillet3", [args9, args17], "圆角 ")
        if not feat:
            raise SwError("圆角失败 (边数=%d)" % n)
        self.rebuild()
        self.steps.append(("fillet", radius, feat.Name))
        return feat

    def chamfer(self, dist, angle=45.0, edges="all", equal=True):
        n = self.select_edges(edges)
        if n == 0:
            raise SwError("倒角未选中任何边")
        d = abs(dist) * MM
        ctype = CHAMFER_EQUAL_DISTANCE if equal else CHAMFER_ANGLE_DISTANCE
        args8 = [ctype, d, math.radians(angle), False, 0.0, 0.0, 0.0, 0.0]
        try:
            feat = _try_call(self.fm, "FeatureChamferType", [args8], "倒角 ")
        except SwError:
            feat = _try_call(self.fm, "FeatureChamfer", [args8], "倒角 ")
        if not feat:
            raise SwError("倒角失败 (边数=%d)" % n)
        self.rebuild()
        self.steps.append(("chamfer", dist, feat.Name))
        return feat

    # ---------------------------------------------------------------- 显示
    def hide_ref_geometry(self):
        """隐藏基准面/原点，截图更干净。"""
        self.clear()
        for feat in self._iter_features():
            try:
                if _v(feat, "GetTypeName2") in ("RefPlane", "OriginProfileFeature"):
                    feat.Select2(True, 0)
            except Exception:
                continue
        try:
            self.doc.BlankRefGeom()
        except Exception:
            pass
        self.clear()

    def show_ref_geometry(self):
        self.clear()
        for feat in self._iter_features():
            try:
                if _v(feat, "GetTypeName2") in ("RefPlane", "OriginProfileFeature"):
                    feat.Select2(True, 0)
            except Exception:
                continue
        try:
            self.doc.UnblankRefGeom()
        except Exception:
            pass
        self.clear()

    # ---------------------------------------------------------------- 输出
    def save(self, path):
        self.rebuild()
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.exists(path):
            try:
                cur = _v(self.doc, "GetPathName")
            except Exception:
                cur = ""
            if cur and os.path.abspath(cur).lower() != path.lower():
                try:
                    self.sw.app.CloseDoc(path)      # 关掉上一轮遗留的同名文档
                except Exception:
                    pass
            try:
                os.remove(path)
            except OSError:
                base, ext = os.path.splitext(path)
                i = 2
                while os.path.exists(path):
                    path = "%s_v%d%s" % (base, i, ext)
                    i += 1
        self.doc.SaveAs(path)
        if not os.path.exists(path):
            raise SwError("保存失败: %s" % path)
        return path

    def set_view(self, view="iso"):
        candidates = VIEW_NAMES.get(view, [view])
        cached = self.sw._view_ok.get(view)
        ordered = ([cached] if cached else []) + [c for c in candidates if c != cached]
        for name in ordered:
            try:
                self.doc.ShowNamedView2(name, -1)
                self.sw._view_ok[view] = name
                return True
            except Exception:
                continue
        return False

    def screenshot(self, path, view="iso", width=1400, height=1000):
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.set_view(view)
        try:
            _v(self.doc, "ViewZoomtofit2")
        except Exception:
            pass
        self.doc.ClearSelection2(True)
        if os.path.exists(path):
            os.remove(path)
        bmp = os.path.splitext(path)[0] + ".bmp"
        if os.path.exists(bmp):
            os.remove(bmp)
        self.doc.SaveBMP(bmp, width, height)      # SaveBMP 只会写 BMP
        if not os.path.exists(bmp):
            raise SwError("截图失败（SaveBMP 无输出）")
        if path.lower().endswith(".png"):
            from PIL import Image
            Image.open(bmp).save(path)
            os.remove(bmp)
        else:
            os.replace(bmp, path)
        return path

    def export_views(self, outdir, prefix="view"):
        paths = {}
        self.hide_ref_geometry()
        try:
            for view in ("front", "top", "right", "iso"):
                try:
                    paths[view] = self.screenshot(
                        os.path.join(outdir, "%s_%s.png" % (prefix, view)), view)
                except Exception as exc:
                    log("  截图 %s 失败: %s" % (view, exc))
        finally:
            self.show_ref_geometry()
        return paths


class SketchCtx:
    def __init__(self, part: Part, plane=None, face=None, offset=None):
        self.part = part
        self.plane = plane
        self.face = face
        self.offset = offset
        self.sketch = None
        self.name = None
        self.inward = None

    def __enter__(self):
        self.sketch = self.part._open_sketch(plane=self.plane, face=self.face,
                                             offset=self.offset)
        return self.sketch

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            try:
                self.part.sm.InsertSketch(True)
            except Exception:
                pass
            return False
        self.name = self.part._close_sketch()
        self.part._last_inward = self.sketch.inward if self.sketch else None
        return False


class Sketch:
    """以模型 2D 坐标(mm)下料，内部换算到草图局部坐标（映射已标定）。"""

    def __init__(self, part: Part, sk, key, inward=None):
        self.part = part
        self.sk = sk
        self.mgr = part.sm
        self.key = key
        self.inward = inward
        self._su, self._sv = SKETCH_SIGN[key]
        self.segments = 0

    def _to_sketch(self, u_mm, v_mm):
        return self._su * u_mm * MM, self._sv * v_mm * MM

    # -- 图元 ----------------------------------------------------------------
    def rect(self, u1, v1, u2, v2):
        p1 = self._to_sketch(u1, v1)
        p2 = self._to_sketch(u2, v2)
        # 在草图坐标里归一化成左下->右上，保证逆时针环绕（否则切除会反选）
        x1, x2 = sorted((p1[0], p2[0]))
        y1, y2 = sorted((p1[1], p2[1]))
        self.mgr.CreateCornerRectangle(x1, y1, 0, x2, y2, 0)
        self.segments += 4
        return self

    def circle(self, u, v, r):
        c = self._to_sketch(u, v)
        self.mgr.CreateCircleByRadius(c[0], c[1], 0, abs(r) * MM)
        self.segments += 1
        return self

    def circles(self, r, positions):
        for u, v in positions:
            self.circle(u, v, r)
        return self

    def line(self, u1, v1, u2, v2):
        p1 = self._to_sketch(u1, v1)
        p2 = self._to_sketch(u2, v2)
        self.mgr.CreateLine(p1[0], p1[1], 0, p2[0], p2[1], 0)
        self.segments += 1
        return self

    def centerline(self, u1, v1, u2, v2):
        p1 = self._to_sketch(u1, v1)
        p2 = self._to_sketch(u2, v2)
        self.mgr.CreateCenterLine(p1[0], p1[1], 0, p2[0], p2[1], 0)
        return self

    def polyline(self, points, close=True):
        pts = [self._to_sketch(u, v) for u, v in points]
        for i in range(len(pts) - 1):
            self.mgr.CreateLine(pts[i][0], pts[i][1], 0,
                                pts[i + 1][0], pts[i + 1][1], 0)
            self.segments += 1
        if close and len(pts) > 2:
            self.mgr.CreateLine(pts[-1][0], pts[-1][1], 0, pts[0][0], pts[0][1], 0)
            self.segments += 1
        return self

    def polygon(self, cu, cv, radius, sides, rotate_deg=0.0):
        r = radius * MM
        base = math.radians(rotate_deg)
        cx, cy = self._to_sketch(cu, cv)
        # 注意：草图里 y 轴可能翻转，半径方向在草图空间用同样的符号即可
        pts = []
        for i in range(sides):
            ang = base + 2 * math.pi * i / sides
            pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
        for i in range(sides):
            a, b = pts[i], pts[(i + 1) % sides]
            self.mgr.CreateLine(a[0], a[1], 0, b[0], b[1], 0)
            self.segments += 1
        return self

    def arc(self, cu, cv, r, a0_deg, a1_deg, ccw=True):
        c = self._to_sketch(cu, cv)
        p0 = self._to_sketch(cu + r * math.cos(math.radians(a0_deg)),
                             cv + r * math.sin(math.radians(a0_deg)))
        p1 = self._to_sketch(cu + r * math.cos(math.radians(a1_deg)),
                             cv + r * math.sin(math.radians(a1_deg)))
        self.mgr.CreateArc(c[0], c[1], 0, p0[0], p0[1], 0, p1[0], p1[1], 0,
                           1 if ccw else -1)
        self.segments += 1
        return self

    def slot(self, u1, v1, u2, v2, width):
        """直槽：两个半圆 + 两条直线。"""
        r = width / 2.0
        dx, dy = u2 - u1, v2 - v1
        length = math.hypot(dx, dy)
        if length < 1e-9:
            raise SwError("槽长不能为 0")
        ux, uy = dx / length, dy / length
        nx, ny = -uy, ux
        # 端点圆周方向
        a_start = math.degrees(math.atan2(ny, nx))
        a_end = math.degrees(math.atan2(-ny, -nx))
        self.arc(u1, v1, r, a_start, a_start + 180)
        self.arc(u2, v2, r, a_end, a_end + 180)
        self.line(u1 + nx * r, v1 + ny * r, u2 + nx * r, v2 + ny * r)
        self.line(u1 - nx * r, v1 - ny * r, u2 - nx * r, v2 - ny * r)
        return self


class Drawing:
    def __init__(self, sw: SolidWorks, doc):
        self.sw = sw
        self.doc = doc

    def _create_views(self, names, model_path):
        for name in names:
            try:
                views = getattr(self.doc, name)(model_path)
                if views:
                    return views
            except Exception:
                continue
        raise SwError("创建三视图失败")

    def create_first_angle_views(self, model_path):
        return self._create_views(("Create1stAngleViews2", "Create1stAngleViews"), model_path)

    def create_third_angle_views(self, model_path):
        return self._create_views(("Create3rdAngleViews2", "Create3rdAngleViews"), model_path)

    def save(self, path):
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.exists(path):
            try:
                cur = _v(self.doc, "GetPathName")
            except Exception:
                cur = ""
            if cur and os.path.abspath(cur).lower() != path.lower():
                try:
                    self.sw.app.CloseDoc(path)      # 关掉上一轮遗留的同名文档
                except Exception:
                    pass
            try:
                os.remove(path)
            except OSError:
                base, ext = os.path.splitext(path)
                i = 2
                while os.path.exists(path):
                    path = "%s_v%d%s" % (base, i, ext)
                    i += 1
        self.doc.SaveAs(path)
        if not os.path.exists(path):
            raise SwError("工程图保存失败")
        return path

    def screenshot(self, path, width=1800, height=1300):
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            _v(self.doc, "ViewZoomtofit2")
        except Exception:
            pass
        self.doc.ClearSelection2(True)
        if os.path.exists(path):
            os.remove(path)
        bmp = os.path.splitext(path)[0] + ".bmp"
        if os.path.exists(bmp):
            os.remove(bmp)
        self.doc.SaveBMP(bmp, width, height)
        if not os.path.exists(bmp):
            raise SwError("工程图截图失败")
        if path.lower().endswith(".png"):
            from PIL import Image
            Image.open(bmp).save(path)
            os.remove(bmp)
        else:
            os.replace(bmp, path)
        return path
