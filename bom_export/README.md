# BOM 自动导出工具 v9.2（插件化 Pipeline + V2 规则引擎 + 新规格测量引擎）

## 快速开始

**GUI 模式**（推荐）：双击 `BomExport.exe` → 选择文件夹或文件 → 点击"开始导出"

**命令行模式**：
```bash
python bom_export.py <CATPart路径> [输出Excel路径]
python bom_export.py <CATPart路径> --split [输出目录]
python bom_export.py --batch <文件夹路径>
```

**依赖**: pywin32 + openpyxl + numpy + scipy + V2 规则系统（../V2，含 rulespec 引擎与 rules/）

## 功能

| 模块 | 说明 |
|------|------|
| Body 遍历 | 跳过 PartBody、布尔运算隐藏 Body；工艺辅具/接头等由 V2 filter 域规则过滤（关键词在规则编辑器维护） |
| STP 导出 + 计数 | 每个 Body 单独导出 STP，MANIFOLD_SOLID_BREP 计数，内建 3 次重试 |
| 规则推理 | **V2 RuleSpec 引擎（唯一规则源）**：filter → normalize（同义词）→ gr → spec → material → remark → companion → purchase → number 十域流水线 |
| 材质/热处理 | V2 material 域，HRC 同体系去重 |
| 规格测量 | **geometry_engine（唯一权威引擎）**：PCA + DE + NM 形状分析：长方体 L*W*H，圆柱/圆盘/圆环 Φ直径×长度；逐实体测量计数合并；稀疏点云（<200 点）自动走 PCA+圆形度启发式链路；带孔方板由 stp_features 面级交叉验证纠正；尺寸保留 1 位小数、整数去 .0（50 点云用户确认基准 50/50） |
| 配套件 | V2 companion 域自动追加螺钉、垫圈等；companionGrPolicy 决定 GR 跟随策略 |
| 零件号 | V2 number 域分段：模架 1-99、自制·镶配 100-199、其他 200+ |
| BOM 按 GR 拆分 | **自动**：写总 BOM 的同时按 `零件GR号` 拆成 `{模号}-{GR}.xlsx` 多份（主件与紧固件都按各自 GR 归组） |
| 文件拆分 | `--split`：CATPart 按 GR 组织到 `{模号}-parts/{GR}/{零件号}-{零件名称}/`，每个 GR 单独打包 `{模号}-{GR}.zip`，并生成「发给蔡师傅」文件夹汇总完整+细分明细表（CATPart 用 ASCII 命名，CATIA SaveAs 兼容） |
| GUI | tkinter 深色科技风界面，进度动画 + 日志 |
| 打包 | PyInstaller 单文件 exe（含 V2 规则），无需 Python |

## 技术特性

- **规格测量**：STP 拓扑 BFS 取实体顶点 → 形状分类（投影特征 + 圆形度校验）→ OBB（PCA + 微分进化 DE + Nelder-Mead）或最小体积圆柱（33 方向候选 + NM 精炼）→ 圆柱件输出 Φ 规格
- **稳定性**：COM 导出 3 次重试，Copy/Paste 后刷新几何，AABB 正则兜底
- **性能**：RefreshDisplay=OFF + 多进程并行规格提取
- **规则外置**：全部业务规则在 V2/rules/（10 域），CLI `python -m rulespec validate|infer|snapshot` 管理，快照可回滚；规则优先级在编辑器手动调整（默认 500，越大越优先）

## 文件结构

```
bom_export/
├── bom_export.py         # 核心引擎
├── geometry_engine.py    # 规格测量引擎（点云形状分析）
├── stp_features.py       # STP 面级 B-rep 特征提取（孔提取/螺钉匹配；退化点云兜底 + 带孔方板交叉验证）
├── v2_bridge.py          # V2 规则引擎桥接（唯一规则源）
├── bom_gui.py            # GUI 界面
├── dist/                 # 打包产物
│   └── BomExport.exe     # 单文件可执行
├── TEST/                 # 测试数据（50 点云基准 + 真实 CATIA 测试集）
└── 参考bom表/            # 历史 BOM 数据（V2 规则录入参考）
```

规则系统位于 `../V2/`（rulespec 引擎 + rules/ + 录入工具 editor/table_editor/wizard）。
