# -*- coding: utf-8 -*-
"""BomExport 批量处理模块（2026-08-18 重构自 bom_export.py）。

批量处理文件夹中所有 CATPart，输出合并 Excel（单实例串行）。
"""

import glob
import os
import shutil
import tempfile

from bom_common import log
from bom_pipeline import default_ctx, run_pipeline, DEFAULT_STAGES
from bom_companions import add_companions
from bom_numbering import assign_part_numbers
from bom_writer import write_bom_excel


def _batch_parse_one(catia_app, cp, per_file_stages):
    """串行模式：单文件 解析→填充规格，返回 results 列表。

    异常安全（2026-08-13 P0-4）：pipeline 失败也保证关闭源文档、清理临时目录。
    """
    temp_dir = tempfile.mkdtemp(prefix="bom_batch_")
    src_doc = None
    try:
        ctx = default_ctx(catia_app, os.path.abspath(cp), temp_dir, "")
        try:
            ctx = run_pipeline(ctx, stages=per_file_stages)
        finally:
            # 异常路径也要先拿到 src_doc，再交给外层 finally 关闭（2026-08-19 修复：
            # 原来 run_pipeline 抛异常时 src_doc 仍为 None，CATIA 文档会泄漏）
            src_doc = ctx.get("src_doc")
        for item in ctx["results"]:
            item["_source"] = os.path.basename(cp)
        return ctx["results"]
    finally:
        if src_doc is not None:
            try: src_doc.Close()
            except Exception: pass
        try: shutil.rmtree(temp_dir)
        except OSError: pass


def batch_process(catia_app, folder_path: str):
    """批量处理文件夹中所有 CATPart，输出合并 Excel（单实例串行）。

    注: 已实测 CATIA 为单实例 COM 服务器，多实例并行不可行（RPC 冲突），
        故固定串行。逐文件只跑 解析→填充规格，配套/编号/写出在跨文件合并后统一做。
    """
    catparts = sorted(glob.glob(os.path.join(folder_path, "*.CATPart")))
    if not catparts:
        log.error("文件夹中无 CATPart 文件: %s", folder_path)
        return

    log.info("批量处理 %d 个 CATPart", len(catparts))
    # 逐文件: 解析 → 填充规格；配套/编号/写出在跨文件合并后统一做。
    per_file_stages = [(n, f) for n, f in DEFAULT_STAGES
                       if n in ("解析CATPart", "填充规格")]

    all_results = []
    for idx, cp in enumerate(catparts, 1):
        log.info("[%d/%d] %s", idx, len(catparts), os.path.basename(cp))
        try:
            all_results.extend(_batch_parse_one(catia_app, cp, per_file_stages))
        except Exception as e:
            log.error("[%d/%d] %s 失败: %s", idx, len(catparts),
                      os.path.basename(cp), e)

    all_results = add_companions(all_results)
    all_results = assign_part_numbers(all_results)
    out = os.path.join(folder_path, "batch_BOM.xlsx")
    write_bom_excel(all_results, out)
    log.info("批量结果: %s (%d 行)", out, len(all_results))
