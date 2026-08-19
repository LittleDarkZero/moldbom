# -*- coding: utf-8 -*-
"""V2 规则引擎桥接（2026-08-13 重构）：V2 成为唯一规则源。

老 gr_rules.json 系统已整体删除（learned_mapping/companion_rules/categories 等
全部废弃），bom_export 所有规则推理改走本桥接层：
  - get_engine()        加载 V2 全部 active 规则（10 域），供解析/推理消费
  - infer_part(name)    零件级推理（name_spec=False）→ bom_export.infer_gr_and_detail
  - apply_v2_spec(...)  规格级精化（规格已知后：GR/材质/备注/配套/outputSpec 改写）
  - extract_name_spec   名称读型号（测量前拦截）

V2 目录解析：开发 = 同级 ../V2；frozen（exe 打包）= _MEIPASS/V2 随包分发。
加载失败（目录缺失 / 规则损坏 / import 错误）→ 返回 None，调用方按默认值兜底
（DEFAULT_GR 等常量在 bom_export 中定义），老部署不受影响。
"""

import logging
import os
import sys
import time

log = logging.getLogger("bom_export.v2")


def _v2_dir():
    """定位 V2 根目录（含 rulespec 包与 rules/）。

    优先级（frozen 模式）：
      1. exe 旁外部 V2/ — 规则热更新写入处，外部目录需同时有 rulespec 和 rules
      2. _MEIPASS/V2   — PyInstaller 随包分发的内置副本（兜底）
      3. _MEIPASS      — 极端兜底

    开发模式：脚本同级 ../V2。
    """
    if getattr(sys, "frozen", False):
        exe_base = os.path.dirname(os.path.abspath(sys.executable))
        meipass = getattr(sys, "_MEIPASS", None) or exe_base
        # 外部目录优先（热更新规则所在）
        for base in (os.path.join(exe_base, "V2"),
                     os.path.join(meipass, "V2"), meipass):
            if os.path.isdir(os.path.join(base, "rulespec")) and \
               os.path.isdir(os.path.join(base, "rules")):
                return base
        return meipass
    return os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "V2"))


_ENGINE = None
_LOAD_ERR = None
_LOADED = False
_FAILED_AT = None          # 上次加载失败时间戳（冷却期后自动重试）
_RETRY_COOLDOWN = 30       # 秒


def get_engine():
    """加载 V2 规则引擎（全部 active 规则，10 域）。

    返回 dict: {"engine": RuleEngine, "canonical_spec": fn,
               "extract_model_from_name": fn, "version": str}；
    失败返回 None（调用方回退默认值）。
    """
    global _ENGINE, _LOAD_ERR, _LOADED, _FAILED_AT
    if _LOADED:
        return _ENGINE
    if _FAILED_AT is not None and time.time() - _FAILED_AT < _RETRY_COOLDOWN:
        return None
    try:
        v2 = _v2_dir()
        rules_dir = os.path.join(v2, "rules")
        if not os.path.isdir(rules_dir):
            _LOAD_ERR = f"V2 rules 目录不存在: {rules_dir}"
            _FAILED_AT = time.time()
            return None
        if v2 not in sys.path:
            sys.path.insert(0, v2)
        from rulespec.model import load_ruleset
        from rulespec.engine import RuleEngine
        from rulespec.matcher import canonical_spec, extract_model_from_name
        manifest, rules = load_ruleset(rules_dir)
        active = [r for r in rules if r.get("meta", {}).get("status") == "active"]
        _ENGINE = {
            "engine": RuleEngine(active),
            "canonical_spec": canonical_spec,
            "extract_model_from_name": extract_model_from_name,
            "version": manifest.get("version", "?"),
        }
        _LOADED = True
        _FAILED_AT = None
        log.info("V2 规则引擎已加载: %d 条规则（v%s）",
                 len(active), manifest.get("version", "?"))
        return _ENGINE
    except Exception as e:  # noqa: BLE001 桥接失败必须回退，不能拖垮主流程
        _LOAD_ERR = str(e)
        _FAILED_AT = time.time()
        log.warning("V2 规则引擎加载失败（按默认值兜底，%ds 后重试）: %s",
                    _RETRY_COOLDOWN, e)
        return None


def reset_engine():
    """清空引擎缓存，下次 get_engine() 重新加载。

    规则热更新后调用，使新规则立即生效（无需重启 exe）。
    注意：仅影响当前进程；多进程 worker 各自持有引擎，
    规则更新要求工具处于空闲状态（GUI running == False）。
    """
    global _ENGINE, _LOAD_ERR, _LOADED, _FAILED_AT
    _ENGINE, _LOAD_ERR, _LOADED, _FAILED_AT = None, None, False, None
    log.info("V2 引擎缓存已重置，下次调用将重新加载规则")


def infer_part(name):
    """零件级推理（规格未知）：返回 (out, prov)；V2 不可用返回 None。

    调用方（bom_export.infer_gr_and_detail）负责异常兜底。
    """
    eng = get_engine()
    if eng is None:
        return None
    return eng["engine"].infer(name, name_spec=False)


def extract_name_spec(name):
    """零件名 → 型号（nameSpec，测量前拦截用）；V2 不可用时返回 None。"""
    eng = get_engine()
    if eng:
        return eng["extract_model_from_name"](name)
    return None


def apply_v2_spec(results):
    """规格已知后，用 V2 规则精化（替代旧 _apply_spec_gr，2026-08-13 唯一规格级路径）。

    原地改 results；V2 不可用返回 None（调用方保持原值）。
    - 首个实体的推理结果整体并入 item["_v2"]（含配套/免配套/编号等下游消费字段）
    - gr / material / remark：spec 级规则命中才覆盖（provenance 记录即命中）
    - outputSpec：命中改写 item["规格"]（BOM 打印型号，如 40*60*12 → 40*60）
    - 多实体：item["_spec_list"] 存在时【逐实体】匹配 outputSpec，改写后重新计数合并
    - RuleConflictError：该项保持原值，log 警告（零猜测设计不静默）
    """
    eng = get_engine()
    if eng is None:
        return None
    e = eng["engine"]
    canon = eng["canonical_spec"]
    try:
        from rulespec.engine import RuleConflictError
    except ImportError:
        RuleConflictError = Exception
    hit = 0
    for item in results:
        name = item.get("零部件名", "")
        raw_list = item.get("_spec_list")
        if raw_list:
            specs = [canon(s) for s in raw_list if s]
        else:
            s = canon(item.get("规格", ""))
            specs = [s] if s else []
        if not specs:
            continue
        try:
            from collections import Counter
            spec_counts = Counter(specs)
            out_specs = []
            gr = mat = rm = None
            gr_hit = mat_hit = rm_hit = False
            companions_by_spec = []   # [{count, companions}] 逐规格（供 add_companions 按规格算数量）
            # 2026-08-19 性能修复：同规格实体只推理一次，输出按实体数展开
            # （多实体标准件数量可达几百上千，原实现对每个实体重复 infer）
            for i, (sp, cnt) in enumerate(spec_counts.items()):
                out, prov = e.infer(name, spec_value=sp, name_spec=False)
                if i == 0:   # 首个实体：GR/材质/备注/配套（与旧"最紧包围单值"行为最接近）
                    if "gr" in prov and out.get("gr"):
                        gr, gr_hit = out["gr"], True
                    if "material" in prov and out.get("material"):
                        mat, mat_hit = out["material"], True
                    if "remark" in prov and out.get("remark"):
                        rm, rm_hit = out["remark"], True
                    # 整体并入 _v2：配套/免配套/编号/外购等下游消费（2026-08-13）
                    item["_v2"] = {**item.get("_v2", {}),
                                   **{k: v for k, v in out.items() if k != "provenance"}}
                # 逐规格收集配套件：多实体异规格时，每个规格的紧固件数量可能不同，
                # 需要 (该规格实体数 × 该规格单件紧固件数) 求和（2026-08-13 修复）
                comps = out.get("companions") or []
                if comps:
                    companions_by_spec.append({
                        "count": cnt,
                        "companions": comps,
                    })
                ospec = out.get("outputSpec") if "outputSpec" in prov else None
                out_specs.extend([ospec or sp] * cnt)
            item["_companions_by_spec"] = companions_by_spec
            # ④ gr / material / remark
            if gr_hit:
                old = item.get("零件GR号", "")
                if gr != old:
                    item["零件GR号"] = gr
                    log.info("V2 规格级 GR: %s %s → %s (原 %s)",
                             name, specs[0], gr, old or "-")
            if mat_hit:
                item["材质"] = mat
                log.info("V2 规格级材质: %s %s → %s", name, specs[0], mat)
            if rm_hit:
                item["加工备注"] = rm
                log.info("V2 规格级备注: %s %s → %s", name, specs[0],
                         rm.replace("\n", " / ")[:60])
            # ⑥ outputSpec：逐实体改写后重新计数合并
            # （2026-08-19：改从 bom_measure 引入，避免桥接层反向依赖门面 bom_export）
            from bom_measure import _format_spec_counts
            new_spec = _format_spec_counts(out_specs)
            if new_spec != item.get("规格", ""):
                log.info("V2 打印规格: %s %s → %s (原 %s)",
                         name, specs[0], new_spec.replace("\n", " / "),
                         item.get("规格", "-").replace("\n", " / "))
            item["规格"] = new_spec
        except RuleConflictError as ex:
            log.warning("V2 规格规则冲突（该项保持原值）: %s", ex)
            continue
        except Exception as ex:  # noqa: BLE001 单项失败不影响整体
            log.warning("V2 规格推理异常 %s %s: %s",
                        name, specs[0] if specs else "", ex)
            continue
        hit += 1
    if hit:
        log.info("V2 规格级精化: %d 项", hit)
    return results
