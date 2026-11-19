"""
UI 皮肤插件系统（插件化：背景皮肤 + 控件透明度可配置）。

皮肤插件放到 plugins/skins/ 下（支持子目录皮肤包，如
plugins/skins/midnight/skin.py），继承本模块的 Skin 并填写元数据即可被
SkinManager 自动发现，无需改核心代码。

一个 Skin 描述：
  - 元数据：plugin_key / plugin_name / plugin_version / plugin_author / plugin_description
  - 视觉资源：
      background  背景图路径（相对皮肤包目录）或 CSS 颜色/渐变字符串
      shader     可选 .glsl 片段路径（供未来 OpenGL UI 使用，QSS 皮肤忽略）
      palette    命名颜色 dict：bg / panel / card / text / primary / accent / muted
      opacity    各控件区域默认不透明度（0~1）：window / panel / card / text
      stylesheet 可选 QSS 片段（QSS 皮肤用，可留空由 build_qss 统一生成）
      preview    可选预览图路径
  - render_hint：'qss' | 'opengl' —— 告诉 UI 层如何应用本皮肤
                 （未来 OpenGL 液态玻璃 UI 由用户按《重构.md》实现，读取
                  background/shader/palette/opacity；QSS 皮肤由 build_qss 生成）

「控件透明度可设置」：每个区域(region)的不透明度 = 用户配置 user_config.ui.opacity[region]
优先于皮肤默认值。SkinManager.set_opacity(region, value) 写入并持久化。
"""
from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path
from typing import Optional

from core.plugin import PLUGIN_DIR, discover_plugins, plugin_id

logger = logging.getLogger("MusicAI.Skin")


class Skin:
    # ---- 元数据 ----
    plugin_key: str = ""
    plugin_name: str = ""
    plugin_version: str = "1.0"
    plugin_author: str = ""
    plugin_description: str = ""

    # ---- 视觉资源 ----
    background: Optional[str] = None          # 背景图路径 / CSS 颜色或渐变
    shader: Optional[str] = None              # 可选 .glsl（OpenGL UI 用）
    palette: dict = {}                        # 命名颜色
    # 默认不透明度（控件透明度，0~1）；可被 user_config.ui.opacity 覆盖
    opacity: dict = {
        "window": 1.0,
        "panel": 0.82,
        "card": 0.70,
        "text": 1.0,
    }
    render_hint: str = "qss"                  # 'qss' | 'opengl'
    stylesheet: str = ""                      # 可选 QSS 片段
    preview: Optional[str] = None             # 可选预览图路径

    # ---- 渲染类型 / 媒体描述（供 OpenGL 背景层 Pass1 消费）----
    # kind 决定背景层画什么：
    #   qss            纯 QSS 皮肤，背景用 palette.bg 纯色（无媒体）
    #   image          静态图片背景            -> render_descriptor 给 path
    #   video          视频 / 动态壁纸          -> path + loop/mute/volume
    #   web            动态网页（QWebEngineView 渲染到纹理） -> url
    #   wallpaper_engine 壁纸引擎工程（WallpaperEngineSkin 解析 project.json）
    #   scene          3D 场景（壁纸引擎 scene/application 类型无法在 OpenGL 直渲，
    #                   退化为 preview 图片）
    kind: str = "qss"
    # content：媒体皮肤描述字典（image/video/web 用）；wallpaper_engine 用 source_dir 解析
    content: dict = {}

    # 默认调色板（子类可覆盖）
    DEFAULT_PALETTE = {
        "bg": "#0e1116",
        "panel": "#161b22",
        "card": "#1c2330",
        "text": "#e6edf3",
        "muted": "#8b949e",
        "primary": "#58a6ff",
        "accent": "#bc8cff",
    }

    # ---------------- 资源解析 ----------------
    @property
    def resource_dir(self) -> Path:
        """皮肤定义文件所在目录（用于解析相对资源路径）。"""
        try:
            return Path(inspect.getfile(type(self))).resolve().parent
        except (TypeError, OSError):
            return PLUGIN_DIR / "skins"

    def resolve(self, p: Optional[str]) -> Optional[str]:
        """把皮肤内的相对资源路径解析为绝对路径；非相对（http/绝对）原样返回。"""
        if not p:
            return None
        if "://" in p or p.startswith("/") or (len(p) > 1 and p[1] == ":"):
            return p
        return str((self.resource_dir / p).resolve())

    def get_palette(self) -> dict:
        merged = dict(self.DEFAULT_PALETTE)
        merged.update(self.palette or {})
        return merged

    def meta(self) -> dict:
        return {
            "key": plugin_id(type(self)) or self.plugin_key,
            "name": self.plugin_name or self.plugin_key,
            "version": self.plugin_version,
            "author": self.plugin_author,
            "description": self.plugin_description,
            "render_hint": self.render_hint,
            "kind": self.kind,
            "preview": self.resolve(self.preview),
            "background": self.resolve(self.background),
            # content 带上，供 list_wallpapers 的 _wallpaper_entry 取 image/video
            # 皮肤的真实媒体路径（否则 url 字段恒空，前端只能靠 key 猜）。
            "content": self.content or {},
        }

    # ---------------- 渲染描述（OpenGL 背景层 Pass1 消费） ----------------
    def render_descriptor(self) -> dict:
        """统一渲染描述，供 OpenGL 背景层（见《重构.md》Pass1 水波/液态玻璃层）消费。

        返回字段：
          kind           皮肤 kind（image/video/web/wallpaper_engine/qss/color）
          type           渲染层类型：image|video|web|scene_preview|color
          path           本地媒体绝对路径（image/video/scene_preview）
          url            网页 URL（web）
          loop/mute/volume  视频/网页播放参数
          palette        命名调色板（供粒子/卡片取色）
          background_color 纯色回退
          window_opacity 窗口层不透明度（来自皮肤 opacity.window）
          shader         自定义 .glsl（若有）
          title/preview  显示用
          extra          扩展字段（web 属性 / 壁纸引擎工程原始字段）
        """
        p = self.get_palette()
        op = self.opacity or {}
        base = {
            "kind": self.kind,
            "type": "color",
            "path": None,
            "url": None,
            "loop": True,
            "mute": True,
            "volume": 1.0,
            "palette": p,
            "background_color": p.get("bg", "#0e1116"),
            "window_opacity": op.get("window", 1.0),
            "shader": self.resolve(self.shader),
            "title": self.plugin_name or self.plugin_key,
            "preview": self.resolve(self.preview),
            "extra": {},
        }
        c = self.content or {}
        if self.kind == "image":
            base["type"] = "image"
            base["path"] = self.resolve(c.get("path") or self.background)
        elif self.kind == "video":
            base["type"] = "video"
            base["path"] = self.resolve(c.get("path"))
            base["loop"] = bool(c.get("loop", True))
            base["mute"] = bool(c.get("mute", True))
            try:
                base["volume"] = float(c.get("volume", 1.0))
            except (TypeError, ValueError):
                base["volume"] = 1.0
        elif self.kind == "web":
            base["type"] = "web"
            base["url"] = c.get("url") or self.background
            base["extra"] = c.get("extra", {}) or {}
        elif self.kind in ("wallpaper_engine", "scene"):
            # 普通 Skin 没有 source_dir；退化为纯色背景（WallpaperEngineSkin 会重写）
            base["type"] = "color"
            base["background_color"] = self.resolve(self.background) or p.get("bg")
        else:  # qss
            base["type"] = "color"
            base["background_color"] = self.resolve(self.background) or p.get("bg")
        return base


class DefaultSkin(Skin):
    """内置默认皮肤（深色玻璃）。始终存在，作为回退与示例。"""
    plugin_key = "default"
    plugin_name = "默认深色"
    plugin_version = "1.0"
    plugin_author = "MusicAI"
    plugin_description = "内置深色玻璃皮肤；控件透明度可在设置中调节。"
    background = "#0e1116"
    render_hint = "qss"
    palette = {
        "bg": "#0e1116",
        "panel": "#161b22",
        "card": "#1c2330",
        "text": "#e6edf3",
        "muted": "#8b949e",
        "primary": "#58a6ff",
        "accent": "#bc8cff",
    }
    opacity = {"window": 1.0, "panel": 0.82, "card": 0.70, "text": 1.0}


class MediaSkin(Skin):
    """通用媒体皮肤基类：image/video/web 用。

    子类（通常由壁纸导入器自动生成）只需填 kind + content 即可：
        kind = "image"
        content = {"path": "/abs/image.png"}
    渲染层直接读 render_descriptor() 的 type/path/url。
    """


class WallpaperEngineSkin(Skin):
    """壁纸引擎（Wallpaper Engine）皮肤：解析工程根目录的 project.json。

    内容为壁纸引擎导出的文件夹（含 project.json + 媒体文件）。支持类型：
      video      视频壁纸   -> 背景层播放 video.mp4（可静音）
      web       网页壁纸   -> QWebEngineView 渲染 index.html 到纹理
      scene/application 3D 壁纸 -> 无法在 OpenGL 直渲，退化为 preview 图片
    source_dir 指向壁纸文件夹（可外部引用，不强制拷贝，避免大文件重复）。
    """

    kind = "wallpaper_engine"
    source_dir: str = ""               # 壁纸引擎文件夹绝对路径

    def _load_project(self) -> dict:
        try:
            f = Path(self.source_dir) / "project.json"
            if f.exists():
                return json.loads(f.read_text(encoding="utf-8", errors="ignore"))
        except Exception as e:  # noqa
            logger.warning("壁纸引擎工程解析失败 %s: %s", self.source_dir, e)
        return {}

    def meta(self) -> dict:
        m = super().meta()
        proj = self._load_project()
        if proj:
            etype = (proj.get("type") or "video").lower()
            title = (proj.get("title")
                     or (proj.get("titleLocalized") or {}).get("en")
                     or self.plugin_name)
            m["name"] = title
            m["kind"] = "wallpaper_engine"
            m["engine_type"] = etype
            m["workshop_id"] = proj.get("publishedfileid") or proj.get("workshopid")
            preview = proj.get("preview")
            if preview:
                m["preview"] = str((Path(self.source_dir) / preview).resolve())
        return m

    def render_descriptor(self) -> dict:
        proj = self._load_project()
        if not proj:
            # 工程解析失败 / 未配置 source_dir -> 退化为纯色背景
            d = super().render_descriptor()
            d["type"] = "color"
            return d
        folder = Path(self.source_dir)
        etype = (proj.get("type") or "video").lower()
        main_file = proj.get("file")
        main_path = str((folder / main_file).resolve()) if main_file else None
        preview_file = proj.get("preview")
        preview_path = str((folder / preview_file).resolve()) if preview_file else None
        props = ((proj.get("general") or {}).get("properties") or {})
        mute = str(props.get("muteaudio", "0")) == "1"
        try:
            volume = float(props.get("volume", "1"))
        except (TypeError, ValueError):
            volume = 1.0
        # scene/application 无法在 OpenGL 直渲 -> 用 preview 图片当背景
        type_map = {
            "video": "video",
            "web": "web",
            "scene": "scene_preview",
            "application": "scene_preview",
        }
        rtype = type_map.get(etype, "video")
        path = main_path if etype in ("video", "web") else (preview_path or main_path)
        p = self.get_palette()
        title = (proj.get("title")
                 or (proj.get("titleLocalized") or {}).get("en")
                 or self.plugin_name)
        return {
            "kind": "wallpaper_engine",
            "type": rtype,
            "path": path,
            "url": None,
            "loop": True,
            "mute": mute,
            "volume": volume,
            "palette": p,
            "background_color": p.get("bg", "#0e1116"),
            "window_opacity": (self.opacity or {}).get("window", 1.0),
            "shader": self.resolve(self.shader),
            "title": title,
            "preview": preview_path,
            "extra": {
                "engine_type": etype,
                "workshop_id": proj.get("publishedfileid") or proj.get("workshopid"),
                "source_dir": str(folder.resolve()),
                "props": props,
            },
        }


class SkinManager:
    """皮肤注册 / 发现 / 应用 / 透明度配置。"""

    def __init__(self) -> None:
        from config.settings import user_config
        self._user_config = user_config
        self._builtin = {"default": DefaultSkin}
        self._discovered: dict = {}
        self._reload()
        self._active_key = (user_config.ui.get("skin") or "default")
        if self._active_key not in self._all:
            self._active_key = "default"

    def _reload(self) -> None:
        try:
            self._discovered = discover_plugins(
                PLUGIN_DIR / "skins", (Skin,), recursive=True)
        except Exception as e:  # noqa
            logger.warning("皮肤插件发现失败：%s", e)
            self._discovered = {}
        self._all = {**self._builtin, **self._discovered}

    def reload(self) -> None:
        self._reload()

    # ---------------- 查询 ----------------
    def list_skins(self) -> list[dict]:
        return [cls().meta() for cls in self._all.values()]

    def get_skin(self, key: Optional[str] = None) -> Skin:
        k = key or self._active_key
        cls = self._all.get(k) or self._builtin["default"]
        return cls()

    @property
    def active_key(self) -> str:
        return self._active_key

    # ---------------- 切换 / 持久化 ----------------
    def set_active(self, key: str) -> bool:
        if key not in self._all:
            logger.warning("皮肤 '%s' 不存在", key)
            return False
        self._active_key = key
        ui = self._user_config.data.setdefault("ui", {})
        ui["skin"] = key
        self._user_config.save()
        return True

    # ---------------- 透明度（控件透明度可设置） ----------------
    def opacity(self, region: str) -> float:
        """某区域有效不透明度 = 用户配置优先，否则皮肤默认值。"""
        cfg = (self._user_config.ui.get("opacity") or {})
        skin = self.get_skin()
        default = (skin.opacity or {}).get(region, 1.0)
        v = cfg.get(region, default)
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = default
        return max(0.0, min(1.0, v))

    def set_opacity(self, region: str, value: float) -> None:
        """设置并持久化某区域不透明度（0~1）。"""
        v = max(0.0, min(1.0, float(value)))
        ui = self._user_config.data.setdefault("ui", {})
        op = ui.setdefault("opacity", {})
        op[region] = v
        self._user_config.save()

    def opacity_regions(self) -> list:
        """返回所有已知区域（皮肤默认 + 用户配置并集）。"""
        regions = set((self.get_skin().opacity or {}).keys())
        regions.update((self._user_config.ui.get("opacity") or {}).keys())
        return sorted(regions)

    # ---------------- 生成 QSS（QSS 皮肤用；OpenGL UI 另读字段） ----------------
    def build_qss(self, skin: Optional[Skin] = None) -> str:
        """根据皮肤调色板 + 区域不透明度生成 QSS 片段。

        OpenGL UI（用户依《重构.md》实现）不应使用本方法，而应直接读取
        Skin 的 background/shader/palette/opacity 字段绘制。
        """
        skin = skin or self.get_skin()
        p = skin.get_palette()
        op = {r: self.opacity(r) for r in self.opacity_regions()}
        # 透明背景：rgba 中 alpha = 不透明度
        def rgba(color: str, a: float) -> str:
            color = (color or "#000000").lstrip("#")
            if len(color) == 3:
                color = "".join(c * 2 for c in color)
            if len(color) >= 6:
                r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
                return f"rgba({r},{g},{b},{a:.3f})"
            return color

        qss = f"""
        QWidget {{
            background: {rgba(p.get('bg','#0e1116'), op.get('window', 1.0))};
            color: {p.get('text','#e6edf3')};
            font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
        }}
        QFrame#panel, QWidget#panel {{
            background: {rgba(p.get('panel','#161b22'), op.get('panel', 0.82))};
            border-radius: 12px;
        }}
        QFrame#card, QWidget#card {{
            background: {rgba(p.get('card','#1c2330'), op.get('card', 0.70))};
            border-radius: 10px;
        }}
        QLabel#title {{ color: {p.get('text','#e6edf3')}; font-weight: bold; }}
        QLabel#muted {{ color: {p.get('muted','#8b949e')}; }}
        QPushButton {{
            background: {rgba(p.get('primary','#58a6ff'), 0.18)};
            color: {p.get('text','#e6edf3')};
            border: 1px solid {rgba(p.get('primary','#58a6ff'), 0.4)};
            border-radius: 8px;
            padding: 6px 12px;
        }}
        QPushButton:hover {{
            background: {rgba(p.get('primary','#58a6ff'), 0.32)};
        }}
        QListWidget, QTextBrowser {{
            background: {rgba(p.get('card','#1c2330'), op.get('card', 0.70))};
            border: none;
        }}
        """
        # 拼接皮肤自带 QSS 片段（若有），便于插件做深度定制
        if skin.stylesheet:
            qss += "\n" + skin.stylesheet
        return qss

    def apply_qss(self, widget) -> None:
        """把当前皮肤的 QSS 应用到给定 Qt widget（QSS 皮肤用）。"""
        try:
            from PySide6.QtWidgets import QWidget
            if isinstance(widget, QWidget):
                widget.setStyleSheet(self.build_qss())
        except Exception as e:  # noqa
            logger.warning("应用皮肤 QSS 失败：%s", e)

    # ---------------- 背景资源（OpenGL UI 用） ----------------
    def background_resource(self) -> Optional[str]:
        """返回当前皮肤背景的绝对路径或 CSS 颜色（供 OpenGL 水波/液态玻璃层）。"""
        return self.get_skin().resolve(self.get_skin().background)

    def shader_resource(self) -> Optional[str]:
        return self.get_skin().resolve(self.get_skin().shader)

    # ---------------- 壁纸导入（图片/视频/网页/壁纸引擎/链接） ----------------
    @property
    def _importer(self) -> "WallpaperImporter":
        from core.wallpaper_import import WallpaperImporter
        if not hasattr(self, "_imp"):
            self._imp = WallpaperImporter()
        return self._imp

    def _finish_import(self, key: str) -> None:
        """导入后刷新注册表，并自动激活新皮肤（用户导入即想看到）。"""
        self.reload()
        if key and key in self._all:
            self.set_active(key)

    def import_image(self, path, name: str | None = None, copy: bool = True) -> str:
        """导入图片壁纸（默认拷贝到 data/skins/assets）。"""
        key = self._importer.import_image(path, name, copy)
        self._finish_import(key)
        return key

    def import_video(self, path, name: str | None = None, copy: bool = True) -> str:
        """导入视频/动态壁纸（默认拷贝）。大文件建议 copy=False 引用原路径。"""
        key = self._importer.import_video(path, name, copy)
        self._finish_import(key)
        return key

    def import_web(self, url: str, name: str | None = None) -> str:
        """导入动态网页壁纸（URL，由 QWebEngineView 渲染到纹理）。"""
        key = self._importer.import_web(url, name)
        self._finish_import(key)
        return key

    def import_wallpaper_folder(self, folder, name: str | None = None,
                                copy: bool = False) -> str:
        """导入壁纸引擎工程文件夹（默认引用不拷贝，避免大文件重复）。"""
        key = self._importer.import_wallpaper_folder(folder, name, copy)
        self._finish_import(key)
        return key

    def import_from_link(self, url: str, name: str | None = None) -> str:
        """从链接导入：壁纸引擎 Steam 创意工坊链接/ID、直链 zip/图片/视频、或网页。"""
        key = self._importer.import_from_link(url, name)
        self._finish_import(key)
        return key

    def remove_skin(self, key: str) -> bool:
        """删除导入的皮肤插件（仅删 generated 插件目录）。"""
        ok = self._importer.remove(key)
        if ok and self._active_key == key:
            self.set_active("default")
        self.reload()
        return ok

    # ---------------- 渲染描述（供 OpenGL 背景层 Pass1 消费） ----------------
    def render_descriptor(self) -> dict:
        """当前激活皮肤的统一渲染描述 + 背景适配模式。"""
        d = self.get_skin().render_descriptor()
        ui = self._user_config.ui
        d["background_fit"] = (ui.get("background_fit")
                               or ui.get("background_mode") or "cover")
        d["active_key"] = self._active_key
        return d

    def background_fit(self) -> str:
        ui = self._user_config.ui
        return ui.get("background_fit") or ui.get("background_mode") or "cover"

    def set_background_fit(self, mode: str) -> None:
        ui = self._user_config.data.setdefault("ui", {})
        ui["background_fit"] = mode
        self._user_config.save()
