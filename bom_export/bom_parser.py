# -*- coding: utf-8 -*-
"""BomExport CATPart 解析主流程模块（2026-08-18 重构自 bom_export.py 模块4）。

打开 CATPart → 全显示 Body/Shape → 逐 Body 过滤（PartBody/布尔件/V2 filter）
→ V2 推理 GR + 导出 STP 计数 → 组装结果行。
"""

import os
import re

from bom_common import log, as_part_document
from bom_infer import infer_gr_and_detail
from bom_stp import export_body_to_stp_and_count


def _show_all_bodies(doc, bodies):
    """全选所有 Body + Shape，一次 SetShow(0) 全部设为显示。

    SetShow(0) 是设置操作（非翻转）：将所有选中的元素设为 (0,0) 可见态。
    Body 和 Shape 是两层独立可见性，必须都选中再操作。
    """
    sel = doc.Selection
    sel.Clear()

    # 全选所有 Body
    for i in range(1, bodies.Count + 1):
        try:
            sel.Add(bodies.Item(i))
        except Exception:
            pass

    # 全选所有 Body 内的 Shape（几何体）
    for i in range(1, bodies.Count + 1):
        try:
            shapes = bodies.Item(i).Shapes
            for j in range(1, shapes.Count + 1):
                try:
                    sel.Add(shapes.Item(j))
                except Exception:
                    pass
        except Exception:
            pass

    sel.VisProperties.SetShow(0)
    sel.Clear()
    log.info("已显示所有元素（Body + 几何体）")


def parse_catpart(catia_app, filepath: str, temp_dir: str, progress_cb=None):
    filepath = os.path.normpath(filepath)
    doc = catia_app.Documents.Open(filepath)
    # 2026-08-19: gen_py 静态绑定下 Document 无 Part 属性，需转 PartDocument
    part = as_part_document(doc).Part
    bodies = part.Bodies

    # 显示所有隐藏的 Body（隐藏的 Body 无法 Copy/PasteSpecial 导出实体）
    # RefreshDisplay 已在 _setup_catia_session 关闭，避免全显示卡顿
    _show_all_bodies(doc, bodies)

    total = bodies.Count
    results = []
    skipped = 0
    excluded = 0
    # 记录 Body 引用用于后续拆分
    body_refs = {}  # name → body_object
    # 过滤/外购/跳过测量等一律由 V2 规则驱动（2026-08-13 老 gr_rules.json 已删）；
    # V2 不可用时仅保留结构性过滤（PartBody/布尔件/空名）。

    # 用 MainBody 引用比较识别 PartBody，避免硬编码 "index 1 是 PartBody" 的假设
    try:
        main_body = part.MainBody
    except Exception:
        main_body = None

    def _is_main_body(body):
        if main_body is None:
            return False
        try:
            return body.Name == main_body.Name and \
                   body.Parent.Name == main_body.Parent.Name
        except Exception:
            return False

    # 单遍遍历：过滤 + 收集 body_refs + 推理/导出一次完成
    for i in range(1, total + 1):
        body = bodies.Item(i)
        if _is_main_body(body):
            continue
        if body.InBooleanOperation:
            skipped += 1; continue
        name = body.Name.strip()
        if not name: continue

        detail = infer_gr_and_detail(name)
        v2_out = detail["_v2"]

        # V2 filter 域：工艺辅具/油缸附属件/空 Body 过滤（解析前置，不浪费 STP 导出）
        if v2_out.get("skipped"):
            log.info("Body 过滤(V2 filter): %s（%s）",
                     name, v2_out.get("skipReason", "未说明"))
            excluded += 1; continue

        body_refs[name] = body
        if progress_cb:
            progress_cb(i, total, name)

        # 外购件（V2 purchase 域 purchaseFixedQty，自旧规则迁移：热流道=1）：
        # 数量固定、跳过 STP 导出与实体计数
        fixed_qty = v2_out.get("purchaseFixedQty")
        if fixed_qty:
            solid_count, stp_path = int(fixed_qty), ""
        else:
            solid_count, stp_path = export_body_to_stp_and_count(
                catia_app, body, temp_dir, seq=i)

        # 注意: "数量" 字段全程语义 = 实体数 = BOM 数量；其中 0 是"STP 导出失败"
        # 的哨兵值（export_body_to_stp_and_count 重试耗尽返回 0），fill_specs_from_stp
        # 据此跳过测量并记录失败原因，不会作为真实数量进入 BOM。

        material = detail["材质"]
        ht = detail["热处理"]
        if ht:
            mat_hardness = re.findall(r'(HRC|HV)', material)
            ht_hardness = re.findall(r'(HRC|HV)', ht)
            duplicate = bool(set(mat_hardness) & set(ht_hardness))
            ht_in_mat = ht in material
            if not duplicate and not ht_in_mat:
                material = f"{material}\n{ht}".strip()

        # 加工备注统一来自 V2 remark 域（infer_gr_and_detail 已生成）
        remark = detail["加工备注"]

        results.append({
            "零件号": "",
            "零部件名": name,
            "数量": solid_count,
            "规格": "",
            "材质": material,
            "零件GR号": detail["零部件GR名"],
            "零部件GR名": "",
            "备注": "",
            "加工备注": remark,
            "_stp_path": stp_path,
            "_v2": v2_out,
        })

    return results, skipped, excluded, doc, body_refs
