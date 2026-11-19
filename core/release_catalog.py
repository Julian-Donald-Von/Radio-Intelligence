"""release_catalog.py —— 启动时扫描两个独立文件夹：plugins/ 与 models/。

输出 data_runtime/release_catalog.json，并在启动日志打印摘要。
扫描失败绝不影响应用启动。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("MusicAI.ReleaseCatalog")
ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "plugins"
MODEL_DIR = ROOT / "models"


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def scan_plugins() -> dict:
    out: dict = {"sources": [], "frontend": [], "tts": [], "skins": [], "ui": []}
    if not PLUGIN_DIR.exists():
        return out
    for kind in list(out):
        d = PLUGIN_DIR / kind
        if not d.exists():
            continue
        if kind in ("sources", "tts"):
            out[kind] = [p.name for p in sorted(d.glob("*.py")) if not p.name.startswith("__")]
        elif kind == "frontend":
            for sub in sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith("__")):
                mf = sub / "manifest.json"
                pid = None
                if mf.exists():
                    try:
                        pid = json.loads(mf.read_text(encoding="utf-8")).get("plugin_id")
                    except Exception:
                        pass
                out[kind].append({"dir": sub.name, "plugin_id": pid})
        else:
            out[kind] = [p.name for p in sorted(d.iterdir()) if not p.name.startswith("__")]
    return out


MODEL_EXTS = {".gguf", ".onnx", ".pt", ".pth", ".safetensors", ".bin", ".npz"}


def scan_models() -> list[dict]:
    out: list[dict] = []
    if not MODEL_DIR.exists():
        return out
    for p in sorted(MODEL_DIR.rglob("*")):
        if p.is_file() and p.suffix.lower() in MODEL_EXTS:
            out.append({
                "path": str(p.relative_to(MODEL_DIR)).replace("\\", "/"),
                "bytes": _size(p),
            })
    return out


def run() -> dict:
    catalog = {
        "plugins": scan_plugins(),
        "models": scan_models(),
    }
    try:
        target = ROOT / "data_runtime" / "release_catalog.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa
        logger.warning("release_catalog 写盘失败: %s", e)
    p = catalog["plugins"]
    m = catalog["models"]
    logger.info("[release] 插件扫描 plugins/: sources=%s frontend=%s tts=%s skins=%s",
                len(p["sources"]), len(p["frontend"]), len(p["tts"]), len(p["skins"]))
    logger.info("[release] 模型扫描 models/: %d 个模型文件, %.1f GB",
                len(m), sum(x["bytes"] for x in m) / 1e9)
    return catalog


if __name__ == "__main__":
    run()
