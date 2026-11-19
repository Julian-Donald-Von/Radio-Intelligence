"""
webapi/plugin_local.py — 本地插件扫描器（第一版：本地文件夹，预留 GitHub 市场）

设计目标（用户 2026-11-19）：
  「下载好插件安装在 plugin 相关文件夹里面，一扫描就出来。第一版是这样的，
    但也要预留 GitHub 市场那种可能性。」

扫描约定：
  插件根目录 plugins/art-effects/ 下，每个子目录 = 一个插件：
      plugins/art-effects/<id>/
          manifest.json     —— 必需，插件元数据 + 能力声明
          plugin.js         —— 必需，冷层插件实现（暴露 window.ArtPlugins[id]）
          README.md         —— 可选

  manifest.json 字段（v1）：
      id, name, version, type, slot, execution, capabilities, entry, hostApi
      source: "local"（本地第一版）｜"github"（预留市场位，本版不实现）
      repository / license / signature —— 预留，市场阶段用

  扫描器只读 manifest + 检查文件存在与门禁规则；不执行插件代码。
  前端拿到清单后由 ArtHost 走 register → hydrate → enable 两段式执行。

零外部依赖（仅标准库），可被 server.py 与验证脚本安全导入。
不改任何冻结核心文件。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROJECT_ROOT / "plugins" / "art-effects"

# 与 webapi/static/ui-art-gate.js 保持一致（同一份规则的 Python 镜像）
FORBIDDEN_SLOTS = {"water", "bg-effect"}
REGISTRABLE_SLOTS = {
    "breathing", "ripple", "confetti", "progress-glow", "lens",
}
ALLOWED_TYPES = {"art-effect", "ornament"}
ALLOWED_EXECUTION = {"cold", "hot-limited"}   # 2026-11-19 用户拍板：堆芯需自持 rAF
ALLOWED_CAPABILITIES = {
    "dom:body", "dom:#dock", "dom:#aibar", "dom:#cards",
    "css.vars", "assets",
    "bus:mood@2hz", "bus:beats@event", "bus:track_changed@event",
    "bus:favorite@event", "bus:art.energy@10hz", "bus:plugins.changed@event",
}


def validate_manifest(m: dict) -> tuple[bool, str]:
    """返回 (ok, code/reason)，与 ArtGate.checkManifest 规则对齐。"""
    if not isinstance(m, dict):
        return False, "E_BAD_MANIFEST"
    pid = m.get("id", "")
    if not isinstance(pid, str) or not pid or len(pid) > 64:
        return False, "E_BAD_ID"
    if not all(c.isalnum() or c in "._-" for c in pid) or not pid[0].isalnum():
        return False, "E_BAD_ID"
    if not m.get("name") or not isinstance(m.get("version"), str):
        return False, "E_BAD_META"
    slot = m.get("slot")
    if slot in FORBIDDEN_SLOTS:
        return False, "E_FORBIDDEN_SLOT"
    if slot not in REGISTRABLE_SLOTS:
        return False, "E_UNKNOWN_SLOT"
    if m.get("type") not in ALLOWED_TYPES:
        return False, "E_BAD_TYPE"
    if m.get("execution") not in ALLOWED_EXECUTION:
        return False, "E_EXECUTION_NOT_OPEN"
    entry = m.get("entry")
    if not isinstance(entry, str) or not entry.endswith(".js") or "/" in entry or "\\" in entry:
        return False, "E_BAD_ENTRY"
    caps = m.get("capabilities", [])
    if not isinstance(caps, list):
        return False, "E_BAD_CAPABILITIES"
    for cap in caps:
        if cap not in ALLOWED_CAPABILITIES:
            return False, "E_BAD_CAPABILITY"
    host_api = str(m.get("hostApi", ""))
    if host_api and "1." not in host_api:
        return False, "E_HOST_API"
    return True, "OK"


def scan(root: Path | None = None) -> dict[str, Any]:
    """扫描本地插件目录，返回 {count, plugins, rejected, root, note}。

    不执行任何插件代码；只读取 manifest.json 并核对入口文件存在。
    """
    root = Path(root) if root else PLUGIN_ROOT
    result: dict[str, Any] = {
        "count": 0,
        "plugins": [],
        "rejected": [],
        "root": str(root),
        "note": "第一版：本地文件夹扫描；source=github 为市场预留位，本版不实现。",
    }
    if not root.exists():
        return result

    for d in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        mf_path = d / "manifest.json"
        if not mf_path.exists():
            result["rejected"].append({"dir": d.name, "reason": "E_NO_MANIFEST"})
            continue
        try:
            manifest = json.loads(mf_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            result["rejected"].append({"dir": d.name, "reason": "E_BAD_JSON", "detail": str(e)})
            continue
        ok, code = validate_manifest(manifest)
        if not ok:
            result["rejected"].append({"dir": d.name, "reason": code})
            continue
        entry = manifest["entry"]
        if not (d / entry).exists():
            result["rejected"].append({"dir": d.name, "reason": "E_NO_ENTRY", "detail": entry})
            continue
        result["plugins"].append({
            "id": manifest["id"],
            "name": manifest["name"],
            "version": manifest["version"],
            "type": manifest["type"],
            "slot": manifest["slot"],
            "execution": manifest["execution"],
            "capabilities": manifest.get("capabilities", []),
            "source": manifest.get("source", "local"),
            "repository": manifest.get("repository"),
            "license": manifest.get("license"),
            "dir": d.name,
            "entry": entry,
            # 给 ArtHost.register/hydrate 用的完整 manifest（前端门禁复用）
            "manifest": manifest,
        })

    result["count"] = len(result["plugins"])
    return result


# ---------------- 音源插件（Python 类）静态扫描 ----------------
# 与美术插件不同：音源插件是 plugins/sources/*.py 里的 MusicSource 子类。
# 这里用 ast 只做静态解析（不 import、不执行代码），提取 key/label/searchable。
# 真正的加载/启用走 data.sources 的 REGISTRY + set_setting 白名单，见 webapi/server.py。
SOURCE_ROOT = PROJECT_ROOT / "plugins" / "sources"
def _builtin_source_keys() -> set:
    """动态读取当前构建的内置音源 key（发布副本已把 netease/qqmusic/local 移到插件）。"""
    try:
        from data.sources import BUILTIN_REGISTRY
        return set(BUILTIN_REGISTRY.keys())
    except Exception:  # noqa: BLE001
        return set()


def _class_const(node, name: str):
    """取类体内 `name = 字面量` 的字面值；不执行任何表达式。"""
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    try:
                        return ast.literal_eval(stmt.value)
                    except Exception:  # noqa: BLE001
                        return None
    return None


def _is_music_source(node: ast.ClassDef) -> bool:
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id == "MusicSource":
            return True
        if isinstance(base, ast.Attribute) and base.attr == "MusicSource":
            return True
    return False


def scan_sources(root: Path | None = None, *, with_config: bool = True) -> dict[str, Any]:
    """静态扫描 plugins/sources/*.py 的 MusicSource 子类插件。

    不执行插件代码；返回可用清单（含内置键冲突标注与当前 enabled 状态）。
    with_config=True 时读取 user_config.sources[key].enabled 反映启用状态。
    """
    root = Path(root) if root else SOURCE_ROOT
    out: dict[str, Any] = {
        "kind": "source", "count": 0, "plugins": [], "rejected": [], "root": str(root),
        "note": "Python 音源插件；启用写 sources.<key>.enabled 并热重载 REGISTRY。",
    }
    if not root.exists():
        return out

    enabled_map: dict[str, bool] = {}
    if with_config:
        try:
            from config.settings import user_config
            for k, v in (user_config.data.get("sources") or {}).items():
                if isinstance(v, dict) and "enabled" in v:
                    enabled_map[k] = bool(v["enabled"])
        except Exception:  # noqa: BLE001
            pass

    for f in sorted(p for p in root.glob("*.py") if not p.name.startswith("__")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            out["rejected"].append({"file": f.name, "reason": "E_SYNTAX", "detail": str(e)[:120]})
            continue
        found = [n for n in tree.body if isinstance(n, ast.ClassDef) and _is_music_source(n)]
        if not found:
            continue  # 普通 .py（helper）跳过，不算拒绝
        for node in found:
            key = _class_const(node, "key") or _class_const(node, "plugin_key")
            if not key or not isinstance(key, str):
                out["rejected"].append({"file": f.name, "reason": "E_NO_KEY", "class": node.name})
                continue
            label = _class_const(node, "label") or node.name
            searchable = _class_const(node, "searchable")
            if searchable is None:
                searchable = True  # 基类默认 True
            out["plugins"].append({
                "id": key,
                "name": label,
                "class": node.name,
                "file": f.name,
                "kind": "source",
                "source": "local",
                "searchable": bool(searchable),
                "qr": any(isinstance(n, ast.FunctionDef) and n.name in ("start_qr_login", "check_qr_login") for n in node.body),
                "builtin_conflict": key in _builtin_source_keys(),
                "enabled": enabled_map.get(key, False),
            })

    out["count"] = len(out["plugins"])
    return out


def scan_all(root: Path | None = None, *, with_config: bool = True) -> dict[str, Any]:
    """统一清单：美术插件（kind=art-effect）+ 音源插件（kind=source）。"""
    art = scan(root)
    for p in art["plugins"]:
        p.setdefault("kind", "art-effect")
    src = scan_sources(with_config=with_config)
    return {
        "art": art,
        "sources": src,
        "count": art["count"] + src["count"],
        "note": "本地插件统一清单；kind 区分 art-effect / source。市场 source=github 为预留位。",
    }
