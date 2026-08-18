# -*- coding: utf-8 -*-
"""新手向导（wizard.py）— 零基础也能填对的规则录入工具。

设计目标：一个从没接触过本工具的人，不做培训、不看文档，
照提示在 5 分钟内完成一次正确录入，出错率趋近于零。

实现思路：
- 隐藏全部专业概念（域/规则/条件/动作/优先级…），只留一张零件信息表单；
- 一个表单自动生成对应的规则（分类/材质/加工说明各自成规则，内部处理）；
- 三步引导：第 1 步填写 → 第 2 步确认 → 第 3 步保存生效；
- 每个输入框配大白话说明 + 实时示例 + 口语化错误提示；
- 默认值保证"不改也能用"（只填零件名也能保存）；
- 重复录入同零件 = 更新已有规则，不会越存越多互相打架；
- 保存走全部门禁（结构/冲突/基准核对/快照），失败用大白话告诉你怎么改。

运行：python wizard.py
"""

import copy
import datetime
import json
import os
import re
import sys

import tkinter as tk
from tkinter import ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rulespec import lifecycle
from rulespec.corpus import corpus_ids, load_corpus
from rulespec.engine import RuleEngine
from rulespec.model import load_ruleset
from rulespec.schema import (GR_SUGGESTIONS, PRIORITY_DEFAULT, PRIORITY_MAX,
                             PRIORITY_MIN)
from rulespec.validator import dry_run, validate_ruleset

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES_DIR = os.path.join(BASE_DIR, "rules")
DEFAULT_CORPUS_DIR = os.path.join(BASE_DIR, "corpus")
CONFIG_PATH = os.path.join(BASE_DIR, ".editor_config.json")

C = {
    "bg": "#0d1117", "card": "#161b22", "card2": "#1c2128", "border": "#30363d",
    "text": "#e6edf3", "text2": "#8b949e", "accent": "#1f6feb",
    "ok": "#3fb950", "warn": "#d29922", "err": "#f85149", "dim": "#6e7681",
}
FONT = ("Microsoft YaHei UI", 11)
FONT_SM = ("Microsoft YaHei UI", 9)
FONT_HINT = ("Microsoft YaHei UI", 9)

COMMON_GRS = list(GR_SUGGESTIONS)

# 规格两种合法形态：尺寸（40*60*12 / Φ70×310）或标准件型号（CB16-100 / M6×16 / 6204）
SPEC_OK = re.compile(
    r"^\s*[Φφ]?\s*[\d.]+(\s*[*x×]\s*[\d.]+)+\s*$"        # 尺寸：40*60*12
    r"|^\s*[Φφ]\s*[\d.]+(\s*[x×]\s*[\d.]+)?\s*$"          # 圆柱：Φ70 / Φ70×310
    r"|^\s*(?=.*[A-Za-z])[A-Za-z0-9][A-Za-z0-9.×*/()\-_ ]*\s*$"   # 型号：CB16-100 / M6×16 / 6204ZZ
    r"|^\s*\d{2,}\s*$"                                    # 纯数字型号：6204
)
SPEC_SAMPLE = "40*60*12（或用 × 号）；圆柱：Φ70×310；标准件型号：CB16-100、M6×16"


class Wizard:
    def __init__(self, root, rules_dir=None, corpus_dir=None):
        self.root = root
        self.rules_dir = rules_dir or DEFAULT_RULES_DIR
        self.corpus_dir = corpus_dir or os.path.join(
            os.path.dirname(self.rules_dir), "corpus")
        self.manifest, self.rules = load_ruleset(self.rules_dir)
        if os.path.isdir(self.corpus_dir):
            self.entries = load_corpus(self.corpus_dir)
            self.cids = corpus_ids(self.entries)
        else:
            self.entries, self.cids = [], None
        self.author = self._load_config()
        self.step = 1            # 1 填写 / 2 确认 / 3 保存 / 4 完成
        self.plan = []           # 本次要新增/更新的规则清单
        self.last_version = ""
        self._build_ui()
        self.render()

    # ---------------- 配置 ----------------
    def _load_config(self):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f).get("author", "")
        except Exception:
            return ""

    # ---------------- 界面骨架 ----------------
    def _build_ui(self):
        self.root.title("零件信息录入向导")
        self.root.geometry("800x680")
        self.root.configure(bg=C["bg"])
        self._style = ttk.Style()
        self._style.theme_use("clam")
        self._style.configure("TCombobox", fieldbackground=C["card2"],
                              background=C["card2"], foreground=C["text"],
                              arrowcolor=C["text2"])
        self._style.configure("TSpinbox", fieldbackground=C["card2"],
                              background=C["card2"], foreground=C["text"],
                              arrowcolor=C["text2"])
        self._style.configure("Accent.TButton", background=C["accent"], foreground="#ffffff")
        self._style.map("Accent.TButton", background=[("active", "#2f81f7")])
        self._style.configure("TButton", background=C["card2"], foreground=C["text"],
                              borderwidth=1, focusthickness=0)
        self._style.map("TButton", background=[("active", "#2d333b")])

        # 顶部：标题 + 步骤指示
        head = tk.Frame(self.root, bg=C["bg"])
        head.pack(fill="x", padx=24, pady=(18, 6))
        tk.Label(head, text="零件信息录入", bg=C["bg"], fg=C["text"],
                 font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        tk.Label(head, text="跟着下面三步走，填完点「下一步」就行。拿不准的空着也行。",
                 bg=C["bg"], fg=C["text2"], font=FONT_HINT).pack(anchor="w", pady=(2, 8))
        self.step_bar = tk.Frame(head, bg=C["bg"])
        self.step_bar.pack(fill="x", pady=(4, 0))

        # 中部：内容区（随步骤切换）
        self.content = tk.Frame(self.root, bg=C["card"])
        self.content.pack(fill="both", expand=True, padx=24, pady=8)

        # 底部：导航按钮 + 状态
        foot = tk.Frame(self.root, bg=C["bg"])
        foot.pack(fill="x", padx=24, pady=(4, 14))
        self.btn_back = tk.Button(foot, text="← 上一步", command=self.back,
                                  bg=C["card2"], fg=C["text"], activebackground="#2d333b",
                                  activeforeground=C["text"], relief="flat",
                                  font=FONT, padx=16, pady=6)
        self.btn_back.pack(side="left")
        self.btn_next = tk.Button(foot, text="下一步 →", command=self.next_step,
                                  bg=C["accent"], fg="#ffffff", activebackground="#2f81f7",
                                  activeforeground="#ffffff", relief="flat",
                                  font=FONT, padx=20, pady=6)
        self.btn_next.pack(side="right")
        self.foot_msg = tk.Label(foot, text="", bg=C["bg"], fg=C["warn"],
                                 font=FONT_HINT, anchor="w")
        self.foot_msg.pack(side="right", padx=12)

    def _draw_steps(self, current):
        for w in self.step_bar.winfo_children():
            w.destroy()
        names = ["① 填写信息", "② 确认内容", "③ 保存生效"]
        for i, name in enumerate(names, 1):
            done = i < current
            active = i == current
            fg = C["ok"] if done else (C["text"] if active else C["dim"])
            bg = "#12301f" if done else (C["accent"] if active else C["card2"])
            tk.Label(self.step_bar, text=("✓ " if done else "") + name,
                     bg=bg, fg=fg, font=FONT_SM, padx=12, pady=4,
                     ).pack(side="left", padx=(0, 8))
            if i < 3:
                tk.Label(self.step_bar, text="→", bg=C["bg"], fg=C["dim"],
                         font=FONT_SM).pack(side="left")

    # ---------------- 主渲染 ----------------
    def render(self):
        self._draw_steps(self.step)
        for w in self.content.winfo_children():
            w.destroy()
        if self.step == 1:
            self._render_step1()
        elif self.step == 2:
            self._render_step2()
        elif self.step == 3:
            self._render_step3()
        else:
            self._render_done()
        # 按钮状态
        if self.step == 1:
            self.btn_back.configure(state="disabled")
            self.btn_next.configure(state="normal", text="下一步 →",
                                    command=self.next_step)
        elif self.step == 2:
            self.btn_back.configure(state="normal")
            self.btn_next.configure(state="normal", text="保存并生效",
                                    command=self.submit)
        elif self.step == 3:
            self.btn_back.configure(state="normal", text="← 上一步",
                                    command=self.back)
            self.btn_next.configure(state="normal", text="保存并生效",
                                    command=self.submit)
        else:
            self.btn_back.configure(state="disabled")
            self.btn_next.configure(state="normal", text="再录一个零件",
                                    command=self.reset_form)

    # ================= 第 1 步：填写 =================
    def _render_step1(self):
        box = tk.Frame(self.content, bg=C["card"])
        box.pack(fill="both", expand=True)
        self._form_fields = {}
        self._form_errors = {}
        self._vals = {}

        self._field(box, "零件名", "* 必填",
                    "零件在三维模型里的名字。比如：调整板、定1模框。\n名字记不全没关系，填几个能认出来的字也行（比如：模框）。",
                    "例如：调整板", self._entry, "name")
        self._field(box, "规格", "可留空",
                    "只有「同名字、不同规格要分开处理」时才需要填；\n"
                    "留空 = 兜底：这个零件的所有规格都按这条处理（规格不统一的零件就留空）。\n"
                    "尺寸类：长宽高用 * 隔开；标准件：直接填型号（如 CB16-100、M6×16）。",
                    "例如：" + SPEC_SAMPLE, self._entry, "spec")
        self._field(box, "归到哪一类（GR）", "默认：仓库备件",
                    "这个零件在材料清单（BOM）里算哪一类？\n拿不准就保持默认的「仓库备件」，以后想改随时能改。",
                    "例如：仓库备件、模架、自制件", self._combobox, "gr")
        self._field(box, "型号（打印用）", "可留空",
                    "BOM 表上印的规格和测量尺寸不一样时才填（比如量出来是 100*80*50，\n"
                    "但 BOM 上要印型号 BZ500.80/50）。留空就印测量尺寸。\n"
                    "（引擎会自动尝试从零件名提取型号，如『油缸 BOD-AG-63-50-V』）",
                    "例如：BZ500.80/50、CB16-100", self._entry, "model")
        self._field(box, "材质", "可留空",
                    "零件用什么材料做的？不知道就留空。",
                    "例如：45#、Cr12、50#锻件", self._entry, "material")
        self._field(box, "加工说明", "可留空",
                    "要加工什么、怎么加工、注意什么。每行写一条。",
                    "例如：\n外协精加工到位\n去毛刺", self._text, "remark")
        self._field(box, "备注", "可留空",
                    "写一句「为什么这么定」，方便以后别人（和以后的你）看懂。",
                    "例如：12 厚调整板是仓库常备料", self._entry, "rationale")

        # 高级设置（折叠）
        adv = tk.Frame(box, bg=C["card"])
        adv.pack(fill="x", pady=(10, 0))
        self._adv_visible = False
        tk.Button(adv, text="高级设置（一般不用动）", command=self._toggle_adv,
                  bg=C["card2"], fg=C["text2"], activebackground="#2d333b",
                  activeforeground=C["text"], relief="flat", font=FONT_SM, padx=8, pady=3
                  ).pack(anchor="w")
        self._adv_panel = tk.Frame(box, bg=C["card2"], padx=12, pady=8)
        self._adv_panel.pack(fill="x", pady=(6, 0))
        self._adv_panel.pack_forget()
        self._adv_priority = tk.IntVar(value=PRIORITY_DEFAULT)
        self._adv_match = tk.StringVar(value="contains")
        self._adv_tests = tk.StringVar(value="")

        r1 = tk.Frame(self._adv_panel, bg=C["card2"])
        r1.pack(fill="x", pady=3)
        tk.Label(r1, text="优先级（数字越大越优先）", bg=C["card2"], fg=C["text2"],
                 font=FONT_SM, width=26, anchor="w").pack(side="left")
        sp = ttk.Spinbox(r1, from_=PRIORITY_MIN, to=PRIORITY_MAX, width=8,
                         textvariable=self._adv_priority, font=FONT_SM)
        sp.pack(side="left")
        tk.Label(r1, text="默认 500 就行，一般不用改。", bg=C["card2"], fg=C["dim"],
                 font=FONT_HINT).pack(side="left", padx=8)

        r2 = tk.Frame(self._adv_panel, bg=C["card2"])
        r2.pack(fill="x", pady=3)
        tk.Label(r2, text="零件名怎么匹配", bg=C["card2"], fg=C["text2"],
                 font=FONT_SM, width=26, anchor="w").pack(side="left")
        cb = ttk.Combobox(r2, textvariable=self._adv_match, state="readonly", width=22,
                          values=["包含这个名字（推荐）", "必须完全一样",
                                  "按命名规则关键词匹配"],
                          font=FONT_SM)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>",
                lambda e: self._adv_match.set("contains" if cb.get().startswith("包含")
                                              else ("eq" if cb.get().startswith("必须")
                                                    else "keyword")))

        r3 = tk.Frame(self._adv_panel, bg=C["card2"])
        r3.pack(fill="x", pady=3)
        tk.Label(r3, text="测试编号（可留空）", bg=C["card2"], fg=C["text2"],
                 font=FONT_SM, width=26, anchor="w").pack(side="left")
        tk.Entry(r3, textvariable=self._adv_tests, bg=C["card"], fg=C["text"],
                 insertbackground=C["text"], relief="flat", highlightthickness=1,
                 highlightbackground=C["border"], font=FONT_SM).pack(side="left",
                                                                     fill="x", expand=True, ipady=2)

    def _toggle_adv(self):
        self._adv_visible = not self._adv_visible
        if self._adv_visible:
            self._adv_panel.pack(fill="x", pady=(6, 0))
        else:
            self._adv_panel.pack_forget()

    def _field(self, parent, label, tag, hint, example, builder, key):
        card = tk.Frame(parent, bg=C["card2"], padx=14, pady=8)
        card.pack(fill="x", pady=5)
        head = tk.Frame(card, bg=C["card2"])
        head.pack(fill="x")
        tk.Label(head, text=label, bg=C["card2"], fg=C["text"],
                 font=FONT).pack(side="left")
        tk.Label(head, text=tag, bg=C["card2"], fg=C["warn"] if "必填" in tag else C["dim"],
                 font=FONT_SM).pack(side="left", padx=8)
        err = tk.Label(card, text="", bg=C["card2"], fg=C["err"], font=FONT_SM,
                       anchor="w", justify="left")
        err.pack(fill="x", pady=(2, 0))
        self._form_errors[key] = err
        widget = builder(card, key)
        tk.Label(card, text=hint, bg=C["card2"], fg=C["text2"], font=FONT_HINT,
                 anchor="w", justify="left", wraplength=680).pack(fill="x", pady=(4, 0))
        tk.Label(card, text=example, bg=C["card2"], fg=C["dim"], font=FONT_HINT,
                 anchor="w", justify="left", wraplength=680).pack(fill="x")

    def _entry(self, parent, key):
        var = tk.StringVar(value=self._vals.get(key, ""))
        self._form_fields[key] = var

        def sync(*_a):
            self._vals[key] = var.get().strip()
            self._validate_field(key)

        var.trace_add("write", sync)
        ent = tk.Entry(parent, textvariable=var, bg=C["card"], fg=C["text"],
                       insertbackground=C["text"], relief="flat",
                       highlightthickness=1, highlightbackground=C["border"],
                       font=FONT)
        ent.pack(fill="x", ipady=5)
        return ent

    def _combobox(self, parent, key):
        var = tk.StringVar(value=self._vals.get(key) or "仓库备件")
        self._form_fields[key] = var
        self._vals[key] = var.get().strip()   # 默认值立即入内存（否则校验误报为空）
        values = list(COMMON_GRS)
        for r in self.rules:
            g = (r.get("then") or {}).get("gr")
            if isinstance(g, str) and g and g not in values:
                values.append(g)
        cb = ttk.Combobox(parent, textvariable=var, values=values, font=FONT)
        cb.pack(fill="x", ipady=4)

        def sync(*_a):
            self._vals[key] = var.get().strip()
            self._validate_field(key)

        cb.bind("<<ComboboxSelected>>", sync)
        var.trace_add("write", sync)
        return cb

    def _text(self, parent, key):
        txt = tk.Text(parent, height=3, bg=C["card"], fg=C["text"],
                      insertbackground=C["text"], relief="flat",
                      highlightthickness=1, highlightbackground=C["border"],
                      font=FONT)
        if self._vals.get(key):
            txt.insert("1.0", self._vals[key])
        txt.pack(fill="x", ipady=2)
        self._form_fields[key] = txt

        def sync(_e=None):
            self._vals[key] = txt.get("1.0", "end").strip()
            self._validate_field(key)

        def on_modified(_e=None):
            if txt.edit_modified():
                sync()
                txt.edit_modified(False)

        txt.bind("<<Modified>>", on_modified)
        txt.bind("<FocusOut>", sync)
        return txt

    # ---------------- 实时校验（口语化） ----------------
    def _get(self, key):
        return self._vals.get(key, "")

    def _validate_field(self, key):
        err = self._form_errors[key]
        msg = ""
        if key == "name":
            if not self._get("name"):
                msg = "零件名还没填——先在上面填零件名，比如：调整板"
        elif key == "spec":
            s = self._get("spec")
            if s and not SPEC_OK.match(s):
                msg = (f"规格的写法不太对。\n尺寸照着这个写：{SPEC_SAMPLE}。\n"
                       "标准件不用量尺寸，直接填型号就行。")
        elif key == "gr":
            if not self._get("gr"):
                msg = "还没选分类——不知道选哪个就用默认的「仓库备件」"
        err.configure(text=msg)
        return msg

    def _step1_errors(self):
        errs = []
        for k in ("name", "spec", "gr"):
            m = self._validate_field(k)
            if m:
                errs.append(m)
        return errs

    # ================= 第 2 步：确认 =================
    def _build_plan(self, rules_list):
        """在给定规则列表上构建计划（副本操作，不污染正式数据）。

        规则构建逻辑统一走 rulespec.entry（与表格编辑器共用，单一数据源）。
        """
        from rulespec import entry
        name = self._get("name")
        spec = self._get("spec")
        gr = self._get("gr") or "仓库备件"
        material = self._get("material")
        remark = self._get("remark")
        model = self._get("model")
        rationale = self._get("rationale")
        tests = [t.strip() for t in self._adv_tests.get().replace("，", ",").split(",")
                 if t.strip()]
        if tests and self.cids:
            bad = [t for t in tests if t not in self.cids]
            if bad:
                self._adv_tests.set("")
                return {"error": "测试编号 " + "、".join(bad) + " 不存在（可留空）"}
        try:
            prio = self._adv_priority.get()
        except Exception:
            prio = PRIORITY_DEFAULT
        prio = prio if PRIORITY_MIN <= prio <= PRIORITY_MAX else PRIORITY_DEFAULT
        return entry.plan_entry(rules_list, name=name, spec=spec, gr=gr,
                                material=material, remark=remark,
                                fasteners=None, model=model, prio=prio,
                                match_op=self._adv_match.get(),
                                rationale=rationale, tests=tests,
                                author=self.author)

    def _render_step2(self):
        result = self._build_plan(copy.deepcopy(self.rules))
        if "error" in result:
            self.foot_msg.configure(text="请回到上一步修正")
            tk.Label(self.content, text="出错了：" + result["error"],
                     bg="#3d1518", fg=C["err"], font=FONT, padx=16, pady=10
                     ).pack(fill="x")
            return
        self.plan = result["plan"]
        box = tk.Frame(self.content, bg=C["card"], padx=18, pady=14)
        box.pack(fill="both", expand=True)
        tk.Label(box, text="请确认下面的内容对不对", bg=C["card"], fg=C["text"],
                 font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        name = self._get("name")
        spec = self._get("spec")
        match_txt = {"contains": "零件名里包含",
                     "eq": "零件名必须完全等于",
                     "keyword": "零件名按命名规则分词后关键词等于"}.get(
            self._adv_match.get(), "零件名里包含")
        tk.Label(box, text=f"匹配对象：{match_txt}「{name}」"
                           + (f"，规格 {spec}" if spec else "（所有规格）"),
                 bg=C["card"], fg=C["text2"], font=FONT, anchor="w", justify="left"
                 ).pack(anchor="w", pady=(8, 4))

        for p in self.plan:
            tag = "新增" if p["action"] == "new" else "更新已有"
            line = (f"{'＋' if p['action'] == 'new' else '✎'} {p['plain_name']}："
                    f"{p['plain_effect']}（{tag} {p['id']}）")
            tk.Label(box, text=line, bg=C["card"], fg=C["text"],
                     font=FONT, anchor="w", justify="left").pack(anchor="w", pady=2)

        tk.Label(box, text="保存后立即生效；错了随时能改回来（撤销/重新录入均可）。",
                 bg=C["card"], fg=C["dim"], font=FONT_HINT).pack(anchor="w", pady=(10, 0))

    # ================= 第 3 步：保存 =================
    def _render_step3(self):
        box = tk.Frame(self.content, bg=C["card"], padx=18, pady=14)
        box.pack(fill="both", expand=True)
        tk.Label(box, text="第 3 步：保存生效", bg=C["card"], fg=C["text"],
                 font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        tk.Label(box, text="点右下角「保存并生效」。保存前系统会自动做一次全面检查：\n"
                           "· 填的内容格式对不对\n"
                           "· 有没有和已有规则打架\n"
                           "· 有没有和已经确认过的答案不一致\n"
                           "检查不过会告诉你怎么改，不会保存一半的坏数据。",
                 bg=C["card"], fg=C["text2"], font=FONT, anchor="w", justify="left"
                 ).pack(anchor="w", pady=(10, 0))

    def submit(self):
        self._render_step3()
        # 在副本上构建计划并跑门禁：失败不污染内存，成功才整体提交
        cand = copy.deepcopy(self.rules)
        result = self._build_plan(cand)
        if "error" in result:
            self.foot_msg.configure(text="")
            self._show_submit_result("出错了：" + result["error"], ok=False)
            return
        for p in result["plan"]:
            if p["action"] == "new":
                cand.append(p["rule"])
        vr = validate_ruleset(cand, self.cids)
        rep = dry_run(cand, self.entries)
        problems = []
        for e in vr["errors"]:
            problems.append(self._plain_error(e))
        for x in rep["wrong"] + rep["missing"]:
            problems.append(self._plain_corpus_error(x))
        if problems:
            self._show_submit_result("暂时没能保存，原因如下，改好再试：\n\n"
                                     + "\n".join("· " + p for p in problems[:8]),
                                     ok=False)
            return
        try:
            ver = lifecycle.snapshot(self.rules_dir, cand, self.manifest)
            self.manifest["version"] = ver
            self.rules = cand
            self.last_version = ver
            self._show_submit_result("", ok=True)
        except Exception as e:
            self._show_submit_result("保存时出了点问题：" + str(e), ok=False)

    def _plain_error(self, e):
        if "优先级" in e or "priority" in e:
            return "优先级填的数字不对（要在 0 到 1000 之间），高级设置里改一下"
        if "静态冲突" in e or "裁决歧义" in e:
            return "和另一条规则打架了（同一个零件被定了两种结果）。请回上一步换一个分类/规格，或到编辑器里删掉冲突的那条"
        if "测试引用" in e:
            return "测试编号填错了（系统里没有这个编号），清空或改成列表里有的"
        return e

    def _plain_corpus_error(self, x):
        return ("和已经确认过的答案对不上：" + x + "。如果新填的才对，"
                "需要先到编辑器里更新基准答案；否则请改回原来确认的值")

    def _show_submit_result(self, msg, ok):
        for w in self.content.winfo_children():
            w.destroy()
        if ok:
            box = tk.Frame(self.content, bg="#12301f", padx=18, pady=16)
            box.pack(fill="both", expand=True)
            tk.Label(box, text="✓ 保存成功！", bg="#12301f", fg=C["ok"],
                     font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
            tk.Label(box, text=f"已经保存并生效（规则集版本 v{self.last_version}）。\n"
                               "你现在可以：\n"
                               "· 点右下角「再录一个零件」继续录入\n"
                               "· 或直接关闭窗口（数据已保存）",
                     bg="#12301f", fg=C["text"], font=FONT, anchor="w",
                     justify="left").pack(anchor="w", pady=(8, 0))
        else:
            box = tk.Frame(self.content, bg="#3d1518", padx=18, pady=16)
            box.pack(fill="both", expand=True)
            tk.Label(box, text="还没保存成功", bg="#3d1518", fg=C["err"],
                     font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
            tk.Label(box, text=msg, bg="#3d1518", fg=C["text"], font=FONT,
                     anchor="w", justify="left").pack(anchor="w", pady=(8, 0))
            tk.Label(box, text="点「← 上一步」回去改，改完再回来保存。",
                     bg="#3d1518", fg=C["dim"], font=FONT_HINT).pack(anchor="w", pady=(8, 0))
            self.btn_back.configure(state="normal", text="← 上一步", command=self.back)
            self.btn_next.configure(state="normal", text="重新保存",
                                    command=self.submit)
            self.foot_msg.configure(text="")

    # ================= 完成页 =================
    def _render_done(self):
        pass  # 结果页由 _show_submit_result 渲染

    # ---------------- 导航 ----------------
    def next_step(self):
        if self.step == 1:
            errs = self._step1_errors()
            if errs:
                self.foot_msg.configure(text="还有 " + str(len(errs))
                                        + " 处没填对，红字提示里有改法")
                return
            self.foot_msg.configure(text="")
            self.step = 2
        elif self.step == 2:
            self.submit()
            return
        elif self.step == 3:
            self.submit()
            return
        self.render()

    def back(self):
        self.foot_msg.configure(text="")
        if self.step in (2, 3):
            self.step = 1
        self.render()

    def reset_form(self):
        self.foot_msg.configure(text="")
        self.step = 1
        self.plan = []
        self._vals = {}
        self.render()


def main():
    root = tk.Tk()
    try:
        Wizard(root)
    except Exception as e:
        import traceback
        traceback.print_exc()
        tk.Label(root, text="启动失败：" + str(e), fg=C["err"], bg=C["bg"]).pack(padx=20, pady=20)
    root.mainloop()


if __name__ == "__main__":
    main()
