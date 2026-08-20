# MoldBOM — 模具 BOM 导出工具

CATIA CATPart → 模具明细表 BOM 自动导出工具。读取 CATPart 中各 Body，  
按 V2 规则引擎推理 GR / 材质 / 热处理 / 加工备注，几何测量规格，自动补全  
配套紧固件并分配零件号，输出对齐模板的 Excel；支持按 GR 拆分 CATPart 与打包。

支持 **GitHub 自动更新**：程序（exe）全量更新 + V2 规则热更新（免重启）。

## 目录结构

```
moldbom/
├── bom_export/              # 主工具（模块化拆分，见下）
│   ├── bom_export.py        # 公共门面：再导出各模块 API + __version__ + CLI 入口
│   ├── bom_common.py        # 基础设施：版本/路径/日志/常量/COM 重试
│   ├── bom_utils.py         # 通用工具：模号提取/文件名安全化/GR 分组
│   ├── bom_infer.py         # GR 推理（零件级 + 规格级精化）
│   ├── bom_stp.py           # STP 导出与实体计数
│   ├── bom_measure.py       # 规格测量（geometry_engine 集成）
│   ├── bom_parser.py        # CATPart 解析主流程
│   ├── bom_numbering.py     # 零件号分配
│   ├── bom_companions.py    # 配套紧固件补全
│   ├── bom_writer.py        # Excel/CSV 输出
│   ├── bom_split.py         # 拆分导出（按 GR 组织 + 打包 zip）
│   ├── bom_pipeline.py      # 插件化 Pipeline（6 阶段）+ process_one_part
│   ├── bom_batch.py         # 批量处理
│   ├── bom_catia.py         # CATIA 会话管理 + 缓存清理
│   ├── bom_cli.py           # 命令行入口 main()
│   ├── bom_gui.py           # 深色 GUI（Tkinter）
│   ├── v2_bridge.py         # V2 规则引擎桥接（唯一规则源入口）
│   ├── geometry_engine.py   # 规格测量引擎（PCA / DE / NM 形状分析）
│   ├── stp_features.py      # STP 面级 B-rep 特征提取
│   ├── updater.py           # 自动更新引擎（exe 更新 + 规则热更新）
│   ├── publish_release.py   # GitHub 发布脚本（开发侧，不打包进 exe）
│   ├── BomExport.spec       # PyInstaller 配置（仓库相对路径，任何机器可构建）
│   ├── test_bom_logic.py    # 单元测试（内存引擎注入，无需 CATIA）
│   ├── test_stp_features.py # STP 特征测试
│   └── verify_user_confirmed.py  # 点云基准验证（需专有数据，见下）
├── V2/                      # V2 规则系统（唯一规则源）
│   ├── rulespec/            # 规则引擎包（10 域模型 / 门禁 / 快照）
│   ├── rules/               # 规则数据（*.rules.json + manifest.json + snapshots/）
│   ├── editor.py            # 规则编辑器（python editor.py）
│   ├── table_editor.py      # 零件信息表录入
│   ├── wizard.py            # 新手向导
│   ├── tests/               # 规则引擎测试
│   └── docs/                # 规则系统设计规范（权威版）
├── docs/                    # 项目级文档
│   └── 遗留问题清单.md
├── README.md
├── requirements.txt         # 运行时依赖
└── requirements-dev.txt     # 开发/打包依赖
```

## 环境要求

- Windows 10/11
- CATIA V5（通过 COM 自动化驱动，本机需已安装且许可可用）
- Python 3.12+（含 tkinter）

## 快速开始（开发模式）

```bash
# 1. 安装依赖（或用下方“本机预置环境”，可跳过）
pip install -r requirements.txt

# 2. 运行 GUI
python bom_export/bom_export.py

# 3. 或 CLI 模式
python bom_export/bom_export.py <CATPart路径> [输出Excel路径]
python bom_export/bom_export.py --batch <文件夹路径>
```

## 本机预置环境（推荐）

本机已配置好独立 Python 环境（Python 3.13.14，含 numpy / scipy / openpyxl /
pywin32 / pytest / pyinstaller），路径：

```
C:\Users\littledark\.workbuddy\binaries\python\envs\default\Scripts\python.exe
```

- 一键跑全部测试：
  ```bash
  powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1
  ```
- 开发模式启动 GUI / CLI：双击仓库根目录 `run.bat`，或
  ```bash
  run.bat <CATPart路径> [输出Excel路径]
  run.bat --batch <文件夹路径>
  ```

## 运行测试

```bash
cd bom_export
python test_bom_logic.py        # 26 用例（内存引擎注入，无需 CATIA）
python test_stp_features.py     # 20 用例

cd ../V2
python tests/test_engine.py     # 规则引擎 26 用例
python tests/test_namespec_infer.py  # nameSpec 7 用例
python tests/test_keyword_op.py      # keyword 3 用例

# 点云基准验证（50 例）需要专有基准数据 TEST/（不入库），clone 后无此目录属正常
# python bom_export/verify_user_confirmed.py
```

## 打包 exe

```bash
cd bom_export
pyinstaller --clean --noconfirm BomExport.spec
# 产物: dist/BomExport.exe（onefile / windowed，双击进入 GUI）
```

spec 使用 `SPECPATH` 相对定位 V2/，clone 到任何路径均可直接构建。

### 自动更新（公开仓库 + Gitee 镜像，无需 token）

仓库已公开，`BomExport.exe` **不再内置任何 token**；更新检查与下载
对任何用户开箱即用。

`scripts/build_exe.ps1` 直接打包：

```powershell
.\scripts\build_exe.ps1
```

构建后 `dist/` 只含 `BomExport.exe`，**不生成/复制 `update_config.json`**：
repo（GitHub 公开仓库）、Gitee 镜像、自动检查间隔等全部内置在 exe 内；
唯一可能生成的是 `%APPDATA%\MoldBOM\update_state.json`（只存
last_check / auto_check 运行时状态，不含 repo/token，也不随 exe 分发）。

更新源顺序（逐源自动回退）：
Gitee 镜像 `https://gitee.com/LittleDarkZero/moldbom/raw/master/update.json`
→ GitHub Contents API → GitHub raw。

## 版本号规范

| 对象 | 位置 | 说明 |
| --- | --- | --- |
| 程序 | `bom_export/bom_common.py` 顶部 `__version__` | 唯一真源，改动代码后 bump |
| 规则 | `V2/rules/manifest.json` 的 `version` | 快照机制自动 PATCH+1 |

## 发布更新（维护者）

自动更新依赖 GitHub Releases 分发，仓库根目录维护 `update.json` 清单  
（由发布脚本自动创建/更新，勿手动编辑）。当前清单仅含 exe 条目，
规则已随 exe 内置，不再单独发布规则更新。

```bash
# 环境变量（发布前设置）
set GITHUB_TOKEN=ghp_xxxxxxxx          # PAT（需 Contents 读写权限）
rem GITHUB_REPO 可省略：脚本会自动从 git remote origin 推导为当前仓库
rem set GITHUB_REPO=LittleDarkZero/moldbom

cd bom_export

# 发布程序更新（自动读取 __version__，默认取 dist/BomExport.exe）
python publish_release.py exe --notes "修复规格测量问题"

# 发布规则更新（可选：规则已内置 exe，默认不再单独发布）
python publish_release.py rules --notes "新增 gr 域 3 条规则"
```

脚本自动完成：算 sha256 → 创建 Release（tag: `exe-v9.3.0`）→  
上传 asset → 更新仓库根 `update.json`。

## 自动更新机制（最终用户）

1. 无需任何配置文件：把 `BomExport.exe` 单独分发即可（repo、token、
   自动检查等全部内置在 exe 内；`%APPDATA%\MoldBOM\update_state.json`
   仅保存 last_check / auto_check 运行时状态，不含 repo/镜像/token）
2. GUI 右上角「检查更新」，或启动时自动检查（可在左栏关闭）
3. 检查结果必有明确反馈：
   - **已是最新版本**：状态栏 + 弹窗提示
   - **检查失败**：弹窗显示原因（网络 / 认证 / 清单格式等），状态栏与日志同步记录
   - **有新版本**：弹窗显示版本号与更新说明，可勾选下载更新
   （启动时自动检查只写状态栏与日志，不弹窗打扰）
4. 网络加速：检查更新按「国内镜像（如 Gitee，可选内置）→ api.github.com → raw.githubusercontent.com」
   顺序尝试、自动回退；exe 下载走 api.github.com asset 地址（私有仓库浏览器直链会 404）
5. （可选）若所在网络连 GitHub 仍慢/失败：以管理员运行 `scripts\setup_github_hosts.bat` 一键写入 hosts 加速（可 `undo` 撤销；仅对 DNS 污染有效，GitHub IP 变化时需更新脚本内 IP）
6. 规则更新即时生效（免重启）；程序更新下载完成后自动重启替换

安全设计：sha256 + size 双校验、断点续传、替换失败自动回滚、  
规则损坏自动回退 exe 内置副本——任何失败都不会导致工具不可用。

## V2 规则维护

```bash
cd V2
python editor.py                    # 规则编辑器（分组卡片 / 拖拽排序 / Ctrl+Z）
python table_editor.py              # 零件信息表录入
python -m rulespec validate         # 规则校验
python -m rulespec snapshot         # 快照（自动 PATCH+1）
python -m rulespec restore <ver>    # 回滚
```

规则规范见 `V2/docs/规则系统设计规范.md`（权威版）。

**注意**：所有业务规则必须外置在 `V2/rules/*.json`，禁止在  
bom_export 代码中硬编码（算法阈值与结构常量除外）。

## 专有数据说明（不入库）

以下目录含商业数据，已在 `.gitignore` 排除，clone 后不存在属正常：

- `bom_export/TEST/` — 真实 CATIA 测试集与规格测量基准（50 例点云）
- `bom_export/参考bom表/` — 历史 BOM（V2 规则录入参考）
- `V2/rules_backup_*/` — 旧格式规则备份（回滚请用 snapshots 机制）
- `%APPDATA%\MoldBOM\update_state.json` — 运行时状态（last_check / auto_check），位于用户目录，不含 repo/镜像/token，不入库
