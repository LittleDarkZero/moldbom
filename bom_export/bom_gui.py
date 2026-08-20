#!/usr/bin/env python
"""BOM 导出工具 — 科技风简约 GUI（2026-08-11 移除 BOM 预览 Tab，右侧为日志区）"""
import sys
import os
import json
import threading
import glob
import logging
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)
import bom_export as core

# ========== 色彩系统 ==========
C = {
    "bg":      "#0d1117",
    "card":    "#161b22",
    "border":  "#21262d",
    "accent":  "#58a6ff",
    "accent2": "#3fb950",
    "warn":    "#d29922",
    "err":     "#f85149",
    "text":    "#c9d1d9",
    "text2":   "#8b949e",
    "text3":   "#484f58",
    "hover":   "#1f2937",
    "sel":     "#1f6feb33",
}

FONT_TITLE = ("Microsoft YaHei UI", 20, "bold")
FONT_H2 = ("Microsoft YaHei UI", 12, "bold")
FONT_BODY = ("Microsoft YaHei UI", 9)
FONT_MONO = ("Cascadia Code", 9)
FONT_SMALL = ("Microsoft YaHei UI", 8)
FONT_BTN = ("Microsoft YaHei UI", 10)


class RoundedButton(tk.Canvas):
    """圆角按钮"""
    def __init__(self, parent, text, command, width=140, height=34, accent=False):
        super().__init__(parent, width=width, height=height,
                         bg=C["bg"], highlightthickness=0, cursor="hand2")
        self.command = command
        self.text = text
        self.w = width; self.h = height
        self.accent = accent
        self._enabled = True
        self._draw()
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda e: self._draw(hover=True))
        self.bind("<Leave>", lambda e: self._draw(hover=False))

    def _draw(self, hover=False):
        self.delete("all")
        r = 6
        fill = C["accent"] if self.accent else C["card"]
        if hover and self._enabled:
            fill = C["hover"] if not self.accent else "#4998f5"
        if not self._enabled:
            fill = C["text3"]
        self.create_rounded_rect(2, 2, self.w-2, self.h-2, r, fill=fill, outline=C["border"])
        self.create_text(self.w//2, self.h//2, text=self.text,
                         fill=C["text"] if not self.accent else "#ffffff",
                         font=FONT_BTN)

    def create_rounded_rect(self, x1, y1, x2, y2, r, **kw):
        kw.setdefault('outline', '')
        self.create_arc(x1, y1, x1+2*r, y1+2*r, start=90, extent=90, style="pieslice", **kw)
        self.create_arc(x2-2*r, y1, x2, y1+2*r, start=0, extent=90, style="pieslice", **kw)
        self.create_arc(x1, y2-2*r, x1+2*r, y2, start=180, extent=90, style="pieslice", **kw)
        self.create_arc(x2-2*r, y2-2*r, x2, y2, start=270, extent=90, style="pieslice", **kw)
        self.create_rectangle(x1+r, y1, x2-r, y2, **kw)
        self.create_rectangle(x1, y1+r, x2, y2-r, **kw)

    def _click(self, e):
        if self._enabled and self.command:
            self.command()

    def set_enabled(self, val):
        self._enabled = val
        self._draw()


class GuiLogHandler(logging.Handler):
    """将 bom_export 的 log 消息转发到 GUI 日志区"""
    def __init__(self, gui):
        super().__init__(level=logging.INFO)
        self.gui = gui
        self.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s',
                                              datefmt='%H:%M:%S'))
    def emit(self, record):
        self.gui._log_async(self.format(record))


class BomGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MoldBOM — 模具 BOM 导出工具")
        self.geometry("900x700")
        self.minsize(800, 550)
        self.configure(bg=C["bg"])
        self.catparts = []
        self.running = False
        self.current_results = []
        self._log_handler = GuiLogHandler(self)  # 捕获 bom_export 日志

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TProgressbar", thickness=4, background=C["accent"], troughcolor=C["card"])
        style.configure("Treeview", background=C["card"], foreground=C["text"],
                        fieldbackground=C["card"], borderwidth=0, font=FONT_SMALL)
        style.configure("Treeview.Heading", background=C["bg"], foreground=C["text2"],
                        font=("Microsoft YaHei UI", 8, "bold"), borderwidth=0)
        style.map("Treeview", background=[("selected", C["sel"])],
                  foreground=[("selected", C["accent"])])

        self._build()
        self._init_version()
        self._maybe_auto_check()

    def _build(self):
        # ---- 顶部 ----
        header = tk.Frame(self, bg=C["bg"])
        header.pack(fill="x", padx=30, pady=(25, 0))

        tk.Label(header, text="Mold", font=FONT_TITLE, fg=C["text"], bg=C["bg"]).pack(side="left")
        tk.Label(header, text="BOM", font=FONT_TITLE, fg=C["accent"], bg=C["bg"]).pack(side="left")
        self.ver_label = tk.Label(header, text="", font=FONT_SMALL, fg=C["text3"], bg=C["bg"])
        self.ver_label.pack(side="left", padx=8, pady=(10, 0))

        # 检查更新按钮（header 右侧，状态指示左边）
        self.update_btn = RoundedButton(header, "检查更新", self._check_updates,
                                         width=80, height=22)
        self.update_btn.pack(side="right", padx=(0, 4), pady=(5, 0))

        self.status_dot = tk.Canvas(header, width=10, height=10, bg=C["bg"], highlightthickness=0)
        self._draw_dot("idle")
        self.status_dot.pack(side="right", padx=5, pady=(8, 0))
        self.status_txt = tk.Label(header, text="就绪", font=FONT_SMALL, fg=C["text3"], bg=C["bg"])
        self.status_txt.pack(side="right", pady=(8, 0))

        # ---- 主内容区 ----
        main = tk.Frame(self, bg=C["bg"])
        main.pack(fill="both", expand=True, padx=30, pady=(20, 10))

        # 左：控制面板
        left = tk.Frame(main, bg=C["card"], width=240)
        left.pack(side="left", fill="y", padx=(0, 12))
        left.pack_propagate(False)

        tk.Label(left, text="文件选择", font=FONT_H2, fg=C["text"], bg=C["card"]).pack(padx=16, pady=(14, 6))
        tk.Label(left, text="选择一个文件夹或 CATPart 文件", font=FONT_SMALL, fg=C["text2"], bg=C["card"],
                 wraplength=200).pack(padx=16, pady=(0, 10))

        btn_frame = tk.Frame(left, bg=C["card"])
        btn_frame.pack(padx=16, pady=(0, 10))
        RoundedButton(btn_frame, "选择文件夹", self._select_folder).pack(pady=3)
        RoundedButton(btn_frame, "选择文件", self._select_files).pack(pady=3)

        self.count_label = tk.Label(left, text="未选择", font=FONT_SMALL, fg=C["text3"], bg=C["card"])
        self.count_label.pack(padx=16, pady=(2, 10))

        # 文件列表
        list_frame = tk.Frame(left, bg=C["border"], height=1)
        list_frame.pack(fill="x", padx=16)

        self.file_list = tk.Text(left, font=FONT_MONO, bg=C["card"], fg=C["text2"],
                                  wrap="none", state="disabled", height=8,
                                  borderwidth=0, highlightthickness=0)
        self.file_list.pack(fill="both", expand=True, padx=16, pady=10)

        tk.Label(left, text="选项", font=FONT_H2, fg=C["text"], bg=C["card"]).pack(padx=16, pady=(6, 4))
        self.split_var = tk.BooleanVar(value=True)
        split_cb = tk.Checkbutton(left, text="自动拆分零件", variable=self.split_var,
                                   font=FONT_BODY, fg=C["text"], bg=C["card"],
                                   selectcolor=C["card"], activebackground=C["card"],
                                   activeforeground=C["text"])
        split_cb.pack(padx=16, anchor="w", pady=(0, 4))

        # 自动检查更新选项
        try:
            import updater as _upd
            _cfg = _upd.load_config()
        except Exception:
            _cfg = {"auto_check": True}
        self.auto_update_var = tk.BooleanVar(value=_cfg.get("auto_check", True))
        auto_cb = tk.Checkbutton(left, text="启动时检查更新", variable=self.auto_update_var,
                                  font=FONT_BODY, fg=C["text"], bg=C["card"],
                                  selectcolor=C["card"], activebackground=C["card"],
                                  activeforeground=C["text"],
                                  command=self._on_auto_update_toggle)
        auto_cb.pack(padx=16, anchor="w", pady=(0, 10))

        self.clear_btn = RoundedButton(left, "清空列表", self._clear_files)
        self.clear_btn.pack(padx=16, side="bottom", pady=10)

        self.run_btn = RoundedButton(left, "▶ 开始导出", self._start, accent=True)
        self.run_btn.pack(padx=16, side="bottom", pady=(0, 10))

        self.progress = ttk.Progressbar(left, mode="determinate")
        self.progress.pack(fill="x", padx=16, side="bottom", pady=(0, 12))

        # 右：结果区（2026-08-11 移除 BOM 预览 Tab，直接显示日志）
        right = tk.Frame(main, bg=C["card"])
        right.pack(side="left", fill="both", expand=True)

        # 日志区
        self.log_frame = tk.Frame(right, bg=C["card"])
        self.log_frame.pack(fill="both", expand=True, padx=10, pady=(10, 5))
        self.log_text = tk.Text(self.log_frame, font=FONT_MONO, bg=C["card"],
                                 fg=C["text2"], wrap="word", state="disabled",
                                 borderwidth=0, highlightthickness=0)
        self.log_text.pack(fill="both", expand=True)

        # 底部摘要
        self.summary = tk.Label(right, text="", font=FONT_SMALL, fg=C["text2"], bg=C["card"])
        self.summary.pack(padx=10, pady=(0, 8))

        # ---- 底部角落彩蛋 ----
        tk.Label(self, text="再也不用担心蔡师傅乱分明细表了",
                 font=FONT_SMALL, fg=C["text3"], bg=C["bg"]).pack(side="right", padx=12, pady=(0, 6))

    # ========== 版本与更新 ==========
    def _init_version(self):
        """填充版本标签：v{exe} / 规则 v{rules}。"""
        try:
            import updater
            exe_ver = updater.current_exe_version()
            rules_ver = updater.current_rules_version() or "?"
            self.ver_label.configure(text="v%s / 规则 v%s" % (exe_ver, rules_ver))
        except Exception:
            self.ver_label.configure(text="")

    def _on_auto_update_toggle(self):
        """自动检查勾选项变化时保存配置。"""
        try:
            import updater
            cfg = updater.load_config()
            cfg["auto_check"] = self.auto_update_var.get()
            updater.save_config(cfg)
        except Exception:
            pass

    def _maybe_auto_check(self):
        """启动时后台静默检查（间隔到期 + 配置开启）。"""
        try:
            import updater
            cfg = updater.load_config()
            if not updater.should_auto_check(cfg):
                return
            threading.Thread(target=self._auto_check_thread, daemon=True).start()
        except Exception:
            pass

    @staticmethod
    def _updater_error_msg(e):
        """从异常提取用户可读信息（优先 updater.UpdaterError.user_message）。"""
        msg = getattr(e, "user_message", None)
        return msg or str(e) or "未知错误"

    def _auto_check_thread(self):
        """后台自动检查线程（启动时静默：状态栏+日志反馈，失败不弹窗打扰）。"""
        try:
            import updater
            cfg = updater.load_config()
            info = updater.check_for_updates(cfg)
            exe = info.get("exe")
            rules = info.get("rules")
            if exe or rules:
                parts = []
                if exe:
                    parts.append("程序 v%s" % exe["version"])
                if rules:
                    parts.append("规则 v%s" % rules["version"])
                msg = "有新版本: " + " / ".join(parts)
                self.after(0, lambda: self.status_txt.configure(text=msg, fg=C["warn"]))
                self._pending_update_info = info
                self._log_async("自动检查更新：发现新版本\n" + updater.format_update_info(info))
            else:
                self.after(0, lambda: self.status_txt.configure(text="已是最新", fg=C["text3"]))
                self._log_async("自动检查更新：当前已是最新版本")
        except Exception as e:
            err_msg = self._updater_error_msg(e)
            self.after(0, lambda: self.status_txt.configure(text="检查更新失败", fg=C["err"]))
            self._log_async("自动检查更新失败：" + err_msg)

    def _check_updates(self):
        """手动检查更新（按钮点击）。"""
        if self.running:
            messagebox.showwarning("提示", "请等待当前导出任务完成后再检查更新", parent=self)
            return
        self.update_btn.set_enabled(False)
        self.status_txt.configure(text="正在检查更新...", fg=C["accent"])
        threading.Thread(target=self._manual_check_thread, daemon=True).start()

    def _manual_check_thread(self):
        """手动检查线程（状态栏 + 弹窗 + 日志，三类结果均有明确反馈）。"""
        try:
            import updater
            cfg = updater.load_config()
            if not cfg.get("repo"):
                self._log_async("检查更新：未配置 GitHub 仓库地址")
                self.after(0, lambda: (
                    self.status_txt.configure(text="未配置更新源", fg=C["err"]),
                    messagebox.showinfo("提示",
                        "尚未配置 GitHub 仓库地址。\n\n"
                        "请在 exe 同目录的 update_config.json 中设置:\n"
                        '  "repo": "https://github.com/OWNER/REPO"',
                        parent=self)))
                return
            info = updater.check_for_updates(cfg)
            exe = info.get("exe")
            rules = info.get("rules")
            if not exe and not rules:
                self._log_async("检查更新：当前已是最新版本")
                self.after(0, lambda: (
                    self.status_txt.configure(text="已是最新版本", fg=C["text3"]),
                    messagebox.showinfo("检查更新", "当前已是最新版本", parent=self)))
                return
            # 有新版本：状态栏 + 日志 + 更新对话框
            parts = []
            if exe:
                parts.append("程序 v%s" % exe["version"])
            if rules:
                parts.append("规则 v%s" % rules["version"])
            detail = updater.format_update_info(info)
            self._log_async("检查更新：发现新版本\n" + detail)
            self.after(0, lambda: (
                self.status_txt.configure(text="有新版本: " + " / ".join(parts), fg=C["warn"]),
                self._show_update_dialog(info)))
        except Exception as e:
            err_msg = self._updater_error_msg(e)
            self._log_async("检查更新失败：" + err_msg)
            self.after(0, lambda: (
                self.status_txt.configure(text="检查失败", fg=C["err"]),
                messagebox.showerror("检查更新失败", err_msg, parent=self)))
        finally:
            self.after(0, lambda: self.update_btn.set_enabled(True))

    def _show_update_dialog(self, info):
        """显示更新对话框。"""
        dlg = UpdateDialog(self, info)
        self.wait_window(dlg)

    def _apply_updates(self, info, update_exe, update_rules, progress_cb, done_cb):
        """在后台线程执行更新（由 UpdateDialog 调用）。"""
        try:
            import updater
            cfg = updater.load_config()
            results = {}

            if update_rules and info.get("rules"):
                progress_cb("下载规则更新...", 0, 1)
                ver = updater.download_and_install_rules(
                    info["rules"], cfg,
                    progress_cb=lambda d, t: progress_cb("下载规则...", d, t))
                results["rules_version"] = ver
                # 更新版本标签
                self.after(0, self._init_version)

            if update_exe and info.get("exe"):
                progress_cb("下载程序更新...", 0, 1)
                new_path = updater.download_exe_update(
                    info["exe"], cfg,
                    progress_cb=lambda d, t: progress_cb("下载程序...", d, t))
                results["exe_path"] = new_path

            done_cb(None, results)
        except Exception as e:
            err_msg = str(e)
            try:
                if hasattr(e, 'user_message'):
                    err_msg = e.user_message
            except Exception:
                pass
            done_cb(err_msg, None)

    # ========== 文件选择 ==========
    def _select_folder(self):
        path = filedialog.askdirectory(title="选择包含 CATPart 的文件夹")
        if not path:
            return
        found = sorted(glob.glob(os.path.join(path, "*.CATPart")))
        if not found:
            self._log("文件夹中无 CATPart 文件")
            return
        self.catparts = found
        self._refresh_list()

    def _select_files(self):
        paths = filedialog.askopenfilenames(
            title="选择 CATPart 文件",
            filetypes=[("CATPart", "*.CATPart"), ("所有文件", "*.*")]
        )
        if not paths:
            return
        self.catparts = sorted(paths)
        self._refresh_list()

    def _clear_files(self):
        self.catparts = []
        self._refresh_list()
        self.summary.configure(text="")
        self.current_results = []

    def _refresh_list(self):
        self.file_list.configure(state="normal")
        self.file_list.delete("1.0", "end")
        for cp in self.catparts:
            self.file_list.insert("end", "  " + os.path.basename(cp) + "\n")
        self.file_list.configure(state="disabled")
        self.count_label.configure(text=f"共 {len(self.catparts)} 个文件")
        self._log(f"已加载 {len(self.catparts)} 个 CATPart")

    # ========== 日志 ==========
    def _log(self, msg):
        """直接写日志区（仅主线程调用）。"""
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _log_async(self, msg):
        """后台线程安全版：通过 after 回到主线程再写日志。"""
        self.after(0, self._log, msg)

    def _draw_dot(self, state):
        colors = {"idle": C["text3"], "running": C["accent"], "done": C["accent2"], "err": C["err"]}
        self.status_dot.delete("all")
        self.status_dot.create_oval(1, 1, 9, 9, fill=colors.get(state, C["text3"]), outline="")

    # ---- 状态动画 + 俏皮文案 ----
    SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    PUNS = [
        "正在拆解「{name}」，螺丝一颗都不能少...",
        "「{name}」的三围正在测量中，请勿偷看...",
        "CATIA 正在对「{name}」上下其手...",
        "「{name}」已经在怀疑零件人生了...",
        "正在给「{name}」贴标签分门别类...",
        "「{name}」的材质已查明，下一个...",
    ]

    def _pick_pun(self):
        msg = self.PUNS[hash(self._current_name) % len(self.PUNS)]
        return msg.format(name=self._current_name)

    def _start_spinner(self):
        if not self.running:
            return
        self._spinner_idx = (self._spinner_idx + 1) % len(self.SPINNER)
        frame = self.SPINNER[self._spinner_idx]
        self.status_dot.delete("all")
        self.status_dot.create_text(5, 5, text=frame, fill=C["accent"],
                                     font=("Segoe UI", 10), anchor="center")
        self._spinner_job = self.after(120, self._start_spinner)

    def _set_status(self, state, txt):
        """状态更新统一回主线程执行（后台线程也会调用）。"""
        self.after(0, self._set_status_impl, state, txt)

    def _set_status_impl(self, state, txt):
        if state != "running":
            if hasattr(self, '_spinner_job'):
                self.after_cancel(self._spinner_job)
            self._draw_dot(state)
        self.status_txt.configure(text=txt, fg=C["text"] if state != "idle" else C["text3"])

    # ========== 运行 ==========
    def _start(self):
        if not self.catparts:
            messagebox.showwarning("提示", "请先选择 CATPart 文件或文件夹", parent=self)
            return
        if self.running:
            return
        self.running = True
        self.run_btn.set_enabled(False)
        self.clear_btn.set_enabled(False)
        self.progress["maximum"] = len(self.catparts)
        self.progress["value"] = 0
        self._spinner_idx = 0
        first = os.path.basename(self.catparts[0]) if self.catparts else ""
        self._current_name = os.path.splitext(first)[0]
        self._set_status("running", self._pick_pun())
        self._start_spinner()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        do_split = self.split_var.get()
        import pythoncom, tempfile, shutil

        pythoncom.CoInitialize()
        catia = None
        all_bom_rows = []
        core.log.addHandler(self._log_handler)  # 开始捕获日志
        try:
            catia = core.connect_catia()  # 动态绑定，规避 gen_py 跨类型库问题
            self._log_async("已连接 CATIA（动态绑定）")

            core._setup_catia_session(catia)  # 关刷新 + 抑制弹窗

            for idx, cp in enumerate(self.catparts):
                name = os.path.basename(cp)
                self._current_name = os.path.splitext(name)[0]  # 去掉 .CATPart 后缀
                self.after(0, lambda n=self._current_name: self._set_status("running", self._pick_pun()))
                self._log_async(f"[{idx+1}/{len(self.catparts)}] {name}")

                temp_dir = tempfile.mkdtemp(prefix="bom_gui_")
                try:
                    # Body 级实时进度：线程安全地更新状态条
                    def _progress(i, total, part_name):
                        self.after(0, lambda p=part_name, ii=i, tt=total:
                                   self.status_txt.configure(text=f"[{ii}/{tt}] {p}"))

                    base = os.path.splitext(name)[0]
                    out_dir = os.path.dirname(cp)
                    xlsx_out = os.path.join(out_dir, f"{base}_BOM.xlsx")
                    split_dir = os.path.join(out_dir, f"{base}_parts") if do_split else ""

                    # 统一走 pipeline 编排（与 CLI 同一入口）
                    ctx = core.process_one_part(
                        catia, cp, xlsx_out, temp_dir,
                        progress_cb=_progress, do_split=do_split, split_dir=split_dir,
                        out_fmt="xlsx", close_doc=True
                    )
                    results = ctx["results"]
                    self._log_async(f"  → {os.path.basename(ctx['output_path'])}")
                    if do_split:
                        self._log_async(f"  → 拆分 {ctx.get('split_count', 0)} 个 CATPart")

                    for item in results:
                        item["_source"] = name
                    all_bom_rows.extend(results)

                finally:
                    try: shutil.rmtree(temp_dir)
                    except OSError: pass

                # 进度条更新也回主线程（Tk 非线程安全，2026-08-19 修复）
                self.after(0, lambda v=idx + 1: self.progress.configure(value=v))
                self._update_summary(all_bom_rows)

            self.current_results = all_bom_rows
            self._set_status("done", f"完成 — {len(all_bom_rows)} 行")
            self._log_async("全部完成!")

        except Exception as e:
            import traceback
            self._log_async(f"错误: {e}")
            core.log.error("GUI 导出异常: %s", ''.join(traceback.format_exc()))
            self._set_status("err", "失败")

        finally:
            # 2026-08-13 P0-4: 与 core 会话恢复对齐（RefreshDisplay + 告警弹窗全对称）
            if catia:
                try: core._restore_catia_session(catia)
                except Exception: pass
            pythoncom.CoUninitialize()
            core.log.removeHandler(self._log_handler)  # 停止捕获
            self.running = False
            # 按钮恢复也回主线程（Tk 非线程安全，2026-08-19 修复）
            self.after(0, lambda: (
                self.run_btn.set_enabled(True),
                self.clear_btn.set_enabled(True)))

    def _update_summary(self, rows):
        """底部摘要：总计行数 + 各来源文件行数（2026-08-11 替代 BOM 预览）。"""
        sources = {}
        for item in rows:
            s = item.get("_source", "")
            sources[s] = sources.get(s, 0) + 1
        parts = [f"{os.path.splitext(k)[0]}: {v}行" for k, v in sources.items()]
        self.after(0, lambda: self.summary.configure(
            text=f"总计 {len(rows)} 行 | " + " | ".join(parts)))


class UpdateDialog(tk.Toplevel):
    """更新对话框（深色风格，显示版本信息+进度条）。"""

    def __init__(self, parent, info):
        super().__init__(parent)
        self.title("软件更新")
        self.geometry("460x420")
        self.resizable(False, False)
        self.configure(bg=C["bg"])
        self.transient(parent)
        self.grab_set()

        self.parent = parent
        self.info = info
        self._updating = False

        exe = info.get("exe")
        rules = info.get("rules")

        # ---- 标题 ----
        tk.Label(self, text="发现新版本", font=FONT_H2, fg=C["accent"], bg=C["bg"]).pack(
            padx=24, pady=(20, 10), anchor="w")

        # ---- 版本信息卡片 ----
        card = tk.Frame(self, bg=C["card"], highlightthickness=1,
                        highlightbackground=C["border"])
        card.pack(fill="x", padx=24, pady=(0, 12))

        try:
            import updater
            cur_exe = updater.current_exe_version()
            cur_rules = updater.current_rules_version() or "未知"
        except Exception:
            cur_exe = "?"
            cur_rules = "?"

        if exe:
            tk.Label(card, text="程序", font=FONT_BODY, fg=C["text2"], bg=C["card"]).pack(
                padx=16, pady=(12, 0), anchor="w")
            tk.Label(card,
                     text="v%s → v%s" % (cur_exe, exe["version"]),
                     font=("Microsoft YaHei UI", 11, "bold"), fg=C["accent2"], bg=C["card"]).pack(
                padx=16, anchor="w")
            if exe.get("notes"):
                tk.Label(card, text=exe["notes"], font=FONT_SMALL, fg=C["text2"],
                         bg=C["card"], wraplength=380, justify="left").pack(
                    padx=16, pady=(2, 6), anchor="w")

        if rules:
            tk.Label(card, text="规则", font=FONT_BODY, fg=C["text2"], bg=C["card"]).pack(
                padx=16, pady=(8 if exe else 12, 0), anchor="w")
            tk.Label(card,
                     text="v%s → v%s" % (cur_rules, rules["version"]),
                     font=("Microsoft YaHei UI", 11, "bold"), fg=C["accent2"], bg=C["card"]).pack(
                padx=16, anchor="w")
            if rules.get("notes"):
                tk.Label(card, text=rules["notes"], font=FONT_SMALL, fg=C["text2"],
                         bg=C["card"], wraplength=380, justify="left").pack(
                    padx=16, pady=(2, 6), anchor="w")

        if not exe and not rules:
            tk.Label(card, text="当前已是最新版本", font=FONT_BODY, fg=C["text2"],
                     bg=C["card"]).pack(padx=16, pady=20)

        # ---- 勾选项 ----
        self.exe_var = tk.BooleanVar(value=bool(exe))
        self.rules_var = tk.BooleanVar(value=bool(rules))

        opts = tk.Frame(self, bg=C["bg"])
        opts.pack(fill="x", padx=24, pady=(0, 8))
        if exe:
            tk.Checkbutton(opts, text="更新程序 (exe)", variable=self.exe_var,
                           font=FONT_BODY, fg=C["text"], bg=C["bg"],
                           selectcolor=C["card"], activebackground=C["bg"],
                           activeforeground=C["text"]).pack(anchor="w", pady=2)
        if rules:
            tk.Checkbutton(opts, text="更新规则 (热更新，无需重启)", variable=self.rules_var,
                           font=FONT_BODY, fg=C["text"], bg=C["bg"],
                           selectcolor=C["card"], activebackground=C["bg"],
                           activeforeground=C["text"]).pack(anchor="w", pady=2)

        # ---- 进度区 ----
        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=24, pady=(4, 4))
        self.progress_label = tk.Label(self, text="", font=FONT_SMALL, fg=C["text2"], bg=C["bg"])
        self.progress_label.pack(padx=24, anchor="w")

        # ---- 按钮 ----
        btn_frame = tk.Frame(self, bg=C["bg"])
        btn_frame.pack(fill="x", padx=24, pady=(8, 20))
        RoundedButton(btn_frame, "取消", self._cancel, width=90, height=30).pack(side="right", padx=(8, 0))
        self.start_btn = RoundedButton(btn_frame, "开始更新", self._start_update,
                                        width=100, height=30, accent=True)
        self.start_btn.pack(side="right")

        if not exe and not rules:
            self.start_btn.set_enabled(False)

    def _cancel(self):
        if self._updating:
            return  # 更新中不允许关闭
        self.destroy()

    def _start_update(self):
        """开始更新流程。"""
        if self._updating:
            return
        do_exe = self.exe_var.get()
        do_rules = self.rules_var.get()
        if not do_exe and not do_rules:
            messagebox.showwarning("提示", "请至少选择一项更新", parent=self)
            return

        # 检查是否有导出任务在运行
        if getattr(self.parent, 'running', False):
            messagebox.showwarning("提示", "请等待导出任务完成后再更新", parent=self)
            return

        self._updating = True
        self.start_btn.set_enabled(False)
        self.progress["value"] = 0
        self.progress["maximum"] = 100

        # exe 更新需要确认
        if do_exe:
            if not messagebox.askyesno("确认",
                    "更新程序将下载新版本并自动重启。\n"
                    "请确保已保存所有工作。\n\n继续？", parent=self):
                self._updating = False
                self.start_btn.set_enabled(True)
                return

        threading.Thread(target=self._update_thread, args=(do_exe, do_rules),
                         daemon=True).start()

    def _update_thread(self, do_exe, do_rules):
        """后台更新线程。"""

        def progress_cb(label, downloaded, total):
            """进度回调（在子线程中调用，需 after 回主线程）。"""
            if total > 0:
                pct = int(downloaded * 100 / total)
            else:
                pct = 0
            size_str = "%.1fMB / %.1fMB" % (
                downloaded / 1048576, total / 1048576) if total else "%.1fMB" % (downloaded / 1048576)
            self.after(0, lambda: (
                self.progress.configure(value=pct),
                self.progress_label.configure(text="%s %s (%d%%)" % (label, size_str, pct))))

        def done_cb(err, results):
            """更新完成回调（在子线程中调用）。"""
            if err:
                self.after(0, lambda: (
                    self.progress_label.configure(text="更新失败: " + err, fg=C["err"]),
                    self.start_btn.set_enabled(True),
                    setattr(self, '_updating', False)))
                self.after(0, lambda: messagebox.showerror("更新失败", err, parent=self))
            else:
                # 规则更新成功提示
                if results and results.get("rules_version") and not results.get("exe_path"):
                    self.after(0, lambda: (
                        self.progress_label.configure(
                            text="规则更新完成 v%s，已生效" % results["rules_version"],
                            fg=C["accent2"]),
                        self.progress.configure(value=100)))
                    self.after(1500, self.destroy)
                # exe 更新成功 → 提示重启
                elif results and results.get("exe_path"):
                    self.after(0, lambda: (
                        self.progress_label.configure(text="下载完成，3 秒后自动重启...", fg=C["accent2"]),
                        self.progress.configure(value=100)))
                    self.after(2500, lambda: self._do_exe_restart(results["exe_path"]))

        self.parent._apply_updates(self.info, do_exe, do_rules, progress_cb, done_cb)

    def _do_exe_restart(self, new_exe_path):
        """执行 exe 替换并重启。"""
        try:
            import updater
            updater.apply_exe_update_and_restart(new_exe_path)
        except Exception as e:
            err_msg = getattr(e, 'user_message', str(e))
            messagebox.showerror("更新失败", err_msg, parent=self)
            self.start_btn.set_enabled(True)
            self._updating = False


if __name__ == "__main__":
    app = BomGUI()
    app.mainloop()
