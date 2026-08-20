# -*- coding: utf-8 -*-
"""BomExport 自动更新引擎（2026-08-18）

双通道更新：
  - exe 全量更新：GitHub Releases 下载新版 exe → sha256 校验 → Windows rename
    技巧替换运行中的 exe → 重启
  - V2 规则热更新：GitHub Releases 下载 rules.zip → 校验 → 安装前验证 →
    原子换名 → reset_engine 重载（无需重启 exe）

设计约束：
  - 仅用标准库（urllib/hashlib/json/zipfile），不引入 requests 依赖
  - 任何失败都不 brick 工具：exe 替换失败回滚，规则损坏回退内置
  - 网络层 10s 超时 + 3 次指数退避重试
  - 断点续传：Range header（GitHub Releases S3 支持）
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import zipfile
from datetime import datetime, timezone

log = logging.getLogger("bom_export.updater")

# ============================================================
# 异常
# ============================================================


class UpdaterError(Exception):
    """更新错误，带中文 user_message 供 GUI 直接展示。"""

    def __init__(self, message, user_message=None):
        super().__init__(message)
        self.user_message = user_message or message


# ============================================================
# 配置
# ============================================================

# 默认仓库：全部配置内置进 exe，不依赖、也不生成 update_config.json
DEFAULT_REPO = "https://github.com/LittleDarkZero/moldbom"
GITEE_MIRROR = "https://gitee.com/LittleDarkZero/moldbom/raw/master"   # 国内镜像（Gitee 公开仓库，仅 update.json）

DEFAULT_CONFIG = {
    "repo": DEFAULT_REPO,    # 内置仓库（非机密；改仓库需改源码重新构建）
    "mirror": GITEE_MIRROR,     # 国内镜像 base（Gitee raw 根，公开 update.json）
    "auto_check": True,      # 启动时自动检查
    "check_interval_hours": 24,
    "last_check": "",        # 运行时状态（仅存 %APPDATA%，不写 exe 同目录）
    "exe_channel": "stable", # 预留：stable / beta
}


def _exe_dir():
    """可写目录：frozen = exe 所在目录；开发 = 脚本目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def state_path():
    """运行时状态文件：%APPDATA%/MoldBOM/update_state.json。

    只存 last_check / auto_check 这类运行时状态，绝不含 repo/token，
    也不放在 exe 同目录——用户只需分发一个 BomExport.exe。
    """
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "MoldBOM", "update_state.json")


def load_config():
    """配置全部内置（repo/镜像/间隔等（公开仓库无需 token））；仅合并 %APPDATA% 的运行时状态。

    不再读取/生成 update_config.json：exe 单独一份即可完成自动更新。
    """
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(_load_state())
    return cfg


def _load_state():
    """读取运行时状态（last_check / auto_check）；缺失或损坏 → {}。"""
    try:
        with open(state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {k: data[k] for k in ("last_check", "auto_check") if k in data}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_config(cfg):
    """持久化运行时状态（仅 last_check / auto_check）到 %APPDATA%。

    绝不写 repo/token，也绝不生成 exe 同目录的 update_config.json。
    """
    state = {k: cfg[k] for k in ("last_check", "auto_check") if k in cfg}
    try:
        p = state_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log.warning("保存更新状态失败: %s", e)



# ============================================================
# 版本管理
# ============================================================


def version_key(v):
    """版本字符串 → 可比较元组。

    "v9.3.0" → (9, 3, 0)
    "2.0.40" → (2, 0, 40)
    "9.3"    → (9, 3, 0)   # 不足补 0
    解析失败 → (0,)
    """
    if not v:
        return (0,)
    v = str(v).strip().lstrip("vV")
    parts = re.findall(r"\d+", v)
    if not parts:
        return (0,)
    nums = [int(p) for p in parts[:4]]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def current_exe_version():
    """当前 exe 版本（bom_export.__version__）。"""
    try:
        import bom_export
        return getattr(bom_export, "__version__", "0.0.0")
    except Exception:
        return "0.0.0"


def current_rules_version():
    """当前规则版本（manifest.json 的 version 字段）；读取失败返回 None。"""
    try:
        import v2_bridge
        v2_dir = v2_bridge._v2_dir()
        manifest_path = os.path.join(v2_dir, "rules", "manifest.json")
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("version")
    except Exception:
        return None


def is_newer(remote_ver, local_ver):
    """远程版本是否比本地新。"""
    return version_key(remote_ver) > version_key(local_ver)


# ============================================================
# 网络层（urllib，10s 超时，3 次退避重试）
# ============================================================

_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 60
_MAX_RETRIES = 3
_CHUNK = 64 * 1024  # 64KB


def _effective_token(cfg):
    """取生效 token：仅读用户显式配置；公开仓库默认无需 token。

    exe 不再内置 token（仓库已公开）；保留此函数以兼容未来需要
    认证的私有镜像/自建更新源场景。
    """
    return cfg.get("token", "") or ""


def _resolve_download_url(info, cfg):
    """下载地址解析：公开仓库用浏览器地址；显式配置了 token 时用 API asset 地址。

    公开仓库（无 token）继续用浏览器地址；有 token 且 manifest 提供
    api_url 时优先走 api.github.com 的资产端点。
    """
    if info and _effective_token(cfg) and info.get("api_url"):
        return info["api_url"]
    return (info or {}).get("url", "")


def _build_headers(cfg, extra=None):
    """构造请求头（仅当显式配置 token 时加 Authorization，公开仓库无需）。"""
    headers = {"User-Agent": "BomExport-Updater/1.0"}
    token = _effective_token(cfg)
    if token:
        headers["Authorization"] = "Bearer " + token
    if extra:
        headers.update(extra)
    return headers


def _http_get(url, cfg, headers=None, timeout=None):
    """GET 请求，返回 (response, content_length)。调用方负责 read。"""
    last_err = None
    for attempt in range(_MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers=_build_headers(cfg, headers))
            resp = urllib.request.urlopen(req, timeout=timeout or (_CONNECT_TIMEOUT + _READ_TIMEOUT))
            return resp
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise UpdaterError("远程文件不存在: %s" % url,
                                   user_message="更新文件不存在，请检查仓库配置")
            if e.code == 401 or e.code == 403:
                raise UpdaterError("认证失败 (%d)" % e.code,
                                   user_message="GitHub 认证失败，请检查 token 或仓库权限")
            last_err = e
        except (urllib.error.URLError, OSError) as e:
            last_err = e
        if attempt < _MAX_RETRIES - 1:
            wait = 2 ** attempt  # 1s, 2s, 4s
            log.debug("网络重试 %d/%d，等待 %ds: %s", attempt + 1, _MAX_RETRIES, wait, last_err)
            time.sleep(wait)
    raise UpdaterError("网络请求失败（重试 %d 次）: %s" % (_MAX_RETRIES, last_err),
                       user_message="网络连接失败，请检查网络后重试")


def _manifest_urls(cfg):
    """返回按顺序尝试的 update.json 候选地址（镜像 → api.github.com → raw）。

    镜像源（cfg["mirror"]，如 Gitee raw 根 https://gitee.com/x/y/raw/main）为
    国内加速首选，可为空；api.github.com 比 raw.githubusercontent.com 在国内更稳定，
    公开仓库直接读 contents API（无需 token）。
    """
    repo = cfg.get("repo", "")
    if not repo:
        raise UpdaterError("未配置 GitHub 仓库地址",
                           user_message="更新源未内置，请联系开发者重新打包程序")
    mirror = (cfg.get("mirror") or "").rstrip("/")
    urls = []
    if mirror:
        urls.append(mirror + "/update.json")
    m = re.match(r"https?://github\.com/([^/]+/[^/]+)", repo)
    if m:
        owner_repo = m.group(1)
        urls.append("https://api.github.com/repos/%s/contents/update.json?ref=main" % owner_repo)
        urls.append("https://raw.githubusercontent.com/%s/main/update.json?t=%d"
                    % (owner_repo, int(time.time())))
    else:
        urls.append(repo.rstrip("/") + "/raw/main/update.json?t=" + str(int(time.time())))
    return urls


def fetch_manifest(cfg):
    """获取远程 update.json（多源逐级回退），返回 dict。"""
    urls = _manifest_urls(cfg)
    last_err = None
    for url in urls:
        try:
            headers = None
            if url.startswith("https://api.github.com/"):
                headers = {"Accept": "application/vnd.github.raw+json"}
            resp = _http_get(url, cfg, headers=headers)
            try:
                data = json.loads(resp.read().decode("utf-8"))
            finally:
                resp.close()
            if not isinstance(data, dict):
                raise UpdaterError("update.json 格式异常")
            log.info("检查更新清单: %s", url.split("?")[0])
            return data
        except UpdaterError as e:
            last_err = e
            log.warning("更新清单源不可用 (%s): %s", url, e)
    raise last_err


def download_file(url, dest, cfg, sha256=None, expected_size=None,
                  progress_cb=None, resume=True):
    """下载文件到 dest，支持断点续传和进度回调。

    progress_cb(downloaded, total) — 在调用方线程执行（GUI 用 after 回主线程）。
    """
    # 磁盘空间预检
    if expected_size:
        try:
            usage = shutil.disk_usage(os.path.dirname(os.path.abspath(dest)))
            if usage.free < expected_size * 12 // 10:  # 需 1.2 倍余量
                raise UpdaterError(
                    "磁盘空间不足: 需要 %d 字节，可用 %d 字节" % (expected_size, usage.free),
                    user_message="磁盘空间不足，请清理后重试")
        except OSError:
            pass

    # 断点续传：检查已有 .part 文件大小
    existing = 0
    if resume and os.path.exists(dest):
        existing = os.path.getsize(dest)

    headers = {}
    if existing > 0:
        headers["Range"] = "bytes=%d-" % existing
    if url.startswith("https://api.github.com/"):
        headers["Accept"] = "application/octet-stream"

    resp = _http_get(url, cfg, headers=headers)
    total = int(resp.headers.get("Content-Length", 0))
    if existing > 0 and resp.status == 206:  # 206 Partial Content
        total += existing
        mode = "ab"
    elif existing > 0 and resp.status == 200:
        # 服务器不支持 Range，从头下
        existing = 0
        total = int(resp.headers.get("Content-Length", 0))
        mode = "wb"
    else:
        mode = "wb"

    if not total and expected_size:
        total = expected_size

    downloaded = existing
    try:
        with open(dest, mode) as f:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb:
                    try:
                        progress_cb(downloaded, total)
                    except Exception:
                        pass  # 进度回调失败不影响下载
        resp.close()
    except OSError as e:
        raise UpdaterError("下载写入失败: %s" % e,
                           user_message="文件写入失败，请检查磁盘空间和权限")

    # sha256 校验
    if sha256:
        actual = _sha256_file(dest)
        if actual.lower() != sha256.lower():
            try:
                os.remove(dest)
            except OSError:
                pass
            raise UpdaterError(
                "sha256 校验失败: 期望 %s，实际 %s" % (sha256[:16], actual[:16]),
                user_message="文件校验失败（可能下载损坏），已删除请重试")

    # size 校验
    if expected_size:
        actual_size = os.path.getsize(dest)
        if actual_size != expected_size:
            raise UpdaterError(
                "文件大小不符: 期望 %d，实际 %d" % (expected_size, actual_size),
                user_message="下载文件大小不符，请重试")

    return dest


def _sha256_file(path):
    """计算文件 sha256。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ============================================================
# 检查更新
# ============================================================


def should_auto_check(cfg):
    """是否需要自动检查（间隔到期）。"""
    if not cfg.get("auto_check"):
        return False
    if not cfg.get("repo"):
        return False
    last = cfg.get("last_check", "")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
        return hours >= cfg.get("check_interval_hours", 24)
    except (ValueError, TypeError):
        return True


def check_for_updates(cfg):
    """检查更新，返回 {"exe": info|None, "rules": info|None, "manifest": dict}。

    info = {"version", "url", "sha256", "size", "notes"}
    无更新或不可用对应键为 None。
    """
    manifest = fetch_manifest(cfg)
    result = {"exe": None, "rules": None, "manifest": manifest}

    exe_info = manifest.get("exe")
    if exe_info and exe_info.get("version"):
        local = current_exe_version()
        if is_newer(exe_info["version"], local):
            result["exe"] = exe_info

    rules_info = manifest.get("rules")
    if rules_info and rules_info.get("version"):
        local = current_rules_version()
        if local is None or is_newer(rules_info["version"], local):
            result["rules"] = rules_info

    # 更新 last_check
    cfg["last_check"] = datetime.now(timezone.utc).isoformat()
    save_config(cfg)

    return result


# ============================================================
# 规则热更新
# ============================================================

def _updates_dir():
    """临时下载目录（exe 旁 updates/）。"""
    d = os.path.join(_exe_dir(), "updates")
    os.makedirs(d, exist_ok=True)
    return d


def _external_v2_dir():
    """exe 旁外部 V2 目录路径。"""
    return os.path.join(_exe_dir(), "V2")


def _ensure_external_rulespec():
    """确保外部 V2 目录有 rulespec 包（从 _MEIPASS 复制）。"""
    ext_v2 = _external_v2_dir()
    ext_rulespec = os.path.join(ext_v2, "rulespec")
    if os.path.isdir(ext_rulespec):
        return
    # 从 _MEIPASS 或开发目录复制
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None) or _exe_dir()
        src = os.path.join(meipass, "V2", "rulespec")
    else:
        src = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "V2", "rulespec"))
    if os.path.isdir(src):
        shutil.copytree(src, ext_rulespec)
        log.info("已复制 rulespec 到外部 V2 目录")


def download_and_install_rules(rules_info, cfg, progress_cb=None):
    """下载并安装规则更新，返回新版本号。

    流程：下载 zip → 校验 → 解压暂存 → load_ruleset 预验证 → 原子换名 → reset_engine
    """
    url = _resolve_download_url(rules_info, cfg)
    sha256 = rules_info.get("sha256", "")
    expected_size = rules_info.get("size")
    new_version = rules_info["version"]

    # 1. 下载
    zip_path = os.path.join(_updates_dir(), "rules.zip")
    log.info("开始下载规则更新 v%s ...", new_version)
    download_file(url, zip_path, cfg, sha256=sha256,
                  expected_size=expected_size, progress_cb=progress_cb)

    # 2. 解压到暂存目录
    ext_v2 = _external_v2_dir()
    staging_dir = os.path.join(ext_v2, "rules.staging")
    if os.path.exists(staging_dir):
        shutil.rmtree(staging_dir)
    os.makedirs(staging_dir, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(staging_dir)
    except (zipfile.BadZipFile, OSError) as e:
        raise UpdaterError("规则包解压失败: %s" % e,
                           user_message="规则更新包损坏，请重试")
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass

    # 3. 安装前验证：load_ruleset 试加载
    try:
        _ensure_external_rulespec()
        # 临时把 ext_v2 加入 sys.path 以 import rulespec
        if ext_v2 not in sys.path:
            sys.path.insert(0, ext_v2)
        from rulespec.model import load_ruleset
        manifest, rules = load_ruleset(staging_dir)
        active_count = sum(1 for r in rules if r.get("meta", {}).get("status") == "active")
        log.info("规则预验证通过: %d 条 active（v%s）", active_count,
                 manifest.get("version", "?"))
    except Exception as e:
        # 清理暂存
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise UpdaterError("规则预验证失败: %s" % e,
                           user_message="新规则格式有误，已取消安装")

    # 4. 原子换名
    ext_rules = os.path.join(ext_v2, "rules")
    old_rules = os.path.join(ext_v2, "rules.old")

    # 清理上次残留
    if os.path.exists(old_rules):
        shutil.rmtree(old_rules, ignore_errors=True)

    # 如果已有外部 rules，先改名
    if os.path.exists(ext_rules):
        os.rename(ext_rules, old_rules)

    try:
        os.rename(staging_dir, ext_rules)
    except OSError as e:
        # 换名失败回滚
        if os.path.exists(old_rules):
            os.rename(old_rules, ext_rules)
        raise UpdaterError("规则安装失败（换名）: %s" % e,
                           user_message="规则安装失败，已回退")

    # 清理 old
    if os.path.exists(old_rules):
        shutil.rmtree(old_rules, ignore_errors=True)

    # 5. reset_engine + 复验
    try:
        import v2_bridge
        v2_bridge.reset_engine()
        engine = v2_bridge.get_engine()
        if engine is None:
            # 重载失败 → 删除外部 V2，回退内置
            log.warning("外部规则重载失败，回退内置规则")
            shutil.rmtree(ext_v2, ignore_errors=True)
            v2_bridge.reset_engine()
            v2_bridge.get_engine()  # 重新加载内置
            raise UpdaterError("新规则加载失败，已回退内置规则",
                               user_message="新规则加载失败，已自动回退内置规则")
        actual_ver = engine.get("version", "?")
        log.info("规则更新完成: v%s（引擎报告 v%s）", new_version, actual_ver)
    except UpdaterError:
        raise
    except Exception as e:
        log.warning("规则重载异常: %s", e)
        # 不 raise——规则已安装到磁盘，下次启动会正常加载

    return new_version


# ============================================================
# exe 更新
# ============================================================

def download_exe_update(exe_info, cfg, progress_cb=None):
    """下载 exe 更新到 updates/BomExport.exe.new，返回路径。"""
    url = _resolve_download_url(exe_info, cfg)
    sha256 = exe_info.get("sha256", "")
    expected_size = exe_info.get("size")

    part_path = os.path.join(_updates_dir(), "BomExport.exe.part")
    new_path = os.path.join(_updates_dir(), "BomExport.exe.new")

    # 清理上次残留 .new
    if os.path.exists(new_path):
        os.remove(new_path)

    log.info("开始下载 exe 更新 v%s ...", exe_info.get("version", "?"))
    download_file(url, part_path, cfg, sha256=sha256,
                  expected_size=expected_size, progress_cb=progress_cb)

    # 校验通过，改名到 .new
    os.rename(part_path, new_path)
    log.info("exe 下载完成，校验通过: %s", new_path)
    return new_path


def apply_exe_update_and_restart(new_exe_path, restart=True):
    """用 rename 技巧替换运行中的 exe 并重启。

    Windows 允许重命名运行中的 exe，但不能写入/删除它。
    """
    if not os.path.exists(new_exe_path):
        raise UpdaterError("新 exe 文件不存在: %s" % new_exe_path,
                           user_message="更新文件丢失，请重新下载")

    exe_path = sys.executable  # frozen 模式下即 BomExport.exe
    backup = exe_path + ".old"

    # 1. 清理上次残留 .old
    try:
        if os.path.exists(backup):
            os.remove(backup)
    except OSError as e:
        log.warning("清理旧 .old 失败（可能被占用）: %s", e)

    # 2. 运行中的 exe → .old
    try:
        os.rename(exe_path, backup)
    except OSError as e:
        raise UpdaterError("无法重命名当前 exe: %s" % e,
                           user_message="无法替换程序文件，请关闭杀毒软件实时防护后重试")

    # 3. 新 exe 就位
    try:
        shutil.move(new_exe_path, exe_path)
    except OSError:
        # 就位失败 → 立即回滚
        try:
            os.rename(backup, exe_path)
        except OSError:
            pass  # 最坏情况：exe 没了但 .old 在，用户手动改名
        raise UpdaterError("新 exe 就位失败，已回滚",
                           user_message="程序替换失败，已恢复原版本")

    log.info("exe 替换成功，准备重启...")

    if restart:
        # 启动新版本
        subprocess.Popen([exe_path])
        # os._exit 绕过 finally（避免 CATIA COM 清理拖时间）
        os._exit(0)


def cleanup_artifacts():
    """清理更新残留文件（启动时调用）。"""
    removed = 0
    exe_dir = _exe_dir()

    # .old 文件
    old_exe = os.path.join(exe_dir, "BomExport.exe.old")
    if os.path.exists(old_exe):
        try:
            os.remove(old_exe)
            removed += 1
            log.info("已清理旧版本文件: BomExport.exe.old")
        except OSError:
            pass  # 可能被占用，下次再清

    # updates 目录残留
    updates_dir = os.path.join(exe_dir, "updates")
    if os.path.isdir(updates_dir):
        for name in ("BomExport.exe.part", "BomExport.exe.new", "rules.zip"):
            p = os.path.join(updates_dir, name)
            if os.path.exists(p):
                try:
                    os.remove(p)
                    removed += 1
                except OSError:
                    pass

    # rules.old 残留
    old_rules = os.path.join(exe_dir, "V2", "rules.old")
    if os.path.isdir(old_rules):
        try:
            shutil.rmtree(old_rules)
            removed += 1
            log.info("已清理旧规则目录: V2/rules.old")
        except OSError:
            pass

    return removed


# ============================================================
# 便捷：一次性检查+格式化文本
# ============================================================


def format_update_info(info):
    """将 check_for_updates 结果格式化为人类可读文本（供 GUI 日志）。"""
    lines = []
    exe = info.get("exe")
    rules = info.get("rules")

    if not exe and not rules:
        return "当前已是最新版本"

    if exe:
        lines.append("程序新版本: v%s（当前 v%s）" % (
            exe["version"], current_exe_version()))
        if exe.get("notes"):
            lines.append("  更新内容: %s" % exe["notes"])

    if rules:
        local_rules = current_rules_version() or "未知"
        lines.append("规则新版本: v%s（当前 v%s）" % (
            rules["version"], local_rules))
        if rules.get("notes"):
            lines.append("  更新内容: %s" % rules["notes"])

    return "\n".join(lines)
