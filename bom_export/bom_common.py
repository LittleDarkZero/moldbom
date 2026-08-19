# -*- coding: utf-8 -*-
"""BomExport 基础设施模块（2026-08-18 重构自 bom_export.py）。

统一承载：版本号、路径解析、日志系统、未捕获异常兜底、COM 重试装饰器，
以及非业务的结构常量（业务规则一律外置在 V2/rules/，见 V2 规则系统）。

本模块是所有其他 bom_* 模块的底层依赖，禁止反向 import。
"""

import logging
import os
import sys
import tempfile
import traceback

# 版本号唯一真源（updater.py 与 bom_cli.py 读取此常量做版本比对）
__version__ = "9.3.0"


# -------- CATIA COM 兼容助手 --------
def as_part_document(doc):
    """把 CATIA COM Document 安全转成 PartDocument 接口。

    规避静态绑定（gen_py）下基础 Document 没有 Part 属性的问题：
    Documents.Open() / Documents.Add() 返回的是 Document，而 Part 属性
    只存在于 PartDocument（2026-08-19 运行时报错修复）。
    """
    try:
        _ = doc.Part
        return doc
    except AttributeError:
        from win32com.client import CastTo
        return CastTo(doc, "PartDocument")


# -------- 路径解析 --------
def _app_dir():
    """获取打包资源目录（兼容 PyInstaller 打包）。

    frozen 时返回 sys._MEIPASS——onefile exe 运行时解压的资源（V2 规则等
    datas）所在处，规则文件必须从这里读取。
    """
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _log_dir():
    """日志目录：frozen 时取 exe 所在目录（用户可见可查）。

    不能复用 _app_dir()——_MEIPASS 是临时解压目录，运行结束即被删除，
    日志写那里等于白写（2026-08-03 修复）。
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _resolve_log_file():
    """日志路径：exe/脚本目录优先；不可写则退回系统 Temp（保证一定有日志可查）"""
    primary = os.path.join(_log_dir(), 'bom_export.log')
    try:
        with open(primary, 'a', encoding='utf-8'):
            pass
        return primary
    except OSError:
        fallback = os.path.join(tempfile.gettempdir(), 'bom_export.log')
        return fallback


LOG_FILE = _resolve_log_file()
_handlers = [logging.FileHandler(LOG_FILE, encoding='utf-8')]
if sys.stdout and hasattr(sys.stdout, 'write'):
    _handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=_handlers
)
# 统一使用 "bom_export" 这一 logger 名：所有子模块 from bom_common import log
# 复用同一对象，GUI 通过 core.log.addHandler(...) 挂载处理器即可捕获全部日志。
log = logging.getLogger("bom_export")


def _log_uncaught_exception(exc_type, exc_value, exc_tb):
    """未捕获异常兜底：写入日志文件，避免 windowed 模式下静默失败（2026-08-03）"""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    log.critical("未捕获异常 %s: %s\n%s",
                 exc_type.__name__, exc_value,
                 ''.join(traceback.format_exception(exc_type, exc_value, exc_tb)))


sys.excepthook = _log_uncaught_exception


# -------- 结构常量（非业务规则；业务规则一律录进 V2 规则编辑器）--------
DEFAULT_GR = "小零件"          # V2 未覆盖时的兜底 GR
DEFAULT_NUM_RANGE = {"min": 200, "max": 999}
# mold_number_strip 为文件名处理配置（模号截断段）
MOLD_NUMBER_STRIP = ["ZC", "TEST", "T", "COPY", "TEMP", "BAK"]
