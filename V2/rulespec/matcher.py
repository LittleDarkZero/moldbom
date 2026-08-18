# -*- coding: utf-8 -*-
"""条件匹配器：when 的 9 种算子（eq/contains/keyword/prefix/suffix/regex/in/range/exists）。

keyword = 命名规则关键词（2026-08-05）：零件名按分割符分词，规则值须等于某个
完整词才命中——『油缸』不误命中『开模油缸』；完全命中=命名规则命中，结果可靠。

匹配语义完全显式：不做任何隐式特判（旧系统斜杠 AND 拆分等历史语义不继承）。
字段缺失时返回 False（exists 除外）。
"""

import re


def canonical_spec(s):
    """规格字符串规范化：全角→半角、×→*、⌀→Φ、去空白。匹配前统一执行，保证确定性。

    2026-08-11: 测量输出保留一位小数（40.0*60.0*12.0）——整数后的 .0 归一去尾
    （40.0 → 40、40.0*60.0*12.0 → 40*60*12），使规格级规则（录 40*60*12）
    不受输出格式变化影响；非整数小数（0.4、10.05）原样保留。
    2026-08-13: 直径符号归一化——规则编辑器录入 ⌀（U+2300 直径符号），测量引擎输出
    Φ（U+03A6），此前不归一化导致圆柱件规格级规则全部失配（如 ⌀31.5*12 vs Φ31.5*12）。
    """
    if s is None:
        return None
    t = str(s)
    out = []
    for ch in t:
        code = ord(ch)
        if ch == "×" or ch == "X" or ch == "x" or ch == "ｘ":
            out.append("*")
            continue
        if ch in ("⌀", "ϕ", "⍉"):            # 直径符号变体 → 统一 Φ
            out.append("Φ")
            continue
        if ch == "（":
            out.append("(")
            continue
        if ch == "）":
            out.append(")")
            continue
        if 0xFF01 <= code <= 0xFF5E:          # 全角 ASCII 区 → 半角
            out.append(chr(code - 0xFEE0))
            continue
        out.append(ch)
    t = "".join(out).replace(" ", "")
    # 整数后的 .0 归一去尾（lookbehind 数字、lookahead 非数字：40.0 → 40；0.4 保留）
    t = re.sub(r"(?<=\d)\.0(?![0-9.])", "", t)
    # 长方体规格维度按数值降序（测量引擎输出 L*W*H 降序）——规则侧录入顺序不定
    # （2*50*225 或 225*50*2 均能匹配）。仅纯"数字*数字…"规格排序；含 Φ/字母/连字符
    # 的圆柱（Φ直径×长度）或型号（D32 / CB12-50 / BOD-AG-63-50-V）原样保留。
    if re.fullmatch(r"\d+(?:\.\d+)?(?:\*\d+(?:\.\d+)?)+", t):
        parts = t.split("*")
        t = "*".join(sorted(parts, key=lambda p: float(p), reverse=True))
    return t


def match_field(field, matcher, ctx):
    """评估单个条件。matcher: {"op":..., "value":.../min/max, "negate":bool}。"""
    op = matcher.get("op")
    neg = bool(matcher.get("negate", False))
    if op == "exists":
        base = (field in ctx) == bool(matcher.get("value", True))
        return (not base) if neg else base
    if field not in ctx:
        return False
    v = ctx[field]
    # spec.value 双端归一化：规则里手写的 ×/全角/大写X 与输入侧统一（2026-08-04）
    # （输入侧在 engine.infer 已归一化；这里归一化规则侧值，保证手编规则也能匹配）
    if field == "spec.value" and op not in ("regex", "keyword"):
        if op == "in":
            matcher = {**matcher,
                       "value": [canonical_spec(x) for x in (matcher.get("value") or [])]}
        else:
            matcher = {**matcher, "value": canonical_spec(matcher.get("value"))}
    if op == "eq":
        base = _eq(v, matcher.get("value"))
    elif op == "contains":
        base = str(matcher.get("value", "")) in str(v)
    elif op == "keyword":
        # 命名规则关键词：分词后完整词相等（value 可为列表=任一命中）。
        # 与 contains 的区别：『油缸』不会误命中『开模油缸』（那是完整词，不是子串匹配）。
        want = matcher.get("value")
        words = NAME_SPEC_SEP.split(str(v))
        if isinstance(want, (list, tuple)):
            base = any(str(w) in words for w in want)
        else:
            base = str(want) in words
    elif op == "prefix":
        base = str(v).startswith(str(matcher.get("value", "")))
    elif op == "suffix":
        base = str(v).endswith(str(matcher.get("value", "")))
    elif op == "regex":
        # search 语义（包含匹配）：用户可用 ^…$ 显式锚定
        base = re.search(str(matcher.get("value", "")), str(v)) is not None
    elif op == "in":
        base = v in matcher.get("value", [])
    elif op == "range":
        num = _to_num(v)
        base = num is not None and matcher.get("min", 0) <= num <= matcher.get("max", 0)
    else:
        base = False
    return (not base) if neg else base


def _eq(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    na, nb = _to_num(a), _to_num(b)
    if na is not None and nb is not None:
        return na == nb
    return str(a) == str(b)


def _to_num(v):
    try:
        if isinstance(v, bool):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------- 零件名读型号（nameSpec） ----------

NAME_SPEC_SEP = re.compile(r"[\s,，;；/／|｜、]+")


def extract_model_from_name(name):
    """从零件名识别型号（2026-08-05 用户需求：标准件如『油缸 BOD-AG-63-50-V』）。

    按分割符切分零件名，若切出 ≥2 段，则从后往前找第一个含字母/数字的段
    （型号特征）作为型号；找不到返回 None——单段名（如『定1模框』）与
    纯中文分段（如『油缸 定位块』）都不会误判。

    引擎推理（engine.infer name_spec=True）与编辑器录入（table_editor/wizard
    勾选 nameSpec）共用本函数。
    """
    if not name:
        return None
    parts = [p.strip() for p in NAME_SPEC_SEP.split(str(name)) if p.strip()]
    if len(parts) < 2:
        return None
    for p in reversed(parts):
        if re.search(r"[0-9A-Za-z]", p):
            return p
    return None
