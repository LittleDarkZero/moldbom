# -*- coding: utf-8 -*-
"""验证门禁 G1-G5（schema/命名/引用/静态冲突/行为干跑）。"""

from .engine import RuleEngine
from .model import check_rule


def _may_overlap(wa, wb):
    """启发式：两规则条件是否可能同时命中（保守）。eq 异值=互斥；其余按公共字符判断。"""
    for f in wa:
        ma, mb = wa[f], wb[f]
        if ma.get("op") == "eq" and mb.get("op") == "eq":
            if ma.get("value") != mb.get("value"):
                return False
        else:
            va, vb = str(ma.get("value", "")), str(mb.get("value", ""))
            if not any(c in vb for c in va):
                return False
    return True


def validate_ruleset(rules, corpus_ids=None):
    """G1-G4。返回 {"errors": [...], "warnings": [...]}。"""
    errors, warnings = [], []

    # G1+G3 逐规则
    for r in rules:
        errors.extend(check_rule(r, corpus_ids))

    # G2 命名唯一性
    seen_ids = {}
    seen_names = {}
    for r in rules:
        rid = r.get("id", "")
        if rid in seen_ids:
            errors.append(f"id 重复: {rid}")
        seen_ids[rid] = 1
        name = r.get("name")
        if name:
            key = (r.get("domain"), name)
            if key in seen_names:
                errors.append(f"同域内 name 重复: {r.get('domain')}/{name}")
            seen_names[key] = 1

    # G4 静态冲突（同域同优先级同 when → 必冲突；同 when 字段集 + 异值 → 潜在冲突）
    active = [r for r in rules if r.get("meta", {}).get("status") == "active"]
    for i in range(len(active)):
        for j in range(i + 1, len(active)):
            a, b = active[i], active[j]
            if a.get("domain") != b.get("domain"):
                continue
            if a.get("priority") != b.get("priority"):
                continue
            wa, wb = a.get("when", {}), b.get("when", {})
            shared_attrs = set(a.get("then", {})) & set(b.get("then", {}))
            if not shared_attrs:
                continue
            if wa == wb:
                diffs = [x for x in shared_attrs if a["then"][x] != b["then"][x]]
                if diffs:
                    errors.append(
                        f"静态冲突 C2: {a['id']} 与 {b['id']} 条件完全相同，"
                        f"但属性 {diffs} 值不同（同优先级 {a['priority']}）")
            elif set(wa) == set(wb):
                diffs = [x for x in shared_attrs if a["then"][x] != b["then"][x]]
                if diffs and _may_overlap(wa, wb):
                    warnings.append(
                        f"潜在冲突: {a['id']} 与 {b['id']} 条件字段集相同且条件可能同时命中"
                        f"（属性 {diffs} 值不同），请用语料干跑验证")
    return {"errors": errors, "warnings": warnings}


def dry_run(rules, corpus_entries):
    """G5 行为干跑。返回报告 dict。"""
    engine = RuleEngine(rules)
    report = {"total": len(corpus_entries), "matched": 0,
              "wrong": [], "missing": [], "warnings": []}
    for entry in corpus_entries:
        given = entry.get("given", {})
        expect = entry.get("expect", {})
        allow = set(entry.get("allowMissing", []))
        out, _prov = engine.infer(
            part_name=given.get("partName", ""),
            spec_value=given.get("spec"),
            quantity=given.get("quantity", 1),
            group=given.get("group"),
        )
        if out.get("skipped") and entry.get("skipped", False) is True:
            report["matched"] += 1
            continue
        bad = False
        for attr, want in expect.items():
            if attr == "companions":
                got = {(c.get("name"), c.get("spec")) for c in out.get("companions", [])}
                want_set = {(c.get("name"), c.get("spec")) for c in want}
                if got != want_set:
                    report["wrong"].append(f"{entry['id']}: companions 期望 {sorted(want_set)} 实得 {sorted(got)}")
                    bad = True
            elif attr not in out:
                if attr not in allow:
                    report["missing"].append(f"{entry['id']}: 期望属性 {attr} 未产出")
                    bad = True
            elif out[attr] != want:
                report["wrong"].append(f"{entry['id']}: {attr} 期望 {want!r} 实得 {out[attr]!r}")
                bad = True
        if not bad:
            report["matched"] += 1
    return report


def gate_summary(report):
    """门禁结论：wrong/missing 为阻断项。返回 (ok, 文本)。"""
    if report["wrong"] or report["missing"]:
        return False, (f"干跑失败: {len(report['wrong'])} 处不符, "
                       f"{len(report['missing'])} 处缺失（共 {report['total']} 样本）")
    return True, f"干跑通过: {report['matched']}/{report['total']} 样本完全一致"
