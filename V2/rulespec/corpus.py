# -*- coding: utf-8 -*-
"""基准语料（golden corpus）：加载 + 引用校验辅助。"""

import glob
import json
import os


def load_corpus(corpus_dir):
    """加载 corpus/ 下全部 *.json（每个文件可为列表或 {"entries": [...]}）。"""
    entries = []
    for path in sorted(glob.glob(os.path.join(corpus_dir, "*.json"))):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        items = data if isinstance(data, list) else data.get("entries", [])
        entries.extend(items)
    return entries


def corpus_ids(entries):
    return {e.get("id") for e in entries if e.get("id")}
