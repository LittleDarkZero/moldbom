# -*- coding: utf-8 -*-
"""命令行入口。

用法（在 V2/ 目录下）：
  python -m rulespec validate            # 门禁 G1-G5 全量校验
  python -m rulespec dryrun              # 语料干跑报告
  python -m rulespec new <域> [--scope part] [--priority 500] [--category new] [--name 名称]
  python -m rulespec snapshot            # 手动快照（bump PATCH）
  python -m rulespec restore <版本>       # 回滚到快照
  python -m rulespec snapshots           # 列出快照
  python -m rulespec infer <零件名>       # 推理验证（含名称读型号，--spec 传测量规格）
"""

import argparse
import datetime
import json
import os
import sys

from . import lifecycle
from .corpus import corpus_ids, load_corpus
from .model import load_ruleset
from .validator import dry_run, gate_summary, validate_ruleset


def _paths():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # V2/
    return os.path.join(base, "rules"), os.path.join(base, "corpus")


def cmd_validate(args):
    rules_dir, corpus_dir = _paths()
    manifest, rules = load_ruleset(rules_dir)
    entries = load_corpus(corpus_dir)
    cids = corpus_ids(entries)
    vr = validate_ruleset(rules, cids)
    print(f"规则集 {manifest.get('name', 'default')} v{manifest.get('version')} | 规则 {len(rules)} 条 | 语料 {len(entries)} 条")
    for e in vr["errors"]:
        print("  [错误]", e)
    for w in vr["warnings"]:
        print("  [警告]", w)
    if vr["errors"]:
        return 1
    rep = dry_run(rules, entries)
    ok, text = gate_summary(rep)
    print("  [干跑]", text)
    for w in rep["wrong"][:10]:
        print("    ✗", w)
    for m in rep["missing"][:10]:
        print("    ✗", m)
    return 0 if ok else 2


def cmd_dryrun(args):
    rules_dir, corpus_dir = _paths()
    _, rules = load_ruleset(rules_dir)
    entries = load_corpus(corpus_dir)
    rep = dry_run(rules, entries)
    ok, text = gate_summary(rep)
    print(text)
    for w in rep["wrong"]:
        print("  ✗", w)
    for m in rep["missing"]:
        print("  ✗", m)
    return 0 if ok else 2


def cmd_new(args):
    from .model import check_rule
    from .schema import DOMAINS, PRIORITY_DEFAULT, SCOPES
    rules_dir, _ = _paths()
    _, rules = load_ruleset(rules_dir)
    domain = args.domain
    if domain not in DOMAINS:
        print(f"非法域: {domain}（合法: {', '.join(DOMAINS)}）")
        return 1
    scope = args.scope if args.scope in SCOPES else "part"
    category = (args.category or "new").lower()
    seq = 1
    prefix = f"{domain}.{category}.{scope}."
    for r in rules:
        if r.get("id", "").startswith(prefix):
            n = int(r["id"].rsplit(".", 1)[-1])
            seq = max(seq, n + 1)
    rule = {
        "id": f"{prefix}{seq:03d}",
        "domain": domain,
        "priority": args.priority if args.priority is not None else PRIORITY_DEFAULT,
        "scope": scope,
        "when": {},
        "then": {},
        "meta": {
            "status": "draft",
            "version": 1,
            "author": args.author or "",
            "createdAt": datetime.date.today().isoformat(),
            "updatedAt": datetime.date.today().isoformat(),
            "rationale": "",
            "tests": [],
        },
    }
    if args.name:
        rule["name"] = args.name
    errs = check_rule(rule)
    if errs:
        for e in errs:
            print("  [错误]", e)
        return 1
    rules.append(rule)
    manifest, _ = load_ruleset(rules_dir)
    ver = lifecycle.bump_semver(manifest.get("version", "0.0.1"))
    lifecycle.snapshot(rules_dir, rules, manifest, version=ver)
    print(json.dumps(rule, ensure_ascii=False, indent=2))
    print(f"已创建并快照 v{ver}（draft 状态，保存后需过门禁转 active）")
    return 0


def cmd_snapshot(args):
    rules_dir, _ = _paths()
    manifest, rules = load_ruleset(rules_dir)
    ver = lifecycle.snapshot(rules_dir, rules, manifest)
    print(f"已快照 v{ver}")
    return 0


def cmd_restore(args):
    rules_dir, _ = _paths()
    ver = lifecycle.restore(rules_dir, args.version)
    print(f"已回滚到 v{args.version}，当前版本 v{ver}")
    return 0


def cmd_snapshots(args):
    rules_dir, _ = _paths()
    for v in lifecycle.list_snapshots(rules_dir):
        print(v)
    return 0


def cmd_infer(args):
    """推理单个零件：验证规则识别效果（含 nameSpec 名称读型号）。

    示例：
      python -m rulespec infer "开模油缸 BOD-AG-40-32-V"
      python -m rulespec infer "油缸定位块" --spec 100*80*50
      python -m rulespec infer "定1模框" --no-name-spec
    """
    from .engine import RuleEngine
    rules_dir, _ = _paths()
    manifest, rules = load_ruleset(rules_dir)
    e = RuleEngine(rules)
    out, prov = e.infer(args.name, spec_value=args.spec, name_spec=not args.no_name_spec)
    print(f"规则集 v{manifest.get('version')} | {len(rules)} 条")
    print(f"零件名: {args.name}")
    if out.get("nameSpec"):
        print(f"名称读型号: {out['nameSpec']}（参与匹配的规格: {out.get('spec')}）")
    print(f"GR      : {out.get('gr', '（未命中）')} | 来源: {(prov.get('gr') or {}).get('rule', '-')}")
    print(f"打印规格: {out.get('outputSpec', '（无）')} | 来源: {(prov.get('outputSpec') or {}).get('rule', '-')}")
    print(f"材质    : {out.get('material', '（无）')}")
    print(f"备注    : {repr(out.get('remark', ''))}")
    comps = out.get("companions") or []
    if comps:
        for c in comps:
            print(f"配套    : {c.get('name')} {c.get('spec')} ×{c.get('qty')}@{c.get('gr') or '（随策略）'}")
    else:
        print("配套    : 无")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="RuleSpec 2.0 规则集工具")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("validate", help="门禁 G1-G5 全量校验")
    sub.add_parser("dryrun", help="语料干跑报告")
    pn = sub.add_parser("new", help="生成规则模板（默认值预填）")
    pn.add_argument("domain")
    pn.add_argument("--scope", default="part")
    pn.add_argument("--priority", type=int)
    pn.add_argument("--category", default=None)
    pn.add_argument("--name", default=None)
    pn.add_argument("--author", default=None)
    sub.add_parser("snapshot", help="手动快照")
    pr = sub.add_parser("restore", help="回滚")
    pr.add_argument("version")
    sub.add_parser("snapshots", help="列出快照")
    pi = sub.add_parser("infer", help="推理单个零件（含名称读型号）")
    pi.add_argument("name")
    pi.add_argument("--spec", default=None, help="测量规格（可留空——自动尝试名称读型号）")
    pi.add_argument("--no-name-spec", action="store_true",
                    help="关闭名称读型号（仅用显式规格匹配）")
    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 0
    return {"validate": cmd_validate, "dryrun": cmd_dryrun, "new": cmd_new,
            "snapshot": cmd_snapshot, "restore": cmd_restore,
            "snapshots": cmd_snapshots, "infer": cmd_infer}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
