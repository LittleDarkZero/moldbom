# -*- coding: utf-8 -*-
"""BomExport 配套紧固件补全模块（2026-08-18 重构自 bom_export.py 模块7）。

配套件来自 V2 companion 域推理输出，数量 = Σ(每种规格的实体数 × 该规格单件
紧固件数)，配套件 GR 由 V2 引擎按 companionGrPolicy 解析。
"""

import re

from bom_common import log

# P1-8：配套件规格上限（业务决策：CB>M20、CBW>16 拦截）
CB_MAX_SIZE = 20     # CB 螺钉上限 M20
CBW_MAX_SIZE = 16    # CBW 弹簧垫圈上限 16


def _companion_over_limit(c: dict) -> bool:
    """判断紧固件是否超规格上限：CB 系列 > M20、CBW 系列 > 16。"""
    name = str(c.get("name", ""))
    spec = str(c.get("spec", ""))
    m = re.match(r"^CBW?(\d+)", spec)
    if not m:
        return False
    size = int(m.group(1))
    if name.startswith("CBW") or spec.startswith("CBW"):
        return size > CBW_MAX_SIZE
    if name.startswith("CB") or spec.startswith("CB"):
        return size > CB_MAX_SIZE
    return False


def add_companions(results: list) -> list:
    """配套补全（2026-08-13 V2 化：companion 域为唯一规则源）。

    配套件来自 V2 推理输出；suppressCompanions → 不补配套（如热流道外购件）。
    数量 = Σ(每种规格的实体数 × 该规格单件紧固件数)——多实体异规格时各规格
    紧固件数量可能不同，按规格分别计算再按 (紧固件名, 规格) 聚合求和。
    配套件 GR 已由 V2 引擎按 companionGrPolicy 解析（follow-part / warehouse 兜底）。
    模架配套保持排在最后（原行为）。
    """
    v2_plain, v2_mold = [], []
    for item in results:
        out = item.get("_v2") or {}
        if out.get("suppressCompanions"):
            continue
        name = item.get("零部件名", "")

        # 聚合：(紧固件名, 规格) -> [总数, GR]
        agg = {}
        by_spec = item.get("_companions_by_spec")
        if by_spec is not None:
            # 逐规格：数量 = 该规格实体数 × 单件紧固件数（2026-08-13 修复多规格求和）
            for entry in by_spec:
                cnt = _to_int(entry.get("count"), 1)
                for c in entry.get("companions", []):
                    _acc_companion(agg, c, cnt)
        else:
            # 回退（无 _companions_by_spec，如 V2 规格级未跑）：数量 × 单件
            comps = out.get("companions") or []
            if not comps:
                continue
            parent_qty = _to_int(item.get("数量"), 1)
            for c in comps:
                _acc_companion(agg, c, parent_qty)

        for (cname, cspec), (qty, comp_gr) in agg.items():
            # P1-8 规格上限校验：CB>M20 / CBW>16 拦截（不进入 BOM + 日志告警）
            if _companion_over_limit({"name": cname, "spec": cspec}):
                log.warning("配套件规格超上限已拦截: %s %s ×%d（CB≤M%d / CBW≤M%d）",
                            cname, cspec, qty, CB_MAX_SIZE, CBW_MAX_SIZE)
                continue
            row = _make_companion({
                "零部件名": cname,
                "规格": cspec,
                "数量": qty,
                "GR": comp_gr,
            }, name, item.get("零件GR号", ""))
            # 结构化父件引用（2026-08-19 修复 P1-5：不再只靠 "→ " 字符串前缀）
            row["_is_companion"] = True
            row["_parent_ref"] = name
            row["_source"] = item.get("_source", "")
            if row["零件GR号"] == "模架":
                v2_mold.append(row)
            else:
                v2_plain.append(row)

    expanded = list(results)
    expanded.extend(v2_plain)
    expanded.extend(v2_mold)
    return expanded


def _to_int(v, default=1):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _acc_companion(agg: dict, c: dict, count: int):
    """按 (紧固件名, 规格) 聚合一条紧固件，数量 += 单件 qty × count。"""
    key = (c.get("name", "螺钉"), c.get("spec", ""))
    unit = _to_int(c.get("qty", 1), 1)
    q = unit * count
    cur = agg.get(key)
    if cur is None:
        agg[key] = [q, c.get("gr", "")]
    else:
        cur[0] += q


def _make_companion(comp: dict, parent_name: str, parent_gr: str = "") -> dict:
    mat = comp.get("材质", ""); ht = comp.get("热处理", "")
    if ht and ht not in mat: mat = f"{mat} {ht}".strip()
    # 数量已由 add_companions 聚合为"总数量"（Σ规格数量×单件），此处直接用
    qty = comp.get("数量", "")
    if qty == "":
        qty = 1

    # 配套件 GR 由 V2 引擎在推理时已按 companionGrPolicy 解析（2026-08-13：
    # 旧 companion_gr_follow 名单已迁移为 V2 companion.mold-frame-policy 规则）
    comp_gr = comp.get("GR", ""); comp_name = comp.get("零部件名", "")
    return {
        "零件号": "", "零部件名": comp_name, "数量": qty,
        "规格": comp.get("规格", ""), "材质": mat,
        "零件GR号": comp_gr, "零部件GR名": "",
        "备注": f"→ {parent_name}", "加工备注": comp.get("加工备注", ""),
        # 结构化字段（P1-5）：_is_companion 判定配套件，_parent_ref 记录父件
        "_is_companion": True, "_parent_ref": parent_name,
    }
