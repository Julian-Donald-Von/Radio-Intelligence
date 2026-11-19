"""
壁纸导入器（MusicAI 皮肤系统扩展）。

把「图片 / 视频 / 动态网页 / 壁纸引擎工程 / 壁纸引擎链接」导入为可被
SkinManager 自动发现的皮肤插件，写到 plugins/skins/<key>/skin.py。
渲染层（依《重构.md》的 OpenGL 背景层 Pass1）通过 Skin.render_descriptor()
拿到统一的媒体描述（type/path/url/loop/mute/volume…）。

设计要点：
- 壁纸引擎文件夹默认「引用」不拷贝（视频壁纸动辄上百 MB，避免重复占盘）；
  单文件图片/视频导入默认拷贝到 data/skins/assets/ 便于管理。
- 生成的皮肤插件只是 tiny 的子类：MediaSkin（填 kind+content）或
  WallpaperEngineSkin（填 source_dir），真正的解析在 core/skin 内完成。
- 所有失败都抛清晰异常，由调用方（UI）提示用户，不静默吞掉。

依赖：仅标准库（json/re/urllib/...），不引入重依赖，保持插件框架轻盈。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import urllib.request
from pathlib import Path

from core.plugin import PLUGIN_DIR, PROJECT_ROOT

logger = logging.getLogger("MusicAI.WallpaperImport")

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")
VIDEO_EXT = (".mp4", ".webm", ".mov", ".mkv", ".avi")
MEDIA_EXT = IMAGE_EXT + VIDEO_EXT

STEAM_APP_ID = "431960"  # Wallpaper Engine 的 Steam appid


class WallpaperImporter:
    def __init__(self) -> None:
        self.skins_dir = PLUGIN_DIR / "skins"
        self.assets_dir = PROJECT_ROOT / "data" / "skins" / "assets"
        self.skins_dir.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self._tmp = PROJECT_ROOT / "data" / "skins" / "_tmp"
        self._tmp.mkdir(parents=True, exist_ok=True)

    # ---------------- 工具 ----------------
    @staticmethod
    def _safe_key(name: str, seed: str = "") -> str:
        base = re.sub(r"[^\w\u4e00-\u9fff-]", "_", name or "skin").strip("_")[:40] or "skin"
        h = hashlib.sha1((name + seed).encode("utf-8")).hexdigest()[:6]
        return f"{base}_{h}"

    def _unique_dir(self, key: str) -> Path:
        d = self.skins_dir / key
        i = 1
        while d.exists():
            d = self.skins_dir / f"{key}_{i}"
            i += 1
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _emit(skin_dir: Path, key: str, name: str, desc: str,
              cls_import: str, cls_name: str, body: list[str]) -> None:
        """写入 plugins/skins/<key>/skin.py。

        注意：嵌入到 .py 源码的字面量必须用 Python repr()（产出 True/False/None、
        合法字符串），不能用 json.dumps()（产出 true/false/null 在 Python 中非法）。
        body 为类体内的属性行（值已用 repr 编码）。
        """
        header = (
            "# 自动生成（MusicAI 壁纸导入）。删除请用 SkinManager.remove_skin()。\n"
            f"{cls_import}\n"
            f"class Skin({cls_name}):\n"
        )
        lines = [
            f"    plugin_key = {key!r}",
            f"    plugin_name = {name!r}",
            f"    plugin_description = {desc!r}",
        ] + [f"    {b}" for b in body]
        (skin_dir / "skin.py").write_text(
            header + "\n".join(lines) + "\n", encoding="utf-8")

    # ---------------- 解析壁纸引擎工程 ----------------
    @staticmethod
    def parse_project(folder: str | Path) -> dict:
        f = Path(folder) / "project.json"
        if not f.exists():
            raise FileNotFoundError(f"找不到 project.json：{folder}")
        return json.loads(f.read_text(encoding="utf-8", errors="ignore"))

    # ---------------- Steam workshop 定位 ----------------
    def locate_steam_workshop(self, workshop_id: str) -> Path | None:
        """定位本地已订阅/下载的壁纸引擎工程目录。

        依次检查：
          - 默认 Steam 库 ProgramFiles(x86)/Steam/.../content/431960/<id>
          - 通过 libraryfolders.vdf 解析的其他 Steam 库
        返回含 project.json 的目录，找不到返回 None。
        """
        wid = str(workshop_id).strip()
        candidates: list[Path] = []
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(env)
            if base:
                candidates.append(
                    Path(base) / "Steam" / "steamapps" / "workshop"
                    / "content" / STEAM_APP_ID / wid)
        # 解析 libraryfolders.vdf 找额外库
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(env)
            if not base:
                continue
            vdf = Path(base) / "Steam" / "steamapps" / "libraryfolders.vdf"
            if vdf.exists():
                text = vdf.read_text(encoding="utf-8", errors="ignore")
                for m in re.finditer(r'"path"\s+"([^"]+)"', text):
                    candidates.append(
                        Path(m.group(1)) / "steamapps" / "workshop"
                        / "content" / STEAM_APP_ID / wid)
        for c in candidates:
            if (c / "project.json").exists():
                return c
        return None

    # ---------------- 下载 ----------------
    @staticmethod
    def _download(url: str, dest: Path, timeout: int = 60) -> None:
        req = urllib.request.Request(url, headers={"User-Agent": "MusicAI/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)

    # ---------------- 导入：单文件媒体 ----------------
    def _import_media_file(self, src: Path, name: str | None, copy: bool,
                           kind: str) -> str:
        key = self._safe_key(name or src.stem, seed=str(src))
        dest_path = str(src)
        if copy:
            target = self.assets_dir / f"{key}{src.suffix.lower()}"
            shutil.copy2(src, target)
            dest_path = str(target.resolve())
        skin_dir = self._unique_dir(key)
        content = {"path": Path(dest_path).as_posix()}
        body = [f'kind = "{kind}"', f"content = {content!r}"]
        self._emit(skin_dir, key, name or src.stem,
                   f"导入的{kind}壁纸：{src.name}",
                   "from core.skin import MediaSkin", "MediaSkin", body)
        return key

    def import_image(self, path: str | Path, name: str | None = None,
                     copy: bool = True) -> str:
        src = Path(path)
        if not src.exists():
            raise FileNotFoundError(f"图片不存在：{path}")
        return self._import_media_file(src, name, copy, "image")

    def import_video(self, path: str | Path, name: str | None = None,
                     copy: bool = True) -> str:
        src = Path(path)
        if not src.exists():
            raise FileNotFoundError(f"视频不存在：{path}")
        return self._import_media_file(src, name, copy, "video")

    # ---------------- 导入：动态网页 ----------------
    def import_web(self, url: str, name: str | None = None) -> str:
        if not re.match(r"^https?://", url, re.I):
            raise ValueError(f"不是合法网页 URL：{url}")
        key = self._safe_key(name or url, seed=url)
        skin_dir = self._unique_dir(key)
        content = {"url": url, "extra": {"interactive": True}}
        body = [f'kind = "web"', f"content = {content!r}"]
        self._emit(skin_dir, key, name or url,
                   f"动态网页壁纸：{url}",
                   "from core.skin import MediaSkin", "MediaSkin", body)
        return key

    # ---------------- 导入：壁纸引擎文件夹 ----------------
    def import_wallpaper_folder(self, folder: str | Path, name: str | None = None,
                                copy: bool = False) -> str:
        folder = Path(folder)
        proj = self.parse_project(folder)  # 校验 project.json 存在
        title = (proj.get("title")
                 or (proj.get("titleLocalized") or {}).get("en")
                 or folder.name)
        key = self._safe_key(name or title, seed=str(folder))
        skin_dir = self._unique_dir(key)
        if copy:
            # 拷贝整个文件夹到 assets（大文件，慎用）
            target = self.assets_dir / key
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(folder, target)
            src_dir = str(target.resolve())
        else:
            src_dir = str(folder.resolve())
        body = [f"source_dir = {src_dir!r}"]
        self._emit(skin_dir, key, name or title,
                   f"壁纸引擎工程：{title}",
                   "from core.skin import WallpaperEngineSkin",
                   "WallpaperEngineSkin", body)
        return key

    # ---------------- 导入：链接（壁纸引擎链接 / 直链 / 网页） ----------------
    def import_from_link(self, url: str, name: str | None = None) -> str:
        url = url.strip()
        # 1) Steam workshop 链接或纯 ID
        m = re.search(r"steamcommunity\.com/(?:sharedfiles/filedetails|workshop/"
                      r"filedetails)\?.*?id=(\d+)", url, re.I)
        if not m:
            m = re.search(r"[?&]id=(\d+)", url) if "steam" in url.lower() else None
        if not m:
            m = re.fullmatch(r"\d{4,}", url)
            wid = m.group(0) if m else None
        else:
            wid = m.group(1)
        if wid:
            folder = self.locate_steam_workshop(wid)
            if folder is None:
                raise FileNotFoundError(
                    f"未在本地 Steam 库找到壁纸引擎订阅内容（id={wid}）。\n"
                    f"请先在 Steam 创意工坊订阅并等待下载完成，"
                    f"或把工程文件夹直接拖入『导入文件夹』。")
            return self.import_wallpaper_folder(folder, name)

        # 2) 直接 .zip（壁纸引擎导出 / 打包）
        if url.lower().endswith(".zip"):
            return self._import_zip(url, name)

        # 3) 直链媒体（图片/视频）
        low = url.lower().split("?")[0]
        if low.endswith(IMAGE_EXT):
            dest = self._tmp / self._safe_key(name or url, url) + Path(url).suffix.lower()
            self._download(url, dest)
            return self.import_image(dest, name, copy=True)
        if low.endswith(VIDEO_EXT):
            dest = self._tmp / self._safe_key(name or url, url) + Path(url).suffix.lower()
            self._download(url, dest)
            return self.import_video(dest, name, copy=True)

        # 4) 其它 http(s) -> 动态网页
        return self.import_web(url, name)

    def _import_zip(self, url: str, name: str | None) -> str:
        import zipfile
        dest = self._tmp / f"{self._safe_key(name or url, url)}.zip"
        self._download(url, dest)
        try:
            return self._import_zip_file(dest, name)
        finally:
            try:
                dest.unlink()
            except OSError:
                pass

    def _import_zip_file(self, zip_path: str | Path, name: str | None) -> str:
        """从**本地已有 zip 文件**导入壁纸引擎工程 / 单媒体包。

        与 _import_zip 的区别：跳过下载（浏览器上传的 zip 已落临时盘）。
        解包逻辑一致 —— 先找 project.json（scene→preview 图/video/web 场景
        由 WallpaperEngineSkin 解析），否则取首个媒体文件按扩展名导入。
        """
        import zipfile
        zpath = Path(zip_path)
        extract_dir = self._tmp / f"{self._safe_key(name or str(zpath), str(zpath) + '_x')}"
        if extract_dir.exists():
            shutil.rmtree(extract_dir, ignore_errors=True)
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zpath) as zf:
            zf.extractall(extract_dir)
        # 找 project.json
        for pj in sorted(extract_dir.rglob("project.json")):
            return self.import_wallpaper_folder(pj.parent, name, copy=False)
        # 无 project.json：取首个媒体文件按扩展名导入
        for ext in MEDIA_EXT:
            hits = sorted(extract_dir.rglob(f"*{ext}"))
            if hits:
                src = hits[0]
                return (self.import_image if ext in IMAGE_EXT else self.import_video)(
                    src, name, copy=True)
        raise ValueError(f"压缩包内未找到 project.json 或媒体文件：{zpath}")

    # ---------------- 删除 ----------------
    def remove(self, key: str) -> bool:
        """删除插件生成的皮肤目录（仅限 skins_dir 内，防路径穿越）。

        同时清理 data/skins/assets 下该 key 的媒体拷贝（import_image/video
        拷贝过去的 <key><ext> 文件），避免删了皮肤留下孤儿大文件。
        """
        skin_dir = (self.skins_dir / key).resolve()
        try:
            skin_dir.relative_to(self.skins_dir.resolve())
        except ValueError:
            logger.warning("拒绝删除 skins 目录外的路径：%s", key)
            return False
        removed = False
        if skin_dir.exists() and skin_dir.is_dir():
            shutil.rmtree(skin_dir)
            removed = True
        # 清理 assets 拷贝：<key>.<ext>（导入单文件媒体时拷贝到 assets 的命名）
        if self.assets_dir.exists():
            for f in self.assets_dir.glob(f"{key}.*"):
                try:
                    f.unlink()
                    removed = True
                except OSError as e:  # noqa
                    logger.warning("清理壁纸资源失败 %s: %s", f, e)
        return removed
