# -*- coding: utf-8 -*-
"""BomExport GR 推理模块（2026-08-18 重构自 bom_export.py 模块1）。

零件级推理（infer_gr_and_detail / infer_gr_name）与规格级精化
（_apply_spec_gr_v2）。规则唯一源 = V2 引擎（v2_bridge 桥接），
V2 不可用按常量兜底（DEFAULT_GR）。
"""

from bom_common import log, DEFAULT_GR


def infer_gr_and_detail(part_name: str) -> dict:
    """V2 规则引擎推理零件级 GR/材质/热处理/备注。

    返回 {"零部件GR名","材质","热处理","加工备注","_v2"}；
    _v2 携带引擎原始输出（purchaseFixedQty/skipMeasurement/numberRange/
    companions/suppressCompanions/skipped 等），供各 pipeline 阶段消费。
    V2 不可用或推理异常 → 默认 GR（DEFAULT_GR）+ 空 _v2，不中断主流程。
    """
    if not part_name or not part_name.strip():
        return {"零部件GR名": "待定", "材质": "", "热处理": "",
                "加工备注": "", "_v2": {}}
    name = part_name.strip()
    v2_out = {}
    try:
        from v2_bridge import infer_part
        r = infer_part(name)
        if r is not None:
            v2_out, _prov = r
    except Exception as e:  # noqa: BLE001 单件推理失败按默认值兜底
        log.warning("V2 推理异常 %s: %s", name, e)
    return {
        "零部件GR名": v2_out.get("gr") or DEFAULT_GR,
        "材质": v2_out.get("material", ""),
        "热处理": v2_out.get("heatTreatment", ""),
        "加工备注": v2_out.get("remark", ""),
        "_v2": v2_out,
    }


def _apply_spec_gr_v2(results: list) -> list:
    """规格级精化：V2 规格引擎（唯一规格级路径，2026-08-13）。

    老 learned_mapping 规格级映射已随 gr_rules.json 删除；V2 不可用时
    保持原推理结果（规格值不变、GR 保持零件级推理值）。
    """
    from v2_bridge import apply_v2_spec
    r = apply_v2_spec(results)
    return r if r is not None else results
