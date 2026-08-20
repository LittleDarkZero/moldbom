# -*- coding: utf-8 -*-
"""构建期内嵌的自动更新 token（默认空）。

**不要把真实 token 提交到仓库！** 构建脚本 scripts/build_exe.ps1 会在打包前
把 MOLDBOM_TOKEN 环境变量（或 -Token 参数）写入本文件，PyInstaller 打包完成后
自动恢复为空。exe 运行时 updater 会优先用本文件的 token（若用户侧
update_config.json 未配置 token）。
"""

EMBEDDED_TOKEN = ""
