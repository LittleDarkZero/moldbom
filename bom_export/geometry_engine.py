#!/usr/bin/env python3
"""
=============================================================================
 规格测量引擎 — 点云形状分析与包围盒计算
 Geometry Engine: Point Cloud Shape Analysis & Bounding Box Computation
=============================================================================

 来源: pointcloud_analyzer/pointcloud_analyzer.py（2026-07-31 迁移，替代旧 GA+QR+NM OBB 实现）
 职责: 对单个点集(实体的顶点云)做形状识别 + 包围体计算，供 bom_export 规格测量链路使用。

 功能:
   1. 根据点云空间分布自动识别实体形状（立方体/圆柱体/球体/圆环/含孔槽圆柱）
   2. 对长方体计算最小有向包围盒 (OBB) 尺寸
   3. 对圆柱体计算最小体积圆柱体包围盒参数（Φ 规格）

 算法:
   - PCA 特征值分析 → 形状分类
   - OBB: PCA 方向投影 + 微分进化(DE)全局搜索 + Nelder-Mead 精炼，与 AABB 智能比较取最小
   - 最小体积圆柱: 30 方向候选（PCA 3 轴 + Fibonacci 球面 27）+ 凸包精确最小包围圆（_mec_exact）+ NM 精炼
   - 截面圆形度校验（R/半宽比）防矩形误判为圆柱

 高层接口（bom_export 使用）:
   - analyze_points(points) -> dict   单点集完整分析（形状 + OBB + 圆柱）
   - analysis_volume(analysis) -> float  包围体体积（圆柱件取圆柱体积，其余取 OBB 体积）
   - format_spec(analysis) -> str     规格字符串: 圆柱/圆环 → Φ直径×长度；其余 → L*W*H

 依赖: numpy, scipy（项目 venv 已安装）
=============================================================================
"""

import warnings
import logging
import numpy as np
from scipy.spatial import ConvexHull

# 仅抑制 scipy 内部的 UserWarning（DE 收敛/退化警告），不吞其他模块告警
warnings.filterwarnings("ignore", category=UserWarning, module="scipy.*")

log = logging.getLogger("bom_export.geometry")


# ============================================================================
#  工具函数
# ============================================================================

def make_rotation_matrix(axis, angle):
    """Rodrigues 旋转公式: 绕任意轴旋转"""
    axis = axis / np.linalg.norm(axis)
    K = np.array([
        [0, -axis[2], axis[1]],
        [axis[2], 0, -axis[0]],
        [-axis[1], axis[0], 0]
    ])
    return np.eye(3) + np.sin(angle) * K + (1 - np.cos(angle)) * (K @ K)


def fibonacci_sphere(n):
    """Fibonacci 球面均匀采样方向"""
    directions = []
    phi = np.pi * (3.0 - np.sqrt(5.0))
    for i in range(n):
        y = 1 - (i / float(n - 1)) * 2
        radius = np.sqrt(1 - y * y)
        theta = phi * i
        directions.append(np.array([np.cos(theta) * radius, y, np.sin(theta) * radius]))
    return directions


def _zyx_rot(rx, ry, rz):
    """ZYX 欧拉角旋转矩阵（_compute_obb_de 内两处内联矩阵提为公共函数）。"""
    cx, sx = np.cos(rx), np.sin(rx)
    cy, sy = np.cos(ry), np.sin(ry)
    cz, sz = np.cos(rz), np.sin(rz)
    return np.array([
        [cy * cz, cz * sx * sy - cx * sz, cx * cz * sy + sx * sz],
        [cy * sz, cx * cz + sx * sy * sz, -cz * sx + cx * sy * sz],
        [-sy, cy * sx, cx * cy],
    ])


def _plane_basis(axis):
    """返回垂直于 axis 的右手正交基 (u, v)。

    axis 接近 Z 轴时退化（cross 结果≈0），改用 X 轴叉乘避免奇异。
    原在 5 个截面方法中内联重复，2026-08-18 提为公共函数。
    """
    axis = np.asarray(axis, dtype=np.float64)
    if abs(axis[2]) < 0.999:
        u = np.cross(axis, [0, 0, 1])
    else:
        u = np.cross(axis, [1, 0, 0])
    u = u / np.linalg.norm(u)
    v = np.cross(axis, u)
    return u, v


def _cross_section_obb(pts_2d):
    """2D 点集的 PCA 主轴包围矩形尺寸，返回 (obb_x, obb_y)。

    原在 4 个截面方法中内联重复，2026-08-18 提为公共函数。
    """
    p2 = pts_2d - pts_2d.mean(axis=0)
    cov = p2.T @ p2
    w2, V2 = np.linalg.eigh(cov)
    proj_main = p2 @ V2[:, int(np.argmax(w2))]
    proj_orth = p2 @ V2[:, int(np.argmin(w2))]
    obb_x = proj_main.max() - proj_main.min()
    obb_y = proj_orth.max() - proj_orth.min()
    return obb_x, obb_y


# ============================================================================
#  投影特征分类容差（2026-08-11 系统性优化，可配置微调）
#
#  判定原理（人类几何直觉）: 沿 3 个正交主轴投影——
#    box      → 3 个矩形（尺寸可不同）
#    cylinder → 1 个圆 + 2 个等宽矩形（等宽侧 = 圆柱直径，另侧 = 高度）
#    sphere   → 3 个等径圆
# ============================================================================
PROJ_CIRC_RATIO = 1.15        # 投影 R_over_maxhalf < 该值 → 候选圆形（容忍噪声圆 ~1.12）
PROJ_CIRC_SQUARE = 0.12       # 圆形投影允许的最大方形度（|w-h|/max(w,h)）
PROJ_SPHERE_RADIAL = 0.06     # 3D 点到中心距离一致性（球≈0.01、正方体≈0.17——区分球 vs 方）
PROJ_SAME_TOL = 0.06          # 圆柱两矩形等宽侧一致性容差（相对偏差）
PROJ_CONF_FLOOR = 0.60        # 投影判定置信度下限（低于则回退启发式）
PROJ_CONF_OVERRIDE = 0.88     # 置信度 >= 该值 → 提前返回（跳过重计算）

# 2026-08-13 P0-1 修复: 稀疏点云阈值——真实 STP 边界折点仅 16~100 个
# （合成测试 1500+ 点），圆形投影判定在稀疏折点下不可靠（支撑柱 29 点、
# 地侧支撑柱 16 点均被 proj_3rect 误判 box；挡料环投影直径 104.4 vs 真值 110）。
# 低于该点数的点云，投影分类置信度封顶在 PROJ_CONF_FLOOR 之下不提前返回，
# 交给 PCA+圆形度启发式（08-03 用户确认链路）+ 30 方向最小体积圆柱取精确尺寸。
PROJ_SPARSE_N = 200

# 2026-08-13 修复: 圆柱 gap 检测的 gap_ratio 上限。gap_ratio = 最小包围圆半径 /
# 截面短半宽——真圆柱含孔槽 ≈1.2~3，薄板截面短半宽≈0 使 gap_ratio 巨大
# （复位杆调整板 32 万+、压线板A 4.6），设上限拦截薄板。
CYLINDER_GAP_RATIO_MAX = 3.0

# 2026-08-13 修复: 主轴1 长轴圆柱判定的 gap_ratio 上限（沿 PCA 主轴1 截面）。
# 主轴1 截面比全局最小体积圆柱轴截面更"扁"：稀疏圆柱(16点地侧支撑柱)≈4.0、
# 吊环≈2.7、导柱≈1.0，薄板/细条 ≥6.7——取 5.0 分隔（比宽高比 0.15/0.25 的窄缝可靠）。
CYLINDER_GAP_RATIO_MAX_AXIS = 5.0

# ============================================================================
#  PCA 启发式分类阈值（2026-08-18 从 classify_shape 散落裸阈值提为命名常量，
#  与投影分类 PROJ_* 常量同风格；数值与历史完全一致，仅可读性提升）
# ============================================================================
SPHERE_UNIFORMITY_MIN = 0.75    # sphere_uniformity > 该值 → 球体
CYL_AXIAL_MIN = 0.50            # 长圆柱 PCA 判定 cylinder_axial 下限
CYL_LINEARITY_MIN = 0.20        # 长圆柱 linearity 下限
CYL_ASPECT_RATIO = 1.5          # 高径比 > 该值 → 长圆柱；否则圆盘/圆环
DISK_RADIAL_MIN = 0.70          # 圆盘 disk_radial 下限
DISK_FLATNESS_MIN = 0.40        # 圆盘 flatness 下限
LINEARITY_SLENDER = 0.50        # 细长件 linearity 下限（主轴1 判定 + 孔槽检测共用）
RING_CONF_MIN = 0.35            # 圆拟合环形件置信度下限
HULL_FILL_BOX_CONF = 0.40       # hull_fill < 该值 → box 降置信
SPARSE_N = 100                  # 稀疏点云辅助判断点数门槛（区别于投影的 PROJ_SPARSE_N=200）
SPARSE_CROSS_ASPECT = 0.85      # 稀疏件截面方形度门槛
SPARSE_CYL_AXIAL = 0.40         # 稀疏件圆柱 axial 下限
SPARSE_CONF = 0.80              # 稀疏圆柱近似判定置信度
BOX_DEFAULT_CONF = 0.90         # box 默认置信度（decision 起始值）
BOX_HULL_LOW_CONF = 0.85        # hull_fill 偏低时的 box 置信度


# ============================================================================
#  最小包围圆 (2D) — 自实现凸包精确法 + 大点集均值中心圆
# ============================================================================

def _convex_hull_2d(pts):
    """Andrew monotone chain 2D 凸包（纯 numpy，零 scipy 依赖）。

    2026-07-31 引入: 替代 scipy ConvexHull——后者在 Windows 每次构造创建
    临时文件，本运行环境 os.remove 被沙箱重定向到回收站（~12ms/次），
    圆柱 15 方向粗搜 + NM 精炼共 ~74 次 mec/实体，累计 1-2s 纯文件开销。
    返回凸包顶点数组（逆时针）。
    """
    pts = np.asarray(pts, dtype=np.float64)
    order = np.lexsort((pts[:, 1], pts[:, 0]))
    sorted_pts = pts[order]
    n = len(sorted_pts)
    if n <= 2:
        return sorted_pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in sorted_pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in sorted_pts[::-1]:
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return np.asarray(lower[:-1] + upper[:-1])


def circumcenter_2d(a, b, c):
    """三点外接圆圆心 (2D)"""
    d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
    if abs(d) < 1e-15:
        return None
    a2 = a[0] ** 2 + a[1] ** 2
    b2 = b[0] ** 2 + b[1] ** 2
    c2 = c[0] ** 2 + c[1] ** 2
    ux = (a2 * (b[1] - c[1]) + b2 * (c[1] - a[1]) + c2 * (a[1] - b[1])) / d
    uy = (a2 * (c[0] - b[0]) + b2 * (a[0] - c[0]) + c2 * (b[0] - a[0])) / d
    return np.array([ux, uy])


def _mec_exact(hull_pts):
    """精确最小包围圆: 枚举凸包直径对 + 三点外接圆"""
    best_center = None
    best_radius = float("inf")
    h = len(hull_pts)
    # 所有直径对
    for i in range(h):
        for j in range(i + 1, h):
            c = (hull_pts[i] + hull_pts[j]) / 2
            r = np.linalg.norm(hull_pts[i] - hull_pts[j]) / 2
            if np.all(np.sum((hull_pts - c) ** 2, axis=1) <= (r + 1e-10) ** 2):
                if r < best_radius:
                    best_radius = r
                    best_center = c
    # 所有三点外接圆
    for i in range(h):
        for j in range(i + 1, h):
            for k in range(j + 1, h):
                c = circumcenter_2d(hull_pts[i], hull_pts[j], hull_pts[k])
                if c is None:
                    continue
                r = np.linalg.norm(hull_pts[i] - c)
                if np.all(np.sum((hull_pts - c) ** 2, axis=1) <= (r + 1e-10) ** 2):
                    if r < best_radius:
                        best_radius = r
                        best_center = c
    return best_center, best_radius


def min_enclosing_circle_2d(points_2d):
    """
    计算 2D 点集的最小包围圆（鲁棒混合算法）。

    策略（2026-07-31 重构）:
      - 点数 ≤ 60 → 自实现 2D 凸包（Andrew，无 scipy 临时文件开销）
        + 凸包顶点枚举精确法（直径对 + 三点外接圆），半径精确；
      - 点数 > 60 → 均值中心圆（近圆/均匀大点集，质心 ≈ 圆心，误差 <5%）。

    修复记录: 中间版本统一均值中心圆导致稀疏点云（50 点）直径高估 4-6%
    （动模定位圈 Φ200→Φ213、地侧支撑柱 Φ160→Φ167），精确法恢复后与
    旧引擎（Welzl 最小圆）一致。

    Returns (center, radius)
    """
    pts = np.asarray(points_2d, dtype=np.float64)
    n = len(pts)
    if n == 0:
        return np.zeros(2), 0.0
    if n == 1:
        return pts[0].copy(), 0.0
    if n > 60:
        return _mec_iterative(pts)
    hull_pts = _convex_hull_2d(pts)
    if len(hull_pts) < 3:
        center = hull_pts.mean(axis=0)
        radius = np.sqrt(np.max(np.sum((hull_pts - center) ** 2, axis=1)))
        return center, radius
    return _mec_exact(hull_pts)


def _mec_iterative(all_pts):
    """均值中心包围圆（Ritter-like 简化版）。

    2026-07-31 适配: 原实现用凸包半径 + 全点最远点做中心修正，
    但两者同源时 excess 恒为 0，等价于均值中心圆；对均匀点集
    （圆/矩形截面）均值≈几何中心，半径误差 <5%，BOM 取整足够。
    对偏心点集（弧/部分环）半径偏高——圆形度检查方向更保守（安全）。
    """
    center = all_pts.mean(axis=0)
    radius = np.sqrt(np.max(np.sum((all_pts - center) ** 2, axis=1)))
    return center, radius


# ============================================================================
#  主分析类
# ============================================================================

class PointCloudAnalyzer:
    """点云形状分析与包围盒计算（增强版）"""

    def __init__(self, points, name="未命名实体"):
        self.points = np.asarray(points, dtype=np.float64)
        self.name = name
        self.N = len(self.points)

        # PCA
        self.center = self.points.mean(axis=0)
        centered = self.points - self.center
        U, S, Vt = np.linalg.svd(centered, full_matrices=False)
        self.eigenvalues = (S ** 2) / max(self.N - 1, 1)
        self.eigenvectors = Vt

        order = np.argsort(self.eigenvalues)[::-1]
        self.eigenvalues = self.eigenvalues[order]
        self.eigenvectors = self.eigenvectors[order]

        # 世界坐标轴 (identity)
        self._world_axes = np.eye(3)

        # 凸包
        if self.N >= 4:
            try:
                hull = ConvexHull(self.points)
                self._hull_pts = self.points[hull.vertices]
                self._hull_vol = hull.volume
            except Exception:
                self._hull_pts = self.points
                self._hull_vol = 0.0
        else:
            self._hull_pts = self.points
            self._hull_vol = 0.0

        # 缓存
        self._shape_cache = None
        self._obb_cache = None
        self._cylinder_cache = None
        self._circle_cache = None
        self._aabb_cache = None

    # ---------- AABB (轴对齐包围盒) ----------

    def compute_aabb(self):
        """轴对齐包围盒"""
        if self._aabb_cache:
            return self._aabb_cache

        mins = self.points.min(axis=0)
        maxs = self.points.max(axis=0)
        dims = maxs - mins
        dim_order = np.argsort(dims)[::-1]
        dims_sorted = dims[dim_order]
        center = (mins + maxs) / 2
        half = dims_sorted / 2

        corners = np.array([
            [-1,-1,-1],[-1,-1,1],[-1,1,-1],[-1,1,1],
            [1,-1,-1],[1,-1,1],[1,1,-1],[1,1,1]
        ]) * half + center

        result = {
            "length": round(float(dims_sorted[0]), 4),
            "width": round(float(dims_sorted[1]), 4),
            "height": round(float(dims_sorted[2]), 4),
            "volume": round(float(np.prod(dims_sorted)), 4),
            "center": center.tolist(),
            "axes": self._world_axes[dim_order].tolist(),
            "half_extents": half.tolist(),
            "corners": corners.tolist(),
            "type": "AABB",
        }
        self._aabb_cache = result
        return result

    # ---------- OBB 计算（智能选择 AABB vs PCA-OBB）----------

    def compute_obb(self, fast=False):
        """智能有向包围盒：PCA-OBB → DE 全局优化 → AABB 比较，取最小体积。

        fast=True（2026-08-11 投影分类 box 路径）：跳过 DE 全局优化——
        投影分类已确认 box，PCA-OBB 主轴即几何主轴，DE 仅微调（通常 <5%），
        批量测量提速显著。
        """
        if self._obb_cache:
            return self._obb_cache

        # ---- AABB ----
        aabb = self.compute_aabb()

        # ---- PCA-OBB ----
        pca_obb = self._build_obb(self.eigenvectors, self.center)

        # ---- DE 全局优化 OBB（凸包顶点 >= 4 时启用）----
        de_obb = None
        if not fast and len(self._hull_pts) >= 4 and len(self._hull_pts) != len(self.points):
            de_obb = self._compute_obb_de()
            if de_obb and de_obb["volume"] < pca_obb["volume"]:
                pca_obb = de_obb  # DE 找到更优解则替换

        # ---- 智能选择: AABB vs 最优 OBB ----
        pca_vol = pca_obb["volume"]
        aabb_vol = aabb["volume"]
        axes_sorted = np.array(pca_obb["axes"])

        axis_angles = [np.arccos(np.clip(abs(np.dot(axes_sorted[i], self._world_axes[i])), 0, 1))
                       for i in range(3)]
        max_angle_deg = max(np.degrees(a) for a in axis_angles)

        use_aabb = False
        if aabb_vol <= pca_vol * 1.03:
            if max_angle_deg < 20:
                use_aabb = True
            elif aabb_vol < pca_vol:
                use_aabb = True

        result = aabb if use_aabb else pca_obb
        self._obb_cache = result
        return result

    def _build_obb(self, axes, obb_center):
        """根据方向矩阵和中心构建 OBB dict"""
        centered = self.points - np.array(obb_center)
        proj = centered @ axes.T
        mins = proj.min(axis=0)
        maxs = proj.max(axis=0)
        dims = maxs - mins
        half_extents = dims / 2

        dim_order = np.argsort(dims)[::-1]
        dims_sorted = dims[dim_order]
        axes_sorted = axes[dim_order]

        obb_center_proj = (mins + maxs) / 2
        real_center = np.array(obb_center) + obb_center_proj @ axes.T

        corners_local = np.array([
            [-1,-1,-1],[-1,-1,1],[-1,1,-1],[-1,1,1],
            [1,-1,-1],[1,-1,1],[1,1,-1],[1,1,1]
        ]) * half_extents[dim_order]
        corners = real_center + corners_local @ axes_sorted

        return {
            "length": round(float(dims_sorted[0]), 4),
            "width": round(float(dims_sorted[1]), 4),
            "height": round(float(dims_sorted[2]), 4),
            "volume": round(float(np.prod(dims_sorted)), 4),
            "center": real_center.tolist(),
            "axes": axes_sorted.tolist(),
            "half_extents": half_extents[dim_order].tolist(),
            "corners": corners.tolist(),
            "type": "PCA-OBB",
        }

    def _compute_obb_de(self):
        """微分进化 (DE) 全局搜索最小体积 OBB 方向"""
        hull_pts = self._hull_pts

        def volume_from_euler(angles):
            rx, ry, rz = angles
            # ZYX 旋转矩阵（提为模块级 _zyx_rot）
            R = _zyx_rot(rx, ry, rz)
            projected = hull_pts @ R.T
            dims = projected.max(axis=0) - projected.min(axis=0)
            return np.prod(dims)

        try:
            from scipy.optimize import differential_evolution
            bounds = [(-np.pi, np.pi)] * 3

            # 用 PCA 方向初始化一个种子，加速收敛
            # 从 PCA 旋转矩阵反算 Euler 角
            pca_R = self.eigenvectors
            sy = -pca_R[2, 0]
            sy = np.clip(sy, -1, 1)
            cy = np.sqrt(max(0, 1 - sy * sy))
            if cy > 1e-6:
                cz = pca_R[0, 0] / cy
                sz = pca_R[1, 0] / cy
                cx = pca_R[2, 2] / cy
                sx = pca_R[2, 1] / cy
                ry0 = np.arctan2(sy, cy)
                rz0 = np.arctan2(sz, cz)
                rx0 = np.arctan2(sx, cx)
                pca_seed = np.array([rx0, ry0, rz0])
            else:
                pca_seed = np.zeros(3)

            # 多轮 DE + 局部精炼，取最优
            # 2026-07-31 修正: 曾缩减为 2轮×10×40 以省性能，实测带角度点云
            # （定模2、3次型_1 主轴跨度656）收敛不足，OBB 次优偏大
            # （218.7×161.3 vs 原版 218.2×160.0）→ 完全恢复原版参数（3×15×60）
            best_vol = float("inf")
            best_angles = None
            for run in range(3):
                result = differential_evolution(
                    volume_from_euler, bounds,
                    popsize=15, maxiter=60, tol=1e-12,
                    seed=42 + run, polish=False
                )
                if result.fun < best_vol:
                    best_vol = result.fun
                    best_angles = result.x

            # Nelder-Mead 局部精炼
            from scipy.optimize import minimize
            nm = minimize(volume_from_euler, best_angles,
                          method="Nelder-Mead",
                          options={"xatol": 1e-8, "fatol": 1e-12, "maxiter": 100})
            if nm.fun < best_vol:
                best_vol = nm.fun
                best_angles = nm.x
            elif best_vol > volume_from_euler(pca_seed):
                # DE 不如 PCA, 从 PCA 种子做 Nelder-Mead
                nm2 = minimize(volume_from_euler, pca_seed,
                               method="Nelder-Mead",
                               options={"xatol": 1e-8, "fatol": 1e-12, "maxiter": 100})
                if nm2.fun < best_vol:
                    best_vol = nm2.fun
                    best_angles = nm2.x

            rx, ry, rz = best_angles
            R_opt = _zyx_rot(rx, ry, rz)

            obb = self._build_obb(R_opt, self.center)
            obb["type"] = "DE-OBB"
            return obb
        except Exception as e:
            log.warning("DE OBB 优化失败（回退 PCA OBB）%s: %s", self.name, e)
            return None

    # ---------- 形状分类（增强版：多假设对比）----------

    def classify_shape(self):
        """PCA + 凸包 + 圆拟合 + 多假设对比 联合分类"""
        if self._shape_cache:
            return self._shape_cache

        ev = self.eigenvalues
        l1, l2, l3 = ev[0], ev[1], ev[2]
        total = l1 + l2 + l3

        if total < 1e-15:
            result = {"shape_cn": "退化/点状", "shape_en": "degenerate",
                      "confidence": 1.0, "details": {}}
            self._shape_cache = result
            return result

        # ---- 2026-08-11 投影特征分类（box=3矩形 / cylinder=1圆+2等宽矩形）----
        # 高置信提前返回：跳过 compute_cylinder 多假设等重计算，提升批量测量效率；
        # 低置信（含孔槽/带孔/异形）回退下方 PCA 启发式（历史修复分支保留）。
        proj = self._classify_by_projection()
        if proj is not None:
            p_en, p_cn, p_conf, p_reason, p_cyl = proj
            if p_conf >= PROJ_CONF_OVERRIDE:
                if p_cyl:
                    self._cylinder_cache = p_cyl   # format_spec 直接取数
                result = {
                    "shape_cn": p_cn, "shape_en": p_en,
                    "confidence": p_conf, "decision": p_reason,
                    "details": {"eigenvalues": [round(float(x), 4) for x in self.eigenvalues],
                                "proj_class": p_reason},
                }
                self._shape_cache = result
                return result

        r1, r2, r3 = l1 / total, l2 / total, l3 / total

        # 基础 PCA 指标
        sphere_uniformity = 1.0 - (l1 - l3) / (l1 + 1e-15)
        cylinder_axial = 1.0 - abs(l2 - l3) / max(l2, 1e-15)
        linearity = (l1 - l2) / (l1 + 1e-15)
        disk_radial = 1.0 - abs(l1 - l2) / max(l1, 1e-15)
        flatness = (l2 - l3) / (l2 + 1e-15)

        # ---- 多假设体积对比 ----
        box = self.compute_obb()
        cyl = self.compute_cylinder()
        box_vol = box["volume"]
        cyl_vol = cyl["volume"]

        vol_ratio = cyl_vol / max(box_vol, 1e-15)

        # 凸包填充率: 凸包体积 / OBB 体积
        if self._hull_vol > 0:
            hull_fill = self._hull_vol / max(box_vol, 1e-15)
        else:
            hull_fill = 1.0

        # OBB 尺寸比
        dims_obb = sorted([box["length"], box["width"], box["height"]], reverse=True)
        aspect_w = dims_obb[1] / max(dims_obb[0], 1e-9)
        aspect_h = dims_obb[2] / max(dims_obb[0], 1e-9)

        # ---- 圆环检测（扁平件的圆拟合）----
        circle_result = self._try_fit_circle()
        is_ring = circle_result is not None

        # ---- 决策 ----
        shape_cn = "立方体/长方体"
        shape_en = "box"
        confidence = BOX_DEFAULT_CONF
        decision_reason = "default"

        # 圆柱体高度比（用于区分圆柱 vs 圆环）
        cyl = self.compute_cylinder()
        cyl_ar = cyl["height"] / max(cyl["diameter"], 1e-9)  # 高径比

        if sphere_uniformity > SPHERE_UNIFORMITY_MIN:
            shape_cn, shape_en, confidence = "球体", "sphere", sphere_uniformity
            decision_reason = "sphere_PCA"
        elif cylinder_axial > CYL_AXIAL_MIN and linearity > CYL_LINEARITY_MIN:
            # 长圆柱（PCA 确认）→ 需圆形度校验
            is_circ, _ = self._is_cross_section_circular()
            if is_circ:
                if cyl_ar > CYL_ASPECT_RATIO:
                    shape_cn, shape_en = "圆柱体", "cylinder"
                else:
                    shape_cn, shape_en = "圆柱体 (短)", "cylinder"
                confidence = min(cylinder_axial, linearity * 1.2)
                decision_reason = "cylinder_PCA"
            else:
                # PCA 判断为圆柱但截面不圆 → 保持 box
                decision_reason = "cylinder_PCA_rejected_not_circular"
        elif disk_radial > DISK_RADIAL_MIN and flatness > DISK_FLATNESS_MIN:
            # 圆盘（PCA 径向对称 + 扁平）→ 需圆形度校验
            # 2026-07-31 修正: 圆盘的主轴(最大方差方向)在盘面内，截面检查必须用圆柱轴
            # (最小方差方向 eigenvectors[2])，否则 160×25 型截面会被误判为非圆
            is_circ, _ = self._is_cross_section_circular(self.eigenvectors[2])
            if is_circ:
                shape_cn, shape_en = "圆柱体 (圆盘)", "cylinder"
                confidence = min(disk_radial, flatness * 0.9)
                decision_reason = "disk_PCA"
            else:
                decision_reason = "disk_PCA_rejected_not_circular"
        elif linearity > LINEARITY_SLENDER and (pa := self._principal_axis_cylinder()):
            # 主轴1 长轴圆柱判定（2026-07-31 用户反馈修复: 带孔圆柱）——
            # 置于 circle_fit 之前：地侧支撑柱侧面螺钉孔会误导全局最小体积
            # 圆柱选孔方向为轴（盘状包络体积更小，Φ160×23 vs 正确 Φ100×130），
            # 截面呈矩形 → 误判圆环/长方体。真圆柱轴 = 主轴1（PCA 最大方差
            # = 长度方向，圆柱面采样保证），其截面最小包围圆直径 = 圆柱直径
            # （孔壁点在圆内不影响）。
            self._cylinder_cache = pa["cylinder"]  # 覆盖规格缓存供 format_spec 取数
            shape_cn, shape_en = pa["shape_cn"], "cylinder"
            confidence = pa["confidence"]
            decision_reason = "cylinder_principal_axis"
        elif is_ring and circle_result["confidence"] > RING_CONF_MIN:
            # PCA 未识别为圆柱，但圆拟合表明是环形件 → 需圆形度二次确认
            is_circ, _ = self._is_cross_section_circular()
            if not is_circ:
                decision_reason = "circle_fit_rejected_not_circular"
            elif cyl_ar > CYL_ASPECT_RATIO:
                shape_cn, shape_en = "圆柱体 (圆拟合)", "cylinder"
                confidence = circle_result["confidence"]
                decision_reason = "circle_fit_override"
            else:
                shape_cn, shape_en = "圆环/法兰", "cylinder"
                confidence = circle_result["confidence"]
                decision_reason = "circle_fit_override"
        elif linearity > LINEARITY_SLENDER:
            # 细长件：检测截面是否因特征（孔/槽）导致 OBB 失真
            # 注: 主轴1 长轴圆柱判定已在上方独立分支（cylinder_principal_axis）优先处理；
            # 此处仅处理"截面不圆"的细长件（含孔槽圆柱的 gap 检测）。
            gap_result = self._detect_cylinder_gap()
            if gap_result:
                shape_cn = "圆柱体 (含孔槽特征)"
                shape_en = "cylinder"
                confidence = gap_result["confidence"]
                decision_reason = "cylinder_gap_detected"
        else:
            if hull_fill < HULL_FILL_BOX_CONF:
                confidence = BOX_HULL_LOW_CONF
            decision_reason = "box_default"

        # 稀疏点云辅助判断（需圆形度二次确认）
        if self.N < SPARSE_N and shape_en == "box":
            cross_aspect = min(dims_obb[1], dims_obb[2]) / max(dims_obb[1], dims_obb[2], 1e-9)
            if cross_aspect > SPARSE_CROSS_ASPECT and cylinder_axial > SPARSE_CYL_AXIAL:
                is_circ, _ = self._is_cross_section_circular()
                if is_circ:
                    shape_cn, shape_en = "圆柱体 (近似)", "cylinder"
                    confidence = min(confidence, SPARSE_CONF)
                    decision_reason = "sparse_cylinder_heuristic"

        result = {
            "shape_cn": shape_cn,
            "shape_en": shape_en,
            "confidence": round(float(confidence), 3),
            "decision": decision_reason,
            "details": {
                "eigenvalues": [round(float(x), 4) for x in ev],
                "variance_ratios": [round(float(x), 3) for x in [r1, r2, r3]],
                "sphere_score": round(float(sphere_uniformity), 3),
                "cylinder_axial": round(float(cylinder_axial), 3),
                "disk_radial": round(float(disk_radial), 3),
                "linearity": round(float(linearity), 3),
                "flatness": round(float(flatness), 3),
                "box_vol": round(float(box_vol), 1),
                "cyl_vol": round(float(cyl_vol), 1),
                "vol_ratio": round(float(vol_ratio), 3),
                "hull_fill": round(float(hull_fill), 3),
                "obb_dims": dims_obb,
            }
        }
        self._shape_cache = result
        return result

    # ---------- 主轴1 长轴圆柱判定（带孔圆柱修复）----------

    def _principal_axis_cylinder(self):
        """主轴1 长轴圆柱判定（2026-07-31 用户反馈修复）。

        背景: 地侧支撑柱等带侧面螺钉孔的圆柱，全局最小体积圆柱会被孔误导——
        孔方向的盘状包络体积更小（实测: 错轴 Φ160×23 vol=46万 vs 正确轴
        Φ100×130 vol=102万），轴选错后截面呈矩形 → 误判为圆环/长方体。
        真圆柱轴 = 主轴1（PCA 最大方差 = 长度方向，圆柱面采样保证），其
        截面最小包围圆直径 = 圆柱直径（孔壁点在圆内，不影响直径）。

        触发条件（全部满足才判定）:
          1. 细长件: linearity > 0.50（主轴1 明显最长）
          2. 全局最小圆柱轴与主轴1 夹角 > 20°（孔/槽特征干扰信号；
             夹角小时原路径（gap 检测）已能正确识别，不越俎代庖）
          3. 截面（⊥主轴1）圆形度通过（R/major_half < 阈值）
          4. 高径比 h/(2r) > 1.2（长圆柱，排除盘类）

        返回 dict（cylinder 缓存覆盖值）或 None。
        """
        if self.eigenvalues[0] <= 0:
            return None
        linearity = (self.eigenvalues[0] - self.eigenvalues[1]) / self.eigenvalues[0]
        if linearity <= 0.50:
            return None
        cyl_global = self.compute_cylinder()
        axis_ang = np.degrees(np.arccos(np.clip(
            abs(np.dot(cyl_global["axis"], self.eigenvectors[0])), 0, 1)))
        if axis_ang <= 20.0:
            return None
        is_circ, _ = self._is_cross_section_circular(self.eigenvectors[0])
        if not is_circ:
            return None
        # 2026-08-13 排除薄板/细条：细长矩形截面 R≈长半宽也过圆形度检查，
        # 用 gap_ratio（R/短半宽）拦截——圆截面 ≈1~4，薄板 ≥6.7
        if self._cross_section_gap_ratio(self.eigenvectors[0]) > CYLINDER_GAP_RATIO_MAX_AXIS:
            return None
        vol1, r1, h1, *_ = self._evaluate_cylinder_axis(self.eigenvectors[0])
        if h1 <= 0 or h1 / max(2 * r1, 1e-9) <= 1.2:
            return None
        return {
            "shape_cn": "圆柱体 (长轴)",
            "confidence": round(float(min(0.9, linearity)), 3),
            "cylinder": {
                "axis": self.eigenvectors[0].tolist(),
                "radius": round(float(r1), 4),
                "height": round(float(h1), 4),
                "diameter": round(float(2 * r1), 4),
                "volume": round(float(vol1), 4),
                "center": self.center.tolist(),
                "refined": True,
            },
        }

    # ---------- 圆拟合（环/法兰检测）----------

    def _try_fit_circle(self):
        """
        对扁平零件尝试 2D 圆拟合（增强版：支持部分弧）。
        """
        if self._circle_cache is not None:
            return self._circle_cache

        box = self.compute_obb()
        aspect_h = box["height"] / max(box["length"], 1e-9)

        # 放宽扁平度阈值（环件允许更高）
        if aspect_h > 0.35:
            self._circle_cache = None
            return None

        # 2026-08-13 排除细长条/薄板，但保留细长杆（圆柱）：
        # 圆环/圆盘的盘面近圆形（宽≈长）→ 放行；细长杆盘面窄但截面近圆（gap_ratio 小）
        # → 放行；细长条/薄板盘面矩形且截面扁平（gap_ratio 大）→ 排除。
        aspect_w = box["width"] / max(box["length"], 1e-9)
        if aspect_w < 0.5 and \
                self._cross_section_gap_ratio(self.eigenvectors[0]) > CYLINDER_GAP_RATIO_MAX_AXIS:
            self._circle_cache = None
            return None

        flat_axis = self.eigenvectors[2]
        plane_u = self.eigenvectors[0]
        plane_v = self.eigenvectors[1]

        centered = self.points - self.center
        pts_2d = np.column_stack([centered @ plane_u, centered @ plane_v])

        try:
            center_2d, radius = _fit_circle_lsq(pts_2d)
        except Exception:
            self._circle_cache = None
            return None

        if radius < 1e-9:
            self._circle_cache = None
            return None

        dists = np.abs(np.linalg.norm(pts_2d - center_2d, axis=1) - radius)
        mean_err = dists.mean()
        max_err = dists.max()
        rel_mean = mean_err / radius
        rel_max = max_err / radius

        circle_diameter = 2 * radius
        obb_max_dim = max(box["length"], box["width"])

        # 判定是否可能是圆环/弧（放宽阈值支持部分弧）
        # 条件1: 相对拟合误差可接受
        # 条件2: 圆直径至少是 OBB 最大尺寸的 65%
        diam_ratio = circle_diameter / max(obb_max_dim, 1e-9)

        if rel_mean < 0.35 and rel_max < 0.70 and diam_ratio > 0.55:
            confidence = max(0.35, 1.0 - rel_mean * 2.5)
            result = {
                "radius": round(float(radius), 4),
                "diameter": round(float(circle_diameter), 4),
                "center_2d": center_2d.tolist(),
                "plane_normal": flat_axis.tolist(),
                "fit_error_mean": round(float(rel_mean), 4),
                "fit_error_max": round(float(rel_max), 4),
                "confidence": round(float(confidence), 4),
                "diam_ratio": round(float(diam_ratio), 4),
                "obb_max_dim": round(float(obb_max_dim), 4),
            }
            self._circle_cache = result
            return result

        self._circle_cache = None
        return None

    # ---------- 截面圆形度校验 ----------

    def _is_cross_section_circular(self, axis=None):
        """
        校验沿给定轴的截面是否呈圆形（而非矩形）。

        原理: 对截面做 OBB，计算最小包围圆半径 R 和 OBB 半宽。
        - 圆柱体: R ≈ max(OBB半宽)，包围圆相切于矩形边
        - 矩形块: R > max(OBB半宽)，包围圆通过矩形角点

        2026-07-31 修正: 截面 OBB 改用截面自身的 2D PCA 主轴投影——
        原实现直接取投影基 (u,v) 下的 max-min，矩形斜置时范围被高估
        (218×160 斜 45° 可到 263×232)，导致 R/半宽 被低估、圆形度误判。

        返回 (is_circular, R_over_maxhalf)
        """
        if axis is None:
            axis = self.eigenvectors[0]

        centered = self.points - self.center
        proj_axial = centered @ axis
        proj_plane = centered - np.outer(proj_axial, axis)

        u, v = _plane_basis(axis)
        pts_2d = np.column_stack([proj_plane @ u, proj_plane @ v])

        # 截面真实最小包围矩形: 2D PCA 主轴投影（2×2 eigh，开销可忽略）
        obb_x, obb_y = _cross_section_obb(pts_2d)
        major_half = max(obb_x, obb_y) / 2

        if major_half < 1e-9:
            return False, 0.0

        _, R = min_enclosing_circle_2d(pts_2d)
        R_over_maxhalf = R / major_half

        # 动态阈值：点越少越需要警惕，但不过度严格（稀疏采样天然有噪声）
        if self.N >= 200:
            threshold = 1.10
        else:
            threshold = 1.08  # <200点统一阈值，26点圆柱天然噪声~1.05

        is_circular = R_over_maxhalf < threshold

        return is_circular, R_over_maxhalf

    def _cross_section_gap_ratio(self, axis=None):
        """截面（⊥axis）gap_ratio = 最小包围圆半径 / 截面短半宽。

        圆截面 ≈1~2（稀疏采样会放大到 ~4），细长矩形/薄板 >>5。
        供长圆柱判定（_principal_axis_cylinder）排除薄板/细条。
        """
        if axis is None:
            axis = self.eigenvectors[0]
        centered = self.points - self.center
        proj_axial = centered @ axis
        proj_plane = centered - np.outer(proj_axial, axis)
        u, v = _plane_basis(axis)
        pts_2d = np.column_stack([proj_plane @ u, proj_plane @ v])
        obb_x, obb_y = _cross_section_obb(pts_2d)
        major = max(obb_x, obb_y)
        minor = min(obb_x, obb_y)
        if minor < 1e-9:
            return 1e9
        _, R = min_enclosing_circle_2d(pts_2d)
        return R / (minor / 2)

    # ---------- 投影特征分类（2026-08-11 系统性优化）----------

    def _projection_rect_stats(self, axis):
        """沿 axis 的正交投影统计 → (2D 主轴宽 w, 高 h, R_over_maxhalf)。

        投影平面由 axis 的垂直方向张成；w/h 为投影点集 2D PCA 主轴尺寸，
        R_over_maxhalf 为最小包围圆半径 / 主轴半宽——
          圆形投影 ≈ 1.0-1.12（噪声圆/实心圆盘）；矩形投影 > 1.0（角点在圆外，
          且长宽比越大 ratio 越大；靠 sq 方形度排除近方矩形）。
        """
        centered = self.points - self.center
        proj_axial = centered @ axis
        proj_plane = centered - np.outer(proj_axial, axis)
        u, v = _plane_basis(axis)
        pts_2d = np.column_stack([proj_plane @ u, proj_plane @ v])
        obb_x, obb_y = _cross_section_obb(pts_2d)
        major_half = max(obb_x, obb_y) / 2
        if major_half < 1e-9:
            return 0.0, 0.0, 0.0
        _, R = min_enclosing_circle_2d(pts_2d)
        return obb_x, obb_y, R / major_half

    def _radial_consistency_3d(self):
        """3D 点到中心距离一致性（std/mean）——区分球 vs 正方体。

        球（表面点）≈ 0.01-0.03；正方体（面心 a/2 ~ 顶点 √3a/2）≈ 0.17+。
        正方体 PCA 各向同性 → 主轴任意 → 3 个斜投影呈伪圆，需此校验拒绝。
        """
        d = np.linalg.norm(self.points - self.center, axis=1)
        return float(d.std() / max(d.mean(), 1e-9))

    def _classify_by_projection(self):
        """正交投影特征分类（2026-08-11 系统性优化，替代纯 PCA 启发式主判定）。

        沿 3 个正交主轴（PCA eigenvectors）投影，逐个判定圆形/矩形：
          - 3 矩形          → box
          - 1 圆 + 2 矩形   → cylinder（校验两矩形等宽侧 = 直径、另侧 = 高度）
          - 3 等径圆        → sphere
          - 混合异常        → None（回退现有启发式，保留历史修复分支）
        返回 (shape_en, shape_cn, confidence, reason, cylinder_or_None)。

        2026-08-13 P0-1 修复: 稀疏点云（N < PROJ_SPARSE_N，真实 STP 边界折点
        仅 16~100 个）置信度封顶在 PROJ_CONF_FLOOR 之下——稀疏折点的圆形投影
        判定不可靠（proj_3rect 误判圆柱、投影尺寸失准），不提前返回，交给
        PCA+圆形度启发式与 30 方向最小体积圆柱取精确尺寸。
        """
        sparse = self.N < PROJ_SPARSE_N
        stats = []
        for ax in (self.eigenvectors[0], self.eigenvectors[1],
                   self.eigenvectors[2]):
            w, h, ratio = self._projection_rect_stats(ax)
            sq = abs(w - h) / max(w, h, 1e-9)
            # 圆判定：包围圆贴合（ratio 容忍噪声圆）+ 方形度（sq 排除矩形）。
            # 圆柱端面/球面是实心圆盘投影（ratio≈1.02-1.12、sq≈0）→ 判圆；
            # 矩形投影 sq 大（长宽比 → 0.5+）→ 判矩；正方形斜投影由
            # sphere 分支的 3D 径向一致性兜底拒绝（见下）。
            circ = ratio > 0 and ratio < PROJ_CIRC_RATIO and sq < PROJ_CIRC_SQUARE
            stats.append({"w": w, "h": h, "ratio": ratio, "circ": circ})
        n_circ = sum(s["circ"] for s in stats)

        if n_circ == 0:      # 3 矩形 → box
            # "3 个正交投影全非圆"本身是 box 的充分特征（圆柱必有 1 个圆端面投影），
            # 置信度固定高值——不受矩形长宽比（ratio 随长宽比增大）的贴合度惩罚。
            # 但该结论依赖"圆投影可被识别"——稀疏折点下不成立（2026-08-13）。
            conf = 0.92 if not sparse else PROJ_CONF_FLOOR
            return "box", "立方体/长方体", conf, "proj_3rect", None

        if n_circ == 1:      # 1 圆 + 2 矩形 → cylinder（等宽校验）
            circ_i = next(i for i, s in enumerate(stats) if s["circ"])
            # 直径 = 圆投影尺寸（端面圆）；矩形投影两个值 = {直径, 高度} 顺序不定
            # （长圆柱 高>径、圆盘 高<径）——用"接近圆投影尺寸"的一侧判定直径
            d = (stats[circ_i]["w"] + stats[circ_i]["h"]) / 2
            h_sides, d_dev = [], 0.0
            for r in (stats[i] for i in range(3) if i != circ_i):
                vals = (r["w"], r["h"])
                side_d = min(vals, key=lambda x: abs(x - d))
                side_h = max(vals, key=lambda x: abs(x - d))
                h_sides.append(side_h)
                d_dev = max(d_dev, abs(side_d - d) / max(d, 1e-9))
            hgt = (h_sides[0] + h_sides[1]) / 2
            hh = abs(h_sides[0] - h_sides[1]) / max(hgt, 1e-9)
            if d_dev <= PROJ_SAME_TOL and hh <= PROJ_SAME_TOL:
                conf = min(0.97, max(PROJ_CONF_FLOOR, 1.0 - max(d_dev, hh)))
                if sparse:
                    conf = min(conf, PROJ_CONF_FLOOR)
                cyl = {"diameter": d, "height": hgt,
                       "volume": np.pi * (d / 2) ** 2 * hgt}
                return ("cylinder", "圆柱体", round(conf, 3),
                        "proj_1circ_2rect", cyl)
            # 两矩形不等宽 → 近似圆柱（锥台/异形），低置信 → 调用方回退
            cyl = {"diameter": d, "height": hgt,
                   "volume": np.pi * (d / 2) ** 2 * hgt}
            return ("cylinder", "圆柱体 (近似)", round(PROJ_CONF_FLOOR, 3),
                    "proj_cyl_approx", cyl)

        if n_circ == 3:      # 3 圆 → 候选 sphere（3D 径向一致性兜底）
            ds = [(s["w"] + s["h"]) / 2 for s in stats]
            spread = (max(ds) - min(ds)) / max(max(ds), 1e-9)
            radial3d = self._radial_consistency_3d()
            if spread < PROJ_SAME_TOL and radial3d < PROJ_SPHERE_RADIAL:
                conf = min(0.97, max(PROJ_CONF_FLOOR, 1.0 - spread))
                return "sphere", "球体", round(conf, 3), "proj_3circ", None
            # 3 个"圆"投影但 3D 点距分散（如正方体 PCA 斜投影伪圆）→ box（强结论）
            return "box", "立方体/长方体", 0.90, "proj_3circ_rejected_box", None

        return None          # 混合异常（2 圆 1 矩等）→ 回退启发式

    # ---------- 圆柱体间隙检测（孔/槽特征导致截面 OBB 失真）----------

    def _detect_cylinder_gap(self):
        """
        对细长零件检测截面是否存在"缺失区域"（如螺钉孔、槽）。

        两步验证:
          1. 间隙比: R / min(OBB半宽) > 1.15 → 可能有缺失
          2. 圆形度: R / max(OBB半宽) < 1.10 → 截面是圆而非矩形

        Returns dict or None
        """
        cyl = self.compute_cylinder()
        cyl_axis = np.array(cyl["axis"])
        cyl_r = cyl["radius"]

        # 第一步: 间隙比检查
        centered = self.points - self.center
        proj_axial = centered @ cyl_axis
        proj_plane = centered - np.outer(proj_axial, cyl_axis)

        u, v = _plane_basis(cyl_axis)
        pts_2d = np.column_stack([proj_plane @ u, proj_plane @ v])

        # 截面真实最小包围矩形（2D PCA，见 _is_cross_section_circular 修正说明）
        obb_x, obb_y = _cross_section_obb(pts_2d)
        major_half = max(obb_x, obb_y) / 2
        minor_half = min(obb_x, obb_y) / 2

        if major_half < 1e-9:
            return None

        gap_ratio = cyl_r / max(minor_half, 1e-9)

        if gap_ratio <= 1.15:
            return None
        # 2026-08-13 排除薄板：薄板截面 minor_half≈0 使 gap_ratio 巨大（复位杆调整板
        # 32 万+、压线板A 4.6），而真圆柱含孔槽仅 1.2~3——设上限拦截（比宽高比更可靠，
        # 稀疏圆柱 16 点宽高比可低至 0.3 但 gap_ratio 仍 ~1.6）
        if gap_ratio > CYLINDER_GAP_RATIO_MAX:
            return None

        # 第二步: 圆形度校验（排除矩形块）
        is_circular, R_ratio = self._is_cross_section_circular(cyl_axis)
        if not is_circular:
            return None

        confidence = min(0.85, (gap_ratio - 1.0) * 0.8)
        return {
            "radius": round(float(cyl_r), 4),
            "diameter": round(float(2 * cyl_r), 4),
            "obb_cross": [round(float(obb_x), 1), round(float(obb_y), 1)],
            "gap_ratio": round(float(gap_ratio), 3),
            "circularity": round(float(R_ratio), 3),
            "confidence": round(float(confidence), 3),
        }

    # ---------- 最小体积圆柱体 ----------

    def _evaluate_cylinder_axis(self, axis):
        """对给定轴方向计算包围圆柱体体积、半径、高度

        2026-07-31 适配: 用凸包顶点投影代替全点投影——凸包的凸性保证
        包围圆/轴向极值与全点等价，但计算量从 O(N) 降至 O(h)（约快 70 倍）。
        """
        pts_centered = self._hull_pts - self.center
        axis = axis / np.linalg.norm(axis)
        proj_axial = pts_centered @ axis
        h_min, h_max = proj_axial.min(), proj_axial.max()
        height = h_max - h_min
        if height < 1e-12:
            # 2026-08-11 修复: 扁平/退化实体（薄片如 O型圈、厚度≈0）此前只返回
            # 3 个值，调用方按 7 值解包 → "not enough values to unpack (expected 7, got 3)"
            # 批量"无实体"。补齐为 7 值（volume=inf 自然被最小体积候选跳过）。
            return float("inf"), 0, 0, None, None, None, None

        proj_plane = pts_centered - np.outer(proj_axial, axis)
        u, v = _plane_basis(axis)
        pts_2d = np.column_stack([proj_plane @ u, proj_plane @ v])
        circle_center_2d, radius = min_enclosing_circle_2d(pts_2d)
        volume = np.pi * radius * radius * height

        # 也返回细节用于重建完整结果
        return volume, radius, height, circle_center_2d, proj_axial, u, v

    def compute_cylinder(self, n_candidates=30):
        # 2026-07-31 修正: 曾缩减方向候选/NM 迭代以省性能，实测影响带角度点云
        # 的收敛精度（合成噪声圆柱 Φ72→Φ74）→ 恢复原版参数（30 方向、NM 60）
        if self._cylinder_cache:
            return self._cylinder_cache

        center = self.center
        pts_centered = self.points - center

        candidates = list(self.eigenvectors)
        if n_candidates > 3:
            candidates.extend(fibonacci_sphere(n_candidates - 3))

        best = {"volume": float("inf")}

        for axis_raw in candidates:
            axis = axis_raw / np.linalg.norm(axis_raw)
            volume, radius, height, cc_2d, proj_axial, u, v = self._evaluate_cylinder_axis(axis)

            if volume < best["volume"]:
                cyl_center_3d = center + (proj_axial.mean() * axis +
                    cc_2d[0] * u + cc_2d[1] * v)
                best = {
                    "axis": axis.tolist(),
                    "radius": round(float(radius), 4),
                    "height": round(float(height), 4),
                    "diameter": round(float(2 * radius), 4),
                    "volume": round(float(volume), 4),
                    "center": cyl_center_3d.tolist(),
                }

        # ---- 局部精炼: Nelder-Mead 2D 优化轴方向 ----
        axis0 = np.array(best["axis"])
        theta0 = np.arctan2(axis0[1], axis0[0])
        phi0 = np.arccos(np.clip(axis0[2], -1.0, 1.0))

        def _cost_spherical(params):
            th, ph = params
            ax = np.array([np.sin(ph) * np.cos(th),
                           np.sin(ph) * np.sin(th),
                           np.cos(ph)])
            vol, _, _, _, _, _, _ = self._evaluate_cylinder_axis(ax)
            return vol

        from scipy.optimize import minimize
        try:
            res = minimize(_cost_spherical, [theta0, phi0],
                           method="Nelder-Mead",
                           options={"xatol": 1e-7, "fatol": 1e-10, "maxiter": 60})
            th_opt, ph_opt = res.x
            ax_opt = np.array([np.sin(ph_opt) * np.cos(th_opt),
                               np.sin(ph_opt) * np.sin(th_opt),
                               np.cos(ph_opt)])
            ax_opt = ax_opt / np.linalg.norm(ax_opt)

            vol_ref, r_ref, h_ref, cc_ref, pa_ref, u_ref, v_ref = self._evaluate_cylinder_axis(ax_opt)

            if vol_ref < best["volume"] - 1e-8:
                cyl_c = center + (pa_ref.mean() * ax_opt +
                                  cc_ref[0] * u_ref + cc_ref[1] * v_ref)
                best = {
                    "axis": ax_opt.tolist(),
                    "radius": round(float(r_ref), 4),
                    "height": round(float(h_ref), 4),
                    "diameter": round(float(2 * r_ref), 4),
                    "volume": round(float(vol_ref), 4),
                    "center": cyl_c.tolist(),
                    "refined": True,
                }
        except Exception as e:
            log.warning("圆柱 NM 精炼失败（保留粗搜索）%s: %s", self.name, e)
            pass  # 精炼失败则保留粗搜索结果

        self._cylinder_cache = best
        return best


# ============================================================================
#  2D 圆拟合（最小二乘代数法）
# ============================================================================

def _fit_circle_lsq(points_2d):
    """最小二乘代数圆拟合: minimize Σ((x²+y² + ax + by + c)²)"""
    x, y = points_2d[:, 0], points_2d[:, 1]
    A = np.column_stack([x, y, np.ones(len(x))])
    b = -(x**2 + y**2)
    sol, residuals, rank, sv = np.linalg.lstsq(A, b, rcond=None)
    cx = -sol[0] / 2.0
    cy = -sol[1] / 2.0
    r = np.sqrt(max(0, cx**2 + cy**2 - sol[2]))
    return np.array([cx, cy]), r


# ============================================================================
#  测试用点云生成器（迁移自 pointcloud_analyzer，仅用于单元测试/演示）
# ============================================================================

def generate_box_points(size=(10, 8, 6), n_points=2000, noise=0.05, rotation_deg=None):
    """
    生成长方体表面点云。

    Parameters
    ----------
    size : tuple (L, W, H)
    n_points : int
    noise : float 高斯噪声标准差
    rotation_deg : tuple (rx, ry, rz) 绕各轴旋转角度
    """
    L, W, H = size
    half = np.array([L, W, H]) / 2

    # 6 个面, 按实际面积加权分配点数
    # 2026-07-31 修正: 原 areas 配错（+X 面写成 L*W），导致点数失衡、PCA 主轴偏移
    faces = [
        (0,  half[0], (1, 2)),   # +X 面: W*H
        (0, -half[0], (1, 2)),   # -X 面: W*H
        (1,  half[1], (0, 2)),   # +Y 面: L*H
        (1, -half[1], (0, 2)),   # -Y 面: L*H
        (2,  half[2], (0, 1)),   # +Z 面: L*W
        (2, -half[2], (0, 1)),   # -Z 面: L*W
    ]
    areas = [W*H, W*H, L*H, L*H, L*W, L*W]
    total_area = sum(areas)
    face_points = [max(1, int(n_points * a / total_area)) for a in areas]

    # 调整到精确 n_points
    diff = n_points - sum(face_points)
    for i in range(abs(diff)):
        face_points[i % 6] += 1 if diff > 0 else -1

    points = []
    rng = np.random.RandomState(42)
    for (fixed_axis, fixed_val, (a1, a2)), fp in zip(faces, face_points):
        p = np.zeros((fp, 3))
        p[:, a1] = rng.uniform(-half[a1], half[a1], fp)
        p[:, a2] = rng.uniform(-half[a2], half[a2], fp)
        p[:, fixed_axis] = fixed_val
        points.append(p)

    points = np.vstack(points)

    # 加噪
    if noise > 0:
        points += rng.normal(0, noise, points.shape)

    # 旋转
    if rotation_deg:
        rx, ry, rz = np.radians(rotation_deg)
        R = (make_rotation_matrix([1, 0, 0], rx) @
             make_rotation_matrix([0, 1, 0], ry) @
             make_rotation_matrix([0, 0, 1], rz))
        points = points @ R.T

    return points


def generate_cylinder_points(radius=5, height=20, n_points=2000, noise=0.05, rotation_deg=None):
    """
    生成圆柱体表面点云 (含侧面 + 顶底两面)。

    Parameters
    ----------
    radius : float
    height : float
    n_points : int
    noise : float
    rotation_deg : tuple (rx, ry, rz)
    """
    rng = np.random.RandomState(42)
    half_h = height / 2

    # 面积: 侧面 = 2π·r·h, 顶+底 = 2·π·r²
    area_side = 2 * np.pi * radius * height
    area_caps = 2 * np.pi * radius * radius
    total_area = area_side + area_caps

    n_side = max(1, int(n_points * area_side / total_area))
    n_caps = max(1, int((n_points - n_side) / 2))

    # 侧面
    theta = rng.uniform(0, 2 * np.pi, n_side)
    z = rng.uniform(-half_h, half_h, n_side)
    side_pts = np.column_stack([
        radius * np.cos(theta),
        radius * np.sin(theta),
        z
    ])

    # 顶面 + 底面
    caps_pts = []
    for z_val in [half_h, -half_h]:
        r_samples = np.sqrt(rng.uniform(0, 1, n_caps)) * radius
        theta_cap = rng.uniform(0, 2 * np.pi, n_caps)
        cap = np.column_stack([
            r_samples * np.cos(theta_cap),
            r_samples * np.sin(theta_cap),
            np.full(n_caps, z_val)
        ])
        caps_pts.append(cap)

    points = np.vstack([side_pts] + caps_pts)

    if noise > 0:
        points += rng.normal(0, noise, points.shape)

    if rotation_deg:
        rx, ry, rz = np.radians(rotation_deg)
        R = (make_rotation_matrix([1, 0, 0], rx) @
             make_rotation_matrix([0, 1, 0], ry) @
             make_rotation_matrix([0, 0, 1], rz))
        points = points @ R.T

    return points


# ============================================================================
#  高层接口（bom_export 规格测量链路使用）
# ============================================================================

def analyze_points(points, name="未命名实体"):
    """
    对单个点集做完整形状分析（形状分类 + OBB + 最小圆柱）。

    Parameters
    ----------
    points : array-like (N, 3) 实体的顶点坐标
    name : str 实体名（仅用于诊断）

    Returns
    -------
    dict or None（点数 < 3 或形状退化时返回 None）
        {
          "shape_en": "box"|"cylinder"|"sphere"|"degenerate",
          "shape_cn": 中文形状名,
          "confidence": 置信度,
          "decision": 决策依据,
          "obb": OBB dict（length/width/height/volume/...）,
          "cylinder": 最小圆柱 dict（diameter/height/volume/...）,
        }
    """
    pts = np.asarray(points, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[1] != 3 or len(pts) < 3:
        return None

    a = PointCloudAnalyzer(pts, name=name)
    shape = a.classify_shape()
    if shape["shape_en"] == "cylinder":
        # 2026-08-11: 圆柱规格完全由 cylinder dict 表达——投影分类提前返回时
        # _cylinder_cache 已置（跳过 30 方向多假设搜索）；未提前的路径由
        # classify_shape 原逻辑填充缓存。OBB 对圆柱不必要（含 DE 优化），跳过提速。
        return {
            "shape_en": shape["shape_en"],
            "shape_cn": shape["shape_cn"],
            "confidence": shape["confidence"],
            "decision": shape.get("decision", ""),
            "obb": None,
            "cylinder": (a._cylinder_cache if a._cylinder_cache
                         else a.compute_cylinder()),
        }
    # 2026-08-11: 投影分类确认 box 后仍用全量 OBB（DE 优化保证旋转体精度——
    # fast PCA-OBB 旋转体偏差实测 7.6%，不可接受）；效率收益由圆柱路径贡献
    # （投影直接给直径/高度，跳过 30 方向搜索 + OBB，提速约百倍）
    return {
        "shape_en": shape["shape_en"],
        "shape_cn": shape["shape_cn"],
        "confidence": shape["confidence"],
        "decision": shape.get("decision", ""),
        "obb": a.compute_obb(),
        "cylinder": None,
    }


def analysis_volume(analysis):
    """
    包围体体积：圆柱/圆环件取最小圆柱体积，其余取 OBB 体积。
    用于多实体场景选"最紧包围"（体积最小者）。
    """
    if analysis["shape_en"] == "cylinder":
        return analysis["cylinder"]["volume"]
    return analysis["obb"]["volume"]


def format_spec(analysis):
    """
    规格字符串格式化。
    - 圆柱/圆环/孔槽圆柱（shape_en == "cylinder"）→ Φ{直径}×{长度}
    - 其余（长方体/球体等）→ L*W*H（降序）

    2026-08-13 变更：保留 1 位小数，小数为 0 则只显示整数（40.0→40、79.5→79.5、1.9→1.9）。
    """
    def _fmt(x):
        x = round(float(x), 1)  # 先量化到 0.1 消除浮点噪声
        if x == int(x):
            return str(int(x))
        return f"{x:.1f}"

    if analysis["shape_en"] == "cylinder":
        d = analysis["cylinder"]["diameter"]
        h = analysis["cylinder"]["height"]
        return f"Φ{_fmt(d)}×{_fmt(h)}"
    obb = analysis["obb"]
    dims = sorted([obb["length"], obb["width"], obb["height"]], reverse=True)
    return "*".join(_fmt(x) for x in dims)
