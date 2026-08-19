# -*- coding: utf-8 -*-
"""RuleSpec 2.0 — V2 新规则系统（独立于旧系统，无 merge 域）。

V2 与旧系统（bom_export/）完全独立：本包不 import 旧系统任何模块，
规则文件只读 V2/rules/，互不干扰。
"""

import json
import os


def _rules_version():
    """读取 V2/rules/manifest.json 的版本（唯一真源），失败时回退 2.0.0。"""
    try:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "rules", "manifest.json"), encoding="utf-8") as f:
            return json.load(f).get("version", "2.0.0")
    except Exception:  # noqa: BLE001 包版本读取失败不影响使用
        return "2.0.0"


__version__ = _rules_version()
