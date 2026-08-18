# -*- coding: utf-8 -*-
"""STP B-rep 特征提取（2026-08-11 首批：包围盒 + 形状判定；08-12：孔提取 + 螺钉匹配）。

**职责定位（2026-08-13 P0-3 收敛决策）：本模块是面级特征提取工具，不是第二测量引擎。**
规格测量唯一权威 = geometry_engine（50 点云用户确认基准 50/50 闭环验证）；
本模块的包围盒/形状判定仅供诊断对比（analyze_stp 输出不进入 BOM 主链），
生产用途 = 孔提取（extract_holes）+ 孔径→螺钉匹配（match_screw，供 P1-8
CB 规格校验等面级特征需求消费）。

解析 STP（ISO 10303-21）的实体拓扑链，直接从几何面参数计算特征：
  MANIFOLD_SOLID_BREP → CLOSED_SHELL → ADVANCED_FACE → surface 面参数

形状判定（与人工判断一致：先整体轮廓）：
  - 圆柱/回转体：存在圆柱面 + 端面平面；多平面件（n_plane>=BOX_PLANE_MIN）
    且最大半径圆柱面是"孔"（点到轴线最远垂直距离 / r_max > CYL_HOLE_RATIO）
    → box（带孔板），否则 → cylinder
  - ≥6 平面且无圆柱面 → 长方体（面法向分组 OBB，退化回退点 AABB）
  - 混合面型（孔/台阶/倒角）→ 点 AABB（包围体天然忽略内部特征）

包围盒精度核心：**用面参数而非顶点**（边界 VERTEX_POINT 只是多边形折点）：
  - 圆柱：直径 = 最大圆柱面半径×2（精确），长度 = 端面圆圆心沿 PCA 主轴1 投影差
  - 长方体：3 组平行平面法向 → 面 origin 沿法向投影差（包络，孔不影响）

孔提取（extract_holes）：圆柱面按共轴线分组 → 位置（轴线上一点）/ 直径 / 深度
（该圆柱面边界端面圆圆心沿轴投影差）；侧表面（主体圆柱）不计入孔。

螺钉匹配（match_screw）：孔径 → 标准紧固件表（通孔/螺纹底孔双表，容差可配置）。
"""

import math
import os
import re

import numpy as np

# 面类型常量
SURF_PLANE = "PLANE"
SURF_CYL = "CYLINDRICAL_SURFACE"
SURF_CONE = "CONICAL_SURFACE"
SURF_TORUS = "TOROIDAL_SURFACE"
SURF_SPHERE = "SPHERICAL_SURFACE"

# 形状判别容差（2026-08-12 可配置）
BOX_PLANE_MIN = 6          # 平面面数 >= 该值 → 多平面件（板/块），启用"孔判据"
CYL_HOLE_RATIO = 1.5       # 点到最大圆柱面轴线的最远垂直距离 / r_max 超过该值 → 该圆柱面是孔
                           # （2026-08-13 用可靠轴后：侧表面≈1.0、孔≈2.0~5.0，1.5 居中分隔）
HOLE_AXIS_PARALLEL = 0.99  # 共轴判据：轴方向 |dot| >= 该值
HOLE_AXIS_TOL = 0.5        # 共轴判据：两轴线距离（mm）< 该值


def _fmt_dim(x):
    """尺寸格式化：保留 1 位小数，小数为 0 则只显示整数（40.0→40、79.5→79.5、1.9→1.9）。
    先 round(x,1) 消除浮点噪声（STP 面参数 119.49999/119.50001）。"""
    x = round(float(x), 1)
    if x == int(x):
        return str(int(x))
    return f"{x:.1f}"


class StpFeatureExtractor:
    """STP B-rep 特征提取器（单文件，可多实体）。"""

    def __init__(self, stp_path):
        self.path = stp_path
        self._entities = {}      # id -> (type, args)
        self._points = {}        # id -> np.ndarray(3)  CARTESIAN_POINT
        self._dirs = {}          # id -> np.ndarray(3)  DIRECTION
        self._parse()

    # ---------------- 基础解析 ----------------
    def _parse(self):
        with open(self.path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        # 全部实体 #N=TYPE(args)（单行）。关键：类型名含数字（AXIS2_PLACEMENT_3D）！
        # `[A-Z_]+` 会漏掉——必须 `[A-Z_][A-Z0-9_]*`。分号前只允许空格/制表
        # （`\s*` 跨行吞并后续实体，#305 吞 #306-309 实测 bug）
        for m in re.finditer(r"#(\d+)\s*=\s*([A-Z_][A-Z0-9_]*)\s*\(([^;]*?)\)[ \t]*;",
                             content):
            eid, etype, args = int(m.group(1)), m.group(2), m.group(3).strip()
            self._entities[eid] = (etype, args)
        for eid, (etype, args) in self._entities.items():
            if etype == "CARTESIAN_POINT":
                cm = re.search(r"\(([-0-9.Ee+]+)\s*,\s*([-0-9.Ee+]+)\s*,\s*([-0-9.Ee+]+)\)", args)
                if cm:
                    self._points[eid] = np.array([float(cm.group(1)),
                                                  float(cm.group(2)),
                                                  float(cm.group(3))])
            elif etype == "DIRECTION":
                dm = re.search(r"\(([^)]*)\)", args)
                if dm:
                    try:
                        vals = [float(x) for x in re.findall(r"[-0-9.Ee+]+", dm.group(1))]
                        if len(vals) == 3:
                            self._dirs[eid] = np.array(vals)
                    except ValueError:
                        pass

    def _entity(self, eid):
        return self._entities.get(eid)

    # ---------------- 实体 → 面 ----------------
    def _solid_faces(self, shell_id):
        """CLOSED_SHELL → face ids 列表。"""
        ent = self._entity(shell_id)
        if not ent or ent[0] != "CLOSED_SHELL":
            return []
        ids = [int(x) for x in re.findall(r"#(\d+)", ent[1])]
        return [x for x in ids if x != shell_id]

    def _face_surface(self, face_id):
        """ADVANCED_FACE → (surface_type, params) 或 None。

        params: {"axis": np.ndarray(3), "origin": np.ndarray(3), "radius": float}
        """
        ent = self._entity(face_id)
        if not ent or ent[0] != "ADVANCED_FACE":
            return None
        refs = [int(x) for x in re.findall(r"#(\d+)", ent[1])]
        if not refs:
            return None
        # ADVANCED_FACE('name',(#bound,...),#surface,.F.) —— surface 在最后一个引用
        surf_id = refs[-1]
        sent = self._entity(surf_id)
        if not sent:
            return None
        stype, sargs = sent
        params = {"type": stype, "radius": None, "axis": None, "origin": None,
                  "id": face_id}
        if stype in (SURF_PLANE, SURF_CYL, SURF_CONE, SURF_TORUS, SURF_SPHERE):
            refs2 = [int(x) for x in re.findall(r"#(\d+)", sargs)]
            if refs2:
                ax = self._axis2(refs2[0])
                if ax:
                    params["axis"], params["origin"] = ax
            if stype == SURF_CYL:
                # args 已剥外层括号（_parse），半径在末尾：'name',#axis2,4.25
                rm = re.search(r",\s*([-0-9.Ee+-]+)\s*$", sargs)
                if rm:
                    params["radius"] = float(rm.group(1))
        return params

    def _axis2(self, ax2_id):
        """AXIS2_PLACEMENT_3D → (z_direction, origin)。

        位置点缺失时 origin 返回 None（消费者过滤，勿默认 (0,0,0)——
        会污染投影极值，实测把板件尺寸撑到 1119.5）。
        """
        ent = self._entity(ax2_id)
        if not ent or ent[0] != "AXIS2_PLACEMENT_3D":
            return None
        refs = [int(x) for x in re.findall(r"#(\d+)", ent[1])]
        origin = None
        z_axis = None
        if refs and refs[0] in self._points:
            origin = self._points[refs[0]]
        for r in refs[1:3]:
            if r in self._dirs:
                z_axis = self._dirs[r]
        if z_axis is None:
            z_axis = np.array([0.0, 0.0, 1.0])
        return z_axis, origin

    # ---------------- 实体边界点（面 → 边界 → 顶点）----------------
    def _solid_points(self, face_ids):
        """沿面边界链收集实体顶点：FACE_BOUND → EDGE_LOOP → ORIENTED_EDGE
        → EDGE_CURVE → VERTEX_POINT → CARTESIAN_POINT。失败返回 None（调用方回退）。

        同时收集该实体的 CIRCLE 圆心（2026-08-11）：端面圆中心沿主轴投影
        = 精确端面位置——回转体长度必须来自端面圆（面 origin 全在轴线上，
        只反映倒角/台阶特征位置，用户实测确认会读错倒角尺寸）。
        """
        pts = []
        centers = []
        seen = set()
        for fid in face_ids:
            ent = self._entity(fid)
            if not ent or ent[0] != "ADVANCED_FACE":
                continue
            # bounds = refs[:-1]（最后一个 ref 是 surface；单 bound 面也覆盖——
            # 旧 [1:-1] 会跳过 'name',(#b),#s 的单 bound 面，点集极稀疏）
            for b in [int(x) for x in re.findall(r"#(\d+)", ent[1])][:-1]:
                bent = self._entity(b)
                if not bent or bent[0] not in ("FACE_OUTER_BOUND", "FACE_BOUND"):
                    continue
                loop_refs = [int(x) for x in re.findall(r"#(\d+)", bent[1])]
                for lid in loop_refs:
                    lent = self._entity(lid)
                    if not lent or lent[0] != "EDGE_LOOP":
                        continue
                    for eid in [int(x) for x in re.findall(r"#(\d+)", lent[1])]:
                        eent = self._entity(eid)
                        if not eent or eent[0] != "ORIENTED_EDGE":
                            continue
                        edge_refs = [int(x) for x in re.findall(r"#(\d+)", eent[1])]
                        for er in edge_refs:
                            curt = self._entity(er)
                            if not curt or curt[0] != "EDGE_CURVE":
                                continue
                            # EDGE_CURVE('',#v1,#v2,#geometry,.F.) —— 第 3 个 ref 是几何
                            refs3 = [int(x) for x in re.findall(r"#(\d+)", curt[1])]
                            if len(refs3) >= 3:
                                gid = refs3[2]
                                gent = self._entity(gid)
                                if (gent and gent[0] == "CIRCLE"
                                        and gid not in seen):
                                    seen.add(gid)
                                    cen = self._circle_center(gid)
                                    if cen is not None:
                                        centers.append(cen)
                            for vr in refs3[:2]:
                                vent = self._entity(vr)
                                if not vent or vent[0] != "VERTEX_POINT":
                                    continue
                                p_refs = [int(x) for x in re.findall(r"#(\d+)", vent[1])]
                                for pr in p_refs:
                                    if pr in self._points and pr not in seen:
                                        seen.add(pr)
                                        pts.append(self._points[pr])
        pts_arr = np.array(pts) if pts else None
        cent_arr = np.array(centers) if centers else None
        return pts_arr, cent_arr

    def _circle_center(self, circle_id):
        """CIRCLE → axis2 → 位置点（圆心）。"""
        ent = self._entity(circle_id)
        if not ent or ent[0] != "CIRCLE":
            return None
        refs = [int(x) for x in re.findall(r"#(\d+)", ent[1])]
        if not refs:
            return None
        ax = self._axis2(refs[0])
        return ax[1] if ax else None

    # ---------------- 形状判定 + 包围盒 ----------------
    def extract_solids(self):
        """返回所有实体: [{name, faces:[{type,radius,axis,origin}], shape, bbox, spec}]"""
        solids = []
        for eid, (etype, args) in self._entities.items():
            if etype != "MANIFOLD_SOLID_BREP":
                continue
            shell_refs = [int(x) for x in re.findall(r"#(\d+)", args)]
            if not shell_refs:
                continue
            face_ids = self._solid_faces(shell_refs[0])
            faces = []
            for fid in face_ids:
                fs = self._face_surface(fid)
                if fs:
                    faces.append(fs)
            pts_arr, cent_arr = self._solid_points(face_ids)
            if pts_arr is None or len(pts_arr) < 3:
                pts_arr = self._fallback_points()
            name_m = re.search(r"^'([^']*)'", args)
            solids.append({
                "id": eid,
                "name": name_m.group(1) if name_m else "",
                "faces": faces,
                "points": pts_arr,
                "circle_centers": cent_arr,
            })
        return solids

    def _fallback_points(self):
        """边界链解析失败的兜底：文件全部 CARTESIAN_POINT（单实体文件等价）。"""
        return np.array(list(self._points.values())) if self._points else np.zeros((0, 3))

    def classify_shape(self, faces, pts=None):
        """面型组合判定：cylinder / box / mixed。

        圆柱/回转体件：存在圆柱面 + 端面平面（锥面/环面同轴共属回转体）。
        带孔板区分（2026-08-12）：多平面件（n_plane >= BOX_PLANE_MIN）且
        最大半径圆柱面是"孔"（点到其轴线最远垂直距离 / r_max > CYL_HOLE_RATIO）
        → box；否则回转体 → cylinder。pts 缺失时不启用孔判据（保守按 cylinder）。
        """
        cnt = {}
        for f in faces:
            cnt[f["type"]] = cnt.get(f["type"], 0) + 1
        n_cyl = cnt.get(SURF_CYL, 0)
        n_plane = cnt.get(SURF_PLANE, 0)
        if n_cyl >= 1 and n_plane >= 2:
            if (n_plane >= BOX_PLANE_MIN and pts is not None
                    and len(pts) >= 3 and self._cyl_is_hole(faces, pts)):
                return "box", cnt
            return "cylinder", cnt
        if n_cyl == 0 and n_plane >= 6:
            return "box", cnt
        return "mixed", cnt

    def _cyl_is_hole(self, faces, pts):
        """最大半径圆柱面是否为孔：点到其轴线的最远垂直距离 / r_max。

        侧表面（实体是圆柱/回转体）→ 比值 ≈ 1.0；
        孔 → 远超（带孔板 2.0~5.0）。轴必须用边界圆法向（CIRCLE axis2，精确）——
        STP 圆柱面的 axis 参数不可靠（实测被解析成对角 [0,0.48,0.88]），
        直接使用会把孔/侧表面判反（2026-08-13 修复：013 带 Φ75 孔方板漏判、
        011 支撑柱真圆柱误判 box，均源于此）。
        """
        cyls = [f for f in faces if f["type"] == SURF_CYL and f["radius"]]
        if not cyls:
            return False
        rmax = max(f["radius"] for f in cyls)
        m0 = next(f for f in cyls if f["radius"] == rmax)
        # 可靠轴：最大圆柱面的边界圆法向（精确）；缺失回退点云 PCA；再回退面 axis
        axis = None
        circs = self._face_circle_data(m0["id"])
        if circs and circs[0][2] is not None:
            axis = np.array(circs[0][2])
        if axis is None and len(pts) >= 3:
            centered = pts - pts.mean(axis=0)
            _, _, Vt = np.linalg.svd(centered, full_matrices=False)
            axis = Vt[0]
        if axis is None:
            axis = (m0["axis"] if m0["axis"] is not None
                    else np.array([0.0, 0.0, 1.0]))
        axis = axis / (np.linalg.norm(axis) + 1e-12)
        origin = m0["origin"] if m0["origin"] is not None else np.zeros(3)
        d = pts - origin
        proj = d @ axis
        perp = np.linalg.norm(d - np.outer(proj, axis), axis=1)
        mper = float(perp.max())
        return (mper / rmax) > CYL_HOLE_RATIO

    def compute_bbox(self, solid):
        """包围盒计算（2026-08-11 首批）。

        精度核心：**用面参数而非顶点**——边界 VERTEX_POINT 只是多边形折点
        （圆/曲面件折点稀疏，圆盘仅 22 点），面参数是精确值：
          - 圆柱：半径 = 面参数（取最大半径=外径），高度 = 所有面 origin 沿轴投影差
          - 长方体：3 组平行平面法向 → 面 origin 沿法向投影差（包络，孔/台阶不影响）
          - 混合/退化：点 AABB 兜底
        返回 {"shape": ..., "dims": (a,b,c) 降序, "spec": 规格字符串, "detail": 依据}
        """
        pts = solid["points"]
        faces = solid["faces"]
        shape, cnt = self.classify_shape(faces, pts)

        if shape == "cylinder":
            cyls = [f for f in faces
                    if f["type"] == SURF_CYL and f["radius"]]
            if cyls:
                r = max(f["radius"] for f in cyls)     # 外径（孔/倒角半径更小）
                # 主轴 = 最大半径面（主体侧表面）的边界圆法向——圆法向是精确的
                # （2026-08-12：PCA 在退化点集上会失效，a71 薄圆盘点集 z 跨 0
                # → PCA 轴 y 与真实圆柱轴 x 垂直 → 高度 0）。圆缺失时回退
                # 点集 PCA 主轴1（ZCZ 实测主体 Φ120 的圆柱面 axis 不可靠，
                # 但圆法向/PCA 都指向真实轴向）。
                axis = None
                rmax_face = next(f for f in cyls if f["radius"] == r)
                circs = self._face_circle_data(rmax_face["id"])
                if circs and circs[0][2] is not None:
                    axis = circs[0][2]
                if axis is None and len(pts) >= 3:
                    centered = pts - pts.mean(axis=0)
                    _, _, Vt = np.linalg.svd(centered, full_matrices=False)
                    axis = Vt[0]
                if axis is None:
                    axis = (cyls[0]["axis"] if cyls[0]["axis"] is not None
                            else np.array([0.0, 0.0, 1.0]))
                axis = axis / (np.linalg.norm(axis) + 1e-12)
                # 长度优先用端面圆圆心（2026-08-11：回转体面 origin 全在轴线上
                # 只反映倒角/台阶位置——用户实测确认会读错倒角尺寸；端面圆圆心
                # 沿轴投影差 = 精确端面位置差）
                h = None
                centers = solid.get("circle_centers")
                if centers is not None and len(centers) >= 2:
                    proj = centers @ axis
                    h = float(proj.max() - proj.min())
                if h is None:
                    ext = [float(f["origin"] @ axis) for f in faces
                           if f["origin"] is not None]
                    if len(ext) >= 2:
                        h = max(ext) - min(ext)
                if h is None and len(pts):
                    proj = pts @ axis
                    h = float(proj.max() - proj.min())
                if h is None:
                    h = 0.0
                d = 2 * r
                return {"shape": "cylinder", "dims": (d, d, h),
                        "spec": f"Φ{_fmt_dim(d)}×{_fmt_dim(h)}",
                        "detail": f"brep_cyl r={r:.3f} h={h:.2f}"}
        if shape == "box":
            # PCA 主轴（面法向分布）→ 面 origin/点/圆心 三源投影取极值
            normals = [f["axis"] for f in faces
                       if f["type"] == SURF_PLANE and f["axis"] is not None]
            axes = self._group_normals(normals)
            if len(axes) >= 2:
                dims = self._plane_axis_dims(faces, axes, pts,
                                             solid.get("circle_centers"))
            if len(dims) >= 3 and min(dims) > 1e-6:
                dims_sorted = tuple(sorted(dims, reverse=True))
                return {"shape": "box", "dims": dims_sorted,
                        "spec": self._spec_str(dims_sorted),
                        "detail": f"brep_box planes={len(normals)}"}
        # 混合/退化 → 点 AABB
        dims = self._aabb(pts)
        return {"shape": "box" if shape == "box" else "mixed",
                "dims": dims,
                "spec": self._spec_str(dims),
                "detail": f"brep_aabb faces={dict(cnt)}"}

    # ---------------- 孔提取 + 螺钉匹配（2026-08-12）----------------
    def _face_circle_data(self, face_id):
        """ADVANCED_FACE 边界上的 CIRCLE 集合 → [(圆心, 半径, 法向)]。

        圆柱面/锥面的边界通常含 2 个 CIRCLE（两端）——圆心沿法向投影差 = 特征深度。
        圆法向 = CIRCLE 的 axis2 z 方向（精确；平面/圆柱面的 axis2 方向不可靠，
        实测 1f7f 孔轴被解析成对角 [0,0.48,0.88]）。
        """
        ent = self._entity(face_id)
        if not ent or ent[0] != "ADVANCED_FACE":
            return []
        out = []
        for b in [int(x) for x in re.findall(r"#(\d+)", ent[1])][:-1]:
            bent = self._entity(b)
            if not bent or bent[0] not in ("FACE_OUTER_BOUND", "FACE_BOUND"):
                continue
            for lid in [int(x) for x in re.findall(r"#(\d+)", bent[1])]:
                lent = self._entity(lid)
                if not lent or lent[0] != "EDGE_LOOP":
                    continue
                for eid in [int(x) for x in re.findall(r"#(\d+)", lent[1])]:
                    eent = self._entity(eid)
                    if not eent or eent[0] != "ORIENTED_EDGE":
                        continue
                    for er in [int(x) for x in re.findall(r"#(\d+)", eent[1])]:
                        curt = self._entity(er)
                        if not curt or curt[0] != "EDGE_CURVE":
                            continue
                        refs3 = [int(x) for x in re.findall(r"#(\d+)", curt[1])]
                        if len(refs3) < 3:
                            continue
                        gent = self._entity(refs3[2])
                        if not gent or gent[0] != "CIRCLE":
                            continue
                        cent = self._circle_center(refs3[2])
                        if cent is None:
                            continue
                        cm = re.search(r",\s*([-0-9.Ee+-]+)\s*$", gent[1])
                        rad = float(cm.group(1)) if cm else 0.0
                        normal = None
                        cref = [int(x) for x in re.findall(r"#(\d+)", gent[1])]
                        if cref:
                            ax = self._axis2(cref[0])
                            if ax:
                                normal = ax[0]
                        out.append((cent, rad, normal))
        return out

    def extract_holes(self, solid):
        """孔提取：圆柱面按共轴线分组 → 位置/直径/深度。

        返回 [{"axis","origin","diameter","radius","depth","n_faces"}]，按直径降序。
        孔轴/位置/深度一律来自**边界圆**（CIRCLE 的 axis2 精确；圆柱面自身的
        axis2 方向/位置不可靠——1f7f 实测孔轴被解析成对角）。共轴判据：
        圆法向 |dot|>=HOLE_AXIS_PARALLEL 且圆心中线距离 < HOLE_AXIS_TOL。
        侧表面（该实体的主体圆柱，与 classify_shape 判定一致）不计入孔。
        """
        cyls = [f for f in solid["faces"]
                if f["type"] == SURF_CYL and f["radius"]]
        if not cyls:
            return []
        pts = solid["points"]
        cnt = {}
        for f in solid["faces"]:
            cnt[f["type"]] = cnt.get(f["type"], 0) + 1
        # 侧表面半径：多平面件且最大圆柱是孔 → 无侧表面（全是孔）；否则最大半径即主体
        lateral = None
        if cnt.get(SURF_PLANE, 0) < BOX_PLANE_MIN or not self._cyl_is_hole(solid["faces"], pts):
            lateral = max(f["radius"] for f in cyls)
        # 每根圆柱面的孔数据（轴/位置取边界圆，精确）
        feats = []
        for f in cyls:
            circs = self._face_circle_data(f["id"])
            centers = [c for c, _r, _n in circs]
            axis = None
            origin = None
            norm = circs[0][2] if circs else None
            if circs:
                origin = circs[0][0]
                axis = circs[0][2]
            if axis is None:
                axis = (f["axis"] if f["axis"] is not None
                        else np.array([0.0, 0.0, 1.0]))
            if origin is None:
                origin = (f["origin"] if f["origin"] is not None
                          else np.zeros(3))
            axis = axis / (np.linalg.norm(axis) + 1e-12)
            feats.append({"radius": f["radius"], "axis": axis,
                          "origin": origin, "centers": centers,
                          "circle_norm": norm})
        # 共轴分组：圆法向平行 + 轴线距离近（任一侧圆缺失只按方向）
        groups = []
        for ft in feats:
            for g in groups:
                ga = g["axis"]
                if abs(float(np.dot(ga, ft["axis"]))) > HOLE_AXIS_PARALLEL:
                    w = ft["origin"] - g["origin"]
                    dist = np.linalg.norm(w - np.dot(w, ga) * ga)
                    if (ft["circle_norm"] is None or g["norm_none"]
                            or dist < HOLE_AXIS_TOL):
                        g["feats"].append(ft)
                        if ft["radius"] > g["radius"]:
                            g["radius"] = ft["radius"]
                        break
            else:
                groups.append({"axis": ft["axis"], "origin": ft["origin"],
                               "radius": ft["radius"], "feats": [ft],
                               "norm_none": ft["circle_norm"] is None})
        holes = []
        for g in groups:
            # 只剔除侧表面半径的面（阶梯孔组内含外圆面 r=lateral 时，
            # 组仍成立——内孔段是孔；旧逻辑整组排除会丢孔）
            feats = [ft for ft in g["feats"]
                     if lateral is None or abs(ft["radius"] - lateral) >= 1e-6]
            if not feats:
                continue
            radius = max(ft["radius"] for ft in feats)
            centers = []
            for ft in feats:
                centers.extend(ft["centers"])
            depth = 0.0
            if centers:
                proj = np.array(centers) @ g["axis"]
                depth = float(proj.max() - proj.min())
            holes.append({
                "axis": g["axis"], "origin": g["origin"],
                "diameter": 2 * radius, "radius": radius,
                "depth": depth, "n_faces": len(feats),
            })
        holes.sort(key=lambda h: -h["diameter"])
        return holes

    def _group_normals(self, normals, tol=0.35):
        """面法向主轴：按方向聚类，按频次取 3 个近正交主轴。

        倒角/圆角法向（45°）面数少、频率低，被真正的 x/y/z 主轴淘汰
        （旧实现按出现顺序 + len>=3 截断会把倒角当主轴，实测 87.7 误判；
        PCA 方案被不对称倒角带偏 15°，同样不可用——频次贪心最稳）。
        """
        if not normals:
            return []
        cos_tol = math.cos(tol)
        groups = []  # [direction, 面数]
        for n in normals:
            n = n / (np.linalg.norm(n) + 1e-12)
            for g in groups:
                if abs(float(np.dot(g[0], n))) > cos_tol:
                    g[1] += 1
                    break
            else:
                groups.append([n, 1])
        if not groups:
            return []
        groups.sort(key=lambda g: -g[1])
        axes = [groups[0][0]]
        if len(groups) >= 2:
            best, best_dot = None, 1.0
            for g in groups[1:]:
                d = abs(float(np.dot(axes[0], g[0])))
                if d < best_dot:
                    best_dot, best = d, g[0]
            if best is not None:
                axes.append(best)
        if len(axes) >= 2:
            ax3 = np.cross(axes[0], axes[1])
            ax3 = ax3 / (np.linalg.norm(ax3) + 1e-12)
            axes.append(ax3)
        return axes

    def _plane_axis_dims(self, faces, axes, pts, centers=None):
        """沿各主轴取 box 尺寸：边界点 + 端面圆心投影极值。

        2026-08-12 弃用面 origin 投影——实测 PLANE 的 axis2 location 点
        不可靠（CATIA 放在任意参考位置，面#3466 location 在 y=-37 而边界
        在 y∈[-1141,-1111]），会污染尺寸（1119.5 假值）。边界顶点是真实
        面角点，圆心是精确端面位置，两者投影极值 = 真实包络。
        """
        dims = []
        for ax in axes:
            vals = []
            if pts is not None and len(pts):
                vals += (pts @ ax).tolist()
            if centers is not None and len(centers):
                vals += (centers @ ax).tolist()
            if len(vals) >= 2:
                dims.append(max(vals) - min(vals))
        return dims

    def _aabb(self, pts):
        if pts is None or len(pts) == 0:
            return (0.0, 0.0, 0.0)
        mn = pts.min(axis=0)
        mx = pts.max(axis=0)
        return tuple(sorted((mx - mn).tolist(), reverse=True))

    @staticmethod
    def _spec_str(dims):
        vals = [_fmt_dim(x) for x in dims]
        while len(vals) < 3:
            vals.append("0")
        return "*".join(vals[:3])


# ---------------- 标准紧固件孔对照（通孔 / 螺纹底孔）----------------
# (M 规格, 通孔直径, 螺纹底孔直径) —— 常用配合公差中值
SCREW_HOLE_TABLE = [
    (3, 3.2, 2.5), (4, 4.3, 3.3), (5, 5.2, 4.2), (6, 6.6, 5.0),
    (8, 8.5, 6.8), (10, 10.5, 8.5), (12, 13.0, 10.2),
    (14, 15.0, 12.0), (16, 17.0, 14.0), (18, 19.0, 15.5),
    (20, 21.0, 17.5), (22, 23.0, 19.5), (24, 25.0, 21.0),
]


def match_screw(hole_diameter, tol=0.6):
    """孔径 → 可能螺钉规格（通孔/底孔双表匹配）。

    返回 {"m": 6, "type": "通孔", "hole": 6.6} 或 None（偏差超 tol 无匹配）。
    tol 可配置：孔径与标准孔直径的最大允许偏差（mm）。
    """
    best = None
    best_d = tol
    for m, clear, tap in SCREW_HOLE_TABLE:
        for htype, hd in (("通孔", clear), ("底孔", tap)):
            d = abs(hole_diameter - hd)
            if d <= best_d:
                best_d = d
                best = {"m": m, "type": htype, "hole": hd}
    return best


# ---------------- 便捷入口 ----------------
def analyze_stp(stp_path):
    """STP → 实体包围盒列表（**仅供诊断对比**，与 geometry_engine 输出风格一致）。

    2026-08-13 P0-3 收敛决策：BOM 规格唯一权威 = geometry_engine；
    本函数结果不进入主链，仅用于两法交叉验证调试。
    """
    ex = StpFeatureExtractor(stp_path)
    return [ex.compute_bbox(s) for s in ex.extract_solids()]


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        print(f"== {os.path.basename(p)} ==")
        for b in analyze_stp(p):
            print(f"  {b['shape']:<8} {b['spec']:<22} ({b['detail']})")
