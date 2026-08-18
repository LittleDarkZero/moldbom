# -*- coding: utf-8 -*-
"""BomExport 零件号分配模块（2026-08-18 重构自 bom_export.py 模块6）。

按 V2 number 域分段规则给主件分配零件号（模架 1-99 / 自制·镶配 100-199 /
其余 200+），配套件继承父件零件号。
"""

from bom_common import DEFAULT_NUM_RANGE


def assign_part_numbers(results: list) -> list:
    """零件号分配（2026-08-13 V2 化：分段规则来自 V2 number 域）。

    每个主件按 V2 推理的 numberRange {min,max} 分桶（自旧 part_number_ranges
    迁移：模架 1-99 / 自制·镶配 100-199 / 其余 200+），桶内按原顺序自 min 递增；
    配套件继承父件零件号。V2 不可用/未命中 → DEFAULT_NUM_RANGE。
    """
    main_parts = [r for r in results if not r["备注"].startswith("→ ")]
    companions = [r for r in results if r["备注"].startswith("→ ")]

    buckets = {}  # min → [items]（插入序保持 results 顺序）
    for item in main_parts:
        rng = (item.get("_v2") or {}).get("numberRange") or DEFAULT_NUM_RANGE
        mn = rng.get("min", DEFAULT_NUM_RANGE["min"])
        buckets.setdefault(mn, []).append(item)

    for mn in sorted(buckets):
        no = mn
        for item in buckets[mn]:
            item["零件号"] = no
            no += 1

    for comp in companions:
        parent_name = comp["备注"].replace("→ ", "")
        for item in main_parts:
            if item["零部件名"] == parent_name:
                comp["零件号"] = item["零件号"]; break

    def sort_key(x):
        no = x.get("零件号", 999)
        is_comp = 1 if x["备注"].startswith("→ ") else 0
        return (no, is_comp)
    return sorted(results, key=sort_key)
