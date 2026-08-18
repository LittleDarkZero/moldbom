# -*- coding: utf-8 -*-
"""规则数据模型：加载 / 保存 / 单规则校验（门禁 G1、G3）。"""

import json
import os
import re

from .schema import (ATTR_KINDS, DOMAINS, ID_RE, OPS, OWNERSHIP, SCOPES,
                     STATUSES, PRIORITY_MIN, PRIORITY_MAX, RULES_DIRNAME)

MANIFEST_NAME = "manifest.json"


class RuleError(Exception):
    """规则校验失败（G1/G2/G3 类错误）。"""


def check_rule(rule, corpus_ids=None):
    """门禁 G1（结构）+ G3（引用）。返回错误列表（空 = 通过）。"""
    errs = []
    if not isinstance(rule, dict):
        return ["规则不是 JSON 对象"]
    rid = str(rule.get("id", ""))
    if not re.fullmatch(ID_RE, rid):
        errs.append(f"[{rid or '?'}] id 不符合命名规范 <domain>.<category>.<scope>.<seq>")
    domain = rule.get("domain")
    if domain not in DOMAINS:
        errs.append(f"[{rid}] 域 {domain!r} 非法（合法域: {', '.join(DOMAINS)}）")
    prio = rule.get("priority")
    if not isinstance(prio, int) or not (PRIORITY_MIN <= prio <= PRIORITY_MAX):
        errs.append(f"[{rid}] priority 必须在 {PRIORITY_MIN}-{PRIORITY_MAX} 之间（整数）")
    scope = rule.get("scope")
    if scope not in SCOPES:
        errs.append(f"[{rid}] scope {scope!r} 非法（合法: {', '.join(SCOPES)}）")

    when = rule.get("when")
    if not isinstance(when, dict):
        errs.append(f"[{rid}] when 必须是对象")
    else:
        if not when and scope != "global":
            errs.append(f"[{rid}] 空 when 仅允许 global 作用域（兜底规则）")
        for f, m in when.items():
            if f not in WHEN_FIELDS_SET:
                errs.append(f"[{rid}] 条件字段 {f} 不在词汇表内")
            if not isinstance(m, dict):
                errs.append(f"[{rid}] 条件 {f} 必须是匹配器对象")
                continue
            op = m.get("op")
            if op not in OPS:
                errs.append(f"[{rid}] 条件 {f} 的 op {op!r} 非法")
                continue
            if op in ("eq", "contains", "prefix", "suffix", "regex", "keyword") and "value" not in m:
                errs.append(f"[{rid}] 条件 {f} 缺 value")
            if op == "in" and not isinstance(m.get("value"), list):
                errs.append(f"[{rid}] 条件 {f} 的 in 算子需要 value 列表")
            if op == "range" and (not isinstance(m.get("min"), (int, float))
                                  or not isinstance(m.get("max"), (int, float))):
                errs.append(f"[{rid}] 条件 {f} 的 range 算子需要 min/max")
            if op == "regex":
                try:
                    re.compile(str(m.get("value", "")))
                except re.error as e:
                    errs.append(f"[{rid}] 条件 {f} 正则非法: {e}")

    then = rule.get("then")
    if not isinstance(then, dict) or not then:
        errs.append(f"[{rid}] then 必须是非空对象")
    else:
        allowed = OWNERSHIP.get(domain, ())
        for a, v in then.items():
            if a not in allowed:
                errs.append(f"[{rid}] 属性 {a} 不属于域 {domain} 的授权表（唯一归属）")
                continue
            kind = ATTR_KINDS.get(a, "str")
            err = _check_value(kind, a, v)
            if err:
                errs.append(f"[{rid}] 动作 {a}: {err}")
        if "purchaseFixedQty" in then and "companions" in then:
            errs.append(f"[{rid}] 跨域一致性: 同一规则同时写 purchaseFixedQty 与 companions 矛盾")
        if "suppressCompanions" in then and "companions" in then:
            errs.append(f"[{rid}] 跨域一致性: suppressCompanions 与 companions 不能同时写")

    meta = rule.get("meta")
    if not isinstance(meta, dict):
        errs.append(f"[{rid}] meta 缺失")
    else:
        if meta.get("status") not in STATUSES:
            errs.append(f"[{rid}] meta.status 非法（draft/active/deprecated/retired）")
        if not isinstance(meta.get("version"), int) or meta.get("version") < 1:
            errs.append(f"[{rid}] meta.version 必须为 ≥1 的整数")
        if corpus_ids is not None:
            for t in meta.get("tests", []) or []:
                if t not in corpus_ids:
                    errs.append(f"[{rid}] 测试引用 {t} 在语料库中不存在")
    return errs


def _check_value(kind, attr, v):
    if v is None:
        return ""
    if kind == "bool":
        return "" if isinstance(v, bool) else "需要布尔值"
    if kind == "int":
        return "" if isinstance(v, int) and not isinstance(v, bool) else "需要整数"
    if kind == "str":
        if isinstance(v, str):
            return ""
        # normalize 域的结构化值：{"replaceAll": [旧, 新]}
        if (isinstance(v, dict) and list(v) == ["replaceAll"]
                and isinstance(v["replaceAll"], list) and len(v["replaceAll"]) == 2
                and all(isinstance(x, str) for x in v["replaceAll"])):
            return ""
        return "需要字符串或 {replaceAll: [旧, 新]}"
    if kind == "strtext":
        # 多行文本：字符串值，换行用真实 \n（编辑器以多行 Text 编辑）
        return "" if isinstance(v, str) else "需要字符串（可含 \\n 多行）"
    if kind == "strlist":
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            return ""
        # 追加型值：{"add": [项...]}
        if (isinstance(v, dict) and list(v) == ["add"]
                and isinstance(v["add"], list) and all(isinstance(x, str) for x in v["add"])):
            return ""
        return "需要字符串列表或 {add: [...]}"
    if kind == "range":
        if not isinstance(v, dict) or "min" not in v or "max" not in v:
            return "需要 {min, max} 对象"
        return ""
    if kind == "companions":
        if not isinstance(v, list):
            return "需要配套件列表"
        for c in v:
            if not isinstance(c, dict) or not c.get("name"):
                return "配套件需要 {name, ...}"
        return ""
    if kind.startswith("enum:"):
        allowed = kind.split(":", 1)[1].split("|")
        return "" if v in allowed else f"需要 {allowed}"
    return ""


WHEN_FIELDS_SET = set((
    "part.name", "part.workingName", "part.material", "part.group",
    "spec.value", "spec.count", "spec.hasMeasured", "gr", "quantity",
    "input.skipBody", "input.skipReason",
))


# ---------- 规则集加载 / 保存 ----------

def load_ruleset(rules_dir):
    """加载规则集：合并 manifest + 各域文件。返回 (manifest, rules列表)。"""
    man_path = os.path.join(rules_dir, MANIFEST_NAME)
    if not os.path.exists(man_path):
        raise RuleError(f"缺少 {MANIFEST_NAME}（不是规则集目录: {rules_dir}）")
    with open(man_path, encoding="utf-8") as f:
        manifest = json.load(f)
    rules = []
    for domain in DOMAINS:
        path = os.path.join(rules_dir, f"{domain}.rules.json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for r in data.get("rules", []):
            rules.append(r)
    return manifest, rules


def group_by_domain(rules):
    groups = {d: [] for d in DOMAINS}
    for r in rules:
        groups.setdefault(r.get("domain"), []).append(r)
    return groups


def save_ruleset(rules_dir, rules, manifest, version=None):
    """按域分文件原子写回；manifest 版本号自动 bump（PATCH+1 默认）。"""
    if version:
        manifest = dict(manifest)
        manifest["version"] = version
    groups = group_by_domain(rules)
    for domain in DOMAINS:
        path = os.path.join(rules_dir, f"{domain}.rules.json")
        payload = {"rules": groups.get(domain, [])}
        _atomic_write(path, payload)
    _atomic_write(os.path.join(rules_dir, MANIFEST_NAME), manifest)
    return manifest


def _atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
