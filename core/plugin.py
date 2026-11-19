"""
统一插件框架（MusicAI 插件化基础设施）。

设计目标：**不改核心代码即可扩展功能**。任意插件只需放到对应目录，
应用启动时自动发现并合并进内置注册表。

插件目录（相对项目根 D:\\MusicAI）：
  plugins/sources/  音源插件   —— 继承 data.sources.base.MusicSource
  plugins/tts/      TTS 引擎   —— 继承 core.tts_base.TTSEngine
  plugins/skins/    UI 皮肤    —— 继承 core.skin.Skin（支持子目录皮肤包）

插件写法（单文件示例）：
  # plugins/sources/my_source.py
  from data.sources.base import MusicSource
  class MySource(MusicSource):
      key = "my"            # 插件唯一 id（也支持 plugin_key / name）
      label = "我的音源"
      def search(self, kw, limit=30): ...
          return []

框架会在启动时扫描这些 .py 并收集其子类。子类必须填一个非空的唯一 id
（plugin_key / key / name 之一），否则被跳过。导入失败的文件仅告警并跳过，
不会拖垮整个应用。

注意：本模块只依赖标准库，可被任何层级（data.sources / core.tts_*）安全导入，
不会引入重依赖或循环导入。
"""
from __future__ import annotations

import importlib.util
import logging
import re
import sys
from pathlib import Path
from typing import Iterable, Type

logger = logging.getLogger("MusicAI.Plugin")

# 项目根：D:\MusicAI（core/plugin.py -> parent=core -> parent.parent=项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 插件根目录
PLUGIN_DIR = PROJECT_ROOT / "plugins"


def ensure_root_on_path() -> None:
    """把项目根加入 sys.path，保证插件文件里 `from data.xxx import ...` 可用。"""
    root = str(PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def plugin_id(cls: type) -> str:
    """取插件唯一 id：plugin_key > key > name。"""
    return (
        getattr(cls, "plugin_key", None)
        or getattr(cls, "key", None)
        or getattr(cls, "name", None)
        or ""
    )


def discover_plugins(
    directory: Path,
    base_classes: Iterable[type],
    *,
    recursive: bool = False,
) -> dict[str, type]:
    """扫描目录下所有插件 .py，返回 {插件id: 类}。

    - recursive=False：仅扫描目录顶层 .py（音源 / TTS 用）。
    - recursive=True ：递归扫描全部 .py（皮肤支持子目录包，如
      plugins/skins/midnight/skin.py）。
    - 仅收集「非基类本身、且是 base_classes 中某一类的子类、且有非空 id」的类。
    - 同名 id 冲突时保留首个并告警（内置优先由调用方在合并时保证）。
    """
    found: dict[str, type] = {}
    directory = Path(directory)
    if not directory.exists():
        return found
    ensure_root_on_path()
    base_set = set(base_classes)
    pattern = "**/*.py" if recursive else "*.py"
    # 稳定排序，保证发现顺序可预期
    files = sorted(p for p in directory.glob(pattern) if not p.name.startswith("__"))
    for path in files:
        mod = _import_file(path)
        if mod is None:
            continue
        for obj in vars(mod).values():
            if not isinstance(obj, type):
                continue
            if obj in base_set:
                continue  # 跳过基类本身
            if not any(issubclass(obj, bc) for bc in base_set):
                continue
            pid = plugin_id(obj)
            if not pid:
                continue
            if pid in found:
                logger.warning(
                    "插件 id 冲突 '%s'：%s 与 %s，保留首个",
                    pid, found[pid].__module__, obj.__module__)
                continue
            found[pid] = obj
    return found


def _import_file(path: Path) -> object | None:
    """把插件 .py 作为模块导入；失败返回 None。

    优先用「规范包路径」导入（如 plugins.ui.liquid_glass.plugin），
    这样与 `import plugins.ui.liquid_glass` 共享同一个模块对象，避免同一插件类
    被加载成两份（否则 isinstance 判断会失败）。仅当该路径不构成合法包时，
    才退回到合成模块名（用于 plugins/sources、plugins/skins 这类非包目录）。
    """
    try:
        rel = path.resolve().relative_to(PLUGIN_DIR)
        # 规范包路径：plugins.<相对 PLUGIN_DIR 的层级>（PLUGIN_DIR 本身是
        # 名为 plugins 的包，位于项目根，已在 sys.path 上）
        dotted = "plugins." + ".".join(rel.with_suffix("").parts)
        if dotted and _try_import_module(dotted) is not None:
            return sys.modules[dotted]
    except ValueError:
        pass

    # 回退：用相对插件的稳定路径生成唯一模块名（避免同 stem 冲突 / hash 随机化）
    rel = path.resolve()
    mod_name = "musicai_plugin_" + re.sub(r"\W", "_", str(rel))
    return _load_as_module(mod_name, path)


def _try_import_module(dotted: str) -> object | None:
    try:
        return importlib.import_module(dotted)
    except Exception:  # noqa: BLE001
        return None


def _load_as_module(mod_name: str, path: Path) -> object | None:
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        # 预先注册，支持插件文件内部相对互引（如皮肤包内的 helper 模块）
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:  # noqa: BLE001
        logger.warning("插件加载失败 %s：%s", path, e)
        sys.modules.pop(mod_name, None)
        return None
