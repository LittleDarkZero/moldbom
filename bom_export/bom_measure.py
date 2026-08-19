# -*- coding: utf-8 -*-
"""BomExport 规格测量模块（2026-08-18 重构自 bom_export.py 模块3）。

STP 拓扑解析 → 逐实体顶点提取（BFS）→ geometry_engine 形状分析
（PCA/DE/NM 分类 + OBB + 最小圆柱）→ 规格字符串；多进程并行
（ProcessPoolExecutor），含面级 B-rep 交叉验证与退化点云兜底。
"""

import os
import re
from concurrent.futures import ProcessPoolExecutor, as_completed

import geometry_engine

from bom_common import log
from bom_infer import _apply_spec_gr_v2

# 1.2 性能: 避免重复读文件
_stp_entity_cache = {}


def _parse_stp_entities(stp_path: str) -> tuple:
    """解析 STP → (entities, cartesian_points)。1.1: 纳入 Vertex + Control Point"""
    if stp_path in _stp_entity_cache:
        return _stp_entity_cache[stp_path]

    with open(stp_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()

    entities = {}
    for m in re.finditer(r'#(\d+)\s*=\s*(\w+)\s*\((.*?)\)\s*;', content, re.DOTALL):
        entities[int(m.group(1))] = (m.group(2), m.group(3))

    # 筛选 Vertex + Control Point
    cartesian_points = {}
    num_re = r'[\d.eE+\-]+'
    coord_re = re.compile(rf'\(\s*({num_re})\s*,\s*({num_re})\s*,\s*({num_re})\s*\)')
    for eid, (etype, args) in entities.items():
        if etype != 'CARTESIAN_POINT':
            continue
        # 提取名称
        name_match = re.match(r"\s*'([^']*)'", args)
        if not name_match:
            continue
        name = name_match.group(1)
        if name not in ('Vertex', 'Control Point'):
            continue
        # 提取坐标
        cm = coord_re.search(args)
        if cm:
            try:
                cartesian_points[eid] = (float(cm.group(1)), float(cm.group(2)), float(cm.group(3)))
            except ValueError:
                pass

    _stp_entity_cache[stp_path] = (entities, cartesian_points)
    return (entities, cartesian_points)


def _parse_stp_fallback(stp_path: str) -> list:
    """直接正则提取 CARTESIAN_POINT 坐标（不依赖实体解析），返回点列表"""
    with open(stp_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    num_r = r'[\d.eE+\-]+'
    pattern = re.compile(
        rf"CARTESIAN_POINT\s*\('((?:Vertex|Control Point))'\s*,\s*\(\s*({num_r})\s*,\s*({num_r})\s*,\s*({num_r})\s*\)\s*\)",
        re.IGNORECASE
    )
    pts = []
    for m in pattern.finditer(content):
        if m.group(1) in ('Vertex', 'Control Point'):
            try:
                pts.append((float(m.group(2)), float(m.group(3)), float(m.group(4))))
            except ValueError:
                pass
    return pts


def _points_to_spec_str(pts) -> str:
    """点列表 → AABB 规格字符串 "L*W*H"（降序整数 mm）。返回空字符串当无效。"""
    if not pts: return ""
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]; zs = [p[2] for p in pts]
    dims = sorted([max(xs)-min(xs), max(ys)-min(ys), max(zs)-min(zs)], reverse=True)
    if dims[0] <= 0: return ""
    return "{}*{}*{}".format(int(round(dims[0])), int(round(dims[1])), int(round(dims[2])))


def extract_aabb_from_stp(stp_path):
    try:
        _, pts = _parse_stp_entities(stp_path)
        return _points_to_spec_str(list(pts.values())) if pts else _points_to_spec_str(_parse_stp_fallback(stp_path))
    except Exception:  # noqa: BLE001 测量兜底不抛
        return ""


def _extract_solid_points(entities, pts_map, solid_args):
    """BFS 拓扑遍历单个 MANIFOLD_SOLID_BREP，提取实体顶点坐标（不含辅助几何）。

    2026-07-31 迁移: 自旧 _measure_multisolid_stp 抽出，供新规格测量引擎
    (geometry_engine) 逐实体分析使用；deque 替代 list.pop(0) 消除 O(n^2)。
    """
    from collections import deque
    queue = deque(int(m) for m in re.findall(r'#(\d+)', solid_args))
    visited = set()
    vertex_ids = set()
    while queue:
        rid = queue.popleft()
        if rid in visited or rid not in entities:
            continue
        visited.add(rid)
        rt, ra = entities[rid]
        if rt == 'VERTEX_POINT':
            vertex_ids.add(rid)
        elif rt != 'CARTESIAN_POINT':
            queue.extend(int(m) for m in re.findall(r'#(\d+)', ra))
    pts = []
    for vid in vertex_ids:
        for pr in re.findall(r'#(\d+)', entities[vid][1]):
            pid = int(pr)
            if pid in pts_map:
                pts.append(pts_map[pid])
                break
    return pts


def _cleanup_stp_artifacts(results: list):
    """清理所有残留的 STP 临时文件并清空实体缓存。异常路径也必须调用。"""
    for item in results:
        stp = item.get("_stp_path", "")
        if stp and os.path.exists(stp):
            try: os.remove(stp)
            except OSError: pass
        item["_stp_path"] = ""
    _stp_entity_cache.clear()


def _measure_one_spec(args):
    """测量一个零件的全部实体规格（多进程 worker，纯本地、无 CATIA 依赖）。

    args = (stp_path, solid_count)；返回 (规格列表, "")——加工备注统一由 V2 remark 域生成，
    测量不再产出"含X个实体"备注（2026-08-13 用户决策）。

    2026-07-31 迁移: 规格测量引擎替换为 geometry_engine（源自 pointcloud_analyzer）——
    STP 拓扑 BFS 取各实体顶点 → 逐实体形状分析（分类 + OBB + 最小圆柱）。
    2026-08-11 变更: 返回【逐实体规格列表】（不再只取最紧包围单值）——
    同一 Body 多实体尺寸各异时，每个实体单独规格化（"L*W*H" / "Φ直径×长度"），
    由主进程计数合并输出（2-1*2*3\\n1-2*2*3）。失败时 AABB 正则兜底（单值列表）。
    """
    stp_path, solid_count = args
    if solid_count == 0:
        return ([], "STP导出重试3次仍失败")
    try:
        entities, pts_map = _parse_stp_entities(stp_path)
    except Exception:
        entities, pts_map = {}, {}
    if not entities:
        aabb = extract_aabb_from_stp(stp_path)
        return ([aabb] if aabb else [], "")

    # 2026-08-13: 面级 B-rep 交叉验证（惰性加载）。两类场景用面级纠正点云法：
    # 1) 退化点云（复位杆调整板圆盘只导出单面共面顶点，厚度丢失）；
    # 2) 带孔方板（点云法把 Φ大孔当主体误判成圆柱，面级读面类型能区分方板带孔 vs 真圆柱）。
    face_solids = None

    def _face_bbox(eid):
        nonlocal face_solids
        if face_solids is None:
            face_solids = {}
            try:
                import stp_features
                ex = stp_features.StpFeatureExtractor(stp_path)
                for s in ex.extract_solids():
                    try:
                        face_solids[s["id"]] = ex.compute_bbox(s)
                    except Exception:
                        pass
            except Exception:
                pass
        return face_solids.get(eid)

    spec_list = []
    for eid, (etype, sargs) in entities.items():
        if etype != 'MANIFOLD_SOLID_BREP':
            continue
        pts = _extract_solid_points(entities, pts_map, sargs)
        if len(pts) < 3:
            continue
        analysis = geometry_engine.analyze_points(pts)
        if analysis is None:
            continue
        spec = geometry_engine.format_spec(analysis)
        shape = analysis.get("shape_en")
        # 交叉验证 1：点云法判圆柱，但面级判 box（带孔方板）→ 用面级纠正
        if shape == "cylinder":
            fb = _face_bbox(eid)
            if fb and fb.get("shape") == "box" and fb.get("spec"):
                spec = fb["spec"]
        # 交叉验证 2：退化判定（OBB 最小维 < 1%，共面/零厚度，点云无法表征 3D 形状）
        obb = analysis.get("obb")
        if obb and isinstance(obb, dict):
            dims = sorted([obb.get("length", 0), obb.get("width", 0),
                           obb.get("height", 0)], reverse=True)
            if dims[0] > 1e-9 and dims[2] / dims[0] < 0.01:
                fb = _face_bbox(eid)
                if fb and fb.get("spec"):
                    spec = fb["spec"]
        if spec:
            spec_list.append(spec)

    if not spec_list:
        aabb = extract_aabb_from_stp(stp_path)
        return ([aabb] if aabb else [], "")

    return (spec_list, "")


def _format_spec_counts(spec_list) -> str:
    """规格计数合并输出（2026-08-11 用户需求）：相同规格计数合并、异规格分行。

    ['1*2*3', '1*2*3', '2*2*3'] → '2-1*2*3\\n1-2*2*3'
    每行 "数量-规格"，顺序按规格首次出现；
    **只有一种规格时不加前缀**（实体数由数量列承载）：
    ['1*2*3', '1*2*3'] → '1*2*3'，['40*60*12'] → '40*60*12'
    """
    from collections import Counter
    if not spec_list:
        return ""
    cnt = Counter(spec_list)
    if len(cnt) == 1:
        return next(iter(cnt))
    return "\n".join(f"{n}-{s}" for s, n in cnt.items())


def fill_specs_from_stp(results: list) -> list:
    """批量填充规格（多进程并行），跳过外购/标准件等无测量意义的零件。

    OBB 为纯 CPU 计算无 CATIA 依赖，用 ProcessPoolExecutor 并行。
    try/finally 保证中途异常也清理临时文件与缓存。

    跳过逻辑（2026-08-13 V2 化）：
      - V2 measure 域 skipMeasurement（自旧 spec_skip_gr 迁移：紧固件/热流道）
      - 无 STP（外购件 purchaseFixedQty / 导出失败）
    """
    need_measure = []
    for item in results:
        stp = item.get("_stp_path", "")
        v2_out = item.get("_v2") or {}
        if v2_out.get("skipMeasurement"):
            log.info("规格测量跳过(V2 measure): %s（%s）",
                     item.get("零部件名"), v2_out.get("measureSkipReason", "未说明"))
            continue
        if not stp or not os.path.exists(stp):
            continue
        # ⑥ nameSpec 名称读型号（2026-08-10）：零件名含型号 → 直接当规格，跳过测量
        # （标准件如『油缸 BOD-AG-63-50-V』，省 STP 导出 + OBB 计算）
        from v2_bridge import extract_name_spec
        ns = extract_name_spec(item.get("零部件名", ""))
        if ns:
            item["规格"] = ns
            n_ent = 1
            try:
                n_ent = max(1, int(item.get("数量", 1) or 1))
            except (TypeError, ValueError):
                n_ent = 1
            # 2026-08-11: 标准件多实体同型号 → 计数合并输出（N-型号）
            item["_spec_list"] = [ns] * n_ent
            item["规格"] = _format_spec_counts(item["_spec_list"])
            log.info("nameSpec 名称读型号: %s → %s（%d 实体，跳过测量）",
                     item.get("零部件名"), ns, n_ent)
            try:
                os.remove(stp)
            except OSError:
                pass
            item["_stp_path"] = ""
            continue
        need_measure.append(item)

    total = len(need_measure)
    if total == 0:
        _cleanup_stp_artifacts(results)
        return _apply_spec_gr_v2(results)
    log.info("STP OBB 规格提取 (%d 个, 并行)...", total)

    try:
        tasks = [(item["_stp_path"], item.get("数量", 1), idx, item["零部件名"])
                 for idx, item in enumerate(need_measure)]
        completed = 0
        # 2026-08-19: 进程数设上限（每进程都要 import numpy/scipy，过多会吃满内存）
        with ProcessPoolExecutor(max_workers=min(total, os.cpu_count() or 4, 8)) as pool:
            futures = {pool.submit(_measure_one_spec, (t[0], t[1])): t for t in tasks}
            for future in as_completed(futures):
                stp_path, solid_count, idx, name = futures[future]
                completed += 1
                try:
                    spec_list, remark = future.result()
                except Exception as e:
                    spec_list, remark = ([], "")
                    log.warning("  [%d/%d] %s: 并行测量异常 %s", completed, total, name, e)
                item = need_measure[idx]
                # 2026-08-11: 逐实体规格列表 → 计数合并输出；_spec_list 供 V2 逐个匹配
                item["_spec_list"] = spec_list
                item["规格"] = _format_spec_counts(spec_list)
                if remark:
                    item["加工备注"] = remark
                try: os.remove(stp_path)
                except OSError: pass
                item["_stp_path"] = ""
                log.info("  [%d/%d] %s: %s", completed, total, name,
                         item["规格"] if item["规格"] else "无实体")
    finally:
        _cleanup_stp_artifacts(results)
    return _apply_spec_gr_v2(results)
