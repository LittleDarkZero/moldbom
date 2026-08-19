# -*- coding: utf-8 -*-
"""BomExport 命令行入口模块（2026-08-18 重构自 bom_export.py 的 main()）。

解析命令行参数（单文件 / --batch / --clean-cache / --split / --format），
无参数时启动 GUI。
"""

import os
import shutil
import sys
import tempfile
import time

import pythoncom
import win32com.client

from bom_common import log, __version__
from bom_catia import _setup_catia_session, _restore_catia_session, cleanup_stale_cache
from bom_batch import batch_process
from bom_pipeline import process_one_part


def main():
    # 启动记录：一运行就让日志文件有内容（2026-08-03，配合 exe 目录日志）
    log.info("===== MoldBOM 启动 (PID=%d, v%s) =====", os.getpid(), __version__)
    log.info("参数: %s", sys.argv[1:])
    # 2026-08-14: 启动时自动清理 Temp 残留缓存（STP 临时目录 / PyInstaller _MEI
    # 解压目录，超 3 天且未被占用；运行中的目录删除失败自动跳过）
    try:
        n = cleanup_stale_cache(max_age_days=3)
        if n:
            log.info("启动自检: 已清理 %d 个遗留缓存目录", n)
    except Exception:
        pass
    # 2026-08-18: 清理自动更新残留文件（.old / 部分下载）
    try:
        import updater
        n = updater.cleanup_artifacts()
        if n:
            log.info("更新清理: 已删除 %d 个残留文件", n)
    except Exception:
        pass
    if len(sys.argv) < 2:
        # 双击运行（无参数）: 启动 GUI（2026-07-31 修复）
        # 原实现打印用法后 sys.exit(1)，双击表现为黑框一闪即退（"闪退"）；
        # README 承诺"双击 BomExport.exe 进入 GUI 模式"，现按承诺启动图形界面。
        # 有参数（拖拽 CATPart / --batch / --learn）时仍走下方 CLI 流程。
        try:
            import bom_gui
        except Exception as e:
            print("GUI 启动失败: %s" % e)
            sys.exit(1)
        app = bom_gui.BomGUI()
        app.mainloop()
        return

    catia = None  # 提前定义，避免 --learn 等无 CATIA 分支在 finally 中 NameError
    pythoncom.CoInitialize()
    try:
        # 2026-08-14: 清理缓存命令（--clean-cache 无视天数全清，无 CATIA 依赖）
        if sys.argv[1] == "--clean-cache":
            n = cleanup_stale_cache(force=True)
            log.info("--clean-cache: 清理 %d 个缓存目录", n)
            print("已清理 %d 个遗留缓存目录" % n)
            return

        # 处理 --batch 模式
        if sys.argv[1] == "--batch":
            if len(sys.argv) < 3:
                print("用法: python bom_export.py --batch <文件夹路径>")
                sys.exit(1)
            try: catia = win32com.client.GetActiveObject("CATIA.Application")
            except Exception:
                catia = win32com.client.Dispatch("CATIA.Application")
                catia.Visible = True
            _setup_catia_session(catia)
            try:
                batch_process(catia, sys.argv[2])
            finally:
                _restore_catia_session(catia)
            return

        # 标准单文件模式 — 解析参数
        catpart_path = os.path.abspath(sys.argv[1])
        if not os.path.exists(catpart_path):
            log.error("文件不存在: %s", catpart_path); sys.exit(1)

        # 检测 --split / --format 和输出路径
        do_split = "--split" in sys.argv
        out_fmt = "xlsx"
        # 重建位置参数列表：剔除 --split 与 --format <值>
        args = []
        i = 1
        while i < len(sys.argv):
            a = sys.argv[i]
            if a == "--split":
                i += 1; continue
            if a == "--format":
                if i + 1 < len(sys.argv):
                    out_fmt = sys.argv[i + 1].lower(); i += 2; continue
                i += 1; continue
            args.append(a); i += 1
        # args[0] = catpart_path, args[1] may be output_path or split_dir
        if do_split:
            split_dir = args[1] if len(args) > 1 else os.path.dirname(catpart_path)
            split_dir = os.path.abspath(split_dir)
            if not os.path.splitext(split_dir)[1]:  # 无扩展名 → 目录
                output_path = os.path.join(split_dir, os.path.splitext(os.path.basename(catpart_path))[0] + "_BOM.xlsx")
            else:
                output_path = split_dir
                split_dir = os.path.dirname(output_path) or "."
        else:
            split_dir = ""
            if len(args) > 1:
                output_path = os.path.abspath(args[1])
            else:
                base = os.path.splitext(os.path.basename(catpart_path))[0]
                output_path = os.path.join(os.path.dirname(catpart_path) or ".", f"{base}_BOM.xlsx")

        log.info("输入: %s", catpart_path)
        log.info("输出: %s", output_path)

        try:
            catia = win32com.client.GetActiveObject("CATIA.Application")
            log.info("已连接到运行中的 CATIA")
        except Exception:
            log.info("正在启动 CATIA...")
            catia = win32com.client.Dispatch("CATIA.Application")
            catia.Visible = True

        _setup_catia_session(catia)
        temp_dir = tempfile.mkdtemp(prefix="bom_export_")

        def progress(i, total, name):
            log.info("  [%d/%d] %s", i, total, name)

        # 确保输出目录存在
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        # 2026-08-13 P0-4: 统一走 process_one_part（内部 finally 关文档）；
        # 临时目录在本 finally 中清理——pipeline 异常时也不残留
        try:
            t0 = time.time()
            ctx = process_one_part(catia, catpart_path, output_path, temp_dir,
                                   progress_cb=progress, split_dir=split_dir,
                                   do_split=do_split, out_fmt=out_fmt)
            elapsed = time.time() - t0

            results = ctx["results"]; skipped = ctx["skipped"]; excluded = ctx["excluded"]
            output_path = ctx["output_path"]

            skip_info = []
            if skipped: skip_info.append(f"跳过 {skipped} 个布尔运算隐藏Body")
            if excluded: skip_info.append(f"排除 {excluded} 个工艺辅具")
            info_str = "，" + "，".join(skip_info) if skip_info else ""
            log.info("解析完成，耗时 %.1fs，共 %d 个零件%s", elapsed, len(results), info_str)

            gr_stats = {}
            for item in results:
                gr = item["零件GR号"]; gr_stats[gr] = gr_stats.get(gr, 0) + 1
            log.info("GR 名分布: %s", dict(sorted(gr_stats.items(), key=lambda x: -x[1])))
            log.info("各阶段耗时: %s", {k: round(v, 1) for k, v in ctx.get("_timings", {}).items()})
            log.info("BOM 已生成: %s", output_path)
            if do_split:
                log.info("拆分完成 (%d 个文件)", ctx.get("split_count", 0))

            log.info("全部完成！")
        finally:
            try: shutil.rmtree(temp_dir)
            except OSError: pass

    finally:
        if catia is not None:
            try: _restore_catia_session(catia)
            except Exception: pass
        pythoncom.CoUninitialize()

    # 如果是从拖拽运行的（无 --batch），暂停让用户看到结果。
    # 非交互环境（stdin 被重定向/管道）下跳过，避免 EOFError。
    if "--batch" not in sys.argv:
        try:
            if sys.stdin and sys.stdin.isatty():
                input("\n按 Enter 关闭...")
        except (EOFError, OSError):
            pass
