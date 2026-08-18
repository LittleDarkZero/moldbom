# -*- coding: utf-8 -*-
"""零件信息表格录入（table_editor.py）— 替代复杂规则编辑器的日常录入工具。

界面布局
┌──────────────────────────────────────────────────────────────┐
│ 工具栏：[导入表格文本] [保存数据] [复制为表格] [应用到规则系统]    │
├──────────────┬───────────────────────────────────────────────┤
│ 层级树         │ 第三级详情（选中某个规格行后显示）               │
│ ▼ 📁 调整板    │ 零件名 / 规格（可改，改名自动整理层级）           │
│   · 40*60*12  │ GR名 [下拉+手输]   材料 [输入]                 │
│   · 40*60*15  │ 加工说明 [多行]                                │
│ 📁 定1模框     │ 紧固件明细：名称 / 规格 / 数量（可多条）          │
│ [＋零件][＋规格]│ [删除这一行]                                   │
└──────────────┴───────────────────────────────────────────────┘

数据模型（结构化 JSON，第三级字段挂在规格下）：
{
  "_格式": "零件信息表 v1",
  "parts": [
    {"partName": "调整板", "specs": [
      {"spec": "40*60*12", "gr": "仓库备件", "material": "45#",
       "remark": "外协精加工到位", "model": "BZ500.80/50",
       "fasteners": [
         {"name": "螺钉", "spec": "CB16-100", "qty": 4, "gr": "标准件"}]}]}
  ]
}

层级：第一级 零件名 → 第二级 规格 → 第三级 材料/GR名/加工说明/型号/紧固件。
型号（model）= BOM 打印规格：与测量尺寸不一致时才填（如量出 100*80*50 → 印 BZ500.80/50）；
留空则打印测量规格。
名称读型号（nameSpec）开关已于 2026-08-05 删除——引擎推理对所有零件自动尝试
从零件名提取型号（name_spec=True 默认，防误判），录入层不再需要开关。

校验：零件名必填；规格**可空**（空 = 兜底，该零件所有规格都适用；
有具名规格行时，具名规格优先、兜底兜剩下的）；
紧固件数量必须为正整数；
导入支持 Tab / 逗号 / 分号 / 竖线 分隔，可带表头行（自动识别跳过）；
紧固件列紧凑写法："CB16-100×4@标准件;CBW16×4@仓库备件"（分号分隔多条，× 分隔数量，
@ 后为该紧固件的 GR 名——可省略，留空走配套策略；
名称按规格前缀推断：CBW* → 弹簧垫圈，其余 → 螺钉；也可写 "螺钉:CB16-100×4@标准件"）。

「应用到规则系统」：每行自动转换为 gr/spec/material/remark/companion 规则
（与 wizard 共用 rulespec.entry 逻辑），更新不重复，过门禁后保存生效。
"""

import copy
import datetime
import json
import os
import re
import sys

import tkinter as tk
from tkinter import messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rulespec import entry, lifecycle                      # noqa: E402
from rulespec.corpus import corpus_ids, load_corpus        # noqa: E402
from rulespec.model import load_ruleset                    # noqa: E402
from rulespec.schema import GR_SUGGESTIONS, PRIORITY_DEFAULT, PRIORITY_MIN, PRIORITY_MAX  # noqa: E402
from rulespec.validator import dry_run, validate_ruleset   # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES_DIR = os.path.join(BASE_DIR, "rules")
DEFAULT_CORPUS_DIR = os.path.join(BASE_DIR, "corpus")
DEFAULT_DATA_DIR = os.path.join(BASE_DIR, "tabledata")
DEFAULT_DATA_PATH = os.path.join(DEFAULT_DATA_DIR, "零件信息表.json")
DATA_FORMAT = "零件信息表 v1"

C = {
    "bg": "#0d1117", "card": "#161b22", "card2": "#1c2128", "border": "#30363d",
    "text": "#e6edf3", "text2": "#8b949e", "accent": "#1f6feb",
    "ok": "#3fb950", "warn": "#d29922", "err": "#f85149", "dim": "#6e7681",
}
FONT = ("Microsoft YaHei UI", 10)
FONT_SM = ("Microsoft YaHei UI", 9)
FONT_HINT = ("Microsoft YaHei UI", 9)


# ---------------- 表格文本解析（模块级，可测试） ----------------

def parse_fasteners(text):
    """解析紧固件紧凑写法："CB16-100×4@标准件;CBW16×4@仓库备件"
    或 "螺钉:CB16-100×4"。@ 后为 GR 名（可省略，留空 = 走配套策略）。
    返回 (ok, 错误信息, 紧固件列表)。"""
    fs = []
    for part in re.split(r"[;；,，]", text or ""):
        part = part.strip()
        if not part:
            continue
        # 先拆 GR 后缀（@ 分隔，名称/规格里不会出现 @）
        gr = ""
        if "@" in part or "＠" in part:
            part, _, gr = part.replace("＠", "@").partition("@")
            gr = gr.strip()
            part = part.strip()
        if not part:
            continue
        m = re.match(r"^(?:([^:：]+)[:：])?(.+?)[×*xX]([\d.]+)$", part)
        if m:
            name, spec, qty = (m.group(1) or "").strip(), m.group(2).strip(), m.group(3)
        else:
            spec, qty, name = part, "1", ""
        if not spec:
            return False, "紧固件缺规格", []
        try:
            qty = int(float(qty))
        except ValueError:
            return False, f"紧固件「{spec}」的数量「{qty}」不是数字", []
        if qty <= 0:
            return False, f"紧固件「{spec}」的数量必须是正整数（不能是 0 或负数）", []
        if not name:
            name = "弹簧垫圈" if spec.startswith("CBW") else "螺钉"
        fs.append({"name": name, "spec": spec, "qty": qty, "gr": gr})
    return True, "", fs


def parse_table_text(text):
    """解析粘贴的表格文本 → (行列表, 错误列表)。行 = (行号, row dict)。
    列：零件名, 规格, 材料, GR名, 加工说明[, 紧固件]。自动识别表头与分隔符。"""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return [], ["没有可导入的内容（表格是空的）"]
    delim = "\t" if any("\t" in l for l in lines) else None
    if delim is None:
        for d in (",", "；", ";", "|"):
            if any(d in l for l in lines):
                delim = d
                break
    start = 0
    first = lines[0].split(delim) if delim else lines[0].split()
    first_cells = [c.strip() for c in first]
    # 表头识别：首格精确等于表头词才跳过（避免把"小零件A"这类行误判为表头）
    is_header = bool(first_cells) and first_cells[0] in (
        "零件名", "零件名称", "名称", "零件", "序号", "编号")
    if not is_header and len(first_cells) >= 2 and "规格" in first_cells:
        is_header = ("材料" in first_cells or "GR" in first_cells
                     or any("加工" in c for c in first_cells))
    if is_header:
        start = 1
    rows, errors = [], []
    for idx in range(start, len(lines)):
        cells = lines[idx].split(delim) if delim else lines[idx].split()
        cells = [c.strip() for c in cells]
        if not any(cells):
            continue
        line_no = idx + 1
        row = {"partName": cells[0] if len(cells) > 0 else "",
               "spec": cells[1] if len(cells) > 1 else "",
               "material": cells[2] if len(cells) > 2 else "",
               "gr": cells[3] if len(cells) > 3 else "",
               "remark": cells[4] if len(cells) > 4 else "",
               "model": cells[6] if len(cells) > 6 else "",
               "fasteners": []}
        if len(cells) > 5 and cells[5]:
            ok, msg, fs = parse_fasteners(cells[5])
            if not ok:
                errors.append(f"第 {line_no} 行：{msg}")
            else:
                row["fasteners"] = fs
        rows.append((line_no, row))
    return rows, errors


def merge_rows(rows):
    """同零件名合并为一个条目（规格为第二级多行；同规格行合并字段、紧固件去重）。"""
    parts_index = {}
    for _line_no, row in rows:
        name = row["partName"].strip()
        spec = row["spec"].strip()
        part = parts_index.get(name)
        if part is None:
            part = {"partName": name, "specs": []}
            parts_index[name] = part
        spec_row = next((s for s in part["specs"] if s["spec"] == spec), None)
        if spec_row is None:
            spec_row = {"spec": spec,
                        "gr": row["gr"].strip(), "material": row["material"].strip(),
                        "remark": row["remark"].strip(), "model": row["model"].strip(),
                        "fasteners": [dict(f) for f in row["fasteners"]]}
            part["specs"].append(spec_row)
        else:
            for f in ("material", "gr", "remark", "model"):
                if row[f]:
                    spec_row[f] = row[f].strip()
            for f in row["fasteners"]:
                for exist in spec_row["fasteners"]:
                    if (exist["name"] == f["name"] and exist["spec"] == f["spec"]
                            and exist.get("gr") == f.get("gr")):
                        exist["qty"] = f["qty"]
                        break
                else:
                    spec_row["fasteners"].append(dict(f))
    return list(parts_index.values())


def rules_to_parts(rules):
    """规则库 → 表格数据（5 域规则反解为零件条目；global 核心规则/其他域规则忽略）。

    零件名 = 第一级分组（同名聚合为一个零件条目，规格为第二级多行）；
    gr/material/spec(型号)/remark(含追加拼接)/companion(去重并集) 写入对应规格行。
    """
    parts_index = {}
    for r in rules:
        if r.get("domain") not in ("gr", "spec", "material", "remark", "companion"):
            continue
        if r.get("scope") == "global":
            continue
        nm = (r.get("when") or {}).get("part.workingName")
        if not isinstance(nm, dict):
            continue
        name = str(nm.get("value", ""))
        if not name:
            continue
        rspec = (r.get("when") or {}).get("spec.value")
        spec = str(rspec.get("value", "")) if isinstance(rspec, dict) else ""
        part = parts_index.get(name)
        if part is None:
            part = {"partName": name, "specs": []}
            parts_index[name] = part
        row = next((s for s in part["specs"] if s["spec"] == spec), None)
        if row is None:
            row = {"spec": spec, "gr": "", "material": "", "remark": "",
                   "model": "", "fasteners": []}
            part["specs"].append(row)
        t = r.get("then") or {}
        if r["domain"] == "gr" and t.get("gr"):
            row["gr"] = t["gr"]
        elif r["domain"] == "material" and t.get("material"):
            row["material"] = t["material"]
        elif r["domain"] == "remark":
            if t.get("remark"):
                row["remark"] = row["remark"] + ("\n" if row["remark"] else "") + t["remark"]
            for x in (t.get("remarkAppend") or {}).get("add", []):
                row["remark"] = row["remark"] + ("\n" if row["remark"] else "") + str(x)
        elif r["domain"] == "spec" and t.get("outputSpec"):
            row["model"] = t["outputSpec"]
        elif r["domain"] == "companion":
            for c in t.get("companions", []):
                if c not in row["fasteners"]:
                    row["fasteners"].append(dict(c))
    return list(parts_index.values())


def validate_data(parts):
    """表格数据校验 → 错误列表（口语化）。

    零件名必填；规格可空（空 = 兜底，该零件所有规格都适用）；
    紧固件：规格必填、数量必须为正整数。
    """
    errs = []
    for part in parts:
        name = (part.get("partName") or "").strip()
        if not name:
            errs.append("有一个零件没填零件名")
            continue
        for s in part.get("specs", []):
            for f in s.get("fasteners", []):
                if not (f.get("spec") or "").strip():
                    errs.append(f"「{name}」：有紧固件没填规格")
                    continue
                qty = f.get("qty")
                if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
                    errs.append(f"「{name}」（规格 {s.get('spec') or '兜底'}）："
                                f"紧固件「{f['spec']}」的数量必须是正整数")
    return errs


def export_table(parts):
    """导出回表格文本（Tab 分隔，可直接粘贴 Excel）。"""
    lines = ["零件名\t规格\t材料\tGR名\t加工说明\t紧固件\t型号"]
    for part in parts:
        for s in part.get("specs", []):
            fstr = ";".join(
                (f"{f['name']}:{f['spec']}×{f['qty']}"
                 if f.get("name") not in ("螺钉", "弹簧垫圈")
                 or (f.get("name") == "弹簧垫圈") != str(f.get("spec", "")).startswith("CBW")
                 else f"{f['spec']}×{f['qty']}")
                + (f"@{f['gr']}" if f.get("gr") else "")
                for f in s.get("fasteners", []))
            lines.append("\t".join([
                part.get("partName", ""), s.get("spec", ""),
                s.get("material", ""), s.get("gr", ""), s.get("remark", ""), fstr,
                s.get("model", "")]))
    return "\n".join(lines)


# ---------------- 编辑器 ----------------

class TableEditor:
    def __init__(self, root, data_path=None, rules_dir=None, corpus_dir=None):
        self.root = root
        self.data_path = data_path or DEFAULT_DATA_PATH
        self.rules_dir = rules_dir or DEFAULT_RULES_DIR
        self.corpus_dir = corpus_dir or os.path.join(
            os.path.dirname(self.rules_dir), "corpus")
        self.manifest, self.rules = load_ruleset(self.rules_dir)
        if os.path.isdir(self.corpus_dir):
            self.entries = load_corpus(self.corpus_dir)
            self.cids = corpus_ids(self.entries)
        else:
            self.entries, self.cids = [], None
        self.data = self._load_data()
        self.sel = None          # (part_idx, spec_idx)
        self.dirty = False
        self._confirm_del = None
        self._build_ui()
        self.refresh_tree()

    # ---------------- 数据 ----------------
    def _load_data(self):
        if os.path.exists(self.data_path):
            try:
                with open(self.data_path, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict) and "parts" in d:
                    return d
            except Exception:
                pass
        return {"_格式": DATA_FORMAT, "parts": []}

    def _save_data(self):
        errs = validate_data(self.data.get("parts", []))
        if errs:
            self._set_status("没保存：" + errs[0] + "（共 " + str(len(errs)) + " 处，见校验提示）", "err")
            self._show_problems(errs)
            return False
        os.makedirs(os.path.dirname(self.data_path), exist_ok=True)
        self.data["_格式"] = DATA_FORMAT
        self.data["updatedAt"] = __import__("datetime").date.today().isoformat()
        tmp = self.data_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.data_path)
        self.dirty = False
        self._set_status(f"已保存到 {os.path.basename(self.data_path)}", "ok")
        return True

    # ---------------- UI 骨架 ----------------
    def _build_ui(self):
        self.root.title("零件信息表录入")
        self.root.geometry("1000x640")
        self.root.configure(bg=C["bg"])
        self._style = ttk.Style()
        self._style.theme_use("clam")
        self._style.configure("Treeview", background=C["card"],
                              fieldbackground=C["card"], foreground=C["text"],
                              borderwidth=0, rowheight=24, font=FONT)
        self._style.configure("Treeview.Heading", background=C["card2"],
                              foreground=C["text2"], borderwidth=0, font=FONT_SM)
        self._style.map("Treeview", background=[("selected", C["accent"])],
                        foreground=[("selected", "#ffffff")])
        self._style.configure("TCombobox", fieldbackground=C["card2"],
                              background=C["card2"], foreground=C["text"],
                              arrowcolor=C["text2"])

        bar = tk.Frame(self.root, bg=C["bg"])
        bar.pack(fill="x", padx=12, pady=8)
        for text, cmd in (("📖 读取现有规则", self.load_rules_into_table),
                          ("导入表格文本", self.import_table),
                          ("保存数据", self._save_data),
                          ("复制为表格", self.copy_table),
                          ("应用到规则系统", self.apply_to_rules),
                          ("🚫 空Body过滤", self.open_body_filter),
                          ("⚙ 核心规则", self.open_core_rules)):
            tk.Button(bar, text=text, command=cmd, bg=C["card2"], fg=C["text"],
                      activebackground="#2d333b", activeforeground=C["text"],
                      relief="flat", font=FONT, padx=12, pady=4).pack(side="left", padx=(0, 6))
        tk.Label(bar, text="粘贴 Excel 表格 → 层级自动整理 → 保存/应用",
                 bg=C["bg"], fg=C["dim"], font=FONT_HINT).pack(side="right")

        paned = tk.PanedWindow(self.root, orient="horizontal", bg=C["bg"],
                               sashwidth=4, sashrelief="flat")
        paned.pack(fill="both", expand=True, padx=8)
        self._build_tree(paned)
        self._build_detail(paned)

        self.status = tk.Label(self.root, text="就绪", bg=C["bg"], fg=C["text2"],
                               font=FONT_HINT, anchor="w", padx=12)
        self.status.pack(fill="x", pady=(2, 6))

    def _build_tree(self, paned):
        left = tk.Frame(paned, bg=C["card"])
        paned.add(left, width=340, minsize=260)
        # 操作按钮放在树上方，醒目且始终可见
        btns = tk.Frame(left, bg=C["card"])
        btns.pack(fill="x", padx=6, pady=(6, 2))
        tk.Button(btns, text="＋手动添加一行", command=self.add_part, bg=C["accent"],
                  fg="#ffffff", activebackground="#2f81f7", activeforeground="#ffffff",
                  relief="flat", font=FONT_SM, padx=10, pady=3).pack(side="left", padx=2)
        tk.Button(btns, text="＋规格", command=self.add_spec, bg=C["card2"],
                  fg=C["text"], activebackground="#2d333b", activeforeground=C["text"],
                  relief="flat", font=FONT_SM, padx=8, pady=3).pack(side="left", padx=2)
        tk.Button(btns, text="删除选中", command=self.delete_row, bg="#3d1518",
                  fg=C["err"], activebackground="#581a1f", activeforeground=C["err"],
                  relief="flat", font=FONT_SM, padx=8, pady=3).pack(side="left", padx=2)

        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-3>", self._on_right_click)
        # 拖拽排序（零件/规格各自同级排序）
        self.tree.bind("<Button-1>", self._drag_start)
        self.tree.bind("<B1-Motion>", self._drag_motion)
        self.tree.bind("<ButtonRelease-1>", self._drag_drop)
        self._drag_iid = None
        self._drag_armed = False
        self._drag_y0 = 0
        self._drag_hl = None
        self.tree.tag_configure("_drag_target", background="#264f78")
        self.tree_menu = tk.Menu(left, tearoff=0, bg=C["card"], fg=C["text"],
                                 activebackground="#1f6feb", activeforeground="#ffffff",
                                 font=FONT_SM)
        tk.Label(left, text="手动编辑：\n"
                            "· ＋手动添加一行 = 新零件 + 一条空规格，右侧直接填\n"
                            "· 点规格行 → 右侧改 GR名/材料/加工说明/紧固件\n"
                            "· 双击零件/规格 = 就地改名；右键 = 增/删/改名菜单\n"
                            "· 拖动零件/规格行 = 调整顺序（保存后生效）\n"
                            "· 导入表格：工具栏「导入表格文本」粘贴 Excel",
                 bg=C["card"], fg=C["dim"], font=FONT_HINT, justify="left",
                 anchor="w", padx=8, pady=6).pack(fill="x", side="bottom")

    # ---------------- 拖拽排序 ----------------
    def _drag_start(self, event):
        iid = self.tree.identify_row(event.y)
        self._drag_iid = iid or None
        self._drag_armed = False
        self._drag_y0 = event.y

    def _drag_motion(self, event):
        if not self._drag_iid:
            return
        if abs(event.y - self._drag_y0) > 6:
            self._drag_armed = True
        if not self._drag_armed:
            return
        target = self.tree.identify_row(event.y)
        self._clear_drag_highlight()
        if target and self._can_drag_to(self._drag_iid, target):
            self.tree.item(target, tags=("_drag_target",))
            self._drag_hl = target
        self.tree.selection_set(self._drag_iid)   # 拖动时保持源行选中

    def _drag_drop(self, event):
        src = self._drag_iid
        self._drag_iid = None
        self._clear_drag_highlight()
        if not src or not self._drag_armed:
            return
        target = self.tree.identify_row(event.y)
        if target and self._can_drag_to(src, target):
            self._move_node(src, target)
        elif not target:
            self._move_to_end(src)               # 拖到空白 = 移到该层末尾

    def _clear_drag_highlight(self):
        if self._drag_hl:
            self.tree.item(self._drag_hl, tags=())
            self._drag_hl = None

    def _can_drag_to(self, src, dst):
        """只允许同级移动：零件→零件；规格→同零件的规格。"""
        if src == dst or src[0] != dst[0]:
            return False
        if src[0] == "p":
            return True
        return src.split(":")[1] == dst.split(":")[1]

    def _move_node(self, src, dst):
        """把 src 行移到 dst 行位置：向上拖→插到目标前，向下拖→插到目标后。"""
        if src[0] == "p":
            parts = self.data.setdefault("parts", [])
            i, j = int(src[2:]), int(dst[2:])
            node = parts.pop(i)
            parts.insert(j, node)
            new_iid = f"p:{j}"
        else:
            _, pi, si = src.split(":")
            _, _, di = dst.split(":")
            specs = self.data["parts"][int(pi)].setdefault("specs", [])
            si, di = int(si), int(di)
            node = specs.pop(si)
            specs.insert(di, node)
            new_iid = f"s:{pi}:{di}"
        self._mark_dirty("调整顺序")
        self.refresh_tree(select=new_iid)
        self.render_detail()

    def _move_to_end(self, src):
        """拖到空白区 → 移动到该层末尾。"""
        if src[0] == "p":
            parts = self.data.setdefault("parts", [])
            i = int(src[2:])
            node = parts.pop(i)
            parts.append(node)
            new_iid = f"p:{len(parts) - 1}"
        else:
            _, pi, si = src.split(":")
            specs = self.data["parts"][int(pi)].setdefault("specs", [])
            si = int(si)
            node = specs.pop(si)
            specs.append(node)
            new_iid = f"s:{pi}:{len(specs) - 1}"
        self._mark_dirty("调整顺序")
        self.refresh_tree(select=new_iid)
        self.render_detail()

    # ---------------- 手动编辑：右键菜单 / 双击改名 ----------------
    def _on_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        menu = self.tree_menu
        menu.delete(0, "end")
        if not iid:
            menu.add_command(label="＋ 新增零件（含一条空规格）", command=self.add_part)
            menu.tk_popup(event.x_root, event.y_root)
            return
        if iid.startswith("p:"):
            menu.add_command(label="＋ 新增规格",
                             command=lambda: self.add_spec(int(iid[2:])))
            menu.add_command(label="✎ 重命名零件（双击也可）",
                             command=lambda: self._inline_rename(iid))
            menu.add_separator()
            menu.add_command(label="🗑 删除零件（含全部规格）",
                             command=lambda: self._delete_part(iid))
        else:
            menu.add_command(label="✎ 重命名规格（双击也可）",
                             command=lambda: self._inline_rename(iid))
            menu.add_separator()
            menu.add_command(label="🗑 删除该行", command=self.delete_row)
        menu.tk_popup(event.x_root, event.y_root)

    def _on_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self._inline_rename(iid)

    def _inline_rename(self, iid):
        """就地重命名：输入框覆盖在树行上，回车/失焦保存，Esc 取消。"""
        bbox = self.tree.bbox(iid)
        if not bbox:
            return
        x, y, w, h = bbox
        if iid.startswith("p:"):
            cur = self.tree.item(iid, "text")[2:].strip()
        else:
            cur = self.tree.item(iid, "text").split("· ", 1)[-1].split("  ")[0].strip()
        var = tk.StringVar(value=cur)
        ent = tk.Entry(self.tree, textvariable=var, bg=C["card2"], fg=C["text"],
                       insertbackground=C["text"], relief="flat",
                       highlightthickness=1, highlightbackground=C["accent"],
                       font=FONT)
        ent.place(x=x, y=y, width=w, height=h)
        ent.focus_set()
        ent.select_range(0, "end")

        def commit(_e=None):
            ent.destroy()
            v = var.get().strip()
            if iid.startswith("p:"):
                if not v:
                    self._set_status("零件名不能为空", "err")
                    return
                self.data["parts"][int(iid[2:])]["partName"] = v
                self._mark_dirty("重命名零件")
            else:
                _, i, j = iid.split(":")
                spec_row = self.data["parts"][int(i)]["specs"][int(j)]
                spec_row["spec"] = v      # 留空 = 兜底（全部规格）
                self._mark_dirty("重命名规格")
            self.refresh_tree(select=iid)

        def cancel(_e=None):
            ent.destroy()

        ent.bind("<Return>", commit)
        ent.bind("<FocusOut>", commit)
        ent.bind("<Escape>", cancel)
        ent._commit = commit    # 测试/程序化驱动用（无头环境不派发键盘事件）
        ent._cancel = cancel

    def _delete_part(self, iid):
        i = int(iid[2:])
        part = self.data["parts"][i]
        name = part.get("partName", "")
        if self._confirm_del != ("p", i):
            self._confirm_del = ("p", i)
            self._set_status(f"再点一次「删除零件」确认删除：{name}（含全部规格）", "err")
            self.root.after(3000, lambda: setattr(self, "_confirm_del", None))
            return
        del self.data["parts"][i]
        self._confirm_del = None
        self.sel = None
        self._mark_dirty(f"删除零件：{name}")
        self.refresh_tree()
        self.render_detail()
        # 同步删除规则系统中的全部对应规则
        if messagebox.askyesno(
                "同步删除规则",
                f"是否同时从规则系统中删除「{name}」的全部规则？\n\n"
                "删除后立即生效（快照可回滚）；选「否」则只删表格数据。",
                parent=self.root):
            n, err = self._delete_rules_for(name, None)
            if err:
                self._set_status("删除规则失败：" + err, "err")
            elif n == 0:
                self._set_status("规则系统中没有「" + name + "」的规则（可能尚未应用过）", "warn")
            else:
                self._set_status(f"已从规则系统删除 {n} 条规则（v{self.manifest.get('version')}）", "ok")

    def _build_detail(self, paned):
        right = tk.Frame(paned, bg=C["card"])
        paned.add(right, width=660, minsize=520)
        self.detail = tk.Frame(right, bg=C["card"])
        self.detail.pack(fill="both", expand=True)

    def _clear_detail(self):
        for w in self.detail.winfo_children():
            w.destroy()

    # ---------------- 树 ----------------
    def refresh_tree(self, select=None):
        self.tree.delete(*self.tree.get_children())
        parts = self.data.get("parts", [])
        for i, part in enumerate(parts):
            pid = f"p:{i}"
            self.tree.insert("", "end", iid=pid,
                             text=f"📁 {part.get('partName') or '（未命名）'}")
            for j, s in enumerate(part.get("specs", [])):
                sid = f"s:{i}:{j}"
                fcnt = len(s.get("fasteners", []))
                suffix = f"  紧固×{fcnt}" if fcnt else ""
                sname = s.get("spec") or "（兜底·全部规格）"
                self.tree.insert(pid, "end", iid=sid,
                                 text=f"· {sname}{suffix}")
        if select:
            self.tree.selection_set(select)
            self.tree.see(select)

    def _on_select(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid.startswith("s:"):
            _, i, j = iid.split(":")
            self.sel = (int(i), int(j))
            self.render_detail()
        else:
            self.tree.selection_remove(iid)

    # ---------------- 详情 ----------------
    def render_detail(self):
        self._clear_detail()
        if not self.sel:
            tk.Label(self.detail, text="左侧选中一个「规格行」，右侧就能填它的信息。\n"
                                       "没有内容？点「导入表格文本」把 Excel 表格贴进来，"
                                       "或点「＋零件」「＋规格」手动建。",
                     bg=C["card"], fg=C["text2"], font=FONT, justify="left").pack(
                anchor="w", padx=16, pady=14)
            return
        i, j = self.sel
        try:
            part = self.data["parts"][i]
            spec_row = part["specs"][j]
        except (IndexError, KeyError):
            self.sel = None
            self.render_detail()
            return
        box = tk.Frame(self.detail, bg=C["card"], padx=16, pady=10)
        box.pack(fill="both", expand=True)

        def label_row(text, label):
            r = tk.Frame(box, bg=C["card"])
            r.pack(fill="x", pady=3)
            tk.Label(r, text=label, width=9, anchor="w", bg=C["card"],
                     fg=C["text2"], font=FONT).pack(side="left")
            return r

        # 零件名（可改）
        r = label_row("", "零件名")
        name_var = tk.StringVar(value=part.get("partName", ""))
        ent = tk.Entry(r, textvariable=name_var, bg=C["card2"], fg=C["text"],
                       insertbackground=C["text"], relief="flat",
                       highlightthickness=1, highlightbackground=C["border"], font=FONT)
        ent.pack(side="left", fill="x", expand=True, ipady=3)

        def commit_name(_e=None):
            v = name_var.get().strip()
            old = part.get("partName", "")
            if v and v != old:
                part["partName"] = v
                self._mark_dirty("零件名已改")
                self.refresh_tree(select=f"s:{i}:{j}")
                # 改名联动：规则系统中旧名的规则自动改名（2026-08-05）
                n, err = self._rename_rules_for(old, v)
                if err:
                    self._set_status("规则改名失败（表格已改，规则未动）：" + err, "err")
                elif n:
                    self._set_status(f"已改名：{old} → {v}（规则系统中 {n} 条已同步）", "ok")
            elif not v:
                self._set_status("零件名不能为空", "err")

        ent.bind("<FocusOut>", commit_name)
        ent.bind("<Return>", commit_name)

        # 规格（可改）
        r = label_row("", "规格")
        spec_var = tk.StringVar(value=spec_row.get("spec", ""))
        ent2 = tk.Entry(r, textvariable=spec_var, bg=C["card2"], fg=C["text"],
                        insertbackground=C["text"], relief="flat",
                        highlightthickness=1, highlightbackground=C["border"], font=FONT)
        ent2.pack(side="left", fill="x", expand=True, ipady=3)

        def commit_spec(_e=None):
            v = spec_var.get().strip()
            if v != spec_row.get("spec"):
                spec_row["spec"] = v      # 留空 = 兜底（全部规格）
                self._mark_dirty("规格已改")
                self.refresh_tree(select=f"s:{i}:{j}")

        ent2.bind("<FocusOut>", commit_spec)
        ent2.bind("<Return>", commit_spec)
        tk.Label(box, text="（留空 = 兜底：这个零件的所有规格都按此行处理；"
                           "填了具体规格 = 只针对该规格）",
                 bg=C["card"], fg=C["dim"], font=FONT_HINT, anchor="w",
                 justify="left", wraplength=560).pack(fill="x", pady=(2, 0))

        # GR 名
        r = label_row("", "GR名")
        grs = list(GR_SUGGESTIONS)
        for p2 in self.data.get("parts", []):
            for s2 in p2.get("specs", []):
                g = s2.get("gr")
                if g and g not in grs:
                    grs.append(g)
        gr_var = tk.StringVar(value=spec_row.get("gr", ""))
        cb = ttk.Combobox(r, textvariable=gr_var, values=grs, font=FONT)
        cb.pack(side="left", fill="x", expand=True, ipady=2)

        def commit_gr(*_a):
            spec_row["gr"] = gr_var.get().strip()
            self._mark_dirty("GR名已改")

        cb.bind("<<ComboboxSelected>>", commit_gr)
        gr_var.trace_add("write", commit_gr)

        # 优先级（默认 500，越大越优先；同名关键词命中冲突时用它定先后）
        r = label_row("", "优先级")
        prio_var = tk.StringVar(value=str(spec_row.get("priority", PRIORITY_DEFAULT)))
        ent_prio = tk.Entry(r, textvariable=prio_var, bg=C["card2"], fg=C["text"],
                            insertbackground=C["text"], relief="flat",
                            highlightthickness=1, highlightbackground=C["border"],
                            font=FONT, width=8)
        ent_prio.pack(side="left", ipady=3)
        tk.Label(r, text=f"（默认 {PRIORITY_DEFAULT}，数字越大越优先；同名关键词冲突时用它定先后）",
                 bg=C["card"], fg=C["dim"], font=FONT_HINT, anchor="w").pack(
            side="left", padx=(8, 0))

        def commit_prio(_e=None):
            v = prio_var.get().strip()
            if not v:
                spec_row.pop("priority", None)
            else:
                try:
                    p = int(v)
                    if not (PRIORITY_MIN <= p <= PRIORITY_MAX):
                        raise ValueError
                except ValueError:
                    self._set_status(f"优先级必须是 {PRIORITY_MIN}-{PRIORITY_MAX} 整数", "err")
                    return
                spec_row["priority"] = p
            self._mark_dirty("优先级已改")

        ent_prio.bind("<FocusOut>", commit_prio)
        ent_prio.bind("<Return>", commit_prio)

        # 型号（输出规格，打印用）
        r = label_row("", "型号（打印用）")
        model_var = tk.StringVar(value=spec_row.get("model", ""))
        ent_model = tk.Entry(r, textvariable=model_var, bg=C["card2"], fg=C["text"],
                             insertbackground=C["text"], relief="flat",
                             highlightthickness=1, highlightbackground=C["border"],
                             font=FONT)
        ent_model.pack(side="left", fill="x", expand=True, ipady=3)
        tk.Label(box, text="BOM 上印的规格和测量尺寸不一样时才填（如量出 100*80*50、"
                           "要印 BZ500.80/50）；留空就印测量尺寸。",
                 bg=C["card"], fg=C["dim"], font=FONT_HINT, anchor="w",
                 justify="left", wraplength=560).pack(fill="x", pady=(2, 0))

        def commit_model(_e=None):
            spec_row["model"] = model_var.get().strip()
            self._mark_dirty("型号已改")

        ent_model.bind("<FocusOut>", commit_model)
        ent_model.bind("<Return>", commit_model)

        # 材料
        r = label_row("", "材料")
        mat_var = tk.StringVar(value=spec_row.get("material", ""))
        ent3 = tk.Entry(r, textvariable=mat_var, bg=C["card2"], fg=C["text"],
                        insertbackground=C["text"], relief="flat",
                        highlightthickness=1, highlightbackground=C["border"], font=FONT)
        ent3.pack(side="left", fill="x", expand=True, ipady=3)

        def commit_mat(_e=None):
            spec_row["material"] = mat_var.get().strip()
            self._mark_dirty("材料已改")

        ent3.bind("<FocusOut>", commit_mat)
        ent3.bind("<Return>", commit_mat)

        # 加工说明（多行）
        r = label_row("", "加工说明")
        txt = tk.Text(r, height=3, bg=C["card2"], fg=C["text"],
                      insertbackground=C["text"], relief="flat",
                      highlightthickness=1, highlightbackground=C["border"], font=FONT)
        txt.insert("1.0", spec_row.get("remark", ""))
        txt.pack(side="left", fill="x", expand=True, ipady=2)

        def commit_remark(*_a):
            if txt.edit_modified():
                spec_row["remark"] = txt.get("1.0", "end").strip()
                txt.edit_modified(False)
                self._mark_dirty("加工说明已改")

        txt.bind("<<Modified>>", commit_remark)

        # 紧固件明细（第三级，可多条）
        tk.Label(box, text="紧固件（可多条；名称/规格/数量/GR——GR 留空走配套策略）", bg=C["card"],
                 fg=C["text2"], font=FONT, anchor="w").pack(anchor="w", pady=(8, 2))
        self._fastener_box = tk.Frame(box, bg=C["card"])
        self._fastener_box.pack(fill="x")
        self._render_fasteners(spec_row)

        tk.Button(box, text="删除这一行", command=lambda: self.delete_row(),
                  bg="#3d1518", fg=C["err"], activebackground="#581a1f",
                  activeforeground=C["err"], relief="flat", font=FONT_SM,
                  padx=10, pady=3).pack(anchor="w", pady=(10, 0))

    def _render_fasteners(self, spec_row):
        for w in self._fastener_box.winfo_children():
            w.destroy()
        for f in spec_row.get("fasteners", []):
            self._fastener_row(spec_row, f)
        tk.Button(self._fastener_box, text="＋添加紧固件",
                  command=lambda: self._add_fastener(spec_row), bg=C["card2"],
                  fg=C["text"], activebackground="#2d333b", activeforeground=C["text"],
                  relief="flat", font=FONT_SM, padx=8, pady=2).pack(anchor="w", pady=3)

    def _fastener_row(self, spec_row, f):
        r = tk.Frame(self._fastener_box, bg=C["card2"], padx=6, pady=3)
        r.pack(fill="x", pady=2)
        for key, lab, width in (("name", "名称", 10), ("spec", "规格", 14),
                                ("qty", "数量", 6)):
            tk.Label(r, text=lab, bg=C["card2"], fg=C["dim"], font=FONT_SM,
                     width=3, anchor="e").pack(side="left", padx=(4, 2))
            var = tk.StringVar(value=str(f.get(key, "")))
            ent = tk.Entry(r, textvariable=var, width=width, bg=C["card"],
                           fg=C["text"], insertbackground=C["text"], relief="flat",
                           highlightthickness=1, highlightbackground=C["border"],
                           font=FONT_SM)

            def commit(_e=None, k=key, v=var):
                val = v.get().strip()
                if k == "qty":
                    try:
                        q = int(val)
                        if q <= 0:
                            self._set_status("数量必须是正整数（1、2、3…）", "err")
                            return
                        f["qty"] = q
                    except ValueError:
                        self._set_status("数量必须是正整数（1、2、3…）", "err")
                        return
                else:
                    if not val:
                        self._set_status("紧固件" + ("名称" if k == "name" else "规格") + "不能为空", "err")
                        return
                    f[k] = val
                self._mark_dirty("紧固件已改")
                self.refresh_tree(select=self._sel_iid())

            ent.bind("<FocusOut>", commit)
            ent.bind("<Return>", commit)
            ent.pack(side="left", padx=2, ipady=2)
        # GR 下拉：仓库备件 / 标准件 / 留空（留空 = 走配套策略）
        tk.Label(r, text="GR", bg=C["card2"], fg=C["dim"], font=FONT_SM,
                 width=3, anchor="e").pack(side="left", padx=(4, 2))
        gr_var = tk.StringVar(value=f.get("gr", ""))
        gr_cb = ttk.Combobox(r, textvariable=gr_var, width=8, font=FONT_SM,
                             values=["", "仓库备件", "标准件"])
        gr_cb.pack(side="left", padx=2, ipady=1)

        def commit_gr(_e=None):
            f["gr"] = gr_var.get().strip()
            self._mark_dirty("紧固件 GR 已改")
            self.refresh_tree(select=self._sel_iid())

        gr_cb.bind("<<ComboboxSelected>>", commit_gr)
        gr_cb.bind("<FocusOut>", commit_gr)
        tk.Button(r, text="×", command=lambda: self._del_fastener(spec_row, f),
                  bg=C["card2"], fg=C["err"], relief="flat", font=FONT_SM,
                  activebackground="#2d333b").pack(side="left")

    def _add_fastener(self, spec_row):
        spec_row.setdefault("fasteners", []).append(
            {"name": "螺钉", "spec": "", "qty": 1, "gr": ""})
        self._mark_dirty("添加紧固件")
        self._render_fasteners(spec_row)
        self.refresh_tree(select=self._sel_iid())

    def _del_fastener(self, spec_row, f):
        spec_row["fasteners"].remove(f)
        self._mark_dirty("删除紧固件")
        self._render_fasteners(spec_row)
        self.refresh_tree(select=self._sel_iid())

    def _sel_iid(self):
        return f"s:{self.sel[0]}:{self.sel[1]}" if self.sel else None

    # ---------------- 增删行 ----------------
    def add_part(self):
        """手动添加一行：新零件 + 一条空规格，右侧直接填。"""
        self.data.setdefault("parts", []).append({
            "partName": "新零件",
            "specs": [{"spec": "", "gr": "", "material": "",
                       "remark": "", "model": "",
                       "fasteners": []}]})
        i = len(self.data["parts"]) - 1
        self.sel = (i, 0)
        self._mark_dirty("手动添加一行")
        self.refresh_tree(select=f"s:{i}:0")
        self.render_detail()
        self._focus_first_field()
        self._set_status("已添加一行——直接在右侧填零件名、规格、GR名等，填完点「保存数据」", "ok")

    def _focus_first_field(self):
        for w in self._walk(self.detail):
            if isinstance(w, tk.Entry):
                w.focus_set()
                break

    @staticmethod
    def _walk(widget):
        for w in widget.winfo_children():
            yield w
            yield from TableEditor._walk(w)

    def add_spec(self, part_idx=None):
        if part_idx is None:
            if not self.sel:
                self._set_status("先在左侧选中一个零件，再点「＋规格」", "warn")
                return
            i, _j = self.sel
        else:
            i = part_idx
        self.data["parts"][i].setdefault("specs", []).append(
            {"spec": "", "gr": "", "material": "", "remark": "", "model": "", "fasteners": []})
        j = len(self.data["parts"][i]["specs"]) - 1
        self.sel = (i, j)
        self._mark_dirty("新增规格")
        self.refresh_tree(select=f"s:{i}:{j}")
        self.render_detail()
        self._focus_first_field()
        self._set_status("已添加一条空规格——右侧填规格和各项信息", "ok")

    def delete_row(self):
        if not self.sel:
            self._set_status("先选中要删除的规格行（或零件下的行）", "warn")
            return
        i, j = self.sel
        part = self.data["parts"][i]
        name = part.get("partName", "")
        spec = part["specs"][j].get("spec", "")
        if self._confirm_del != (i, j):
            self._confirm_del = (i, j)
            self._set_status(f"再点一次「删除选中」确认删除：{name} {spec}", "err")
            self.root.after(3000, lambda: setattr(self, "_confirm_del", None))
            return
        del part["specs"][j]
        if not part["specs"]:
            del self.data["parts"][i]
        self._confirm_del = None
        self.sel = None
        self._mark_dirty(f"删除：{name} {spec}")
        self.refresh_tree()
        self.render_detail()
        # 同步删除规则系统中的对应规则（2026-08-05：表格删除需能删规则，否则规则只增不减）
        self._confirm_sync_delete_rules(name, spec)

    def _confirm_sync_delete_rules(self, name, spec):
        """询问是否同时从规则系统删除该行对应的规则（spec=None=整零件）。"""
        label = f"{name} {spec or '（兜底）'}"
        if not messagebox.askyesno(
                "同步删除规则",
                f"是否同时从规则系统中删除「{label}」对应的规则？\n\n"
                "删除后立即生效（快照可回滚）；选「否」则只删表格行，规则保留。",
                parent=self.root):
            return
        n, err = self._delete_rules_for(name, spec)
        if err:
            self._set_status("删除规则失败：" + err, "err")
        elif n == 0:
            self._set_status("规则系统中没有「" + label + "」对应的规则（可能尚未应用过）", "warn")
        else:
            self._set_status(f"已从规则系统删除 {n} 条规则（v{self.manifest.get('version')}）", "ok")

    def _delete_rules_for(self, name, spec):
        """从规则系统删除该行（零件名+规格）对应的 5 域规则（gr/spec/material/remark/companion）。

        spec=None = 整零件（该名下全部规则）；
        spec=''    = 兜底行（删 part 级规则——when 无规格条件）；
        spec 非空   = 删 spec 级规则（when.spec.value 精确匹配）。
        返回 (删除条数, 错误消息或 None)；门禁不过时不提交（零污染）。
        """
        from rulespec.matcher import canonical_spec
        cand = copy.deepcopy(self.rules)
        kept, removed = [], 0
        for r in cand:
            if r.get("domain") not in ("gr", "spec", "material", "remark", "companion"):
                kept.append(r)
                continue
            rw = r.get("when") or {}
            nm = rw.get("part.workingName")
            if not isinstance(nm, dict):
                kept.append(r)
                continue
            v = str(nm.get("value", ""))
            if not (name in v or v in name):
                kept.append(r)
                continue
            if spec is None:
                removed += 1          # 整零件：删除该名下全部规则
                continue
            rspec = rw.get("spec.value")
            if spec:
                if isinstance(rspec, dict) \
                        and canonical_spec(rspec.get("value")) == canonical_spec(spec):
                    removed += 1      # 具名行：删 spec 级规则
                else:
                    kept.append(r)
            else:
                if not isinstance(rspec, dict):
                    removed += 1      # 兜底行：删 part 级规则
                else:
                    kept.append(r)
        if removed == 0:
            return 0, None
        vr = validate_ruleset(kept, self.cids)
        rep = dry_run(kept, self.entries)
        if vr["errors"] or rep["wrong"] or rep["missing"]:
            first = (vr["errors"] or rep["wrong"] or rep["missing"])[0]
            return 0, first
        try:
            ver = lifecycle.snapshot(self.rules_dir, kept, self.manifest)
            self.manifest["version"] = ver
            self.rules = kept
            return removed, None
        except Exception as e:
            return 0, str(e)

    def _rename_rules_for(self, old, new):
        """规则系统改名联动：5 域规则中零件名**精确等于**旧名的 → 改为新名。

        解决"零件名写错"场景：表格里直接改名，规则跟着改，无需手动删旧规则。
        精确相等（非包含）——子串型规则（如『盖板』）不受影响，避免误伤其他零件。
        返回 (改动条数, 错误消息或 None)；门禁不过时不提交。
        """
        cand = copy.deepcopy(self.rules)
        n = 0
        for r in cand:
            if r.get("domain") not in ("gr", "spec", "material", "remark", "companion"):
                continue
            nm = (r.get("when") or {}).get("part.workingName")
            if isinstance(nm, dict) and str(nm.get("value", "")) == old:
                nm["value"] = new
                n += 1
        if n == 0:
            return 0, None
        vr = validate_ruleset(cand, self.cids)
        rep = dry_run(cand, self.entries)
        if vr["errors"] or rep["wrong"] or rep["missing"]:
            return 0, (vr["errors"] or rep["wrong"] or rep["missing"])[0]
        try:
            ver = lifecycle.snapshot(self.rules_dir, cand, self.manifest)
            self.manifest["version"] = ver
            self.rules = cand
            return n, None
        except Exception as e:
            return 0, str(e)

    # ---------------- 核心规则（global 作用域） ----------------
    def open_body_filter(self):
        """管理「空 Body 过滤」关键词（V2 filter 域规则，2026-08-11 用户需求 B 类）。

        与正常零件 GR 编辑完全分开的独立窗口：每行一个关键词，名字含任一关键词
        的 Body 在解析时直接跳过（不入 BOM、不测量）。保存时重建 filter.wizard.*
        规则（全量，删除同步生效），走门禁 + 快照；其他来源的 filter 规则保留。
        """
        dlg = tk.Toplevel(self.root)
        dlg.title("空 Body 过滤（关键词）")
        dlg.configure(bg=C["card"])
        dlg.geometry("540x440")
        tk.Label(dlg, text="这些 Body 是空的（无实体几何，如分组/工艺特征）。\n"
                           "名字含以下任意关键词 → 解析时直接跳过，不进 BOM、不测量。\n"
                           "每行一个关键词（也支持逗号/分号/空格分隔多条）：",
                 bg=C["card"], fg=C["text2"], font=FONT_SM, justify="left",
                 anchor="w").pack(anchor="w", padx=14, pady=(12, 4))
        kws = set()
        for r in self.rules:
            if r.get("domain") != "filter":
                continue
            m = (r.get("when") or {}).get("part.workingName")
            if isinstance(m, dict) and m.get("op") in ("contains", "eq", "keyword"):
                kws.add(str(m.get("value", "")))
        box = tk.Text(dlg, bg=C["card"], fg=C["text"], insertbackground=C["text"],
                      font=FONT_SM, relief="flat", highlightthickness=1,
                      highlightbackground=C["border"], height=12)
        box.pack(fill="both", expand=True, padx=14, pady=(4, 8))
        box.insert("1.0", "\n".join(sorted(kws)))
        hint = tk.Label(dlg, text="", bg=C["card"], fg=C["dim"], font=FONT_SM,
                        justify="left", anchor="w")
        hint.pack(anchor="w", padx=14, pady=(0, 4))
        btn_bar = tk.Frame(dlg, bg=C["card"])
        btn_bar.pack(fill="x", padx=14, pady=(0, 12))
        tk.Button(btn_bar, text="💾 保存（重建规则）",
                  command=lambda: self._save_body_filter(box, dlg, hint),
                  bg=C["card2"], fg=C["text"], activebackground="#2d333b",
                  activeforeground=C["text"], relief="flat", font=FONT,
                  padx=14, pady=4).pack(side="left", padx=(0, 8))
        tk.Button(btn_bar, text="取消", command=dlg.destroy,
                  bg=C["card2"], fg=C["text"], activebackground="#2d333b",
                  activeforeground=C["text"], relief="flat", font=FONT,
                  padx=14, pady=4).pack(side="left", padx=(0, 8))

    def _save_body_filter(self, box, dlg, hint):
        """保存空 Body 过滤关键词 → 重建 filter.wizard.* 规则（门禁 + 快照）。"""
        raw = box.get("1.0", "end")
        kws = []
        for line in raw.splitlines():
            for kw in re.split(r"[,，;；\s]+", line.strip()):
                if kw:
                    kws.append(kw)
        kws = list(dict.fromkeys(kws))          # 去重保序
        cand = copy.deepcopy(self.rules)
        kept = [r for r in cand if not (r.get("domain") == "filter"
                                        and str(r.get("id", "")).startswith("filter.wizard."))]
        seq = 1
        for r in cand:
            if str(r.get("id", "")).startswith("filter.wizard.part."):
                seq = max(seq, int(r["id"].rsplit(".", 1)[-1]) + 1)
        for kw in kws:
            kept.append({
                "id": f"filter.wizard.part.{seq:03d}",
                "domain": "filter", "priority": 500, "scope": "part",
                "when": {"part.workingName": {"op": "contains", "value": kw}},
                "then": {"input.skipBody": True,
                         "input.skipReason": "空 Body 过滤（名字含关键词）"},
                "meta": {"status": "active", "version": 1, "author": self._load_author(),
                         "createdAt": datetime.date.today().isoformat(),
                         "updatedAt": datetime.date.today().isoformat(),
                         "rationale": "", "tests": []},
            })
            seq += 1
        vr = validate_ruleset(kept, self.cids)
        rep = dry_run(kept, self.entries)
        if vr["errors"] or rep["wrong"] or rep["missing"]:
            first = (vr["errors"] or rep["wrong"] or rep["missing"])[0]
            hint.configure(text="保存未通过校验：" + first, fg=C["err"])
            return
        try:
            ver = lifecycle.snapshot(self.rules_dir, kept, self.manifest)
            self.manifest["version"] = ver
            self.rules = kept
        except Exception as e:
            hint.configure(text="保存失败：" + str(e), fg=C["err"])
            return
        dlg.destroy()
        self._set_status(f"空 Body 过滤已保存：{len(kws)} 个关键词（v{ver}，快照可回滚）", "ok")

    def open_core_rules(self):
        """管理 global 作用域规则（对任何零件生效，如未录入零件的默认分类兜底）。

        普通零件规则用左侧表格维护；核心规则没有对应零件行，独立窗口编辑。
        """
        dlg = tk.Toplevel(self.root)
        dlg.title("核心规则（全局）")
        dlg.configure(bg=C["card"])
        dlg.geometry("580x320")
        tk.Label(dlg, text="核心规则 = 全局生效（如未录入零件的默认分类兜底 gr.default.part.001）。\n"
                           "普通零件规则请用左侧表格维护；其他域的核心规则请用 editor.py 编辑。",
                 bg=C["card"], fg=C["text2"], font=FONT_SM, justify="left",
                 anchor="w").pack(anchor="w", padx=14, pady=(12, 6))
        globals_rules = [r for r in self.rules if r.get("scope") == "global"]
        if not globals_rules:
            tk.Label(dlg, text="（暂无核心规则）", bg=C["card"], fg=C["dim"],
                     font=FONT).pack(pady=24)
            return
        pairs = []
        for r in globals_rules:
            row = tk.Frame(dlg, bg=C["card2"], padx=10, pady=6)
            row.pack(fill="x", padx=14, pady=4)
            desc = {"gr": "默认分类（兜底 GR）"}.get(r.get("domain"), r.get("domain"))
            tk.Label(row, text=f"{r['id']}\n{desc}", bg=C["card2"], fg=C["text2"],
                     font=FONT_SM, justify="left", anchor="w").pack(side="left")
            if r.get("domain") == "gr":
                v = tk.StringVar(value=str((r.get("then") or {}).get("gr", "")))
                cb = ttk.Combobox(row, textvariable=v, values=list(GR_SUGGESTIONS),
                                  font=FONT)
                cb.pack(side="right", ipady=2, padx=6)
                pairs.append((r, v))
            else:
                tk.Label(row, text="（此域请用 editor.py 编辑）", bg=C["card2"],
                         fg=C["dim"], font=FONT_SM).pack(side="right")

        def save():
            cand = copy.deepcopy(self.rules)      # 副本操作，失败零污染
            changed = 0
            for r, v in pairs:
                cur = next((x for x in cand if x.get("id") == r.get("id")), None)
                if cur is None:
                    continue
                val = v.get().strip()
                if not val:
                    self._set_status("默认分类不能为空", "err")
                    return
                if (cur.get("then") or {}).get("gr") != val:
                    cur.setdefault("then", {})["gr"] = val
                    changed += 1
            if changed == 0:
                dlg.destroy()
                self._set_status("核心规则没有变化", "warn")
                return
            vr = validate_ruleset(cand, self.cids)
            rep = dry_run(cand, self.entries)
            if vr["errors"] or rep["wrong"] or rep["missing"]:
                self._set_status("保存未通过校验：" +
                                 (vr["errors"] or rep["wrong"] or rep["missing"])[0], "err")
                return
            try:
                ver = lifecycle.snapshot(self.rules_dir, cand, self.manifest)
                self.manifest["version"] = ver
                self.rules = cand
                dlg.destroy()
                self._set_status(f"核心规则已保存：{changed} 条已更新（v{ver}）", "ok")
            except Exception as e:
                self._set_status("保存失败：" + str(e), "err")

        tk.Button(dlg, text="💾 保存", command=save, bg=C["accent"], fg="#ffffff",
                  activebackground="#2f81f7", activeforeground="#ffffff",
                  relief="flat", font=FONT, padx=14, pady=4
                  ).pack(anchor="e", padx=14, pady=10)

    # ---------------- 导入 / 导出 ----------------
    def import_table(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("导入表格")
        dlg.geometry("760x460")
        dlg.configure(bg=C["card"])
        tk.Label(dlg, text="把表格粘贴到下面（从 Excel/WPS 复制后直接 Ctrl+V）：\n"
                           "列从左到右：零件名、规格、材料、GR名、加工说明、紧固件、型号（可不要）\n"
                           "规格可留空 = 兜底（该零件所有规格都按这行处理）；\n"
                           "型号 = BOM 上印的规格（和测量尺寸不一样才填，如量出 100*80*50 印 BZ500.80/50）；\n"
                           "紧固件写法：CB16-100×4@标准件;CBW16×4@仓库备件\n"
                           "（分号隔开多条，× 隔开数量，@ 后是该紧固件的 GR 名——可省略）",
                 bg=C["card"], fg=C["text2"], font=FONT_SM, justify="left",
                 anchor="w").pack(anchor="w", padx=14, pady=(12, 6))
        txt = tk.Text(dlg, bg=C["card2"], fg=C["text"], insertbackground=C["text"],
                      relief="flat", highlightthickness=1,
                      highlightbackground=C["border"], font=("Consolas", 10))
        txt.pack(fill="both", expand=True, padx=14)
        err_box = tk.Label(dlg, text="", bg=C["card"], fg=C["err"], font=FONT_SM,
                           justify="left", anchor="w")
        err_box.pack(fill="x", padx=14)

        def do_import():
            rows, errors = parse_table_text(txt.get("1.0", "end"))
            if errors:
                err_box.configure(text="\n".join("· " + e for e in errors[:10]))
                return
            parts = merge_rows(rows)
            errs = validate_data(parts)
            if errs:
                err_box.configure(text="\n".join("· " + e for e in errs[:10]))
                return
            # 合并进现有数据（同零件同规格更新）
            existing = {(p.get("partName", ""), s.get("spec", ""))
                        for p in self.data.get("parts", []) for s in p.get("specs", [])}
            new_parts = []
            for part in parts:
                merged = None
                for ep in self.data.get("parts", []):
                    if ep.get("partName") == part["partName"]:
                        merged = ep
                        break
                if merged is None:
                    merged = {"partName": part["partName"], "specs": []}
                    new_parts.append(merged)
                for s in part["specs"]:
                    for es in merged["specs"]:
                        if es["spec"] == s["spec"]:
                            for f in ("gr", "material", "remark"):
                                if s.get(f):
                                    es[f] = s[f]
                            for f in s["fasteners"]:
                                for ef in es["fasteners"]:
                                    if (ef["name"] == f["name"] and ef["spec"] == f["spec"]
                                            and ef.get("gr") == f.get("gr")):
                                        ef["qty"] = f["qty"]
                                        break
                                else:
                                    es["fasteners"].append(f)
                            break
                    else:
                        merged["specs"].append(s)
            self.data.setdefault("parts", []).extend(new_parts)
            self._mark_dirty(f"导入表格：{len(rows)} 行")
            dlg.destroy()
            self.refresh_tree()
            self._set_status(f"已导入 {len(rows)} 行（按 零件名→规格 自动整理层级）", "ok")

        tk.Button(dlg, text="导入", command=do_import, bg=C["accent"], fg="#ffffff",
                  activebackground="#2f81f7", activeforeground="#ffffff",
                  relief="flat", font=FONT, padx=16, pady=4).pack(anchor="e", padx=14, pady=10)

    def copy_table(self):
        text = export_table(self.data.get("parts", []))
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._set_status("表格已复制到剪贴板——直接粘贴到 Excel/WPS 即可", "ok")

    # ---------------- 应用到规则系统 ----------------
    def apply_to_rules(self):
        errs = validate_data(self.data.get("parts", []))
        if errs:
            self._set_status("表格还有问题：" + errs[0], "err")
            self._show_problems(errs)
            return
        cand = copy.deepcopy(self.rules)
        plans = []
        problems = []
        for part in self.data.get("parts", []):
            for s in part.get("specs", []):
                r = entry.plan_entry(cand, name=part.get("partName", ""),
                                     spec=s.get("spec"), gr=s.get("gr"),
                                     material=s.get("material"),
                                     remark=s.get("remark"),
                                     model=s.get("model"),
                                     fasteners=s.get("fasteners"),
                                     prio=s.get("priority", PRIORITY_DEFAULT),
                                     author=self._load_author())
                if "error" in r:
                    problems.append(f"「{part.get('partName')}」{s.get('spec')}：{r['error']}")
                    continue
                for p in r["plan"]:
                    if p["action"] == "new":
                        cand.append(p["rule"])
                    plans.append(p)
        if problems:
            self._show_problems(problems)
            self._set_status("应用失败：" + problems[0], "err")
            return
        vr = validate_ruleset(cand, self.cids)
        rep = dry_run(cand, self.entries)
        if vr["errors"] or rep["wrong"] or rep["missing"]:
            probs = [("· " + e) for e in vr["errors"][:6]] \
                + [("· " + x) for x in (rep["wrong"] + rep["missing"])[:6]]
            self._show_problems(probs)
            self._set_status("规则校验未通过（可能和已确认答案冲突），未保存", "err")
            return
        ver = lifecycle.snapshot(self.rules_dir, cand, self.manifest)
        self.manifest["version"] = ver
        self.rules = cand
        n_new = sum(1 for p in plans if p["action"] == "new")
        n_upd = sum(1 for p in plans if p["action"] == "update")
        self._set_status(f"已应用到规则系统：新增 {n_new} 条、更新 {n_upd} 条，"
                         f"版本 v{ver}（快照已存）", "ok")

    def load_rules_into_table(self):
        """读取规则库 → 反解为表格数据（双向闭环：读规则 → 编辑 → 应用写回）。

        5 域零件规则（gr/spec/material/remark/companion）反解为零件条目；
        global 核心规则与其他域规则保持由核心窗口/editor.py 管理。
        """
        if self.dirty and not messagebox.askyesno(
                "读取现有规则",
                "当前表格有未保存的修改，读取规则库将**替换**表格内容。\n\n"
                "继续？（建议先点「保存数据」备份）", parent=self.root):
            return
        parts = rules_to_parts(self.rules)
        if not parts:
            self._set_status("规则库中暂无零件规则（5 域），无法读取", "warn")
            return
        self.data = {"_格式": DATA_FORMAT, "parts": parts}
        self.dirty = False
        self.sel = None
        self.refresh_tree()
        self.render_detail()
        n_spec = sum(len(p["specs"]) for p in parts)
        n_comp = sum(len(s["fasteners"]) for p in parts for s in p["specs"])
        self._set_status(
            f"已读取规则库：{len(parts)} 个零件 / {n_spec} 行 / {n_comp} 个紧固件"
            "（编辑后点「应用到规则系统」写回）", "ok")

    def _load_author(self):
        try:
            with open(os.path.join(BASE_DIR, ".editor_config.json"), encoding="utf-8") as f:
                return json.load(f).get("author", "")
        except Exception:
            return ""

    # ---------------- 杂项 ----------------
    def _mark_dirty(self, msg):
        self.dirty = True
        self._set_status(msg + "（记得点「保存数据」）", "warn")

    def _set_status(self, text, kind="info"):
        color = {"ok": C["ok"], "err": C["err"], "warn": C["warn"]}.get(kind, C["text2"])
        self.status.configure(text=text, fg=color)

    def _show_problems(self, problems):
        dlg = tk.Toplevel(self.root)
        dlg.title("需要修正的地方")
        dlg.geometry("640x360")
        dlg.configure(bg=C["card"])
        tk.Label(dlg, text="下面这些地方要改一下：", bg=C["card"], fg=C["text2"],
                 font=FONT_SM).pack(anchor="w", padx=14, pady=(12, 4))
        box = tk.Text(dlg, bg=C["card2"], fg=C["text"], relief="flat",
                      highlightthickness=1, highlightbackground=C["border"],
                      font=FONT_SM)
        box.insert("1.0", "\n".join(problems[:20]))
        box.configure(state="disabled")
        box.pack(fill="both", expand=True, padx=14, pady=6)
        tk.Button(dlg, text="知道了", command=dlg.destroy, bg=C["card2"], fg=C["text"],
                  activebackground="#2d333b", activeforeground=C["text"],
                  relief="flat", font=FONT_SM, padx=12, pady=3).pack(anchor="e", padx=14, pady=8)


def main():
    root = tk.Tk()
    try:
        TableEditor(root)
    except Exception as e:
        import traceback
        traceback.print_exc()
        tk.Label(root, text="启动失败：" + str(e), fg=C["err"], bg=C["bg"]).pack(padx=20, pady=20)
    root.mainloop()


if __name__ == "__main__":
    main()
