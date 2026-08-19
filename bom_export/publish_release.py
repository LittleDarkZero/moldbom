#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""BomExport 发布脚本（开发侧，不打包进 exe）

通过 GitHub REST API 自动发布更新：
  1. 计算文件 sha256 + size
  2. 创建 GitHub Release（tag + title）
  3. 上传 asset 到 Release
  4. 更新仓库根目录 update.json

用法：
  # 发布 exe
  python publish_release.py exe dist/BomExport.exe [--notes "更新说明"]

  # 发布规则
  python publish_release.py rules V2/rules [--notes "更新说明"]

  # 仅更新 update.json（手动上传文件后用）
  python publish_release.py manifest --exe-version 9.3.0 --exe-url <URL> --rules-version 2.0.41 --rules-url <URL>

环境变量：
  GITHUB_TOKEN  — GitHub PAT（需 repo 权限或 fine-grained Contents:Read&Write）
  GITHUB_REPO   — 仓库全名 owner/repo（如 LittleDarkZero/moldbom（缺省时自动从 git remote origin 推导））

注意：此脚本不在 BomExport.spec 的 datas/hiddenimports 中，不会打包进 exe。
"""

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

API_BASE = "https://api.github.com"
CHUNK = 64 * 1024


class ReleaseError(Exception):
    """发布流程错误（main 统一捕获并退出，避免库函数直接 sys.exit）。"""


def _default_repo():
    """从 git remote origin 推导 owner/repo，自动指向当前仓库。

    未设置 GITHUB_REPO 环境变量时使用，避免发布到错误仓库。
    """
    try:
        import subprocess
        out = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            stderr=subprocess.DEVNULL, text=True).strip()
        m = re.match(r"(?:https?://[^/]+/|git@[^:]+:)([^/]+/[^/]+?)(?:\.git)?$", out)
        if m:
            return m.group(1)
    except Exception:  # noqa: BLE001 推导失败返回空，由 main 提示设置
        pass
    return ""


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _api_request(method, url, token, data=None, content_type="application/json",
                 max_retries=3):
    """GitHub API 请求。"""
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if data is not None and content_type:
        headers["Content-Type"] = content_type

    body = None
    if data is not None:
        if isinstance(data, (dict, list)):
            body = json.dumps(data).encode("utf-8")
        elif isinstance(data, bytes):
            body = data
        else:
            body = str(data).encode("utf-8")

    last_err = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            resp = urllib.request.urlopen(req, timeout=120)
            raw = resp.read()
            resp.close()
            if raw:
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw
            return {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            if e.code in (404, 401, 403):
                raise ReleaseError("GitHub API 错误 %d: %s" % (e.code, err_body[:200]))
            last_err = "%d %s" % (e.code, err_body[:200])
        except (urllib.error.URLError, OSError) as e:
            last_err = str(e)
        if attempt < max_retries - 1:
            import time
            time.sleep(2 ** attempt)

    raise ReleaseError("GitHub API 请求失败（重试 %d 次）: %s" % (max_retries, last_err))


def _upload_asset(upload_url, asset_path, token, asset_name=None, max_retries=3):
    """上传 asset 到 Release（使用 uploads.github.com，文件流式上传不整读内存）。"""
    if asset_name is None:
        asset_name = os.path.basename(asset_path)

    # upload_url 格式: https://uploads.github.com/repos/OWNER/REPO/releases/REL_ID/assets{?name,label}
    base = upload_url.split("{")[0]
    url = base + "?name=" + asset_name

    size = os.path.getsize(asset_path)
    print("  上传 %s (%.1f MB)..." % (asset_name, size / 1048576))

    parsed = urllib.parse.urlsplit(url)
    conn_host = parsed.hostname
    conn_port = parsed.port or 443
    path = parsed.path + (("?" + parsed.query) if parsed.query else "")

    last_err = None
    for attempt in range(max_retries):
        conn = None
        try:
            conn = http.client.HTTPSConnection(conn_host, conn_port, timeout=300)
            headers = {
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
                "User-Agent": "BomExport-Publish/1.0",
            }
            try:
                with open(asset_path, "rb") as f:
                    conn.request("POST", path, body=f, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                status = resp.status
            finally:
                conn.close()
            if status not in (200, 201):
                raise ReleaseError("上传失败 %d: %s" % (status, raw[:200]))
            result = json.loads(raw) if raw else {}
            browser_url = result.get("browser_download_url", "")
            print("  上传完成: %s" % browser_url)
            return browser_url
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as e:
            last_err = str(e)
            if attempt < max_retries - 1:
                import time
                time.sleep(2 ** attempt)
    raise ReleaseError("上传 asset 失败（重试 %d 次）: %s" % (max_retries, last_err))


def _get_repo_info(token, repo):
    """获取仓库默认分支。"""
    result = _api_request("GET", API_BASE + "/repos/" + repo, token)
    return result.get("default_branch", "main")


def _get_file_sha(token, repo, branch, path):
    """获取仓库文件的当前 SHA（用于更新文件）。"""
    url = API_BASE + "/repos/%s/contents/%s?ref=%s" % (repo, path, branch)
    try:
        result = _api_request("GET", url, token)
        return result.get("sha")
    except ReleaseError:
        return None  # 文件不存在（404）时视为新建


def _update_json_file(token, repo, branch, updates):
    """更新仓库根目录 update.json。"""
    path = "update.json"
    sha = _get_file_sha(token, repo, branch, path)

    # 读取现有内容
    if sha:
        url = API_BASE + "/repos/%s/contents/%s?ref=%s" % (repo, path, branch)
        result = _api_request("GET", url, token)
        import base64
        content = base64.b64decode(result["content"]).decode("utf-8")
        data = json.loads(content)
    else:
        data = {"schema": 1}

    # 合并更新
    data.update(updates)

    # 写回
    import base64
    encoded = base64.b64encode(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")).decode("utf-8")
    payload = {
        "message": "chore: update update.json (%s)" % ", ".join(updates.keys()),
        "content": encoded,
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    url = API_BASE + "/repos/%s/contents/%s" % (repo, path)
    _api_request("PUT", url, token, data=payload)
    print("update.json 已更新")


def create_release(token, repo, tag, title, notes=""):
    """创建 GitHub Release，返回 (release_id, upload_url)。"""
    url = API_BASE + "/repos/%s/releases" % repo
    payload = {
        "tag_name": tag,
        "name": title,
        "body": notes,
        "draft": False,
        "prerelease": False,
    }
    result = _api_request("POST", url, token, data=payload)
    rel_id = result.get("id")
    upload_url = result.get("upload_url", "")
    html_url = result.get("html_url", "")
    print("Release 已创建: %s (ID=%s)" % (html_url, rel_id))
    return rel_id, upload_url


def publish_exe(token, repo, exe_path, notes="", version=None):
    """发布 exe 更新。"""
    if version is None:
        # 版本号唯一真源在 bom_common.py（2026-08-19 修复：重构后 bom_export.py
        # 只是门面，不再直接定义 __version__，旧解析会 IndexError）
        exe_dir = os.path.dirname(os.path.abspath(__file__))
        common_path = os.path.join(exe_dir, "bom_common.py")
        if os.path.exists(common_path):
            with open(common_path, "r", encoding="utf-8") as f:
                for line in f:
                    m = re.match(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", line.strip())
                    if m:
                        version = m.group(1)
                        break
    if not version:
        raise ReleaseError("无法确定 exe 版本号，请用 --version 指定")

    sha256 = _sha256_file(exe_path)
    size = os.path.getsize(exe_path)
    tag = "exe-v%s" % version
    print("发布 exe v%s (sha256=%s..., size=%d)" % (version, sha256[:16], size))

    rel_id, upload_url = create_release(token, repo, tag,
                                         "BomExport v%s" % version, notes)
    browser_url = _upload_asset(upload_url, exe_path, token, "BomExport.exe")

    branch = _get_repo_info(token, repo)
    _update_json_file(token, repo, branch, {
        "exe": {
            "version": version,
            "url": browser_url,
            "sha256": sha256,
            "size": size,
            "notes": notes,
        }
    })
    print("exe 发布完成!")


def publish_rules(token, repo, rules_dir, notes="", version=None):
    """发布规则更新。"""
    if version is None:
        manifest_path = os.path.join(rules_dir, "manifest.json")
        if os.path.exists(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                version = data.get("version")
    if not version:
        raise ReleaseError("无法确定规则版本号，请用 --version 指定")

    # 打 zip（manifest.json + *.rules.json，排除 snapshots/candidates）
    zip_path = os.path.join(tempfile.gettempdir(), "rules.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in os.listdir(rules_dir):
            full = os.path.join(rules_dir, name)
            if os.path.isfile(full) and (name == "manifest.json" or name.endswith(".rules.json")):
                zf.write(full, name)
    sha256 = _sha256_file(zip_path)
    size = os.path.getsize(zip_path)
    tag = "rules-v%s" % version
    print("发布规则 v%s (sha256=%s..., size=%d)" % (version, sha256[:16], size))

    rel_id, upload_url = create_release(token, repo, tag,
                                         "Rules v%s" % version, notes)
    browser_url = _upload_asset(upload_url, zip_path, token, "rules.zip")

    branch = _get_repo_info(token, repo)
    _update_json_file(token, repo, branch, {
        "rules": {
            "version": version,
            "url": browser_url,
            "sha256": sha256,
            "size": size,
            "notes": notes,
        }
    })

    os.remove(zip_path)
    print("规则发布完成!")


def main():
    parser = argparse.ArgumentParser(description="BomExport 发布脚本")
    parser.add_argument("type", choices=["exe", "rules", "manifest"],
                        help="发布类型: exe / rules / manifest")
    parser.add_argument("path", nargs="?", help="文件路径（exe 或 rules 目录）")
    parser.add_argument("--notes", default="", help="更新说明")
    parser.add_argument("--version", default=None, help="版本号（默认自动读取）")
    parser.add_argument("--exe-version", default=None)
    parser.add_argument("--exe-url", default=None)
    parser.add_argument("--rules-version", default=None)
    parser.add_argument("--rules-url", default=None)
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    # 仓库缺省从当前 git remote origin 推导（自动指向本仓库）
    repo = os.environ.get("GITHUB_REPO", "") or _default_repo()
    if not token:
        print("错误: 请设置 GITHUB_TOKEN 环境变量")
        sys.exit(1)
    if not repo:
        print("错误: 无法确定仓库（请设置 GITHUB_REPO 环境变量，如 owner/repo）")
        sys.exit(1)

    try:
        if args.type == "exe":
            # 默认路径 = 本目录下 dist/BomExport.exe（PyInstaller 默认输出）
            if not args.path:
                args.path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "dist", "BomExport.exe")
            if not os.path.exists(args.path):
                print("错误: exe 文件不存在: %s" % args.path)
                sys.exit(1)
            publish_exe(token, repo, args.path, args.notes, args.version)

        elif args.type == "rules":
            # 默认路径 = 仓库根下 V2/rules（本脚本在 bom_export/ 内，../V2/rules）
            if not args.path:
                args.path = os.path.normpath(os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "..", "V2", "rules"))
            if not os.path.isdir(args.path):
                print("错误: 规则目录不存在: %s" % args.path)
                sys.exit(1)
            publish_rules(token, repo, args.path, args.notes, args.version)

        elif args.type == "manifest":
            # 仅更新 update.json
            updates = {}
            if args.exe_version and args.exe_url:
                updates["exe"] = {"version": args.exe_version, "url": args.exe_url}
            if args.rules_version and args.rules_url:
                updates["rules"] = {"version": args.rules_version, "url": args.rules_url}
            if not updates:
                print("错误: manifest 模式需要 --exe-version/--exe-url 或 --rules-version/--rules-url")
                sys.exit(1)
            branch = _get_repo_info(token, repo)
            _update_json_file(token, repo, branch, updates)
            print("update.json 已更新")
    except ReleaseError as e:
        print("错误: %s" % e)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 发布脚本兜底，避免裸 traceback
        print("发布失败: %s" % e)
        sys.exit(1)


if __name__ == "__main__":
    main()
