# -*- coding: utf-8 -*-
"""BomExport 通用工具模块（2026-08-18 重构）。

模号提取、文件名安全化、按 GR 分组——被 bom_split 与 bom_writer 共用，
抽到最底层模块以避免循环依赖。
"""

import os
import re

from bom_common import MOLD_NUMBER_STRIP


def extract_mold_number(filename: str) -> str:
    """从文件名提取模号，如 26-1-42-DING1-ZC-TEST → 26-1-42-DING1"""
    base = os.path.splitext(os.path.basename(filename))[0]
    # 截断段名单（结构常量 MOLD_NUMBER_STRIP，2026-08-13 随旧规则系统外置化删除）
    strip_segments = MOLD_NUMBER_STRIP
    parts = base.split('-')
    result = []
    skip_rest = False
    for p in parts:
        if p.upper() in [s.upper() for s in strip_segments]:
            skip_rest = True
            continue
        if skip_rest:
            continue
        result.append(p)
    return '-'.join(result)


def _safe_name(s):
    """文件/文件夹名安全化：替换 Windows 非法字符并去空白。"""
    return re.sub(r'[\\/:*?"<>|]', '_', str(s)).strip()


def _group_by_gr(results: list):
    """按零件 GR 分组（主件与紧固件都按各自 GR 名归组，2026-08-13 用户需求）。

    紧固件（配套件）也有自己的 GR（模架/仓库备件/标准件等），拆分时按各自 GR 归组，
    不再跟随父件。返回 {GR名: [rows]}，组内顺序与 results 一致。
    """
    groups = {}
    for item in results:
        gr = item.get("零件GR号", "") or "未分类"
        groups.setdefault(gr, []).append(item)
    return groups
