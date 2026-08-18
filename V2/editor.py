# -*- coding: utf-8 -*-
"""RuleSpec 2.0 规则编辑器（V2 新系统，独立于旧编辑器）。

UX 设计要点（旧版 rule_gui.py 已于 2026-08-13 随老规则系统删除）：
1. 字段分组卡片：基本信息 / 条件 when / 动作 then / 元信息 四张卡片，层级清晰不堆叠；
2. 默认值预填：新建规则自动生成 id/作者/日期/优先级/作用域/首条件/首动作，减少重复输入；
3. 撤销/重做：Ctrl+Z / Ctrl+Y（全状态快照，含拖拽排序、增删行）；
4. 实时校验：每次编辑即时跑 G1-G3 + 静态冲突，错误面板内联显示，不弹窗；
5. 拖拽排序：树内拖拽调整规则顺序（交换优先级，语义与引擎裁决一致）；
6. 无弹窗流：新建规则内联表单、删除两步内联确认、干跑报告内联面板。
"""

import copy
import datetime
import json
import os
import sys

import tkinter as tk
from tkinter import ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rulespec import lifecycle
from rulespec.corpus import corpus_ids, load_corpus
from rulespec.engine import RuleEngine
from rulespec.model import (MANIFEST_NAME, check_rule, group_by_domain,
                            load_ruleset)
from rulespec.schema import (ATTR_KINDS, DOMAINS, OPS, OWNERSHIP,
                             PRIORITY_DEFAULT, PRIORITY_MAX, PRIORITY_MIN,
                             SCOPES, STATUSES, WHEN_FIELDS)
from rulespec.validator import dry_run, gate_summary, validate_ruleset

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES_DIR = os.path.join(BASE_DIR, "rules")
DEFAULT_CORPUS_DIR = os.path.join(BASE_DIR, "corpus")
CONFIG_PATH = os.path.join(BASE_DIR, ".editor_config.json")

C = {
    "bg": "#0d1117", "card": "#161b22", "card2": "#1c2128", "border": "#30363d",
    "text": "#e6edf3", "text2": "#8b949e", "accent": "#1f6feb",
    "ok": "#3fb950", "warn": "#d29922", "err": "#f85149", "dim": "#6e7681",
}
FONT = ("Microsoft YaHei UI", 10)
FONT_SM = ("Microsoft YaHei UI", 9)
FONT_MONO = ("Consolas", 10)

STATUS_COLOR = {"active": C["ok"], "draft": C["warn"], "deprecated": C["dim"], "retired": C["dim"]}

# 每个域的白话解释（新手引导用）：中文名 / 管什么事 / 示例
DOMAIN_PLAIN = {
    "filter":    ("挑零件", "决定哪些东西不算零件、不进清单。", "示例：名字含『毛坯』→ 跳过，不生成零件"),
    "normalize": ("统一名字", "把叫法不一样的名字改成统一叫法，方便后续匹配。", "示例：名字含『螺丝』→ 统一叫『螺钉』"),
    "gr":        ("分类（GR）", "决定零件归到哪一类。这是最重要的规则。", "示例：名字含『模框』→ 归到『模架』"),
    "spec":      ("打印规格", "决定 BOM 上印的规格。测量尺寸和型号不一样时才需要。", "示例：量出 100*80*50 → 印『BZ500.80/50』"),
    "material":  ("材质", "决定零件用什么材料。", "示例：分类是『模架』→ 材质 50#锻件"),
    "remark":    ("加工说明", "决定零件要写什么加工备注（可多行）。", "示例：名字含『镶块』→ 外协精加工到位"),
    "companion": ("配套件", "决定给零件配什么螺钉、垫圈。", "示例：分类是『模架』→ 配螺钉 CB16-100 ×4"),
    "number":    ("编号", "决定零件号用哪个号段。", "示例：分类是『模架』→ 编号 1-99"),
    "measure":   ("量尺寸", "决定要不要量尺寸（算出规格）。", "示例：销钉这类非建模件 → 跳过测量"),
    "purchase":  ("数量", "决定外购件固定数量。", "示例：热流道 → 固定 1 件"),
}

# 每个域动作框的空态示例（新手填法提示）
DOMAIN_EXAMPLE = {
    "filter":    "勾选『跳过』（input.skipBody = true）",
    "normalize": "part.workingName = 统一后的名字",
    "gr":        "gr = 模架（或 仓库备件 / 自制件 …）",
    "spec":      "outputSpec = BZ500.80/50（打印规格，可空 = 印测量值）",
    "material":  "material = 45#",
    "remark":    "外协精加工到位（多行，每行一条）",
    "companion": "加一行：名字 螺钉，规格 CB16-100，数量 4",
    "number":    "编号区间 {min: 1, max: 99}",
    "measure":   "勾选『跳过测量』",
    "purchase":  "固定数量 1",
}


def kind_default(kind):
    if kind == "bool":
        return False
    if kind == "int":
        return 1
    if kind == "strlist":
        return []
    if kind == "companions":
        return []
    if kind == "range":
        return {"min": 1, "max": 99}
    if kind.startswith("enum:"):
        return kind.split(":", 1)[1].split("|")[0]
    return ""


class Editor:
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
            # 语料缺失：G3 测试引用校验放宽（None 语义），保存仅做结构+冲突+干跑
            self.entries = []
            self.cids = None
        self.sel = None               # (domain, rule_id) 或 "new"
        self._undo = []
        self._redo = []
        self._drag_iid = None
        self._confirm_state = None
        self._report_visible = False
        self.author = self._load_config()
        self.guide_on = self._load_guide()
        self._build_ui()
        self.refresh_tree()
        self.render_detail()
        self._set_status("新手提示：点左侧任意规则（或分组）看怎么编辑；右上「新手引导」可开关说明", "warn")

    # ---------------- 配置 ----------------
    def _load_config(self):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f).get("author", "")
        except Exception:
            return ""

    def _load_guide(self):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f).get("guide", True)
        except Exception:
            return True

    def _save_config(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({"author": self.author, "guide": self.guide_on},
                          f, ensure_ascii=False)
        except Exception:
            pass

    def toggle_guide(self):
        self.guide_on = not self.guide_on
        self._save_config()
        self.render_detail()
        self._set_status("新手引导已" + ("开启" if self.guide_on else "关闭"),
                         "ok" if self.guide_on else "warn")

    # ---------------- 撤销 / 重做 ----------------
    def _mutate(self, label, fn):
        """任何修改前快照（支持撤销/重做）。"""
        self._undo.append((label, copy.deepcopy((self.rules, self.manifest))))
        if len(self._undo) > 100:
            self._undo.pop(0)
        self._redo.clear()
        fn()
        self._set_status(f"已执行：{label}", "ok")

    def undo(self):
        if not self._undo:
            self._set_status("没有可撤销的操作", "warn")
            return
        label, state = self._undo.pop()
        self._redo.append((label, copy.deepcopy((self.rules, self.manifest))))
        self.rules, self.manifest = state
        self._after_restore()
        self._set_status(f"已撤销：{label}", "ok")
        self.refresh_tree(keep_sel=True)
        self.render_detail()

    def redo(self):
        if not self._redo:
            self._set_status("没有可重做的操作", "warn")
            return
        label, state = self._redo.pop()
        self._undo.append((label, copy.deepcopy((self.rules, self.manifest))))
        self.rules, self.manifest = state
        self._after_restore()
        self._set_status(f"已重做：{label}", "ok")
        self.refresh_tree(keep_sel=True)
        self.render_detail()

    def _after_restore(self):
        """恢复状态后校验选中项：被撤销删除的规则不再选中。"""
        if self.sel and self.sel != "new":
            if self.sel[0] == "domain":
                return
            if not self._find(self.sel[1]):
                self.sel = None

    # ---------------- UI 骨架 ----------------
    def _build_ui(self):
        self.root.title("规则编辑器 v2（RuleSpec 2.0）")
        self.root.geometry("1280x760")
        self.root.configure(bg=C["bg"])
        self._style = ttk.Style()
        self._style.theme_use("clam")
        self._style.configure("Treeview", background=C["card"], fieldbackground=C["card"],
                              foreground=C["text"], borderwidth=0, rowheight=26,
                              font=FONT)
        self._style.configure("Treeview.Heading", background=C["card2"], foreground=C["text2"],
                              borderwidth=0, font=FONT_SM)
        self._style.map("Treeview", background=[("selected", C["accent"])],
                        foreground=[("selected", "#ffffff")])
        self._style.configure("TCombobox", fieldbackground=C["card2"], background=C["card2"],
                              foreground=C["text"], arrowcolor=C["text2"])
        self._style.configure("TSpinbox", fieldbackground=C["card2"], background=C["card2"],
                              foreground=C["text"], arrowcolor=C["text2"])
        self._style.configure("Accent.TButton", background=C["accent"], foreground="#ffffff")
        self._style.map("Accent.TButton", background=[("active", "#2f81f7")])
        self._style.configure("TButton", background=C["card2"], foreground=C["text"],
                              borderwidth=1, focusthickness=0)
        self._style.map("TButton", background=[("active", "#2d333b")])
        self._style.configure("TCheckbutton", background=C["bg"], foreground=C["text"])

        self._build_toolbar()
        paned = tk.PanedWindow(self.root, orient="horizontal", bg=C["bg"],
                               sashwidth=4, sashrelief="flat")
        paned.pack(fill="both", expand=True, padx=8, pady=(4, 0))
        self._build_tree_panel(paned)
        self._build_detail_panel(paned)
        self.status = tk.Label(self.root, text="就绪", bg=C["bg"], fg=C["text2"],
                               font=FONT_SM, anchor="w", padx=12)
        self.status.pack(fill="x", pady=(4, 6))
        self.root.bind("<Control-z>", lambda e: self.undo())
        self.root.bind("<Control-y>", lambda e: self.redo())
        self.root.bind("<Control-Z>", lambda e: self.redo())

    def _build_toolbar(self):
        bar = tk.Frame(self.root, bg=C["bg"])
        bar.pack(fill="x", padx=12, pady=8)
        tk.Button(bar, text="保存（门禁+快照）", command=self.save,
                  bg=C["accent"], fg="#ffffff", activebackground="#2f81f7",
                  activeforeground="#ffffff", relief="flat", font=FONT, padx=14, pady=4
                  ).pack(side="left")
        tk.Button(bar, text="验证（语料干跑）", command=self.validate_all,
                  bg=C["card2"], fg=C["text"], activebackground="#2d333b",
                  activeforeground=C["text"], relief="flat", font=FONT, padx=12, pady=4
                  ).pack(side="left", padx=6)
        tk.Button(bar, text="撤销", command=self.undo,
                  bg=C["card2"], fg=C["text"], activebackground="#2d333b",
                  activeforeground=C["text"], relief="flat", font=FONT, padx=10, pady=4
                  ).pack(side="left")
        tk.Button(bar, text="重做", command=self.redo,
                  bg=C["card2"], fg=C["text"], activebackground="#2d333b",
                  activeforeground=C["text"], relief="flat", font=FONT, padx=10, pady=4
                  ).pack(side="left", padx=(0, 6))
        tk.Button(bar, text="＋ 新建规则", command=self.start_new_rule,
                  bg=C["card2"], fg=C["text"], activebackground="#2d333b",
                  activeforeground=C["text"], relief="flat", font=FONT, padx=10, pady=4
                  ).pack(side="left")
        tk.Button(bar, text="新手引导", command=self.toggle_guide,
                  bg=C["card2"], fg=C["text"], activebackground="#2d333b",
                  activeforeground=C["text"], relief="flat", font=FONT, padx=10, pady=4
                  ).pack(side="left", padx=(0, 6))
        tk.Label(bar, text=f"规则集 {self.manifest.get('name', 'default')} "
                           f"v{self.manifest.get('version')} | 规则 {len(self.rules)} 条 | "
                           f"语料 {len(self.entries)} 条 | Ctrl+Z 撤销 / Ctrl+Y 重做",
                 bg=C["bg"], fg=C["dim"], font=FONT_SM).pack(side="right")

    def _build_tree_panel(self, paned):
        left = tk.Frame(paned, bg=C["card"])
        paned.add(left, width=330, minsize=260)
        self.tree = ttk.Treeview(left, columns=("info",), show="tree headings",
                                 selectmode="browse")
        self.tree.heading("#0", text="规则（按优先级降序）", anchor="w")
        self.tree.heading("info", text="强度", anchor="w")
        self.tree.column("#0", width=250, anchor="w")
        self.tree.column("info", width=60, anchor="e")
        self.tree.tag_configure("active", foreground=C["text"])
        self.tree.tag_configure("draft", foreground=C["warn"])
        self.tree.tag_configure("deprecated", foreground=C["dim"])
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        # 拖拽排序
        self.tree.bind("<ButtonPress-1>", self._drag_press, add="+")
        self.tree.bind("<B1-Motion>", self._drag_motion, add="+")
        self.tree.bind("<ButtonRelease-1>", self._drag_release, add="+")
        tk.Label(left, text="左侧 9 个分组 = 规则按类别存放。\n"
                            "点分组：看它管什么；点规则：看内容并编辑。\n"
                            "拖拽条目可调整顺序（往上拖 = 更优先）。\n"
                            "改错了按 Ctrl+Z 撤销。",
                 bg=C["card"], fg=C["dim"], font=FONT_SM, justify="left",
                 anchor="w", padx=8, pady=6).pack(fill="x", side="bottom")

    def _build_detail_panel(self, paned):
        right = tk.Frame(paned, bg=C["card"])
        paned.add(right, width=940, minsize=620)
        self.canvas = tk.Canvas(right, bg=C["card"], highlightthickness=0)
        vsb = ttk.Scrollbar(right, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.detail_inner = tk.Frame(self.canvas, bg=C["card"])
        self._detail_win = self.canvas.create_window(
            (0, 0), window=self.detail_inner, anchor="nw")
        self.detail_inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self._detail_win, width=e.width))

    # ---------------- 树 ----------------
    def refresh_tree(self, keep_sel=False, select=None):
        self.tree.delete(*self.tree.get_children())
        for domain in DOMAINS:
            rs = [r for r in self.rules if r.get("domain") == domain
                  and r.get("meta", {}).get("status") != "retired"]
            rs.sort(key=lambda r: (-r.get("priority", PRIORITY_DEFAULT), r.get("id", "")))
            if not rs:
                continue
            did = f"d:{domain}"
            self.tree.insert("", "end", iid=did, text=f"📁 {domain}（{len(rs)}）",
                             values=("",), tags=("active",))
            for r in rs:
                rid = r.get("id", "")
                status = r.get("meta", {}).get("status", "draft")
                label = f"  {rid}"
                if r.get("name"):
                    label += f"  {r['name']}"
                self.tree.insert(did, "end", iid=f"r:{rid}", text=label,
                                 values=(r.get("priority", ""),), tags=(status,))
        if not self.tree.get_children():
            # 空规则集：给新手一个明确入口
            self.tree.insert("", "end", iid="empty",
                             text="（还没有规则 —— 点「＋ 新建规则」开始，"
                                  "或运行 wizard.py / table_editor.py 从表格录入）",
                             tags=("deprecated",))
        if select is None and keep_sel and self.sel and self.sel != "new":
            select = self.sel[1]
        if select and self._find(select):
            self.tree.selection_set(f"r:{select}")
            self.tree.see(f"r:{select}")

    # ---------------- 选择与详情 ----------------
    def _on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid.startswith("d:"):
            # 选中域分组 → 显示这个分组管什么（新手引导）
            self.sel = ("domain", iid[2:])
            self.render_detail()
            return
        if not iid.startswith("r:"):
            self.tree.selection_remove(iid)
            return
        rid = iid[2:]
        rule = self._find(rid)
        if not rule:
            return
        self.sel = (rule.get("domain"), rid)
        self.render_detail()

    def _find(self, rid):
        for r in self.rules:
            if r.get("id") == rid:
                return r
        return None

    def _find_index(self, rid):
        for i, r in enumerate(self.rules):
            if r.get("id") == rid:
                return i
        return -1

    def _clear_detail(self):
        for w in self.detail_inner.winfo_children():
            w.destroy()

    # ---------------- 拖拽排序（交换优先级） ----------------
    def _drag_press(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and iid.startswith("r:"):
            self._drag_iid = iid

    def _drag_motion(self, event):
        pass

    def _drag_release(self, event):
        if not self._drag_iid:
            return
        src = self._drag_iid
        self._drag_iid = None
        dst = self.tree.identify_row(event.y)
        if not dst or dst == src or not dst.startswith("r:"):
            return
        src_parent = self.tree.parent(src)
        dst_parent = self.tree.parent(dst)
        if src_parent != dst_parent:
            self._set_status("只能在同一规则域内拖拽排序", "warn")
            return
        a = self._find(src[2:])
        b = self._find(dst[2:])
        if a["priority"] == b["priority"]:
            self._set_status(f"{a['id']} 与 {b['id']} 优先级相同，顺序由 id 决定，无需交换", "warn")
            return

        def _swap():
            a["priority"], b["priority"] = b["priority"], a["priority"]
            a["meta"]["updatedAt"] = datetime.date.today().isoformat()
            b["meta"]["updatedAt"] = datetime.date.today().isoformat()

        self._mutate(f"拖拽排序：{a['id']} ↔ {b['id']}", _swap)
        self.refresh_tree(select=a["id"])
        self.render_detail()

    # ---------------- 新手引导 ----------------
    def _render_guide_banner(self):
        """顶部白话引导横幅（随当前选中内容变换）。"""
        box = tk.Frame(self.detail_inner, bg=C["card2"], padx=10, pady=6)
        box.pack(fill="x", pady=(0, 8))
        sel = self.sel
        lines = []
        if sel == "new":
            lines = ["正在新建规则：选它管什么事（分类/材质/加工说明…），类别名随便起个英文名（如 adjust-plate），点创建。"]
        elif sel and sel[0] == "domain":
            name, purpose, ex = DOMAIN_PLAIN.get(sel[1], (sel[1], "", ""))
            lines = [f"你选中的是「{name}」分组，它管的事：{purpose}",
                     ex or "点下方任意一条规则开始编辑"]
        elif sel:
            rule = self._find(sel[1])
            if rule:
                name, purpose, ex = DOMAIN_PLAIN.get(rule.get("domain"), ("", "", ""))
                lines = [
                    "这个编辑器管的是零件信息规则：一条规则回答一个问题——某个零件归哪类、用什么材质、写什么加工说明。",
                    f"你选中的是【{name}】类的规则：当左边『条件』里说的零件出现时，就把它的信息写成右边『动作』里的值。",
                    "改法：左边条件填『什么零件会命中』→ 右边动作填『要写的信息』→ 点保存。要生效记得把状态选为 active。",
                ]
                if ex:
                    lines.append(ex)
        else:
            lines = ["这个编辑器管的是零件信息规则：一条规则回答一个问题。",
                     "怎么开始：① 点左边任意分组，右侧会告诉你它管什么 → "
                     "② 点分组下的规则看内容 → ③ 右上角「新建规则」或工具栏保存。"]
        for i, t in enumerate(lines):
            tk.Label(box, text=t, bg=C["card2"], fg=C["text"] if i == 0 else C["text2"],
                     font=FONT_SM, anchor="w", justify="left",
                     wraplength=880).pack(fill="x")
        tk.Button(box, text="关掉引导（工具栏「新手引导」可重新打开）",
                  command=self.toggle_guide, bg=C["card2"], fg=C["dim"],
                  relief="flat", font=FONT_SM, activebackground="#2d333b"
                  ).pack(anchor="w", pady=(2, 0))

    def _render_domain_guide(self, domain):
        """选中域分组：白话说明 + 该域现有规则一览。"""
        name, purpose, ex = DOMAIN_PLAIN.get(domain, (domain, "", ""))
        self._card_header(f"「{name}」分组说明", "点左侧任意一条规则开始编辑")
        box = tk.Frame(self.detail_inner, bg=C["card"], padx=12, pady=8)
        box.pack(fill="x", pady=(0, 8))
        tk.Label(box, text="这个分组管的事：" + purpose, bg=C["card"], fg=C["text"],
                 font=FONT, anchor="w", justify="left", wraplength=880).pack(anchor="w", pady=2)
        tk.Label(box, text="写法示例：" + ex, bg=C["card"], fg=C["text2"],
                 font=FONT_SM, anchor="w", justify="left", wraplength=880).pack(anchor="w", pady=2)
        rs = [r for r in self.rules if r.get("domain") == domain
              and r.get("meta", {}).get("status") != "retired"]
        rs.sort(key=lambda r: (-r.get("priority", PRIORITY_DEFAULT), r.get("id", "")))
        tk.Label(box, text=f"这个分组现有 {len(rs)} 条规则（点击左侧条目编辑）：",
                 bg=C["card"], fg=C["text2"], font=FONT_SM).pack(anchor="w", pady=(6, 2))
        for r in rs:
            st = r.get("meta", {}).get("status", "")
            flag = "" if st == "active" else f"（{st}，不生效）"
            tk.Label(box, text=f"· {r['id']}  优先级 {r.get('priority')}{flag}",
                     bg=C["card"], fg=C["text"], font=FONT_SM, anchor="w"
                     ).pack(anchor="w")
        tk.Label(box, text="想加一条新规则？点工具栏「＋ 新建规则」，在弹出来的表单里选这个分组就行。",
                 bg=C["card"], fg=C["dim"], font=FONT_SM, anchor="w",
                 justify="left", wraplength=880).pack(anchor="w", pady=(8, 0))

    # ---------------- 详情渲染 ----------------
    def render_detail(self):
        self._clear_detail()
        if self.guide_on:
            self._render_guide_banner()
        if self.sel == "new":
            self._render_new_form()
            return
        if self.sel and self.sel[0] == "domain":
            self._render_domain_guide(self.sel[1])
            return
        if not self.sel:
            self._render_placeholder()
            return
        rule = self._find(self.sel[1])
        if not rule:
            self.sel = None
            self._render_placeholder()
            return
        errs = self._validate_current(rule)
        if errs:
            box = tk.Frame(self.detail_inner, bg="#3d1518", padx=10, pady=6)
            box.pack(fill="x", pady=(0, 8))
            tk.Label(box, text="即时校验未通过：", bg="#3d1518", fg=C["err"],
                     font=FONT_SM).pack(anchor="w")
            for e in errs[:8]:
                tk.Label(box, text="· " + e, bg="#3d1518", fg=C["err"],
                         font=FONT_SM, anchor="w", justify="left").pack(anchor="w")
            if len(errs) > 8:
                tk.Label(box, text=f"… 共 {len(errs)} 条错误", bg="#3d1518", fg=C["err"],
                         font=FONT_SM).pack(anchor="w")
        self._card_info(rule)
        self._card_when(rule)
        self._card_then(rule)
        self._card_meta(rule)
        self._render_report_panel(rule)

    def _card_header(self, title, hint):
        h = tk.Frame(self.detail_inner, bg=C["card2"], padx=10, pady=6)
        h.pack(fill="x", pady=(0, 6))
        tk.Label(h, text=title, bg=C["card2"], fg=C["text"], font=FONT).pack(side="left")
        if hint:
            tk.Label(h, text=hint, bg=C["card2"], fg=C["dim"], font=FONT_SM).pack(side="left", padx=12)

    def _card_info(self, rule):
        self._card_header("基本信息", "改名称、作用域、优先级、状态。要生效记得把状态选为 active")
        box = tk.Frame(self.detail_inner, bg=C["card"], padx=10, pady=4)
        box.pack(fill="x", pady=(0, 10))
        self._row_label(box, "id", rule.get("id", ""), 0)
        self._row_label(box, "域", rule.get("domain", ""), 1)

        r2 = tk.Frame(box, bg=C["card"])
        r2.pack(fill="x", pady=2)
        tk.Label(r2, text="名称", width=10, anchor="w", bg=C["card"], fg=C["text2"],
                 font=FONT).pack(side="left")
        name_var = tk.StringVar(value=rule.get("name", ""))
        ent = tk.Entry(r2, textvariable=name_var, bg=C["card2"], fg=C["text"],
                       insertbackground=C["text"], relief="flat", highlightthickness=1,
                       highlightbackground=C["border"], font=FONT)
        ent.pack(side="left", fill="x", expand=True, ipady=3)

        def commit_name(*_a):
            v = name_var.get().strip()
            self._mutate("修改名称", lambda: rule.__setitem__(
                "name", v) if v else rule.pop("name", None))

        ent.bind("<FocusOut>", commit_name)
        ent.bind("<Return>", commit_name)

        r3 = tk.Frame(box, bg=C["card"])
        r3.pack(fill="x", pady=2)
        tk.Label(r3, text="作用域", width=10, anchor="w", bg=C["card"], fg=C["text2"],
                 font=FONT).pack(side="left")
        scope_cb = ttk.Combobox(r3, values=list(SCOPES), state="readonly",
                                width=12, font=FONT)
        scope_cb.set(rule.get("scope", "part"))
        scope_cb.pack(side="left")
        tk.Label(r3, text="优先级（越大越优先）", bg=C["card"], fg=C["text2"],
                 font=FONT).pack(side="left", padx=(16, 4))
        prio_sp = ttk.Spinbox(r3, from_=PRIORITY_MIN, to=PRIORITY_MAX, width=8,
                              font=FONT)
        prio_sp.set(rule.get("priority", PRIORITY_DEFAULT))

        def commit_prio(_e=None):
            try:
                v = int(prio_sp.get())
                if not (PRIORITY_MIN <= v <= PRIORITY_MAX):
                    raise ValueError
            except ValueError:
                self._set_status(f"优先级必须是 {PRIORITY_MIN}-{PRIORITY_MAX} 整数", "err")
                return

            def _c():
                rule["priority"] = v
                rule["meta"]["updatedAt"] = datetime.date.today().isoformat()

            self._mutate(f"修改优先级：{v}", _c)
            self.refresh_tree(select=rule["id"])

        prio_sp.bind("<FocusOut>", commit_prio)
        prio_sp.bind("<Return>", commit_prio)

        def commit_scope(_e=None):
            v = scope_cb.get()

            def _c():
                rule["scope"] = v
                rule["meta"]["updatedAt"] = datetime.date.today().isoformat()

            self._mutate(f"修改作用域：{v}", _c)

        scope_cb.bind("<<ComboboxSelected>>", commit_scope)

        r4 = tk.Frame(box, bg=C["card"])
        r4.pack(fill="x", pady=2)
        tk.Label(r4, text="状态", width=10, anchor="w", bg=C["card"], fg=C["text2"],
                 font=FONT).pack(side="left")
        st_cb = ttk.Combobox(r4, values=list(STATUSES), state="readonly", width=12,
                             font=FONT)
        st_cb.set(rule.get("meta", {}).get("status", "draft"))
        st_cb.pack(side="left")

        def commit_status(_e=None):
            v = st_cb.get()

            def _c():
                rule.setdefault("meta", {})["status"] = v
                rule["meta"]["updatedAt"] = datetime.date.today().isoformat()

            self._mutate(f"状态 → {v}", _c)
            self.refresh_tree(select=rule["id"])

        st_cb.bind("<<ComboboxSelected>>", commit_status)
        tk.Label(r4, text="（draft 不参与推理；deprecated=软删除）", bg=C["card"],
                 fg=C["dim"], font=FONT_SM).pack(side="left", padx=10)

        r5 = tk.Frame(box, bg=C["card"])
        r5.pack(fill="x", pady=2)
        tk.Button(r5, text="删除（硬删除）", command=lambda: self._hard_delete(rule),
                  bg="#3d1518", fg=C["err"], activebackground="#581a1f",
                  activeforeground=C["err"], relief="flat", font=FONT_SM, padx=8, pady=2
                  ).pack(side="left")
        tk.Label(r5, text="软删除请用上方状态改为 deprecated；硬删除两步确认。",
                 bg=C["card"], fg=C["dim"], font=FONT_SM).pack(side="left", padx=10)

    def _hard_delete(self, rule):
        if self._confirm_state == rule.get("id"):
            self._confirm_state = None

            def _del():
                self.rules.remove(rule)
                if self.sel and self.sel[1] == rule.get("id"):
                    self.sel = None

            self._mutate(f"删除规则：{rule['id']}", _del)
            self.refresh_tree()
            self.render_detail()
            return
        self._confirm_state = rule.get("id")
        self._set_status("再次点击「删除」确认硬删除（规则从文件移除，可 Ctrl+Z 恢复）", "err")
        self.root.after(3000, self._reset_confirm)

    def _reset_confirm(self):
        self._confirm_state = None

    def _row_label(self, parent, k, v, row):
        f = tk.Frame(parent, bg=C["card"])
        f.pack(fill="x", pady=2)
        tk.Label(f, text=k, width=10, anchor="w", bg=C["card"], fg=C["text2"],
                 font=FONT).pack(side="left")
        tk.Label(f, text=v, anchor="w", bg=C["card"], fg=C["text"],
                 font=FONT_MONO).pack(side="left")

    # ---------------- 条件 when ----------------
    def _card_when(self, rule):
        self._card_header("条件 when（什么零件会命中这条规则）",
                          "全部满足才算命中。最少加一行：零件名（如：调整板）")
        box = tk.Frame(self.detail_inner, bg=C["card"], padx=10, pady=4)
        box.pack(fill="x", pady=(0, 10))
        when = rule.get("when") or {}
        if not when:
            tk.Label(box, text="（还没有条件 = 对所有零件都生效，一般只在全局兜底规则里这样用）",
                     bg=C["card"], fg=C["dim"], font=FONT_SM).pack(anchor="w", pady=2)
        for i, (field, matcher) in enumerate(when.items()):
            self._matcher_row(box, rule, field, matcher, i)
        tk.Button(box, text="＋ 添加条件", command=lambda: self._add_matcher(rule),
                  bg=C["card2"], fg=C["text"], activebackground="#2d333b",
                  activeforeground=C["text"], relief="flat", font=FONT_SM, padx=8, pady=2
                  ).pack(anchor="w", pady=(4, 2))

    def _add_matcher(self, rule):
        def _c():
            when = rule.setdefault("when", {})
            # when 是字典（键唯一），取词汇表中第一个未用字段，避免覆盖已有条件
            field = next((f for f in WHEN_FIELDS if f not in when), "part.name")
            when[field] = {"op": "contains", "value": ""}
            rule["meta"]["updatedAt"] = datetime.date.today().isoformat()

        self._mutate("添加条件", _c)
        self.render_detail()

    def _matcher_row(self, parent, rule, field, matcher, idx):
        row = tk.Frame(parent, bg=C["card2"], padx=4, pady=2)
        row.pack(fill="x", pady=2)

        def rebuild():
            self._matcher_row(parent, rule, field, rule["when"][field], idx)

        field_cb = ttk.Combobox(row, values=list(WHEN_FIELDS), state="readonly",
                                width=22, font=FONT_SM)
        field_cb.set(field)

        def commit_field(_e=None):
            nf = field_cb.get()
            if nf == field:
                return
            when = rule["when"]

            def _c():
                when[nf] = when.pop(field)

            self._mutate(f"条件字段 → {nf}", _c)
            self.render_detail()

        field_cb.bind("<<ComboboxSelected>>", commit_field)
        field_cb.pack(side="left")

        op_cb = ttk.Combobox(row, values=list(OPS), state="readonly", width=10,
                             font=FONT_SM)
        op_cb.set(matcher.get("op", "contains"))

        def commit_op(_e=None):
            no = op_cb.get()
            if no == matcher.get("op"):
                return
            when = rule["when"]

            def _c():
                m = when[field]
                m["op"] = no
                if no == "range":
                    m["min"], m["max"] = 0, 100
                    m.pop("value", None)
                elif no == "in":
                    m["value"] = [""]
                elif no == "exists":
                    m["value"] = True
                    m.pop("min", None)
                    m.pop("max", None)
                else:
                    m.setdefault("value", "")
                    m.pop("min", None)
                    m.pop("max", None)
                rule["meta"]["updatedAt"] = datetime.date.today().isoformat()

            self._mutate(f"条件算子 → {no}", _c)
            self.render_detail()

        op_cb.bind("<<ComboboxSelected>>", commit_op)
        op_cb.pack(side="left", padx=4)

        self._matcher_value(row, rule, field, matcher)
        neg_var = tk.BooleanVar(value=bool(matcher.get("negate")))
        tk.Checkbutton(row, text="取反", variable=neg_var, bg=C["card2"], fg=C["text2"],
                       activebackground=C["card2"], activeforeground=C["text"],
                       selectcolor=C["card2"], font=FONT_SM,
                       command=lambda: self._commit_matcher(rule, field, "negate",
                                                            neg_var.get())).pack(side="left", padx=6)
        tk.Button(row, text="×", command=lambda: self._del_matcher(rule, field),
                  bg=C["card2"], fg=C["err"], relief="flat", font=FONT_SM,
                  activebackground="#2d333b").pack(side="left")

    def _matcher_value(self, row, rule, field, matcher):
        op = matcher.get("op")
        if op == "exists":
            tk.Label(row, text="（存在性判断）", bg=C["card2"], fg=C["dim"],
                     font=FONT_SM).pack(side="left", padx=4)
            return
        if op == "range":
            tk.Label(row, text="min", bg=C["card2"], fg=C["dim"], font=FONT_SM).pack(side="left")
            m1 = tk.Entry(row, width=7, bg=C["card"], fg=C["text"], insertbackground=C["text"],
                          relief="flat", highlightthickness=1, highlightbackground=C["border"],
                          font=FONT_SM)
            m1.insert(0, str(matcher.get("min", 0)))
            m1.bind("<FocusOut>", lambda e, k="min": self._commit_matcher(rule, field, k, m1.get()))
            m1.pack(side="left", padx=2, ipady=2)
            tk.Label(row, text="max", bg=C["card2"], fg=C["dim"], font=FONT_SM).pack(side="left")
            m2 = tk.Entry(row, width=7, bg=C["card"], fg=C["text"], insertbackground=C["text"],
                          relief="flat", highlightthickness=1, highlightbackground=C["border"],
                          font=FONT_SM)
            m2.insert(0, str(matcher.get("max", 100)))
            m2.bind("<FocusOut>", lambda e, k="max": self._commit_matcher(rule, field, k, m2.get()))
            m2.pack(side="left", padx=2, ipady=2)
            return
        if op == "in":
            val = ",".join(str(x) for x in matcher.get("value", []))
            ent = tk.Entry(row, width=18, bg=C["card"], fg=C["text"],
                           insertbackground=C["text"], relief="flat",
                           highlightthickness=1, highlightbackground=C["border"],
                           font=FONT_SM)
            ent.insert(0, val)
            ent.bind("<FocusOut>", lambda e: self._commit_matcher(
                rule, field, "value",
                [x.strip() for x in ent.get().split(",") if x.strip()]))
            ent.pack(side="left", padx=4, ipady=2)
            tk.Label(row, text="（逗号分隔）", bg=C["card2"], fg=C["dim"], font=FONT_SM).pack(side="left")
            return
        ent = tk.Entry(row, width=26, bg=C["card"], fg=C["text"], insertbackground=C["text"],
                       relief="flat", highlightthickness=1, highlightbackground=C["border"],
                       font=FONT_SM)
        ent.insert(0, str(matcher.get("value", "")))
        ent.bind("<FocusOut>", lambda e: self._commit_matcher(rule, field, "value", ent.get()))
        ent.bind("<Return>", lambda e: self._commit_matcher(rule, field, "value", ent.get()))
        ent.pack(side="left", padx=4, ipady=2)

    def _commit_matcher(self, rule, field, key, value):
        def _c():
            m = rule["when"][field]
            if key == "min" or key == "max":
                try:
                    m[key] = int(value)
                except ValueError:
                    self._set_status(f"{field} 的 {key} 需要整数", "err")
                    return
            else:
                m[key] = value
            rule["meta"]["updatedAt"] = datetime.date.today().isoformat()

        self._mutate(f"修改条件：{field}.{key}", _c)
        self._validate_feedback()

    def _del_matcher(self, rule, field):
        def _c():
            rule["when"].pop(field, None)
            rule["meta"]["updatedAt"] = datetime.date.today().isoformat()

        self._mutate(f"删除条件：{field}", _c)
        self.render_detail()

    # ---------------- 动作 then ----------------
    def _card_then(self, rule):
        domain = rule.get("domain")
        allowed = OWNERSHIP.get(domain, ())
        self._card_header("动作 then（命中后要写的信息）",
                          "只能写这个分组允许的字段，不会写错")
        box = tk.Frame(self.detail_inner, bg=C["card"], padx=10, pady=4)
        box.pack(fill="x", pady=(0, 10))
        then = rule.get("then") or {}
        if not then:
            tk.Label(box, text="（还没有动作。点下面的「添加动作」，然后照着写："
                     + DOMAIN_EXAMPLE.get(domain, "") + "）",
                     bg=C["card"], fg=C["dim"], font=FONT_SM).pack(anchor="w", pady=2)
        for attr, value in then.items():
            self._action_row(box, rule, attr, value)
        if len(then) < len(allowed):
            tk.Button(box, text="＋ 添加动作", command=lambda: self._add_action(rule),
                      bg=C["card2"], fg=C["text"], activebackground="#2d333b",
                      activeforeground=C["text"], relief="flat", font=FONT_SM, padx=8, pady=2
                      ).pack(anchor="w", pady=(4, 2))

    def _add_action(self, rule):
        allowed = OWNERSHIP.get(rule.get("domain"), ())
        used = set(rule.get("then", {}))
        free = [a for a in allowed if a not in used]
        if not free:
            self._set_status("本域全部属性已配置", "warn")
            return
        attr = free[0]

        def _c():
            then = rule.setdefault("then", {})
            then[attr] = kind_default(ATTR_KINDS.get(attr, "str"))
            rule["meta"]["updatedAt"] = datetime.date.today().isoformat()

        self._mutate(f"添加动作：{attr}", _c)
        self.render_detail()

    def _action_row(self, parent, rule, attr, value):
        domain = rule.get("domain")
        allowed = OWNERSHIP.get(domain, ())
        row = tk.Frame(parent, bg=C["card2"], padx=4, pady=3)
        row.pack(fill="x", pady=2)

        attr_cb = ttk.Combobox(row, values=list(allowed), state="readonly", width=22,
                               font=FONT_SM)
        attr_cb.set(attr)

        def commit_attr(_e=None):
            na = attr_cb.get()
            if na == attr:
                return
            then = rule["then"]

            def _c():
                then[na] = then.pop(attr)
                rule["meta"]["updatedAt"] = datetime.date.today().isoformat()

            self._mutate(f"动作属性 → {na}", _c)
            self.render_detail()

        attr_cb.bind("<<ComboboxSelected>>", commit_attr)
        attr_cb.pack(side="left")

        # 置空(null) 开关：值=null 表示强制清空上游值
        is_null = value is None
        null_var = tk.BooleanVar(value=is_null)
        tk.Checkbutton(row, text="置空", variable=null_var, bg=C["card2"], fg=C["text2"],
                       activebackground=C["card2"], activeforeground=C["text"],
                       selectcolor=C["card2"], font=FONT_SM,
                       command=lambda: self._commit_null(rule, attr, null_var.get())
                       ).pack(side="left", padx=4)
        if is_null:
            tk.Label(row, text="（null = 强制清空该属性）", bg=C["card2"], fg=C["dim"],
                     font=FONT_SM).pack(side="left", padx=4)
            tk.Button(row, text="×", command=lambda: self._del_action(rule, attr),
                      bg=C["card2"], fg=C["err"], relief="flat", font=FONT_SM,
                      activebackground="#2d333b").pack(side="left")
            return

        kind = ATTR_KINDS.get(attr, "str")
        self._value_editor(row, rule, attr, value, kind)
        tk.Button(row, text="×", command=lambda: self._del_action(rule, attr),
                  bg=C["card2"], fg=C["err"], relief="flat", font=FONT_SM,
                  activebackground="#2d333b").pack(side="left")

    def _commit_null(self, rule, attr, is_null):
        def _c():
            then = rule["then"]
            if is_null:
                then[attr] = None
            else:
                then[attr] = kind_default(ATTR_KINDS.get(attr, "str"))
            rule["meta"]["updatedAt"] = datetime.date.today().isoformat()

        self._mutate(("置空" if is_null else "恢复默认") + f"：{attr}", _c)
        self.render_detail()

    def _del_action(self, rule, attr):
        def _c():
            rule["then"].pop(attr, None)
            rule["meta"]["updatedAt"] = datetime.date.today().isoformat()

        self._mutate(f"删除动作：{attr}", _c)
        self.render_detail()

    # ---------------- 类型化值编辑器 ----------------
    def _value_editor(self, row, rule, attr, value, kind):
        if kind == "bool":
            var = tk.BooleanVar(value=bool(value))
            tk.Checkbutton(row, text="true/false", variable=var, bg=C["card2"],
                           fg=C["text2"], activebackground=C["card2"],
                           activeforeground=C["text"], selectcolor=C["card2"],
                           font=FONT_SM,
                           command=lambda: self._commit_value(rule, attr, var.get())
                           ).pack(side="left", padx=4)
        elif kind == "int":
            ent = tk.Entry(row, width=8, bg=C["card"], fg=C["text"],
                           insertbackground=C["text"], relief="flat",
                           highlightthickness=1, highlightbackground=C["border"],
                           font=FONT_SM)
            ent.insert(0, str(value))

            def commit(_e=None):
                try:
                    v = int(ent.get())
                except ValueError:
                    self._set_status(f"{attr} 需要整数", "err")
                    return
                self._commit_value(rule, attr, v)

            ent.bind("<FocusOut>", commit)
            ent.bind("<Return>", commit)
            ent.pack(side="left", padx=4, ipady=2)
        elif kind == "strtext":
            # 多行文本（如加工说明）：一行 = 一个 \n 段落
            txt = tk.Text(row, width=36, height=3, bg=C["card"], fg=C["text"],
                          insertbackground=C["text"], relief="flat",
                          highlightthickness=1, highlightbackground=C["border"],
                          font=FONT_SM)
            txt.insert("1.0", value if isinstance(value, str) else "")
            txt.pack(side="left", padx=4)

            def commit(_e=None):
                self._commit_value(rule, attr, "\n".join(
                    x for x in txt.get("1.0", "end").splitlines()))

            txt.bind("<FocusOut>", commit)
            txt.bind("<Control-Return>", commit)
            tk.Label(row, text="（多行，回车换行）", bg=C["card2"], fg=C["dim"],
                     font=FONT_SM).pack(side="left")
        elif kind.startswith("enum:"):
            opts = kind.split(":", 1)[1].split("|")
            cb = ttk.Combobox(row, values=opts, state="readonly", width=14, font=FONT_SM)
            cb.set(value if value in opts else opts[0])
            cb.bind("<<ComboboxSelected>>",
                    lambda e: self._commit_value(rule, attr, cb.get()))
            cb.pack(side="left", padx=4)
        elif kind == "strlist":
            self._strlist_editor(row, rule, attr, value)
        elif kind == "range":
            tk.Label(row, text="min", bg=C["card2"], fg=C["dim"], font=FONT_SM).pack(side="left")
            e1 = tk.Entry(row, width=6, bg=C["card"], fg=C["text"], insertbackground=C["text"],
                          relief="flat", highlightthickness=1, highlightbackground=C["border"],
                          font=FONT_SM)
            e1.insert(0, str(value.get("min", 1)))
            e1.bind("<FocusOut>", lambda e: self._commit_range(rule, attr, "min", e1.get()))
            e1.pack(side="left", padx=2, ipady=2)
            tk.Label(row, text="max", bg=C["card2"], fg=C["dim"], font=FONT_SM).pack(side="left")
            e2 = tk.Entry(row, width=6, bg=C["card"], fg=C["text"], insertbackground=C["text"],
                          relief="flat", highlightthickness=1, highlightbackground=C["border"],
                          font=FONT_SM)
            e2.insert(0, str(value.get("max", 99)))
            e2.bind("<FocusOut>", lambda e: self._commit_range(rule, attr, "max", e2.get()))
            e2.pack(side="left", padx=2, ipady=2)
        elif kind == "companions":
            self._companions_editor(row, rule, attr, value)
        elif attr == "part.workingName" and isinstance(value, dict) and "replaceAll" in value:
            tk.Label(row, text="旧", bg=C["card2"], fg=C["dim"], font=FONT_SM).pack(side="left")
            e1 = tk.Entry(row, width=10, bg=C["card"], fg=C["text"], insertbackground=C["text"],
                          relief="flat", highlightthickness=1, highlightbackground=C["border"],
                          font=FONT_SM)
            e1.insert(0, value["replaceAll"][0])
            e1.bind("<FocusOut>", lambda e: self._commit_replace(rule, 0, e1.get()))
            e1.pack(side="left", padx=2, ipady=2)
            tk.Label(row, text="→ 新", bg=C["card2"], fg=C["dim"], font=FONT_SM).pack(side="left")
            e2 = tk.Entry(row, width=10, bg=C["card"], fg=C["text"], insertbackground=C["text"],
                          relief="flat", highlightthickness=1, highlightbackground=C["border"],
                          font=FONT_SM)
            e2.insert(0, value["replaceAll"][1])
            e2.bind("<FocusOut>", lambda e: self._commit_replace(rule, 1, e2.get()))
            e2.pack(side="left", padx=2, ipady=2)
        else:
            ent = tk.Entry(row, width=30, bg=C["card"], fg=C["text"],
                           insertbackground=C["text"], relief="flat",
                           highlightthickness=1, highlightbackground=C["border"],
                           font=FONT_SM)
            ent.insert(0, str(value))

            def commit(_e=None):
                self._commit_value(rule, attr, ent.get())

            ent.bind("<FocusOut>", commit)
            ent.bind("<Return>", commit)
            ent.pack(side="left", padx=4, ipady=2)

    def _commit_value(self, rule, attr, value):
        def _c():
            rule["then"][attr] = value
            rule["meta"]["updatedAt"] = datetime.date.today().isoformat()

        self._mutate(f"修改动作：{attr}", _c)
        self._validate_feedback()

    def _commit_range(self, rule, attr, key, text):
        def _c():
            try:
                rule["then"][attr][key] = int(text)
            except (ValueError, KeyError):
                self._set_status(f"{attr}.{key} 需要整数", "err")
                return
            rule["meta"]["updatedAt"] = datetime.date.today().isoformat()

        self._mutate(f"修改编号区间：{key}", _c)

    def _commit_replace(self, rule, idx, text):
        def _c():
            v = rule["then"].get("part.workingName")
            if isinstance(v, dict) and "replaceAll" in v:
                v["replaceAll"][idx] = text
                rule["meta"]["updatedAt"] = datetime.date.today().isoformat()

        self._mutate("修改替换映射", _c)

    def _strlist_editor(self, row, rule, attr, value):
        items = value if isinstance(value, list) else value.get("add", [])
        txt = tk.Text(row, width=28, height=2, bg=C["card"], fg=C["text"],
                      insertbackground=C["text"], relief="flat",
                      highlightthickness=1, highlightbackground=C["border"],
                      font=FONT_SM)
        txt.insert("1.0", "\n".join(items))
        txt.pack(side="left", padx=4)

        def commit(_e=None):
            lines = [x for x in txt.get("1.0", "end").splitlines() if x.strip()]
            self._commit_value(rule, attr, lines)

        txt.bind("<FocusOut>", commit)
        txt.bind("<Return>", commit)
        tk.Label(row, text="（每行一项）", bg=C["card2"], fg=C["dim"], font=FONT_SM).pack(side="left")

    def _companions_editor(self, row, rule, attr, value):
        sub = tk.Frame(row, bg=C["card2"])
        sub.pack(side="left", fill="x", expand=True)
        for c in value:
            self._companion_row(sub, rule, attr, c)
        tk.Button(sub, text="＋ 配套", command=lambda: self._add_companion(rule, attr),
                  bg=C["card"], fg=C["text"], activebackground="#2d333b",
                  activeforeground=C["text"], relief="flat", font=FONT_SM, padx=6, pady=1
                  ).pack(anchor="w", pady=2)

    def _companion_row(self, parent, rule, attr, comp):
        f = tk.Frame(parent, bg=C["card"])
        f.pack(fill="x", pady=1)
        labels = (("name", "名称", 12), ("spec", "规格", 12), ("qty", "数量", 6),
                  ("gr", "GR", 10))
        for key, lab, w in labels:
            tk.Label(f, text=lab, bg=C["card"], fg=C["dim"], font=FONT_SM, width=3
                     ).pack(side="left")
            ent = tk.Entry(f, width=w, bg=C["card2"], fg=C["text"],
                           insertbackground=C["text"], relief="flat",
                           highlightthickness=1, highlightbackground=C["border"],
                           font=FONT_SM)
            ent.insert(0, str(comp.get(key, "")))

            def commit(_e=None, k=key, e=ent):
                self._commit_comp(rule, attr, comp, k, e.get())

            ent.bind("<FocusOut>", commit)
            ent.bind("<Return>", commit)
            ent.pack(side="left", padx=2, ipady=2)
        tk.Button(f, text="×", command=lambda: self._del_comp(rule, attr, comp),
                  bg=C["card"], fg=C["err"], relief="flat", font=FONT_SM,
                  activebackground="#2d333b").pack(side="left")

    def _commit_comp(self, rule, attr, comp, key, text):
        def _c():
            if key == "qty":
                try:
                    comp[key] = int(text)
                except ValueError:
                    self._set_status("配套件数量需要整数", "err")
                    return
            else:
                comp[key] = text
            rule["meta"]["updatedAt"] = datetime.date.today().isoformat()

        self._mutate(f"修改配套件：{comp.get('name')}.{key}", _c)

    def _add_companion(self, rule, attr):
        def _c():
            rule["then"][attr].append({"name": "螺钉", "spec": "", "qty": 1, "gr": ""})
            rule["meta"]["updatedAt"] = datetime.date.today().isoformat()

        self._mutate("新增配套件", _c)
        self.render_detail()

    def _del_comp(self, rule, attr, comp):
        def _c():
            rule["then"][attr].remove(comp)
            rule["meta"]["updatedAt"] = datetime.date.today().isoformat()

        self._mutate(f"删除配套件：{comp.get('name')}", _c)
        self.render_detail()

    # ---------------- 元信息 ----------------
    def _card_meta(self, rule):
        self._card_header("元信息", "版本自动递增；测试编号可留空")
        box = tk.Frame(self.detail_inner, bg=C["card"], padx=10, pady=4)
        box.pack(fill="x", pady=(0, 10))
        meta = rule.get("meta", {})
        self._row_label(box, "版本", str(meta.get("version", 1)), 0)
        self._row_label(box, "作者", meta.get("author", ""), 1)
        self._row_label(box, "创建", meta.get("createdAt", ""), 2)
        self._row_label(box, "更新", meta.get("updatedAt", ""), 3)

        f = tk.Frame(box, bg=C["card"])
        f.pack(fill="x", pady=2)
        tk.Label(f, text="理由", width=10, anchor="w", bg=C["card"], fg=C["text2"],
                 font=FONT).pack(side="left", anchor="n")
        txt = tk.Text(f, width=60, height=2, bg=C["card2"], fg=C["text"],
                      insertbackground=C["text"], relief="flat",
                      highlightthickness=1, highlightbackground=C["border"],
                      font=FONT)
        txt.insert("1.0", meta.get("rationale", ""))
        txt.pack(side="left", fill="x", expand=True, ipady=2)
        tk.Label(box, text="（理由 = 写一句为什么这么定，方便以后看。可留空）",
                 bg=C["card"], fg=C["dim"], font=FONT_SM, anchor="w").pack(fill="x", pady=(0, 4))

        def commit_rationale(_e=None):
            self._commit_meta(rule, "rationale", txt.get("1.0", "end").strip())

        txt.bind("<FocusOut>", commit_rationale)

        t2 = tk.Frame(box, bg=C["card"])
        t2.pack(fill="x", pady=4)
        tk.Label(t2, text="测试", width=10, anchor="w", bg=C["card"], fg=C["text2"],
                 font=FONT).pack(side="left", anchor="n")
        tests = meta.get("tests", []) or []
        for t in tests:
            chip = tk.Label(t2, text=f"{t} ×", bg=C["card2"], fg=C["text"],
                            font=FONT_SM, padx=6, pady=1)
            chip.pack(side="left", padx=2)
            chip.bind("<Button-1>", lambda e, x=t: self._remove_test(rule, x))
        add = tk.Entry(t2, width=34, bg=C["card2"], fg=C["text"],
                       insertbackground=C["text"], relief="flat",
                       highlightthickness=1, highlightbackground=C["border"],
                       font=FONT_SM)
        add.pack(side="left", padx=4, ipady=2)
        add.insert(0, "输入语料 id 回车添加（点击 chip 移除）")
        add.configure(fg=C["dim"])

        def on_focus_in(_e):
            if add.get().startswith("输入语料"):
                add.delete(0, "end")
                add.configure(fg=C["text"])

        def on_add(_e=None):
            v = add.get().strip()
            if not v or v.startswith("输入语料"):
                return
            self._add_test(rule, v)
            add.delete(0, "end")

        add.bind("<FocusIn>", on_focus_in)
        add.bind("<Return>", on_add)

        # 语料速查
        if self.cids:
            known = sorted(self.cids)
            hints = "语料: " + " / ".join(known[:6]) + (" …" if len(known) > 6 else "")
            tk.Label(box, text=hints, bg=C["card"], fg=C["dim"], font=FONT_SM,
                     anchor="w").pack(fill="x", pady=2)

    def _commit_meta(self, rule, key, value):
        def _c():
            rule["meta"][key] = value
            rule["meta"]["updatedAt"] = datetime.date.today().isoformat()

        self._mutate(f"修改元信息：{key}", _c)

    def _add_test(self, rule, tid):
        if tid not in self.cids:
            self._set_status(f"语料 {tid} 不存在", "err")
            return

        def _c():
            tests = rule["meta"].setdefault("tests", [])
            if tid not in tests:
                tests.append(tid)
            rule["meta"]["updatedAt"] = datetime.date.today().isoformat()

        self._mutate(f"关联测试：{tid}", _c)
        self.render_detail()

    def _remove_test(self, rule, tid):
        def _c():
            tests = rule["meta"].setdefault("tests", [])
            if tid in tests:
                tests.remove(tid)
            rule["meta"]["updatedAt"] = datetime.date.today().isoformat()

        self._mutate(f"移除测试：{tid}", _c)
        self.render_detail()

    # ---------------- 实时校验 ----------------
    def _validate_current(self, rule):
        errs = check_rule(rule, self.cids)
        # 静态冲突（只报涉及当前规则的）
        for other in self.rules:
            if other is rule or other.get("domain") != rule.get("domain"):
                continue
            if other.get("priority") != rule.get("priority"):
                continue
            if rule.get("when") == other.get("when"):
                shared = set(rule.get("then", {})) & set(other.get("then", {}))
                diffs = [x for x in shared if rule["then"][x] != other["then"][x]]
                if diffs:
                    errs.append(f"静态冲突 C2：与 {other['id']} 条件完全相同，"
                                f"但属性 {diffs} 值不同（同优先级）")
        return errs

    def _validate_feedback(self):
        """编辑后即时反馈（不重建面板，避免打断输入）。"""
        if self.sel and self.sel != "new":
            rule = self._find(self.sel[1])
            if rule:
                errs = self._validate_current(rule)
                if errs:
                    self._set_status(f"即时校验：{len(errs)} 处错误（详见右侧错误面板）", "err")
                else:
                    self._set_status("即时校验通过", "ok")

    # ---------------- 新建规则（内联表单） ----------------
    def start_new_rule(self):
        self.sel = "new"
        self.render_detail()

    def _render_new_form(self):
        self._card_header("新建规则", "默认值已预填，照着填就行")
        tk.Label(self.detail_inner,
                 text="这页创建一条新规则：选它管什么事（分类/材质/加工说明…），"
                      "类别名随便起个英文名（如 adjust-plate），然后点「创建规则」。\n"
                      "创建后会自动生成编号和默认值，再在右侧把条件和内容补上即可。",
                 bg=C["card"], fg=C["text2"], font=FONT_SM, anchor="w",
                 justify="left", wraplength=880).pack(anchor="w", padx=12, pady=(0, 6))
        box = tk.Frame(self.detail_inner, bg=C["card"], padx=10, pady=6)
        box.pack(fill="x", pady=(0, 10))

        def row(label, widget):
            f = tk.Frame(box, bg=C["card"])
            f.pack(fill="x", pady=3)
            tk.Label(f, text=label, width=10, anchor="w", bg=C["card"], fg=C["text2"],
                     font=FONT).pack(side="left")
            widget.pack(side="left", fill="x", expand=True)
            return f

        domain_var = tk.StringVar(value=self.sel_domain_default())
        row("域", ttk.Combobox(box, textvariable=domain_var, values=list(DOMAINS),
                               state="readonly", font=FONT))
        cat_var = tk.StringVar(value="new")
        row("类别", tk.Entry(box, textvariable=cat_var, bg=C["card2"], fg=C["text"],
                             insertbackground=C["text"], relief="flat",
                             highlightthickness=1, highlightbackground=C["border"],
                             font=FONT))
        scope_var = tk.StringVar(value="part")
        row("作用域", ttk.Combobox(box, textvariable=scope_var, values=list(SCOPES),
                                   state="readonly", font=FONT))
        prio_var = tk.StringVar(value=str(PRIORITY_DEFAULT))
        row("优先级", tk.Entry(box, textvariable=prio_var, bg=C["card2"], fg=C["text"],
                               insertbackground=C["text"], relief="flat",
                               highlightthickness=1, highlightbackground=C["border"],
                               font=FONT))
        name_var = tk.StringVar(value="")
        row("名称", tk.Entry(box, textvariable=name_var, bg=C["card2"], fg=C["text"],
                             insertbackground=C["text"], relief="flat",
                             highlightthickness=1, highlightbackground=C["border"],
                             font=FONT))

        hint = tk.Label(box, text="id 将按规范自动生成：<域>.<类别>.<作用域>.<序号>；"
                                  "创建后为 draft 状态，保存时需过全部门禁",
                        bg=C["card"], fg=C["dim"], font=FONT_SM, anchor="w", justify="left")
        hint.pack(fill="x", pady=4)

        btns = tk.Frame(box, bg=C["card"])
        btns.pack(fill="x", pady=6)
        tk.Button(btns, text="创建规则", command=lambda: self._create_rule(
            domain_var.get(), cat_var.get(), scope_var.get(), prio_var.get(),
            name_var.get()),
            bg=C["accent"], fg="#ffffff", activebackground="#2f81f7",
            activeforeground="#ffffff", relief="flat", font=FONT, padx=14, pady=4
        ).pack(side="left")
        tk.Button(btns, text="取消", command=lambda: self._cancel_new(),
                  bg=C["card2"], fg=C["text"], activebackground="#2d333b",
                  activeforeground=C["text"], relief="flat", font=FONT, padx=10, pady=4
        ).pack(side="left", padx=6)

    def sel_domain_default(self):
        if self.sel and isinstance(self.sel, tuple):
            return self.sel[0]
        return DOMAINS[0]

    def _cancel_new(self):
        self.sel = None
        self.render_detail()

    def _create_rule(self, domain, category, scope, priority, name):
        category = (category or "new").strip().lower()
        if not category or not category.replace("-", "").replace("_", "").isalnum():
            self._set_status("类别只允许小写字母/数字/短横线（如 adjust-plate）", "err")
            return
        try:
            prio = int(priority)
            if not (PRIORITY_MIN <= prio <= PRIORITY_MAX):
                raise ValueError
        except ValueError:
            self._set_status(f"优先级必须是 {PRIORITY_MIN}-{PRIORITY_MAX} 整数", "err")
            return
        if scope not in SCOPES:
            self._set_status("作用域非法", "err")
            return
        prefix = f"{domain}.{category}.{scope}."
        seq = 1
        for r in self.rules:
            if r.get("id", "").startswith(prefix):
                seq = max(seq, int(r["id"].rsplit(".", 1)[-1]) + 1)
        rule = {
            "id": f"{prefix}{seq:03d}",
            "domain": domain,
            "priority": prio,
            "scope": scope,
            "when": {"part.workingName": {"op": "contains", "value": ""}},
            "then": {},
            "meta": {
                "status": "draft",
                "version": 1,
                "author": self.author,
                "createdAt": datetime.date.today().isoformat(),
                "updatedAt": datetime.date.today().isoformat(),
                "rationale": "",
                "tests": [],
            },
        }
        if name.strip():
            rule["name"] = name.strip()
        # 默认值预填：首个动作属性（按域授权表 + 类型默认值）
        allowed = OWNERSHIP.get(domain, ())
        if allowed:
            rule["then"][allowed[0]] = kind_default(ATTR_KINDS.get(allowed[0], "str"))
        errs = check_rule(rule, self.cids)
        if errs:
            self._set_status("创建失败：" + errs[0], "err")
            return
        self._mutate(f"新建规则：{rule['id']}", lambda: self.rules.append(rule))
        self.sel = (domain, rule["id"])
        self.refresh_tree(select=rule["id"])
        self.render_detail()

    # ---------------- 占位 / 报告 ----------------
    def _render_placeholder(self):
        self._card_header("从哪开始？", "没接触过也能照着做")
        tk.Label(self.detail_inner, text=(
            "这个编辑器管理的是「零件信息规则」，回答三个问题：\n"
            "1) 零件归到哪一类（GR）\n"
            "2) 用什么材质\n"
            "3) 写什么加工说明\n\n"
            "三步上手：\n"
            "· 第 1 步：点左侧任意分组（如「分类（GR）」），右侧会告诉你它管什么；\n"
            "· 第 2 步：点分组下的一条规则，右侧就能编辑它的条件与内容；\n"
            "· 第 3 步：改完点「保存」，系统会自动检查，没问题就生效。\n\n"
            "想加一条新规则：点工具栏「＋ 新建规则」；\n"
            "改错了：Ctrl+Z 撤销（最多 100 步）。\n"
            "只想录零件不想学这些？关掉本窗口，运行 wizard.py——只填一张表单。"),
                 bg=C["card"], fg=C["text2"], font=FONT, justify="left").pack(
            anchor="w", padx=12, pady=10)

    def _render_report_panel(self, _rule=None):
        if not self._report_visible or not hasattr(self, "_report_text"):
            return
        box = tk.Frame(self.detail_inner, bg=C["card2"], padx=10, pady=6)
        box.pack(fill="x", pady=(0, 10))
        tk.Label(box, text="验证报告（语料干跑）", bg=C["card2"], fg=C["text"],
                 font=FONT).pack(anchor="w")
        tk.Label(box, text=self._report_text, bg=C["card2"], fg=C["text2"],
                 font=FONT_SM, justify="left", anchor="w").pack(anchor="w", pady=2)

    def validate_all(self):
        vr = validate_ruleset(self.rules, self.cids)
        rep = dry_run(self.rules, self.entries)
        ok, text = gate_summary(rep)
        lines = [f"结构校验：{len(vr['errors'])} 错误 / {len(vr['warnings'])} 警告",
                 f"干跑：{text}"]
        for e in vr["errors"][:12]:
            lines.append("· " + e)
        for w in vr["warnings"][:8]:
            lines.append("· " + w)
        for x in rep["wrong"][:12]:
            lines.append("· " + x)
        for x in rep["missing"][:12]:
            lines.append("· " + x)
        self._report_text = "\n".join(lines)
        self._report_visible = True
        self.render_detail()
        if vr["errors"] or rep["wrong"] or rep["missing"]:
            self._set_status("验证未通过：结构错误或干跑回归，禁止保存", "err")
        else:
            self._set_status("验证通过：结构无误，干跑全绿", "ok")

    # ---------------- 保存（门禁 + 快照） ----------------
    def save(self):
        vr = validate_ruleset(self.rules, self.cids)
        rep = dry_run(self.rules, self.entries)
        if vr["errors"] or rep["wrong"] or rep["missing"]:
            self._report_text = "\n".join(
                ["结构错误 " + str(len(vr["errors"])) + " 项，干跑不符 "
                 + str(len(rep["wrong"]) + len(rep["missing"])) + " 项，已阻止保存："]
                + [("· " + e) for e in vr["errors"][:10]]
                + [("· " + x) for x in rep["wrong"][:8]]
                + [("· " + x) for x in rep["missing"][:8]])
            self._report_visible = True
            self.render_detail()
            self._set_status("保存被门禁阻止（先修正错误，或 Ctrl+Z 回退）", "err")
            return
        try:
            ver = lifecycle.snapshot(self.rules_dir, self.rules, self.manifest)
            self.manifest["version"] = ver
            self._set_status(f"已保存并快照 v{ver}（备份在 rules/snapshots/，引擎热加载即生效）", "ok")
        except Exception as e:
            self._set_status(f"保存失败：{e}", "err")

    # ---------------- 状态条 ----------------
    def _set_status(self, text, kind="info"):
        color = {"ok": C["ok"], "err": C["err"], "warn": C["warn"]}.get(kind, C["text2"])
        self.status.configure(text=text, fg=color)


def main():
    root = tk.Tk()
    try:
        Editor(root)
    except Exception as e:
        import traceback
        traceback.print_exc()
        tk.Label(root, text=f"启动失败：{e}", fg=C["err"], bg=C["bg"]).pack(padx=20, pady=20)
    root.mainloop()


if __name__ == "__main__":
    main()
