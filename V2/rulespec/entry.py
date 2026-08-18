# -*- coding: utf-8 -*-
"""零件录入 → 规则计划（wizard 与 table_editor 共用，单一数据源）。

一个「零件条目」（零件名 + 规格 + 材料/GR/加工说明/紧固件）会被转换为
若干条规则：gr 域（分类）、material 域（材质）、remark 域（加工说明）、
companion 域（紧固件）——每条规则的条件相同（零件名 [+ 规格]）。

关键语义（与 wizard 一致）：
- 同零件同规格已有规则 → 更新（保留原优先级），不重复新增；
- 新规则 id 按 <域>.wizard.<作用域>.<序号> 自动生成；
- 只在传入的规则列表副本上工作，由调用方决定何时提交。
"""

import copy
import datetime

from .matcher import canonical_spec, extract_model_from_name  # noqa: F401 解析层实现，re-export 兼容旧引用
from .schema import PRIORITY_DEFAULT

PLAIN_NAMES = {
    "gr": "分类（GR）",
    "spec": "型号（打印规格）",
    "material": "材质",
    "remark": "加工说明",
    "companion": "紧固件",
}


def find_existing(rules_list, domain, scope, when):
    """找同零件（+同规格）的已有规则 → 更新而非重复新增。"""
    want_name = when["part.workingName"]["value"]
    want_spec = canonical_spec(when.get("spec.value", {}).get("value"))
    for r in rules_list:
        if r.get("domain") != domain or r.get("scope") != scope:
            continue
        rw = r.get("when") or {}
        nm = rw.get("part.workingName")
        if not isinstance(nm, dict):
            continue
        if not (want_name in str(nm.get("value", ""))
                or str(nm.get("value", "")) in want_name):
            continue
        rspec = canonical_spec(rw.get("spec.value", {}).get("value")) \
            if isinstance(rw.get("spec.value"), dict) else None
        if want_spec != rspec:
            continue
        return r
    return None


def next_id(rules_list, domain, scope):
    prefix = f"{domain}.wizard.{scope}."
    seq = 1
    for r in rules_list:
        rid = r.get("id", "")
        if rid.startswith(prefix):
            seq = max(seq, int(rid.rsplit(".", 1)[-1]) + 1)
    return f"{prefix}{seq:03d}"


def make_rule(rules_list, domain, when, then, scope, prio, rationale, tests,
              author=""):
    """生成一条规则（新增）或在已有规则上合并（更新）。返回 (action, rule)。"""
    existing = find_existing(rules_list, domain, scope, when)
    now = datetime.date.today().isoformat()
    if existing:
        rule = existing
        meta = rule.setdefault("meta", {})
        meta["version"] = int(meta.get("version", 1)) + 1
        meta["updatedAt"] = now
        meta["status"] = "active"
        if rationale:
            meta["rationale"] = rationale
        if tests:
            meta.setdefault("tests", [])
            for t in tests:
                if t not in meta["tests"]:
                    meta["tests"].append(t)
        # 更新时保留已有优先级（新手默认 500 不覆盖专家调好的值），
        # 除非调用方显式给了非默认优先级
        if prio != PRIORITY_DEFAULT:
            rule["priority"] = prio
        # 关键：把新动作写进规则（2026-08-04 修复——重构时遗漏，导致更新只 bump 版本不改值）
        rule["then"].update(then)
        return "update", rule
    rule = {
        "id": next_id(rules_list, domain, scope),
        "domain": domain,
        "priority": prio,
        "scope": scope,
        "when": copy.deepcopy(when),
        "then": {},
        "meta": {
            "status": "active", "version": 1,
            "author": author,
            "createdAt": now, "updatedAt": now,
            "rationale": rationale or "",
            "tests": list(tests or []),
        },
    }
    rule["then"].update(then)
    return "new", rule


def plan_entry(rules_list, *, name, spec=None, gr=None, material=None,
               remark=None, fasteners=None, model=None, prio=PRIORITY_DEFAULT,
               match_op="contains", rationale="", tests=None, author=""):
    """把一个零件条目转换为规则计划。

    返回 {"plan": [...]}，每项含 domain/action/rule/id/spec/plain_name/plain_effect；
    入参不合法时返回 {"error": "..."}。
    """
    name = (name or "").strip()
    if not name:
        return {"error": "零件名不能为空"}
    spec = (spec or "").strip() or None
    scope = "spec" if spec else "part"
    when = {"part.workingName": {"op": match_op, "value": name}}
    if spec:
        # 写入即归一化（×/全角→*），保证与引擎输入侧一致、幂等匹配
        when["spec.value"] = {"op": "eq", "value": canonical_spec(spec)}
    try:
        prio = int(prio)
    except (TypeError, ValueError):
        prio = PRIORITY_DEFAULT

    plan = []
    if gr and (gr or "").strip():
        action, rule = make_rule(rules_list, "gr", when, {"gr": gr.strip()},
                                 scope, prio, rationale, tests, author)
        plan.append({"domain": "gr", "action": action, "rule": rule,
                     "id": rule["id"], "spec": spec,
                     "plain_name": PLAIN_NAMES["gr"],
                     "plain_effect": f"归到「{gr.strip()}」"})
    if material and (material or "").strip():
        action, rule = make_rule(rules_list, "material", when,
                                 {"material": material.strip()}, scope, prio,
                                 rationale, tests, author)
        plan.append({"domain": "material", "action": action, "rule": rule,
                     "id": rule["id"], "spec": spec,
                     "plain_name": PLAIN_NAMES["material"],
                     "plain_effect": f"材质「{material.strip()}」"})
    if remark and (remark or "").strip():
        action, rule = make_rule(rules_list, "remark", when,
                                 {"remark": remark.strip()}, scope, prio,
                                 rationale, tests, author)
        plan.append({"domain": "remark", "action": action, "rule": rule,
                     "id": rule["id"], "spec": spec,
                     "plain_name": PLAIN_NAMES["remark"],
                     "plain_effect": "加工说明已填写"})
    if model and (model or "").strip():
        # 型号（输出规格）：按测量规格匹配，命中则 BOM 打印型号
        action, rule = make_rule(rules_list, "spec", when,
                                 {"outputSpec": model.strip()}, scope, prio,
                                 rationale, tests, author)
        plan.append({"domain": "spec", "action": action, "rule": rule,
                     "id": rule["id"], "spec": spec,
                     "plain_name": PLAIN_NAMES["spec"],
                     "plain_effect": f"打印规格「{model.strip()}」"})
    if fasteners:
        comps = [{"name": f.get("name") or "螺钉",
                  "spec": str(f.get("spec", "")).strip(),
                  "qty": int(f.get("qty", 1)),
                  "gr": str(f.get("gr", "")).strip()} for f in fasteners if f.get("spec")]
        if comps:
            action, rule = make_rule(rules_list, "companion", when,
                                     {"companions": comps}, scope, prio,
                                     rationale, tests, author)
            plan.append({"domain": "companion", "action": action, "rule": rule,
                         "id": rule["id"], "spec": spec,
                         "plain_name": PLAIN_NAMES["companion"],
                         "plain_effect": f"{len(comps)} 种紧固件"})
    return {"plan": plan}
