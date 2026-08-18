# -*- coding: utf-8 -*-
"""生命周期：快照 / 回滚 / 语义化版本 bump。"""

import json
import os
import re
import shutil

from .schema import RULES_DIRNAME, SNAPSHOTS_DIRNAME
from .model import MANIFEST_NAME, load_ruleset, save_ruleset


def semver_tuple(v):
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(v))
    if not m:
        raise ValueError(f"非法语义化版本: {v}")
    return tuple(int(x) for x in m.groups())


def bump_semver(v, level="patch"):
    maj, mino, pat = semver_tuple(v)
    if level == "major":
        return f"{maj + 1}.0.0"
    if level == "minor":
        return f"{maj}.{mino + 1}.0"
    return f"{maj}.{mino}.{pat + 1}"


def snapshot(rules_dir, rules, manifest, version=None):
    """门禁通过后的快照：snapshots/<ver>.json 全量 + 原子写回规则文件。

    未显式给版本时：读 manifest 文件当前版本自动 bump PATCH+1
    （保证同文件连续保存产生不同快照，回滚不被覆盖）。
    """
    if version is None:
        cur = "0.0.0"
        try:
            with open(os.path.join(rules_dir, MANIFEST_NAME), encoding="utf-8") as f:
                cur = json.load(f).get("version", "0.0.0")
        except Exception:
            pass
        version = bump_semver(cur)
    snap_dir = os.path.join(rules_dir, SNAPSHOTS_DIRNAME)
    os.makedirs(snap_dir, exist_ok=True)
    payload = {"version": version, "manifest": manifest, "rules": rules}
    tmp = os.path.join(snap_dir, f".{version}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(snap_dir, f"{version}.json"))
    save_ruleset(rules_dir, rules, manifest, version=version)
    return version


def list_snapshots(rules_dir):
    snap_dir = os.path.join(rules_dir, SNAPSHOTS_DIRNAME)
    if not os.path.isdir(snap_dir):
        return []
    vers = []
    for name in os.listdir(snap_dir):
        if name.endswith(".json") and re.fullmatch(r"[\d.]+\.json", name):
            vers.append(name[:-5])
    return sorted(vers, key=semver_tuple)


def restore(rules_dir, version):
    """从快照恢复：回写规则文件 + manifest，版本 bump PATCH+1 防冲突。"""
    path = os.path.join(rules_dir, SNAPSHOTS_DIRNAME, f"{version}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"快照不存在: {version}")
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    rules = payload["rules"]
    manifest = dict(payload["manifest"])
    new_ver = bump_semver(payload.get("version", version))
    snapshot(rules_dir, rules, manifest, version=new_ver)
    return new_ver


def backup_dir(rules_dir):
    """整目录备份（回滚用），返回备份路径。"""
    bak = rules_dir.rstrip("/\\") + f".bak.{len(os.listdir(os.path.dirname(rules_dir) or '.'))}"
    shutil.copytree(rules_dir, bak)
    return bak
