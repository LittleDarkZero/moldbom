# -*- coding: utf-8 -*-
"""BomExport 插件化 Pipeline 模块（2026-08-18 重构自 bom_export.py）。

stage 可插拔，ctx dict 贯穿全程；内置 6 阶段
（解析CATPart → 填充规格 → 配套补全 → 零件编号 → 写出BOM → 拆分文件），
统一入口 process_one_part / default_ctx。

设计：每个 stage 是一个可调用对象，接收并返回 ctx dict。
  ctx 约定字段：
    catia, filepath, temp_dir, progress_cb, split_dir  (输入)
    results, skipped, excluded, src_doc, body_refs     (parse 产物)
    do_split, output_path, out_fmt                     (输出控制)
新增处理阶段 = 定义一个 fn(ctx)->ctx 并 register_stage()，无需改主流程。
stage 可通过返回 ctx["abort"]=True 提前终止后续阶段。
"""

import os
import shutil
import time

from bom_common import log
from bom_utils import extract_mold_number
from bom_parser import parse_catpart
from bom_measure import fill_specs_from_stp
from bom_companions import add_companions
from bom_numbering import assign_part_numbers
from bom_writer import write_bom, write_bom_by_gr
from bom_split import export_split_parts

_PIPELINE_STAGES = []  # [(name, fn)]


def register_stage(name: str, fn, position: int = None):
    """注册一个处理阶段。position=None 追加到末尾。"""
    entry = (name, fn)
    if position is None:
        _PIPELINE_STAGES.append(entry)
    else:
        _PIPELINE_STAGES.insert(position, entry)
    return fn


def stage(name, position=None):
    """装饰器版注册：@stage('填充规格')"""
    def deco(fn):
        register_stage(name, fn, position)
        return fn
    return deco


def run_pipeline(ctx: dict, stages=None) -> dict:
    """依序执行 stages（默认全部已注册），每个阶段接收并返回 ctx。"""
    seq = _PIPELINE_STAGES if stages is None else stages
    for name, fn in seq:
        if ctx.get("abort"):
            log.info("Pipeline 在 [%s] 前终止", name)
            break
        t = time.time()
        try:
            ctx = fn(ctx) or ctx
        except Exception as e:
            ctx["error"] = e
            log.error("Pipeline 阶段 [%s] 失败: %s", name, e)
            raise
        ctx.setdefault("_timings", {})[name] = time.time() - t
    return ctx


# ---- 内置阶段（与主线性流程一一对应）----

def _stage_parse(ctx):
    r = parse_catpart(ctx["catia"], ctx["filepath"], ctx["temp_dir"],
                      progress_cb=ctx.get("progress_cb"))
    ctx["results"], ctx["skipped"], ctx["excluded"], ctx["src_doc"], ctx["body_refs"] = r
    return ctx


def _stage_fill_specs(ctx):
    ctx["results"] = fill_specs_from_stp(ctx["results"])
    return ctx


def _stage_companions(ctx):
    ctx["results"] = add_companions(ctx["results"])
    return ctx


def _stage_assign(ctx):
    ctx["results"] = assign_part_numbers(ctx["results"])
    return ctx


def _stage_write(ctx):
    ctx["output_path"] = write_bom(ctx["results"], ctx["output_path"],
                                   fmt=ctx.get("out_fmt", "xlsx"))
    # 2026-08-13 新功能1: 自动按 GR 拆分成多个 BOM 文件（与总 BOM 同目录）
    mold_num = extract_mold_number(ctx["filepath"])
    ctx["gr_bom_paths"] = write_bom_by_gr(
        ctx["results"], ctx["output_path"], mold_num, fmt=ctx.get("out_fmt", "xlsx"))
    return ctx


def _stage_split(ctx):
    if ctx.get("do_split"):
        os.makedirs(ctx["split_dir"], exist_ok=True)
        ctx["split_count"] = export_split_parts(
            ctx["catia"], ctx["src_doc"], ctx["body_refs"],
            ctx["results"], ctx["split_dir"], ctx["filepath"])
        # 2026-08-13 用户需求：「发给蔡师傅」文件夹汇总所有明细表（完整 + 细分）
        send_dir = os.path.join(ctx["split_dir"], "发给蔡师傅")
        os.makedirs(send_dir, exist_ok=True)
        # 完整明细表（总 BOM）
        out_path = ctx.get("output_path", "")
        if out_path and os.path.exists(out_path):
            shutil.copy2(out_path, os.path.join(send_dir, os.path.basename(out_path)))
        # 细分明细表（按 GR 拆分）
        for p in ctx.get("gr_bom_paths", []):
            if os.path.exists(p):
                shutil.copy2(p, os.path.join(send_dir, os.path.basename(p)))
        log.info("已生成「发给蔡师傅」文件夹: %s", send_dir)
    return ctx


# 注册默认管线（顺序即执行顺序）
DEFAULT_STAGES = [
    ("解析CATPart", _stage_parse),
    ("填充规格", _stage_fill_specs),
    ("配套补全", _stage_companions),
    ("零件编号", _stage_assign),
    ("写出BOM", _stage_write),
    ("拆分文件", _stage_split),
]
for _n, _f in DEFAULT_STAGES:
    register_stage(_n, _f)


def default_ctx(catia, filepath, temp_dir, output_path,
                progress_cb=None, split_dir="", do_split=False, out_fmt="xlsx"):
    """构造一份默认管线输入 ctx。"""
    return {
        "catia": catia, "filepath": filepath, "temp_dir": temp_dir,
        "output_path": output_path, "progress_cb": progress_cb,
        "split_dir": split_dir, "do_split": do_split, "out_fmt": out_fmt,
        "results": [], "skipped": 0, "excluded": 0,
        "src_doc": None, "body_refs": {},
    }


def process_one_part(catia, catpart_path, output_path, temp_dir,
                     progress_cb=None, do_split=False, split_dir="",
                     out_fmt="xlsx", close_doc=True):
    """处理单个 CATPart 的统一入口（CLI/GUI/batch 共用），返回完成的 ctx。

    调用方负责临时目录的创建与清理。close_doc=False 时保留文档打开（供调用方继续操作）。

    异常安全（2026-08-13 P0-4）：pipeline 任一阶段抛异常也会在 finally 中
    关闭源文档（close_doc=True 时）——不再残留 CATIA 文档。
    """
    ctx = default_ctx(catia, os.path.abspath(catpart_path), temp_dir, output_path,
                      progress_cb=progress_cb, split_dir=split_dir,
                      do_split=do_split, out_fmt=out_fmt)
    try:
        ctx = run_pipeline(ctx)
        return ctx
    finally:
        if close_doc and ctx.get("src_doc") is not None:
            try: ctx["src_doc"].Close()
            except Exception: pass
