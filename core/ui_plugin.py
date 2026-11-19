"""
主界面（UI / 外壳）插件系统 —— 让整个 AI 电台界面可被外部插件整体替换。

与音源 / TTS / 皮肤插件同源：放到 plugins/ui/ 下、继承 MainUIPlugin 并填一个
非空 plugin_key 的类，即可被 UIManager 自动发现，无需改核心代码。

一个主界面插件：**
  - 元数据：plugin_key / plugin_name / plugin_version / plugin_author / plugin_description
  - 契约：build(host) -> QWidget
        host 是 MainWindow 实例，提供后端（radio / player / library / db /
        source_manager / brain / user_config）与辅助方法（toast_requested 信号、
        _to_main / _open_settings 等）。插件据此构建整块中央界面并返回该 QWidget，
        由 MainWindow 设为中央部件。

「经典界面」(classic) 是内置回退，不经由插件目录，由 MainWindow 直接构建；
其余（如 liquid_glass 液态玻璃舱）均为可插拔实现。
"""
from __future__ import annotations

import logging
from typing import Optional

from core.plugin import PLUGIN_DIR, discover_plugins, plugin_id

logger = logging.getLogger("MusicAI.UIPlugin")

# 经典界面（内置回退，不在 plugins/ui/ 目录下）
CLASSIC_KEY = "classic"


class MainUIPlugin:
    """主界面插件基类。子类只需填元数据 + 实现 build(host)。"""

    # ---- 元数据 ----
    plugin_key: str = ""
    plugin_name: str = ""
    plugin_version: str = "1.0"
    plugin_author: str = ""
    plugin_description: str = ""

    def meta(self) -> dict:
        return {
            "key": plugin_id(type(self)) or self.plugin_key,
            "name": self.plugin_name or self.plugin_key,
            "version": self.plugin_version,
            "author": self.plugin_author,
            "description": self.plugin_description,
        }

    # noinspection PyMethodMayBeStatic
    def build(self, host) -> "object":
        """构建并返回整块中央界面 QWidget。host 提供后端与辅助方法。

        子类必须重写。默认抛出 NotImplementedError。
        """
        raise NotImplementedError(
            f"UI 插件 {self.plugin_key or type(self).__name__} 未实现 build(host)")


class UIManager:
    """主界面插件注册 / 发现 / 选择。"""

    def __init__(self) -> None:
        from config.settings import user_config
        self._user_config = user_config
        self._discovered: dict = {}
        self._reload()
        self._active = (user_config.ui.get("main_ui") or CLASSIC_KEY)
        if self._active not in self._all_keys():
            self._active = CLASSIC_KEY

    def _reload(self) -> None:
        try:
            self._discovered = discover_plugins(
                PLUGIN_DIR / "ui", (MainUIPlugin,), recursive=True)
        except Exception as e:  # noqa
            logger.warning("主界面插件发现失败：%s", e)
            self._discovered = {}

    def reload(self) -> None:
        self._reload()

    def _all_keys(self) -> list:
        return [CLASSIC_KEY, *self._discovered.keys()]

    # ---------------- 查询 ----------------
    def list_uis(self) -> list[dict]:
        out = [{
            "key": CLASSIC_KEY,
            "name": "经典界面",
            "version": "1.0",
            "author": "MusicAI",
            "description": "内置经典布局（顶部工具栏 + 左侧导航 + 右侧信息面板 + 底部播放条）。",
            "builtin": True,
        }]
        for cls in self._discovered.values():
            m = cls().meta()
            m["builtin"] = False
            out.append(m)
        return out

    def get_ui(self, key: str) -> Optional[MainUIPlugin]:
        """返回指定 key 的 UI 插件实例；classic / 未知返回 None（由 MainWindow 回退）。"""
        cls = self._discovered.get(key)
        return cls() if cls else None

    @property
    def active_key(self) -> str:
        return self._active

    @property
    def active_is_plugin(self) -> bool:
        return self._active != CLASSIC_KEY and self._active in self._discovered

    # ---------------- 切换 / 持久化 ----------------
    def set_active(self, key: str) -> bool:
        if key not in self._all_keys():
            logger.warning("主界面 '%s' 不存在", key)
            return False
        self._active = key
        ui = self._user_config.data.setdefault("ui", {})
        ui["main_ui"] = key
        self._user_config.save()
        return True
