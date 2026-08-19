# -*- coding: utf-8 -*-
"""BomExport STP 导出与实体计数模块（2026-08-18 重构自 bom_export.py 模块2）。

每个 Body 复制到新 Part（CATPrtResultWithOutLink）→ 导出 STP →
计数 MANIFOLD_SOLID_BREP，内建 COM 重试。
"""

import hashlib
import os
import re
import time

from bom_common import log


def count_solids_in_stp(stp_path: str) -> int:
    with open(stp_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    return len(re.findall(r'MANIFOLD_SOLID_BREP\s*\(', content, re.IGNORECASE))


def copy_body_to_new_part(catia_app, src_doc, body):
    """把 body 以 CATPrtResultWithOutLink 方式复制到一个新建 Part，返回新文档。

    供 STP 导出与 --split 拆分复用，消除重复的 Copy/PasteSpecial 样板。
    调用方负责关闭返回的文档。
    """
    new_doc = catia_app.Documents.Add("Part")
    new_body = new_doc.Part.MainBody
    src_sel = src_doc.Selection
    src_sel.Clear(); src_sel.Add(body); src_sel.Copy(); src_sel.Clear()
    new_doc.Activate()
    dst_sel = new_doc.Selection
    dst_sel.Clear(); dst_sel.Add(new_body)
    dst_sel.PasteSpecial("CATPrtResultWithOutLink"); dst_sel.Clear()
    return new_doc


def export_body_to_stp_and_count(catia_app, body, temp_dir: str, seq: int = 0) -> tuple:
    """导出 STP 并计数，返回 (数量, stp路径)。内部已含 3 次重试。

    注意：本函数内部 try/except 吞掉所有异常并在重试耗尽后 return (0, "")，
    不会向外抛异常——因此不再叠加 @retry_on_com_error（那层装饰器永不触发）。

    seq：Body 在遍历中的序号，参与临时文件名哈希，避免同名 Body 互相覆盖
    （2026-08-19 修复）。
    """
    src_doc = body.Parent.Parent.Parent
    docs = catia_app.Documents
    name_hash = hashlib.md5(f"{seq}:{body.Name}".encode('utf-8')).hexdigest()[:16]
    stp_path = os.path.join(temp_dir, f"_tmp_{name_hash}.stp")

    last_error = None
    for attempt in range(3):
        temp_part_doc = None
        try:
            # 稳定性：导出前刷新几何
            if attempt > 0:
                try: src_doc.Part.Update()
                except Exception: pass
                time.sleep(0.3)

            temp_part_doc = copy_body_to_new_part(catia_app, src_doc, body)

            if os.path.exists(stp_path):
                os.remove(stp_path)
            temp_part_doc.ExportData(stp_path, "stp")
            count = count_solids_in_stp(stp_path)

            if count == 0:
                raise Exception("STP中无MANIFOLD_SOLID_BREP（非实体或导出不完整）")

            return (max(count, 1), stp_path)

        except Exception as e:
            last_error = e
            if attempt < 2:
                delay = 2 * (2 ** attempt)
                log.warning("STP导出重试 %d/3 (%s): %s", attempt + 1, body.Name, e)
                time.sleep(delay)
                if os.path.exists(stp_path):
                    try: os.remove(stp_path)
                    except OSError: pass
            else:
                log.error("STP导出失败(已重试3次): %s — %s", body.Name, e)
        finally:
            if temp_part_doc is not None:
                try: temp_part_doc.Close()
                except Exception: pass

    return (0, "")  # 所有重试失败
