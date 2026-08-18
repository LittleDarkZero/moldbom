# -*- mode: python ; coding: utf-8 -*-

# 仓库相对路径定位（2026-08-18 重构）：SPECPATH = 本 spec 文件所在目录，
# 仓库根 = spec 目录的上一级（moldbom/），V2 规则系统在仓库根下。
# 任何机器 clone 后可直接 `pyinstaller BomExport.spec` 构建，无需改路径。
import os
REPO_ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))
V2_DIR = os.path.join(REPO_ROOT, 'V2')

a = Analysis(
    ['bom_export.py'],
    pathex=[],
    binaries=[],
    datas=[
        # V2 规则引擎（2026-08-10 接入、2026-08-13 成为唯一规则源）：
        # 源码包 + 规则数据（含快照可回滚）
        (os.path.join(V2_DIR, 'rulespec', '*.py'), 'V2/rulespec'),
        (os.path.join(V2_DIR, 'rules'), 'V2/rules'),
    ],
    hiddenimports=[
        'concurrent.futures', 'numpy', 'scipy', 'v2_bridge', 'stp_features', 'updater',
        # 2026-08-18 模块拆分后各功能模块（虽由门面静态导入，仍显式声明以稳健打包）
        'bom_common', 'bom_utils', 'bom_infer', 'bom_stp', 'bom_measure',
        'bom_parser', 'bom_numbering', 'bom_companions', 'bom_writer',
        'bom_split', 'bom_pipeline', 'bom_batch', 'bom_catia', 'bom_cli',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='BomExport',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
