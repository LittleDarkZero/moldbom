# -*- coding: utf-8 -*-
"""RuleSpec 2.0 引擎测试（pytest 兼容 + 内置 runner 双模式）。

运行：python tests/test_engine.py  （或 pytest tests/）
"""

import os
import sys

# Windows GBK 控制台也能打印 ✓/✗（2026-08-19 修复）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rulespec import lifecycle, schema                       # noqa: E402
from rulespec.corpus import load_corpus                      # noqa: E402
from rulespec.engine import RuleConflictError, RuleEngine    # noqa: E402
from rulespec.model import check_rule, load_ruleset          # noqa: E402
from rulespec.validator import dry_run, validate_ruleset     # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES_DIR = os.path.join(BASE, "rules")
CORPUS_DIR = os.path.join(BASE, "corpus")


def load():
    manifest, rules = load_ruleset(RULES_DIR)
    return manifest, rules, RuleEngine(rules)


# 出厂示例规则可能被用户清空（从零录入）或被用户数据取代——
# 仅当「调整板规格示例规则」存在时才运行 shipped 断言（用户录入的 gr.wizard.* 不算）
SHIPPED_MARKER = "gr.adjust-plate.spec.001"
HAS_SHIPPED = any(r.get("id") == SHIPPED_MARKER for r in load_ruleset(RULES_DIR)[1])
try:
    import pytest as _pytest
except ImportError:
    _pytest = None


def _shipped_only(fn):
    """标记用例仅在出厂示例规则存在时运行（pytest 用 skipif，内置 runner 识别属性）。"""
    fn._skip_when_empty = True
    if _pytest is not None:
        fn = _pytest.mark.skipif(
            not HAS_SHIPPED,
            reason="示例规则已清空（用户从零录入），跳过 shipped 断言")(fn)
    return fn


# ---------------- 系统边界 ----------------

def test_no_merge_domain():
    """需求 1：新系统不包含同类合并——merge 不在任何枚举中。"""
    assert "merge" not in schema.DOMAINS
    assert "merge" not in schema.OWNERSHIP
    assert "mergeKey" not in schema.WHEN_FIELDS


def test_no_merge_rule_in_shipment():
    _, rules, _ = load()
    for r in rules:
        assert r.get("domain") != "merge", r
        assert "mergeKey" not in r.get("then", {})


def test_v2_independent_from_old():
    """V2 与旧系统互不干扰：规则目录独立，无旧系统键。"""
    keys = set()
    for r in load()[1]:
        keys.update(r.keys())
    for legacy in ("learned_mapping", "categories", "companion_rules", "decision_order"):
        assert legacy not in keys, legacy


# ---------------- 匹配与裁决 ----------------

def test_matcher_ops():
    from rulespec.matcher import match_field
    ctx = {"part.name": "定1模框", "quantity": 4, "spec.value": "40*60*12"}
    assert match_field("part.name", {"op": "contains", "value": "模框"}, ctx)
    assert match_field("part.name", {"op": "regex", "value": "模框|模架"}, ctx)
    assert match_field("part.name", {"op": "prefix", "value": "定1"}, ctx)
    assert match_field("quantity", {"op": "range", "min": 2, "max": 5}, ctx)
    assert match_field("quantity", {"op": "in", "value": [1, 4, 9]}, ctx)
    assert not match_field("part.name", {"op": "contains", "value": "热流道"}, ctx)
    assert match_field("part.name", {"op": "contains", "value": "模框", "negate": True}, {"part.name": "热流道"})


@_shipped_only
def test_spec_canonicalization():
    """× 号与全角自动归一化为 *。"""
    _, _, e = load()
    out, _ = e.infer("调整板", spec_value="40×60×12")
    assert out["gr"] == "仓库备件"
    out2, _ = e.infer("调整板", spec_value="４０*６０*１２")
    assert out2["gr"] == "仓库备件"


@_shipped_only
def test_spec_rule_beats_part_rule():
    """规格级规则靠特异性覆盖零件级（40*60*15 → 小零件而非仓库备件）。"""
    _, _, e = load()
    out, _ = e.infer("调整板", spec_value="40*60*15")
    assert out["gr"] == "小零件"
    out2, _ = e.infer("调整板", spec_value="40*60*12")
    assert out2["gr"] == "仓库备件"


@_shipped_only
def test_spec_rule_requires_spec():
    """无规格上下文时规格级规则不参与（走零件级/兜底）。"""
    _, _, e = load()
    out, _ = e.infer("调整板")
    assert out["gr"] == "仓库备件"  # 兜底


def test_priority_first_wins():
    custom = [
        {"id": "gr.a.part.001", "domain": "gr", "priority": 900, "scope": "part",
         "when": {"part.name": {"op": "contains", "value": "测试件"}}, "then": {"gr": "高优先级"},
         "meta": {"status": "active", "version": 1}},
        {"id": "gr.a.part.002", "domain": "gr", "priority": 300, "scope": "part",
         "when": {"part.name": {"op": "contains", "value": "测试件"}}, "then": {"gr": "低优先级"},
         "meta": {"status": "active", "version": 1}},
    ]
    e = RuleEngine(custom)
    out, prov = e.infer("测试件A")
    assert out["gr"] == "高优先级"
    assert prov["gr"]["rule"] == "gr.a.part.001"


def test_ambiguity_raises():
    """同强度不同值 → 零猜测，运行期报错。"""
    custom = [
        {"id": "gr.b.part.001", "domain": "gr", "priority": 500, "scope": "part",
         "when": {"part.name": {"op": "eq", "value": "冲突件"}}, "then": {"gr": "甲"},
         "meta": {"status": "active", "version": 1}},
        {"id": "gr.b.part.002", "domain": "gr", "priority": 500, "scope": "part",
         "when": {"part.name": {"op": "eq", "value": "冲突件"}}, "then": {"gr": "乙"},
         "meta": {"status": "active", "version": 1}},
    ]
    e = RuleEngine(custom)
    try:
        e.infer("冲突件")
        raise AssertionError("应抛出 RuleConflictError")
    except RuleConflictError:
        pass


def test_remark_append_union():
    custom = [
        {"id": "remark.c.part.001", "domain": "remark", "priority": 500, "scope": "part",
         "when": {"part.name": {"op": "contains", "value": "油缸"}}, "then": {"remark": "主备注"},
         "meta": {"status": "active", "version": 1}},
        {"id": "remark.c.part.002", "domain": "remark", "priority": 400, "scope": "part",
         "when": {"part.name": {"op": "contains", "value": "油缸"}},
         "then": {"remarkAppend": {"add": ["追加A", "追加B"]}},
         "meta": {"status": "active", "version": 1}},
        {"id": "remark.c.part.003", "domain": "remark", "priority": 300, "scope": "part",
         "when": {"part.name": {"op": "contains", "value": "油缸"}},
         "then": {"remarkAppend": {"add": ["追加C"]}},
         "meta": {"status": "active", "version": 1}},
    ]
    e = RuleEngine(custom)
    out, _ = e.infer("油缸活塞")
    assert out["remark"] == "主备注\n追加A\n追加B\n追加C"  # 追加按优先级升序


@_shipped_only
def test_suppress_companions():
    _, _, e = load()
    out, _ = e.infer("热流道系统")
    assert out["companions"] == []
    assert out.get("suppressCompanions") is True
    assert out.get("purchaseFixedQty") == 1


@_shipped_only
def test_companion_gr_follow():
    _, _, e = load()
    out, _ = e.infer("定1模框")
    for c in out["companions"]:
        assert c["gr"] == "模架"


@_shipped_only
def test_filter_skip():
    _, _, e = load()
    out, _ = e.infer("毛坯1")
    assert out.get("skipped") is True


@_shipped_only
def test_normalize_chain():
    _, _, e = load()
    out, _ = e.infer("内六角螺丝")
    assert out["workingName"] == "内六角螺钉"


@_shipped_only
def test_measure_skip():
    _, _, e = load()
    out, _ = e.infer("动模销钉")
    assert out.get("skipMeasurement") is True


@_shipped_only
def test_provenance_audit():
    _, _, e = load()
    out, prov = e.infer("定1模框")
    assert prov["gr"]["rule"] == "gr.mold-frame.part.001"
    assert prov["material"]["rule"] == "material.mold-frame.part.001"
    assert prov["material"]["version"] == 1


def test_cross_domain_consistency_runtime():
    """固定数量却仍有配套 → 运行期跨域一致性错误。"""
    custom = [
        {"id": "gr.d.part.001", "domain": "gr", "priority": 500, "scope": "part",
         "when": {"part.name": {"op": "contains", "value": "外购件"}}, "then": {"gr": "外购"},
         "meta": {"status": "active", "version": 1}},
        {"id": "companion.d.part.001", "domain": "companion", "priority": 500, "scope": "part",
         "when": {"gr": {"op": "eq", "value": "外购"}},
         "then": {"companions": [{"name": "螺钉", "spec": "CB8-16"}]},
         "meta": {"status": "active", "version": 1}},
        {"id": "purchase.d.part.001", "domain": "purchase", "priority": 600, "scope": "part",
         "when": {"gr": {"op": "eq", "value": "外购"}}, "then": {"purchaseFixedQty": 1},
         "meta": {"status": "active", "version": 1}},
    ]
    e = RuleEngine(custom)
    try:
        e.infer("外购件A")
        raise AssertionError("应抛跨域一致性错误")
    except RuleConflictError:
        pass


# ---------------- 门禁 ----------------

def test_gates_reject_bad_rule():
    bad = {"id": "gr.???.part.001", "domain": "gr", "priority": 9999, "scope": "part",
           "when": {"not.a.field": {"op": "contains", "value": "x"}},
           "then": {"not.owned": "y"},
           "meta": {"status": "live", "version": 0}}
    errs = check_rule(bad)
    assert any("id" in e for e in errs)
    assert any("priority 必须" in e for e in errs)
    assert any("词汇表" in e for e in errs)
    assert any("授权" in e for e in errs)
    assert any("status" in e for e in errs)


def test_gates_reject_same_rule_cross_domain():
    r = {"id": "companion.e.part.001", "domain": "companion", "priority": 500, "scope": "part",
         "when": {"part.name": {"op": "contains", "value": "x"}},
         "then": {"companions": [{"name": "螺钉", "spec": "CB8-16"}], "suppressCompanions": True},
         "meta": {"status": "active", "version": 1}}
    errs = check_rule(r)
    assert any("suppressCompanions" in e for e in errs)


def test_gates_static_conflict():
    rs = [
        {"id": "gr.f.part.001", "domain": "gr", "priority": 500, "scope": "part",
         "when": {"part.name": {"op": "eq", "value": "件"}}, "then": {"gr": "甲"},
         "meta": {"status": "active", "version": 1}},
        {"id": "gr.f.part.002", "domain": "gr", "priority": 500, "scope": "part",
         "when": {"part.name": {"op": "eq", "value": "件"}}, "then": {"gr": "乙"},
         "meta": {"status": "active", "version": 1}},
    ]
    vr = validate_ruleset(rs)
    assert any("静态冲突" in e for e in vr["errors"])


@_shipped_only
def test_shipped_ruleset_clean():
    manifest, rules, _ = load()
    assert manifest["version"]
    vr = validate_ruleset(rules, {e["id"] for e in load_corpus(CORPUS_DIR) if e.get("id")})
    assert vr["errors"] == [], vr["errors"]


@_shipped_only
def test_corpus_dryrun_all_pass():
    _, rules, _ = load()
    entries = load_corpus(CORPUS_DIR)
    rep = dry_run(rules, entries)
    assert rep["wrong"] == [], rep["wrong"]
    assert rep["missing"] == [], rep["missing"]
    assert rep["matched"] == rep["total"]


@_shipped_only
def test_rule_meta_tests_referenced():
    """G3：规则 meta.tests 必须指向存在的语料。"""
    _, rules, _ = load()
    cids = {e["id"] for e in load_corpus(CORPUS_DIR) if e.get("id")}
    for r in rules:
        for t in r.get("meta", {}).get("tests", []):
            assert t in cids, f"{r['id']} 引用不存在的语料 {t}"


def test_entry_find_existing_exact_match():
    """同零件判断必须精确匹配（2026-08-19 修复：子串双向匹配会误更新规则）。"""
    from rulespec.entry import find_existing
    when_a = {"part.workingName": {"op": "contains", "value": "调整板"}}
    when_b = {"part.workingName": {"op": "contains", "value": "调整板2"}}
    rules = [
        {"id": "gr.wizard.part.001", "domain": "gr", "scope": "part",
         "when": when_a, "then": {"gr": "仓库备件"},
         "meta": {"status": "active", "version": 1}},
    ]
    assert find_existing(rules, "gr", "part", when_b) is None
    assert find_existing(rules, "gr", "part", when_a) is not None


# ---------------- 生命周期 ----------------

def test_snapshot_restore(tmp=None):
    import tempfile
    import shutil
    import json
    tmp = tempfile.mkdtemp(prefix="rspec_")
    try:
        src_rules, src_manifest = load()[0], load()[1]
        # 构造一个最小规则集目录
        manifest, rules, _ = load()
        ver = lifecycle.snapshot(tmp, rules, manifest)
        assert lifecycle.list_snapshots(tmp) == [ver]
        # 修改后回滚
        rules.append({"id": "gr.x.part.001", "domain": "gr", "priority": 500,
                      "scope": "part", "when": {}, "then": {"gr": "x"},
                      "meta": {"status": "active", "version": 1}})
        new_ver = lifecycle.snapshot(tmp, rules, manifest)
        restored = lifecycle.restore(tmp, ver)
        m2, r2 = load_ruleset(tmp)
        assert restored != ver  # 回滚后版本 bump
        assert all(r.get("id") != "gr.x.part.001" for r in r2)
        _ = new_ver
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_semver_bump():
    assert lifecycle.bump_semver("1.2.3") == "1.2.4"
    assert lifecycle.bump_semver("1.2.3", "minor") == "1.3.0"
    assert lifecycle.bump_semver("1.2.3", "major") == "2.0.0"


# ---------------- 内置 runner ----------------

def main():
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = skipped = 0
    for t in tests:
        if getattr(t, "_skip_when_empty", False) and not HAS_SHIPPED:
            print(f"  ⊘ 跳过 {t.__name__}（示例规则已清空，从零录入中）")
            skipped += 1
            continue
        try:
            t()
            print(f"  ✓ {t.__name__}")
            passed += 1
        except Exception:
            print(f"  ✗ {t.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} 通过 / {failed} 失败 / {skipped} 跳过（共 {len(tests)}）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
