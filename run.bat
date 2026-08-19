@echo off
rem 使用预置 Python 环境启动 MoldBOM（开发模式）
set "PY=C:\Users\littledark\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0bom_export\bom_export.py" %*
