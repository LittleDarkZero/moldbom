# -*- coding: utf-8 -*-
"""BomExport CATIA 会话管理 + 缓存清理模块（2026-08-18 重构自 bom_export.py）。

优化 CATIA 会话（关刷新 + 抑制告警弹窗）与系统 Temp 残留目录自动清理。
"""

import os
import shutil
import tempfile
import time

from bom_common import log


def connect_catia():
    """连接/启动 CATIA，并强制转为动态绑定（late binding）。

    静态绑定（gen_py）下 CATIA 的 Document 与 PartDocument 分属不同类型库，
    CastTo 会报 "does not appear in the same library"；动态绑定所有属性在
    运行时解析，无此问题（2026-08-19 修复）。
    """
    import win32com.client
    try:
        raw = win32com.client.GetActiveObject("CATIA.Application")
    except Exception:
        raw = win32com.client.Dispatch("CATIA.Application")
        raw.Visible = True
    return win32com.client.dynamic.Dispatch(raw)


def _setup_catia_session(catia):
    """优化 CATIA 会话：关刷新 + 抑制文件告警弹窗（防无人值守卡死）"""
    catia.RefreshDisplay = False
    # 抑制打开文件时的各类模态弹窗（版本/许可/只读/丢失链接等），
    # 否则后台批处理会被弹窗阻塞。失败仅告警，不中断。
    for attr in ("DisplayFileAlerts", "DisplayFileLockAlerts"):
        try:
            setattr(catia, attr, False)
            log.info("CATIA 已优化: %s=False", attr)
        except Exception as e:
            log.debug("%s 设置失败(忽略): %s", attr, e)
    log.info("CATIA 已优化: RefreshDisplay=OFF")


def _restore_catia_session(catia):
    """恢复 CATIA 会话（2026-08-13 P0-4：补 DisplayFileLockAlerts，与 _setup 全对称）"""
    try:
        catia.RefreshDisplay = True
        catia.DisplayFileAlerts = True
        catia.DisplayFileLockAlerts = True
    except Exception:
        pass


# 本工具在系统 Temp 生成的缓存目录前缀（STP 临时目录 / PyInstaller _MEI 解压目录）
_CACHE_DIR_PREFIXES = ("bom_export_", "bom_batch_", "bom_gui_",
                       "bom_feature_", "diag_stp", "_MEI")


def cleanup_stale_cache(max_age_days: float = 3.0, force: bool = False) -> int:
    """清理系统 Temp 下本工具遗留的缓存目录。

    - 只删目录不碰文件；正在运行（文件被占用）的目录删除失败自动跳过
    - 默认只清理 mtime 超过 max_age_days 天的目录（防止误删仍在使用的）；
      force=True 无视天数全清（--clean-cache）
    返回实际删除的目录数。
    """
    tmp = tempfile.gettempdir()
    try:
        entries = os.listdir(tmp)
    except OSError:
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for name in entries:
        if not name.startswith(_CACHE_DIR_PREFIXES):
            continue
        p = os.path.join(tmp, name)
        if not os.path.isdir(p):
            continue
        try:
            if not force and os.path.getmtime(p) > cutoff:
                continue
            shutil.rmtree(p, ignore_errors=True)
            if not os.path.exists(p):      # 占用中的目录删除失败，跳过
                removed += 1
                log.info("已清理缓存目录: %s", p)
        except Exception:  # noqa: BLE001 清理失败不影响主流程
            pass
    if removed:
        log.info("缓存清理完成: %d 个目录（%s）",
                 removed, "force" if force else f">{max_age_days}天")
    return removed
