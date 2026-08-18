# -*- coding: utf-8 -*-
"""推理引擎：流水线 + 裁决算法 + provenance（RuleSpec 2.0）。

流水线（10 域，无 merge）：
  filter → normalize → gr → spec → measure → material → remark → companion → purchase → number
  （spec = 输出规格域：按测量规格改写 BOM 打印型号；nameSpec 名称读型号见 infer）

裁决（每属性 first-wins）：
  priority 降序 → specificity 降序 → id 字典序升序；
  前两名同强度（同 priority 同 specificity）且值不同 → RuleConflictError（零猜测）。
  关键词间的优先级靠规则显式 priority 字段（用户手动调整，默认 500）。
追加属性（remarkAppend）取全部候选并集，按优先级升序拼接。
companions 取最高优先级规则（first-wins）——一个零件只配一套紧固件。
"""

from .matcher import canonical_spec, extract_model_from_name, match_field
from .schema import DOMAINS, SPECIFICITY_WEIGHTS, SPECIFICITY_OTHER


class RuleConflictError(Exception):
    """运行期歧义：同强度不同值的规则竞争同一属性。"""


class RuleEngine:
    def __init__(self, rules):
        self.rules = list(rules)
        self.active = [r for r in self.rules if r.get("meta", {}).get("status") == "active"]
        self.by_domain = {d: [r for r in self.active if r.get("domain") == d] for d in DOMAINS}

    # ---------- 对外入口 ----------
    def infer(self, part_name, spec_value=None, quantity=1, group=None,
              spec_count=None, spec_has_measured=False, name_spec=True):
        """对单个零件实体推理。返回属性字典 + provenance。

        name_spec=True（默认）：当 spec_value 未给出时，尝试从零件名提取型号
        （如『开模油缸 BOD-AG-40-32-V』→ BOD-AG-40-32-V）作为规格参与匹配；
        提取结果同时作为默认输出规格（spec 域规则不命中时）。防误判由
        extract_model_from_name 保证（单段名 / 纯中文分段不提取），
        显式传入的测量规格永远优先于名称提取。2026-08-05 实装，未接入 bom_export。
        """
        spec = canonical_spec(spec_value)
        name_spec_extracted = None
        if spec is None and name_spec:
            name_spec_extracted = extract_model_from_name(part_name)
            if name_spec_extracted:
                spec = canonical_spec(name_spec_extracted)
                spec_has_measured = False          # 名称读取非测量
                spec_count = spec_count if spec_count is not None else 1
        ctx = {
            "part.name": str(part_name),
            "part.workingName": str(part_name),
            "quantity": quantity,
        }
        if group:
            ctx["part.group"] = str(group)
        if spec is not None:
            ctx["spec.value"] = spec
            ctx["spec.count"] = spec_count if spec_count is not None else 1
            ctx["spec.hasMeasured"] = bool(spec_has_measured)

        out = {"workingName": ctx["part.workingName"]}
        if name_spec_extracted:
            out["nameSpec"] = name_spec_extracted    # 从零件名识别出的型号
            out["spec"] = spec                       # 实际参与匹配的规格
        prov = {}

        # 1) filter：实体过滤（first-wins）
        skip, rule = self._first_wins("filter", "input.skipBody", ctx)
        if skip:
            reason, _ = self._first_wins("filter", "input.skipReason", ctx)
            out["skipped"] = True
            out["skipReason"] = reason or "（未说明）"
            if rule:
                prov["input.skipBody"] = self._prov(rule)
            return out, prov
        out["skipped"] = False

        # 2) normalize：名称归一化（全部命中规则按序应用，可链式）
        for r in self._ordered_candidates("normalize", ctx):
            t = r["then"]
            if "part.workingName" in t:
                v = t["part.workingName"]
                if isinstance(v, str):
                    ctx["part.workingName"] = v
                elif isinstance(v, dict) and "replaceAll" in v:
                    a, b = v["replaceAll"][0], v["replaceAll"][1]
                    ctx["part.workingName"] = ctx["part.workingName"].replace(a, b)
                out["workingName"] = ctx["part.workingName"]
                prov["part.workingName"] = self._prov(r)
            if "part.aliases" in t:
                out.setdefault("aliases", [])
                for x in t["part.aliases"]:
                    if x not in out["aliases"]:
                        out["aliases"].append(x)

        # 3) gr：GR 分类（spec 作用域规则仅在规格已知时参与）
        gr, rule = self._first_wins("gr", "gr", ctx)
        if gr is not None:
            ctx["gr"] = out["gr"] = gr
            prov["gr"] = self._prov(rule)

        # 4) spec：输出规格（BOM 打印用——命中则把测量规格改写为型号，如 BZ500.80/50）
        ospec, rule = self._first_wins("spec", "outputSpec", ctx)
        if ospec is not None:
            out["outputSpec"] = ospec
            prov["outputSpec"] = self._prov(rule)
        elif name_spec_extracted:
            # 名称读型号：没有型号改写规则时，默认输出识别出的型号
            out["outputSpec"] = name_spec_extracted
            prov["outputSpec"] = {"rule": "nameSpec",
                                  "detail": "从零件名提取型号（name_spec=True 默认）"}

        # 5) measure：测量控制（first-wins）
        skip_meas, rule = self._first_wins("measure", "skipMeasurement", ctx)
        if skip_meas is not None:
            out["skipMeasurement"] = skip_meas
            prov["skipMeasurement"] = self._prov(rule)
            reason, _ = self._first_wins("measure", "skipReason", ctx)
            if reason is not None:
                out["measureSkipReason"] = reason

        # 6) material（first-wins 每属性）
        mat, rule = self._first_wins("material", "material", ctx)
        if mat is not None:
            out["material"] = mat
            prov["material"] = self._prov(rule)
        ht, rule = self._first_wins("material", "heatTreatment", ctx)
        if ht is not None:
            out["heatTreatment"] = ht
            prov["heatTreatment"] = self._prov(rule)

        # 7) remark：主备注 first-wins；追加备注取并集（优先级升序）
        rem, rule = self._first_wins("remark", "remark", ctx)
        if rem is not None:
            out["remark"] = rem
            prov["remark"] = self._prov(rule)
        appended = []
        for r in self._ordered_candidates("remark", ctx):
            v = r["then"].get("remarkAppend")
            if isinstance(v, dict) and isinstance(v.get("add"), list):
                for x in v["add"]:
                    if x not in appended:
                        appended.append(x)
        if appended:
            out["remark"] = (out.get("remark") or "") + ("\n" if out.get("remark") else "") \
                + "\n".join(appended)

        # 8) companion：免配套 first-wins；配套件取并集
        suppress, suppress_rule = self._first_wins("companion", "suppressCompanions", ctx)
        if suppress is not None:
            out["suppressCompanions"] = suppress
            prov["suppressCompanions"] = self._prov(suppress_rule)
        companions = []
        # companions first-wins：一个零件只配一套紧固件，取最高 priority/specificity
        # 规则（不再做多规则并集）。否则"spec 级精确规则 + part 级兜底规则"同时命中
        # 同一零件时（如定位圈 Φ249.8 命中 spec.011 与 part.007），两套紧固件会被叠加重复。
        for r in self._ordered_candidates("companion", ctx):
            v = r["then"].get("companions")
            if isinstance(v, list) and v:
                companions = list(v)
                break
        policy, rule = self._first_wins("companion", "companionGrPolicy", ctx)
        if policy is not None:
            out["companionGrPolicy"] = policy
            prov["companionGrPolicy"] = self._prov(rule)
        if not out.get("suppressCompanions"):
            if companions:
                out["companions"] = self._resolve_companion_gr(companions, ctx, policy)
        else:
            out["companions"] = []
            prov["companions"] = {"suppressed": True,
                                  "rule": suppress_rule.get("id") if suppress_rule else None}

        # 9) purchase：外购固定数量（first-wins）
        qty, rule = self._first_wins("purchase", "purchaseFixedQty", ctx)
        if qty is not None:
            out["purchaseFixedQty"] = qty
            prov["purchaseFixedQty"] = self._prov(rule)
            if out.get("companions"):
                raise RuleConflictError(
                    f"跨域一致性: {part_name} 固定数量 {qty} 但仍有配套件（需 suppressCompanions）")

        # 10) number：编号分段（first-wins）
        rng, rule = self._first_wins("number", "numberRange", ctx)
        if rng is not None:
            out["numberRange"] = rng
            prov["numberRange"] = self._prov(rule)
        pref, rule = self._first_wins("number", "numberPrefix", ctx)
        if pref is not None:
            out["numberPrefix"] = pref
            prov["numberPrefix"] = self._prov(rule)

        out["provenance"] = prov
        return out, prov

    # ---------- 内部 ----------
    @staticmethod
    def _prov(rule):
        return {"rule": rule.get("id"), "version": rule.get("meta", {}).get("version", 1)}

    def _match_candidates(self, domain, ctx):
        """返回 (rule, specificity, matched_fields) 列表。spec 作用域规则需规格上下文。

        specificity = 匹配字段权重和（按 SPECIFICITY_WEIGHTS）。
        关键词间的优先级靠规则的显式 priority 字段（用户手动调整，默认 500）。
        """
        cands = []
        has_spec = "spec.value" in ctx
        for r in self.by_domain.get(domain, []):
            if r.get("scope") == "spec" and not has_spec:
                continue
            matched = []
            ok = True
            for f, m in (r.get("when") or {}).items():
                if not match_field(f, m, ctx):
                    ok = False
                    break
                matched.append(f)
            if ok:
                spec = sum(SPECIFICITY_WEIGHTS.get(f, SPECIFICITY_OTHER) for f in matched)
                cands.append((r, spec, matched))
        return cands

    def _ordered_candidates(self, domain, ctx):
        """排序后的候选：priority 降序 → specificity 降序 → id 升序。"""
        cands = self._match_candidates(domain, ctx)
        cands.sort(key=lambda t: (-t[0].get("priority", 500), -t[1], t[0].get("id", "")))
        return [t[0] for t in cands]

    def _first_wins(self, domain, attr, ctx):
        """first-wins 裁决。返回 (value, rule)；无候选返回 (None, None)。"""
        cands = self._match_candidates(domain, ctx)
        valid = [(r, s) for r, s, _m in cands if attr in (r.get("then") or {})]
        valid.sort(key=lambda t: (-t[0].get("priority", 500), -t[1], t[0].get("id", "")))
        if not valid:
            return None, None
        top, top_spec = valid[0]
        if len(valid) > 1:
            nxt, nxt_spec = valid[1]
            if (nxt.get("priority", 500) == top.get("priority", 500)
                    and nxt_spec == top_spec
                    and nxt["then"][attr] != top["then"][attr]):
                raise RuleConflictError(
                    f"属性 {attr} 裁决歧义: {top['id']} 与 {nxt['id']} 同优先级同特异性但值不同，"
                    "请调整其一 priority 或收紧条件")
        return top["then"][attr], top

    def _resolve_companion_gr(self, companions, ctx, policy):
        """配套件 GR 解析：条目自带 gr 优先；否则按策略（follow-part=跟随宿主，默认 warehouse）。"""
        resolved = []
        host_gr = ctx.get("gr")
        for c in companions:
            c2 = dict(c)
            if not c2.get("gr"):
                c2["gr"] = host_gr if policy == "follow-part" and host_gr else "仓库备件"
            resolved.append(c2)
        return resolved
