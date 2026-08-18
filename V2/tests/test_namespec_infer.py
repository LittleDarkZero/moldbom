# -*- coding: utf-8 -*-
"""nameSpec 引擎推理模块测试（2026-08-05 实装）。

自建规则集验证，不依赖 shipped 规则（与 tests/test_engine.py 的 HAS_SHIPPED
跳过机制解耦）。覆盖：识别→匹配→输出、防误判、测量优先、开关、CLI。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rulespec.engine import RuleEngine
from rulespec.matcher import extract_model_from_name

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def R(id_, domain, name, spec=None, then=None, prio=500):
    when = {"part.workingName": {"op": "contains", "value": name}}
    if spec:
        when["spec.value"] = {"op": "eq", "value": spec}
    return {"id": id_, "domain": domain, "priority": prio, "scope": "spec" if spec else "part",
            "when": when, "then": then or {},
            "meta": {"status": "active", "version": 1, "author": "t",
                     "createdAt": "2026-08-05", "updatedAt": "2026-08-05"}}


def build_rules():
    rules = [
        R("gr.cyl.part.001", "gr", "油缸", then={"gr": "油缸"}),
        R("gr.cyl.spec.001", "gr", "油缸", "BOD-AG-40-32-V", {"gr": "油缸"}),
        R("gr.cyl.spec.002", "gr", "油缸", "BOD-AG-63-50-V", {"gr": "油缸"}),
        R("companion.cyl.spec.001", "companion", "油缸", "BOD-AG-40-32-V",
          {"companions": [{"name": "螺钉", "spec": "M10*85", "qty": 2, "gr": "仓库备件"},
                          {"name": "弹簧垫圈", "spec": "CBW10", "qty": 2, "gr": "仓库备件"}]}),
        R("material.cyl.spec.001", "material", "油缸", "BOD-AG-40-32-V",
          {"material": "45#" }),
        R("spec.cyl.spec.001", "spec", "油缸", "BOD-AG-40-32-V",
          {"outputSpec": "BOD-AG-40-32-V/40"}),
    ]
    return rules


def test_extract():
    print("== extract_model_from_name ==")
    cases = [
        ("油缸 BOD-AG-63-50-V", "BOD-AG-63-50-V"),
        ("开模油缸 BOD-AG-40-32-V", "BOD-AG-40-32-V"),
        ("油缸,BOD-AG-40-32-V", "BOD-AG-40-32-V"),
        ("油缸 BOD-AG-63-50-V / 黑色", "BOD-AG-63-50-V"),
        ("油缸 定位块", None),
        ("定1模框", None),
        ("连接杆盖板", None),
        ("", None),
    ]
    for name, expect in cases:
        got = extract_model_from_name(name)
        check(f"提取 {name!r} → {expect!r}", got == expect, f"got {got!r}")


def test_infer_namespec():
    print("== 引擎推理（name_spec=True 默认）==")
    e = RuleEngine(build_rules())
    out, prov = e.infer("开模油缸 BOD-AG-40-32-V")
    check("识别型号", out.get("nameSpec") == "BOD-AG-40-32-V", out)
    check("规格命中 spec 级规则 → GR=油缸",
          out.get("gr") == "油缸" and prov.get("gr", {}).get("rule") == "gr.cyl.spec.001",
          (out.get("gr"), prov.get("gr")))
    comps = out.get("companions") or []
    check("配套命中（M10*85×2@仓库备件）",
          any(c["spec"] == "M10*85" and c["qty"] == 2 and c.get("gr") == "仓库备件" for c in comps),
          comps)
    check("材质命中", out.get("material") == "45#", out.get("material"))
    check("型号改写规则命中（outputSpec）",
          out.get("outputSpec") == "BOD-AG-40-32-V/40", out.get("outputSpec"))


def test_infer_default_output():
    print("== 无改写规则时默认输出识别型号 ==")
    rules = build_rules()
    rules = [r for r in rules if r["id"] != "spec.cyl.spec.001"]
    out, prov = RuleEngine(rules).infer("油缸 BOD-AG-63-50-V")
    check("识别 BOD-AG-63-50-V", out.get("nameSpec") == "BOD-AG-63-50-V", out)
    check("GR 命中 spec 级规则", out.get("gr") == "油缸", out.get("gr"))
    check("默认打印规格 = 识别型号",
          out.get("outputSpec") == "BOD-AG-63-50-V",
          out.get("outputSpec"))


def test_no_misjudge():
    print("== 防误判（普通零件不受影响）==")
    rules = build_rules()
    e = RuleEngine(rules)
    # 单段名 → 不提取 → part 级规则（油缸 contains）不命中（名不含"油缸"）
    out, prov = e.infer("定1模框")
    check("单段名不提取、不误命中", out.get("nameSpec") is None and out.get("gr") is None,
          (out.get("nameSpec"), out.get("gr")))
    # 纯中文分段 → 不提取（但名字含"油缸"关键词 → part 级规则正常命中）
    out2, prov2 = e.infer("油缸 定位块")
    check("纯中文分段不提取型号", out2.get("nameSpec") is None, out2.get("nameSpec"))
    check("含油缸关键词 → part 级规则命中（不误走 spec 级）",
          out2.get("gr") == "油缸" and prov2.get("gr", {}).get("rule") == "gr.cyl.part.001",
          (out2.get("gr"), prov2.get("gr")))
    # 名称含"油缸"但无型号 → part 级规则命中（GR=油缸），不提取
    out3, prov3 = e.infer("油缸体")
    check("含关键词无型号 → part 级规则命中",
          out3.get("gr") == "油缸" and prov3.get("gr", {}).get("rule") == "gr.cyl.part.001",
          (out3.get("gr"), prov3.get("gr")))


def test_measured_priority():
    print("== 显式测量规格优先于名称提取 ==")
    rules = build_rules()
    out, prov = RuleEngine(rules).infer("开模油缸 BOD-AG-40-32-V", spec_value="100*80*50")
    check("测量规格时不提取名称型号", out.get("nameSpec") is None, out)
    check("按测量规格匹配（无 100*80*50 规则 → part 级油缸）",
          out.get("gr") == "油缸" and prov.get("gr", {}).get("rule") == "gr.cyl.part.001",
          (out.get("gr"), prov.get("gr")))


def test_switch_off():
    print("== name_spec=False 关闭 ==")
    out, prov = RuleEngine(build_rules()).infer("开模油缸 BOD-AG-40-32-V", name_spec=False)
    check("关闭后不提取型号", out.get("nameSpec") is None, out.get("nameSpec"))
    check("不命中 spec 级规则（来源是 part 级油缸）",
          out.get("gr") == "油缸" and prov.get("gr", {}).get("rule") == "gr.cyl.part.001",
          (out.get("gr"), prov.get("gr")))


def test_engine_regression_imports():
    print("== 兼容性（entry re-export）==")
    from rulespec.entry import extract_model_from_name as f2
    check("entry 仍可 import extract_model_from_name",
          f2("油缸 BOD-AG-63-50-V") == "BOD-AG-63-50-V")


def main():
    test_extract()
    test_infer_namespec()
    test_infer_default_output()
    test_no_misjudge()
    test_measured_priority()
    test_switch_off()
    test_engine_regression_imports()
    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
