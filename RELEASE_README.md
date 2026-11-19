# Radio Intelligence · 发布包

本目录是从 `D:\MusicAI` 整理出的**可完整运行**副本，源目录未被修改。

## 结构
- `plugins/` — 插件市场（启动时扫描）
  - `sources/` — 音源后端：local（第三方音源以插件形式由用户自行添加）
  - `frontend/` — 第三方音源前端片段（用户自行添加）
  - `tts/`, `skins/`, `ui/` — 其它插件槽
- `models/` — 模型目录（安装器按需联网下载 Qwen3-TTS / CLAP / LLM，不随包预置）
- `venv/` — 安装时创建；`start_radio.bat` 为入口

## 已排除
- 全部 `verify_*` / `probe_*` / `_*` 研究脚本、测试、训练、数据集
- legacy UI（`webapi/static/index.html*`）——仅保留新 UI 需要的 live 资源
- 本地歌曲文件（位于本机 `D:\Music` / `D:\CloudMusic`，不随包分发）

## 启动
双击 `start_radio.bat`，浏览 http://127.0.0.1:8765/ 。
每次启动会把插件与模型清单写入 `data_runtime/release_catalog.json`。
