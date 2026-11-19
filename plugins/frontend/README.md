# 音源前端插件

每个在线音源的前端 UI 按插件独立成目录：
- `netease/` — 网易云（设置项、扫码登录、搜索过滤按钮）
- `qqmusic/` — QQ音乐（同上）

每个目录含 `manifest.json`（声明该音源的前端接入点）、`qr-login.js`（扫码登录模块副本）、`settings-fragment.txt`（设置 schema 原文片段）。

本地音源（local）没有独立前端插件：本地歌曲文件不随包分发，由用户在设置里指定本机目录。
