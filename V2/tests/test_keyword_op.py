# -*- coding: utf-8 -*-
"""keyword 命名规则关键词算子测试（2026-08-05 实装）。

语义：零件名按分割符（空格/逗号/斜杠/竖线/顿号，含全角）分词，
规则值须等于某个【完整词】才命中——『油缸』不会误命中『开模油缸』。
"""
import os
import sys

# Windows GBK 控制台也能打印 ✓/✗（2026-08-19 修复）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rulespec.engine import RuleEngine
from rulespec.matcher import match_field
from rulespec.model import check_rule

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


def test_matcher_level():
    print("== matcher 层 ==")
    ctx = {"part.workingName": "开模油缸 BOD-AG-40-32-V"}
    check("keyword 开模油缸 → 命中",
          match_field("part.workingName", {"op": "keyword", "value": "开模油缸"}, ctx))
    check("keyword 油缸 → 不误命中（完整词）",
          not match_field("part.workingName", {"op": "keyword", "value": "油缸"}, ctx))
    check("keyword BOD-AG-40-32-V → 命中（型号也是词）",
          match_field("part.workingName", {"op": "keyword", "value": "BOD-AG-40-32-V"}, ctx))
    check("keyword 列表任一命中",
          match_field("part.workingName", {"op": "keyword", "value": ["合模油缸", "开模油缸"]}, ctx))
    ctx2 = {"part.workingName": "油缸 定位块"}
    check("keyword 油缸 vs『油缸 定位块』→ 命中（独立词）",
          match_field("part.workingName", {"op": "keyword", "value": "油缸"}, ctx2))
    ctx3 = {"part.workingName": "油缸定位块"}
    check("keyword 油缸 vs『油缸定位块』（单段无分隔符）→ 不命中",
          not match_field("part.workingName", {"op": "keyword", "value": "油缸"}, ctx3))
    check("keyword 逗号分隔也命中",
          match_field("part.workingName", {"op": "keyword", "value": "开模油缸"},
                      {"part.workingName": "开模油缸,BOD-AG-40-32-V"}))
    check("negate 生效",
          not match_field("part.workingName",
                          {"op": "keyword", "value": "油缸", "negate": True},
                          {"part.workingName": "油缸 定位块"}))


def test_engine_level():
    print("== 引擎层 ==")
    rules = [
        {"id": "gr.open.part.001", "domain": "gr", "priority": 500, "scope": "part",
         "when": {"part.workingName": {"op": "keyword", "value": "开模油缸"}},
         "then": {"gr": "油缸"},
         "meta": {"status": "active", "version": 1, "author": "t",
                  "createdAt": "2026-08-05", "updatedAt": "2026-08-05"}},
        {"id": "gr.cyl.part.001", "domain": "gr", "priority": 300, "scope": "part",
         "when": {"part.workingName": {"op": "contains", "value": "油缸"}},
         "then": {"gr": "油缸"},
         "meta": {"status": "active", "version": 1, "author": "t",
                  "createdAt": "2026-08-05", "updatedAt": "2026-08-05"}},
    ]
    e = RuleEngine(rules)
    out, prov = e.infer("开模油缸 BOD-AG-40-32-V")
    check("开模油缸 BOD-AG-40-32-V → GR=油缸（keyword 规则）",
          out.get("gr") == "油缸" and prov.get("gr", {}).get("rule") == "gr.open.part.001",
          (out.get("gr"), prov.get("gr")))
    # 普通油缸（非开模）→ contains 低优先级命中
    out2, prov2 = e.infer("油缸 定位块")
    check("油缸 定位块 → GR=油缸（contains 规则）",
          out2.get("gr") == "油缸" and prov2.get("gr", {}).get("rule") == "gr.cyl.part.001",
          (out2.get("gr"), prov2.get("gr")))


def test_gate():
    print("== 门禁 ==")
    ok = check_rule({"id": "gr.t.part.001", "domain": "gr", "priority": 500, "scope": "part",
                     "when": {"part.workingName": {"op": "keyword", "value": "开模油缸"}},
                     "then": {"gr": "油缸"},
                     "meta": {"status": "active", "version": 1, "author": "t",
                              "createdAt": "2026-08-05", "updatedAt": "2026-08-05"}})
    check("check_rule 接受 keyword", not ok, ok)
    bad = check_rule({"id": "gr.t.part.001", "domain": "gr", "priority": 500, "scope": "part",
                      "when": {"part.workingName": {"op": "keyword"}},
                      "then": {"gr": "油缸"},
                      "meta": {"status": "active", "version": 1, "author": "t",
                               "createdAt": "2026-08-05", "updatedAt": "2026-08-05"}})
    check("缺 value 被拦", any("缺 value" in x for x in bad), bad)


def main():
    test_matcher_level()
    test_engine_level()
    test_gate()
    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
