# -*- coding: utf-8 -*-
"""BomExport 拆分导出模块（2026-08-18 重构自 bom_export.py 模块5 的拆分部分）。

主件逐 Body 复制为独立 CATPart，按 GR 组织文件夹并打包 zip。
"""

import os
import shutil

from bom_common import log
from bom_stp import copy_body_to_new_part
from bom_utils import extract_mold_number, _safe_name, _group_by_gr
from bom_writer import write_bom


def export_split_parts(catia_app, doc, body_refs: dict, results: list, output_dir: str, filepath: str) -> int:
    """将每个主零件拆分为独立 CATPart，按 GR 组织到文件夹并打包 zip（2026-08-13 用户需求）。

    结构:
      {output_dir}/{模号}-parts/
        ├── {GR名}/                          # 小零件 / 标准件 / 镶配件 ...
        │   ├── {GR名}-BOM.xlsx              # 该 GR 细分明细表（主件+紧固件）
        │   ├── {零件号}-{零件名称}/           # 每个主件一个子文件夹
        │   │   └── {模号}-part{零件号}.CATPart  # ASCII 命名（CATIA 兼容）
        │   └── ...
        └── ...
      {output_dir}/{模号}-{GR名}.zip          # 每个 GR 单独打包（小零件.zip / 标准件.zip / ...）

    CATPart 先存 ASCII 临时目录（CATIA SaveAs 中文路径会失败），再用 Python 移到中文文件夹。
    """
    mold_num = extract_mold_number(filepath)

    def _is_comp(r):
        if r.get("_is_companion"):
            return True
        return str(r.get("备注", "")).startswith("→ ")  # 旧数据兼容

    main_parts = [r for r in results if not _is_comp(r)]

    parts_root = os.path.join(output_dir, f"{_safe_name(mold_num)}-parts")
    tmp_dir = os.path.join(output_dir, "_parts_tmp")
    os.makedirs(parts_root, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)

    count = 0
    records = []  # (part_no, name, gr, ascii_filename)
    log.info("拆分 Body → 独立 CATPart (%d 个)...", len(main_parts))
    for item in main_parts:
        name = item["零部件名"]
        part_no = item.get("零件号", "")
        if not part_no:
            continue
        body = body_refs.get(name)
        if body is None:
            log.warning("  未找到 Body: %s", name)
            continue

        # 复制 Body 到新 Part，存 ASCII 临时路径
        new_doc = copy_body_to_new_part(catia_app, doc, body)
        # 2026-08-19: 模号做文件名安全化，避免非法字符导致 SaveAs 失败
        file_name = "{}-part{}.CATPart".format(_safe_name(mold_num), part_no)
        tmp_path = os.path.join(tmp_dir, file_name)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        new_doc.SaveAs(tmp_path)
        new_doc.Close()
        count += 1
        records.append((part_no, name, item.get("零件GR号", ""), file_name))
        log.info("  [%d/%d] %s → %s", count, len(main_parts), name, file_name)

    # 移动到 GR 文件夹下的"{零件号}-{零件名称}"子文件夹
    for part_no, name, gr, file_name in records:
        gr_dir = os.path.join(parts_root, _safe_name(gr or "未分类"))
        part_dir = os.path.join(gr_dir, _safe_name(f"{part_no}-{name}"))
        os.makedirs(part_dir, exist_ok=True)
        shutil.move(os.path.join(tmp_dir, file_name),
                    os.path.join(part_dir, file_name))

    # 每个 GR 文件夹写细分明细表（主件+紧固件都按各自 GR 归组）
    groups = _group_by_gr(results)
    for gr, rows in groups.items():
        gr_dir = os.path.join(parts_root, _safe_name(gr or "未分类"))
        os.makedirs(gr_dir, exist_ok=True)  # 该 GR 可能只有紧固件无主件，文件夹未创建
        write_bom(rows, os.path.join(gr_dir, f"{_safe_name(gr)}-BOM.xlsx"),
                  fmt="xlsx", module_name=str(gr))

    shutil.rmtree(tmp_dir, ignore_errors=True)

    # 每个 GR 文件夹单独打包 zip（放在 output_dir 下，zip 名 = {模号}-{GR}.zip）
    zip_files = []
    for gr in groups:
        gr_safe = _safe_name(gr or "未分类")
        gr_dir = os.path.join(parts_root, gr_safe)
        zip_base = os.path.join(output_dir, f"{_safe_name(mold_num)}-{gr_safe}")
        zip_files.append(shutil.make_archive(zip_base, 'zip', root_dir=gr_dir))

    log.info("拆分完成: %d 个文件，%d 个 GR 文件夹 → %s\n已打包 %d 个 zip: %s",
             count, len(groups), parts_root, len(zip_files),
             ", ".join(os.path.basename(z) for z in zip_files))
    return count
