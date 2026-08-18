# V2 — 新规则系统（RuleSpec 2.0）

> 独立于旧系统（`bom_export/`）的全新规则系统。**互不干扰**：本目录不 import 旧系统任何模块，规则文件只在本目录内读写，旧系统文件零改动。
> 按需求：**新系统不包含"同类合并"功能**——无 merge 域、无合并配置项、无合并代码。

## 目录结构

```
V2/
├── rulespec/                  # 规则引擎（纯 Python，零 GUI 依赖）
│   ├── schema.py              #   域/作用域/算子/属性词汇表/唯一归属表（9 域，无 merge）
│   ├── matcher.py             #   条件匹配器（8 算子 + 规格规范化 ×→*）
│   ├── model.py               #   规则模型：加载/原子保存/门禁 G1+G3 校验
│   ├── engine.py              #   推理引擎：流水线 + 裁决 + provenance + 冲突报错
│   ├── validator.py           #   门禁 G4（静态冲突）+ G5（语料干跑）
│   ├── corpus.py              #   基准语料加载
│   ├── lifecycle.py           #   快照 / 回滚 / 语义化版本
│   └── cli.py                 #   python -m rulespec validate|dryrun|new|snapshot|restore
├── rules/                     # 规则集（9 域文件 + manifest + snapshots/ + candidates/）
├── corpus/                    # 基准语料（13 条人工确认样本）
├── editor.py                  # 新规则编辑器（tkinter）
├── docs/规则系统设计规范.md    # 权威设计规范（v1.1，9 域修订版）
└── tests/test_engine.py       # 25 个引擎测试（pytest 兼容 + 内置 runner）
```

## 快速开始

```bash
python table_editor.py            # 表格录入：粘贴 Excel 表格 → 层级树 → 一键转规则
python wizard.py                  # 新手向导：三步录入（填写→确认→保存），零概念门槛
python editor.py                 # 完整编辑器（默认读写 V2/rules/）
python tests/test_engine.py      # 跑 25 个引擎测试
python -m rulespec validate      # 门禁全量校验 + 语料干跑
python -m rulespec dryrun        # 仅干跑报告
python -m rulespec new gr --category adjust-plate --scope spec --priority 700
python -m rulespec snapshot      # 手动快照（版本 PATCH+1）
python -m rulespec restore 1.0.1 # 回滚到快照
```

> **表格入口**：`table_editor.py` 是日常录入主界面——从 Excel 复制零件表格直接粘贴
> （零件名/规格/材料/GR名/加工说明/紧固件/型号），自动整理为 零件名→规格 层级树，
> 行内可展开维护多条紧固件（名称/规格/数量/GR），一键「应用到规则系统」生成/更新规则。
> 紧固件 GR 写法：`CB16-100×4@标准件;CBW16×4@仓库备件`（@ 后为 GR，留空走配套策略）。
> **型号列**：BOM 打印规格与测量尺寸不一致时填（如量出 `100*80*50` → 印 `BZ500.80/50`），留空印测量值。
> **名称读型号（引擎自动，2026-08-05 实装，未接入 bom_export）**：`RuleEngine.infer(name_spec=True)`
> 在未给测量规格时自动从零件名提取型号参与规格级规则匹配（`extract_model_from_name`，位于
> matcher.py），提取结果默认作为打印规格输出；单段名/纯中文分段不误判，显式测量规格永远优先。
> （原表格第 8 列「名称读型号」开关已于 2026-08-05 删除——引擎对所有零件自动尝试，无需开关。）
> **keyword 关键词算子（2026-08-05）**：零件名按分割符（空格/逗号/斜杠/竖线/顿号）分词，
> 规则值须等于某个**完整词**才命中——配合您的命名规则（关键词 + 分隔符 + 型号），
> 完全命中即结果可靠；『油缸』不会误命中『开模油缸』。wizard 高级面板可选。
> 命令行验证：`python -m rulespec infer "油缸 BOD-AG-63-50-V"`（`--spec` 传测量规格、`--no-name-spec` 关闭）。
> **手动编辑**：树上方「＋手动添加一行」（新零件+空规格，右侧直接填）、
> 「＋规格」「删除选中」；双击零件/规格=就地改名；右键=增/删/改名菜单；
> 右侧表单逐字段编辑第三级信息。
> **新手入口**：`wizard.py` 零基础版（单零件录入）。老手用 `editor.py` 完整编辑器。

## 核心机制速览

- **统一规则模型**：`id / domain / priority / scope / when / then / meta`，9 域共用一套 schema；
- **流水线**（无 merge）：`filter → normalize → gr → measure → material → remark → companion → purchase → number`；
- **裁决**：priority 降序 → specificity 降序（条件权重和）→ id 字典序；同强度异值 = 报错（零猜测）；
- **唯一归属**：每个输出属性只有一个域可写（`OWNERSHIP` 表）——平行表在结构上不可能存在；
- **门禁 G1-G6**：结构 / 命名 / 引用 / 静态冲突 / 语料干跑 / 快照；保存被阻断时规则停留在 draft，不污染线上；
- **provenance**：每个输出属性携带来源 `(rule id, version)`，可审计；
- **生命周期**：draft → active → deprecated（软删）→ retired（存档）；每次保存快照，可回滚。

## 编辑器（editor.py）设计要点（旧规则编辑器已于 2026-08-13 随老规则系统删除）

| 维度 | V2 新编辑器 |
|---|---|---|
| 字段组织 | **四张分组卡片**：基本信息 / 条件 when / 动作 then / 元信息，层级清晰不堆叠 |
| 默认值 | **自动预填**：id 按规范生成、作者/日期自动、优先级 500、作用域 part、首条件+首动作默认行 |
| 撤销/重做 | **Ctrl+Z / Ctrl+Y**，全状态快照（含拖拽、增删行、新建），上限 100 步 |
| 校验 | **实时校验**：每次编辑即时跑 G1-G3+静态冲突，错误面板内联显示 |
| 排序 | **拖拽调整顺序**（交换优先级，与引擎裁决语义一致） |
| 弹窗 | **零弹窗流**：新建内联表单、删除两步内联确认、干跑报告内联面板 |
| 冲突处理 | 静态冲突检测 + 运行期歧义报错（零猜测） |
| 数据语义 | 软删除状态机 + 快照回滚 + 学习只产出候选建议 |

## 独立性保证

- `rulespec/` 不 import `bom_export` 任何模块；无共享文件；
- 编辑器/CLI 只读写 `V2/rules/` 与 `V2/corpus/`；
- 删除/修改 V2 内容不影响旧系统运行，反之亦然。
