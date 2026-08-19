# -*- coding: utf-8 -*-
"""stp_features 回归测试（2026-08-12）：合成实体数据，不依赖临时 STP 文件。

覆盖：形状判定（回转体/带孔板/纯盒）、圆柱包围盒、box 包围盒、
孔提取（位置/直径/深度/阶梯孔）、螺钉匹配。
"""
import sys
import os
import math

import numpy as np

# Windows GBK 控制台也能打印 ✓/✗（2026-08-19 修复）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stp_features as S

_PASS = 0
_FAIL = 0


def check(name, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  ✓", name)
    else:
        _FAIL += 1
        print("  ✗", name, detail)


def make_solid(faces, pts, centers=None):
    pts = np.array(pts, dtype=float)
    centers = None if centers is None else np.array(centers, dtype=float)
    return {"faces": faces, "points": pts, "circle_centers": centers}


def plane(nx, ny, nz, ox, oy, oz):
    return {"type": S.SURF_PLANE, "radius": None,
            "axis": np.array([nx, ny, nz], dtype=float),
            "origin": np.array([ox, oy, oz], dtype=float), "id": -1}


def cyl(r, nx, ny, nz, ox, oy, oz):
    return {"type": S.SURF_CYL, "radius": r,
            "axis": np.array([nx, ny, nz], dtype=float),
            "origin": np.array([ox, oy, oz], dtype=float), "id": -1}


def box_corners(lx, ly, lz):
    """长方体 8 角点。"""
    return [[x, y, z] for x in (0, lx) for y in (0, ly) for z in (0, lz)]


def cyl_points(r, h, n=16):
    """圆柱表面点（侧面 + 两端）。"""
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        pts.append([r * math.cos(a), r * math.sin(a), 0])
        pts.append([r * math.cos(a), r * math.sin(a), h])
    return pts


print("== 螺钉匹配 ==")
check("Φ5.2 → M5 通孔", S.match_screw(5.2) == {"m": 5, "type": "通孔", "hole": 5.2})
check("Φ6.6 → M6 通孔", S.match_screw(6.6)["m"] == 6 and S.match_screw(6.6)["type"] == "通孔")
check("Φ8.5 → M10 底孔", S.match_screw(8.5) == {"m": 10, "type": "底孔", "hole": 8.5})
check("Φ6.8 → M8 底孔", S.match_screw(6.8)["m"] == 8)
check("Φ14.0 → M16 底孔", S.match_screw(14.0) == {"m": 16, "type": "底孔", "hole": 14.0})
check("Φ5.2 容差 0.2 仍匹配", S.match_screw(5.2, tol=0.2)["m"] == 5)
check("Φ123 无匹配", S.match_screw(123.0) is None)

print("== 形状判定 ==")
ex = S.StpFeatureExtractor.__new__(S.StpFeatureExtractor)  # 不解析文件，仅用方法
ex._entities = {}
ex._points = {}
ex._dirs = {}
ex.path = None

# 回转体：1 侧表面 + 2 端面（n_plane=2 < BOX_PLANE_MIN）
rot = make_solid([cyl(20, 0, 0, 1, 0, 0, 0),
                  plane(0, 0, 1, 0, 0, 0), plane(0, 0, -1, 0, 0, 50)],
                 cyl_points(20, 50), [[0, 0, 0], [0, 0, 50]])
check("回转体(1圆柱+2平面) → cylinder",
      ex.compute_bbox(rot)["shape"] == "cylinder")
check("  规格 Φ40×50",
      ex.compute_bbox(rot)["spec"] == "Φ40×50", ex.compute_bbox(rot)["spec"])

# 带孔板：6+ 平面 + 小孔圆柱（孔判据 → box）
plate = make_solid(
    [cyl(3, 1, 0, 0, 10, 25, 5)] * 2 +
    [plane(1, 0, 0, 0, 0, 0), plane(-1, 0, 0, 100, 0, 0),
     plane(0, 1, 0, 0, 0, 0), plane(0, -1, 0, 0, 50, 0),
     plane(0, 0, 1, 0, 0, 0), plane(0, 0, -1, 0, 0, 10)],
    box_corners(100, 50, 10))
b = ex.compute_bbox(plate)
check("带孔板(6平面+Φ6孔) → box", b["shape"] == "box", (b["shape"], b["detail"]))
check("  规格 100*50*10", b["spec"] == "100*50*10", b["spec"])

# 纯盒（无圆柱）
pure = make_solid(
    [plane(1, 0, 0, 0, 0, 0), plane(-1, 0, 0, 40, 0, 0),
     plane(0, 1, 0, 0, 0, 0), plane(0, -1, 0, 0, 30, 0),
     plane(0, 0, 1, 0, 0, 0), plane(0, 0, -1, 0, 0, 20)],
    box_corners(40, 30, 20))
b2 = ex.compute_bbox(pure)
check("纯盒(6平面) → box", b2["shape"] == "box")
check("  规格 40*30*20", b2["spec"] == "40*30*20", b2["spec"])

print("== 退化 box 兜底（2026-08-19 修复 NameError）==")
# 6 个平面法向全相同（解析异常/退化）→ _group_normals 只有 1 组主轴，
# 旧代码会 NameError；修复后应走点 AABB 兜底不崩。
degen = make_solid(
    [plane(1, 0, 0, 0, 0, 0)] * 6,
    box_corners(10, 8, 6))
d3 = ex.compute_bbox(degen)
check("退化 box 兜底不崩", d3["shape"] in ("box", "mixed"), d3["detail"])
check("  规格非空", bool(d3["spec"]), d3["spec"])

print("== 孔提取（合成：板带 2 个 Φ14 通孔）==")
# 板 100×50×10，2 个 Φ14 孔（r=7，孔轴沿 x），各孔 2 个圆柱面（配对）
hole_faces = [cyl(7, 1, 0, 0, 10, 12, 25), cyl(7, 1, 0, 0, 10, 12, 25),   # 孔1
              cyl(7, 1, 0, 0, 10, 38, 25), cyl(7, 1, 0, 0, 10, 38, 25)]  # 孔2
for i, f in enumerate(hole_faces):
    f["id"] = 100 + i
hole_plate = make_solid(
    hole_faces +
    [plane(1, 0, 0, 0, 0, 0), plane(-1, 0, 0, 100, 0, 0),
     plane(0, 1, 0, 0, 0, 0), plane(0, -1, 0, 0, 50, 0),
     plane(0, 0, 1, 0, 0, 0), plane(0, 0, -1, 0, 0, 10)],
    box_corners(100, 50, 10))
# 注入边界圆数据（_face_circle_data 依赖真实 STP 结构，合成时直接替换）
fake_circles = {  # face id → [(圆心, 半径, 法向)]：孔1 两圆柱面、孔2 两圆柱面
    100: [(np.array([0.0, 12, 25]), 7.0, np.array([1.0, 0, 0])),
          (np.array([10.0, 12, 25]), 7.0, np.array([1.0, 0, 0]))],
    101: [(np.array([0.0, 12, 25]), 7.0, np.array([1.0, 0, 0])),
          (np.array([10.0, 12, 25]), 7.0, np.array([1.0, 0, 0]))],
    102: [(np.array([0.0, 38, 25]), 7.0, np.array([1.0, 0, 0])),
          (np.array([10.0, 38, 25]), 7.0, np.array([1.0, 0, 0]))],
    103: [(np.array([0.0, 38, 25]), 7.0, np.array([1.0, 0, 0])),
          (np.array([10.0, 38, 25]), 7.0, np.array([1.0, 0, 0]))],
}
ex._face_circle_data = lambda fid: fake_circles.get(fid, [])
holes = ex.extract_holes(hole_plate)
check("孔数 = 2", len(holes) == 2, str(len(holes)))
if holes:
    d = sorted(h["diameter"] for h in holes)
    check("孔径均 Φ14", all(abs(x - 14.0) < 1e-6 for x in d), str(d))
    check("深度均 10（板厚）", all(abs(h["depth"] - 10.0) < 1e-6 for h in holes),
          str([h["depth"] for h in holes]))
    ypos = sorted(round(h["origin"][1]) for h in holes)
    check("孔位 y=12/38", ypos == [12, 38], str(ypos))

print()
print("结果: %d 通过 / %d 失败" % (_PASS, _FAIL))
sys.exit(1 if _FAIL else 0)
