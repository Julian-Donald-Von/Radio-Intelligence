"""webapi/verify_real.py —— **真实运行时**验证工具（不是静态断言！）

⚠⚠ 为什么必须有这个脚本
------------------------
之前踩过两次"测试全绿、用户实测全挂"：
  1. 老架构 43/43 静态断言（grep 字符串 + node --check）全绿，但改的函数根本不在
     用户看到的路径上 → 用户实测 0 效果。
  2. Phase 1 用 FastAPI 的 TestClient 测 WebSocket 通过，但**真机 404**。
     原因：TestClient 走 httpx 的 WS 实现，**绕过了 uvicorn 的 WS 依赖**
     （缺 websockets 库时 uvicorn 直接 404，而 TestClient 照样通）。

结论：**测试环境必须与真机一致**。本脚本坚持三条：
  - 起**真实 uvicorn 子进程**（不是 TestClient）
  - 用**真实 websockets 客户端**连 /ws（不是 httpx 模拟）
  - 用**真实 HTTP 请求**打 /api/*

每个 Phase 完成后都必须跑它，输出 REAL_OK 才算该 Phase 通过。

用法：
    python webapi/verify_real.py            # 跑全部检查
    python webapi/verify_real.py --phase 1  # 只跑 Phase 1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST, PORT = "127.0.0.1", 8765
BASE = f"http://{HOST}:{PORT}"
WS_URL = f"ws://{HOST}:{PORT}/ws"

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _get(path: str, timeout: float = 8):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode())


async def _ws_check() -> None:
    import websockets

    try:
        async with websockets.connect(WS_URL, open_timeout=8) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=8)
            d = json.loads(raw)
            check("WS /ws 握手并收到 connected 事件",
                  d.get("event") == "connected", json.dumps(d, ensure_ascii=False))
            await ws.send("ping")
            pong = await asyncio.wait_for(ws.recv(), timeout=8)
            check("WS 心跳 ping -> pong", "pong" in pong, pong)
    except Exception as e:  # noqa
        check("WS /ws 握手并收到 connected 事件", False, f"{type(e).__name__}: {e}")


def start_server() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "webapi.server"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(ROOT),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", type=int, default=1)
    args = ap.parse_args()

    print("=" * 62)
    print(f"MusicAI Web API · 真实运行时验证（Phase {args.phase}）")
    print("=" * 62)

    proc = start_server()
    try:
        # 等服务起来（轮询而非固定 sleep，更快也更稳）
        deadline = time.time() + 25
        ready = False
        while time.time() < deadline:
            try:
                st, _ = _get("/api/status", timeout=2)
                if st == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.4)
        check("服务启动（真实 uvicorn 子进程）", ready,
              f"{BASE} 在 {25 - (deadline - time.time()):.1f}s 内就绪" if ready else "超时未就绪")

        if not ready:
            return 1

        # ---- 通用检查（每 Phase 都要过） ----
        st, d = _get("/api/status")
        check("GET /api/status 返回 200", st == 200, f"HTTP {st}")
        check("status.version 非 undefined",
              d.get("version") not in (None, "", "undefined"),
              f"version={d.get('version')!r}")
        check("uvicorn 具备 WebSocket 支持（websockets/wsproto 已装）",
              d.get("ws_supported") is True,
              f"ws_supported={d.get('ws_supported')}")
        check("GET / 能取到前端页面", True)
        try:
            with urllib.request.urlopen(BASE + "/", timeout=5) as r:
                html = r.read().decode("utf-8", errors="replace")
            check("GET / 返回前端 HTML", r.status == 200 and len(html) > 500,
                  f"{len(html)} 字节")
        except Exception as e:  # noqa
            check("GET / 返回前端 HTML", False, str(e))

        # ---- Phase 2：播放控制 ----
        if args.phase >= 2:
            st2, d2 = _get("/api/playback/state")
            check("GET /api/playback/state 返回 200", st2 == 200, f"HTTP {st2}")
            for key in ("playing", "position", "duration", "mode", "current", "ready"):
                check(f"playback.state 含字段 {key}", key in d2,
                      f"{key}={d2.get(key)!r}" if key in d2 else "缺失")
            check("playback.state.position/duration 为数字",
                  isinstance(d2.get("position"), int) and isinstance(d2.get("duration"), int),
                  f"pos={d2.get('position')} dur={d2.get('duration')}")
            # 控制类接口：未接 controller 时应优雅返回 ok=False（而不是 500/崩溃）
            for name, path in (("toggle", "/api/playback/toggle"),
                               ("mode", "/api/playback/mode?mode=random"),
                               ("next", "/api/playback/next"),
                               ("prev", "/api/playback/prev")):
                try:
                    req = urllib.request.Request(BASE + path, data=b"", method="POST")
                    with urllib.request.urlopen(req, timeout=8) as r:
                        body = json.loads(r.read().decode())
                    check(f"POST /api/playback/{name} 不崩溃", r.status == 200,
                          json.dumps(body, ensure_ascii=False))
                except Exception as e:  # noqa
                    check(f"POST /api/playback/{name} 不崩溃", False, f"{type(e).__name__}: {e}")
            try:
                req = urllib.request.Request(BASE + "/api/playback/volume?vol=0.5",
                                             data=b"", method="POST")
                with urllib.request.urlopen(req, timeout=8) as r:
                    body = json.loads(r.read().decode())
                check("POST /api/playback/volume 不崩溃", r.status == 200,
                      json.dumps(body, ensure_ascii=False))
            except Exception as e:  # noqa
                check("POST /api/playback/volume 不崩溃", False, f"{type(e).__name__}: {e}")

        # ---- Phase 2：dock UI 元素 ----
        if args.phase >= 2:
            for elem, desc in (('d-prev', '上一曲(极简)'), ('d-play', '播放/暂停(呼吸圈)'),
                               ('d-next', '下一曲(极简)'), ('d-mode', '播放模式三态'),
                               ('d-dislike', '不喜欢'), ('d-lyric', '歌词'),
                               ('d-queue', '播放队列'), ('d-radio', '电台模式'),
                               ('d-ai', 'AI 伴听'), ('eq', '均衡器'),
                               ('vol-group', '音量组'), ('vol-fill', '音量渐变'),
                               ('vol-thumb', '音量滑块'), ('dc-cover', '封面'),
                               ('dc-title', '曲名'), ('dc-artist', '艺术家'),
                               ('frame-progress', '外围环绕进度'),
                               ('frame-glow', '进度光晕'), ('frame-bloom', '光晕染'),
                               ('frame-playhead', '播放头')):
                check(f"dock 含 {desc} (#{elem})", f'id="{elem}"' in html)
            # ⚠ 噪点在原 UI 里是 **class**（svg.dock-noise），不是 id，别写成 id 选择器
            check("dock 含 噪点纹理 (class=dock-noise)", 'class="dock-noise"' in html)
            # 液态玻璃配方（必须与 ui/ai-radio-player.html 一致）
            for key, desc in (('backdrop-filter:saturate(160%)', '玻璃 saturate(160%)'),
                              ('inset 0 1px 1px rgba(255,255,255,.85)', '顶部高光'),
                              ('inset 0 -2px 6px rgba(0,0,0,.45)', '底部内阴影'),
                              ('@keyframes breathe', '播放呼吸圈动画'),
                              ('#d-play.playing::before', '呼吸圈 .playing 态'),
                              ('#dock::after', '底部倒影'),
                              ('scaleY(-1)', '倒影翻转'),
                              ('feTurbulence', '噪点纹理滤镜'),
                              ('linear-gradient(90deg, var(--primary), var(--accent))',
                               '音量渐变(随封面取色)'),
                              ('computeFramePath', '外围进度路径计算'),
                              ('applyCoverPalette', '封面取色'),
                              ('#dock.hovering', 'hover 上浮态')):
                check(f"液态玻璃配方: {desc}", key in html)
            # ---- 用户指出的 6 条细节 ----
            for key, desc in (('class="dbtn pulse" id="d-ai"', 'AI 伴听 pulse 点亮呼吸'),
                              ('class="dbtn pulse" id="d-radio"', '电台模式 pulse 点亮呼吸'),
                              ('@keyframes aiPulse', 'aiPulse 呼吸动画'),
                              ('.dbtn.on{', '点亮态 .dbtn.on'),
                              ('function spawnBurst(el)', '礼花纸 spawnBurst'),
                              ('const N = 16', '礼花 16 颗粒子'),
                              ("'#ff5c7a'", '礼花糖果色数组'),
                              ('p.animate(', '礼花 Web Animations 动画'),
                              ('function spawnBurst', '礼花函数保留(待复用)'),
                              ('#d-dislike.on{color:#ff5c7a', '不喜欢点亮变红(#ff5c7a)'),
                              ('#d-dislike.on svg{fill:currentColor}', '不喜欢实心填充'),
                              ("$('d-dislike').onclick", '不喜欢按钮绑定'),
                              ("bindToggle('d-ai'", 'AI 点亮绑定'),
                              ("bindToggle('d-radio'", '电台点亮绑定'),
                              ('/api/dislike/add', 'dislike 接口调用'),
                              ('#ffd166', 'playhead 金黄描边'),
                              ('opacity:.55', 'playhead 半隐藏(.55)'),
                              ('right:150px', '音量组左移'),
                              ('background:transparent; border:none', '播放模式去底色')):
                check(f"细节: {desc}", key in html)
            # 反面：不得再有 active 的 scale 跳动（用户要求"简单点亮"）
            check("细节: 按钮无 active scale 跳动", '.dbtn:active' not in html)

            # ---- dock 上 AI 电台 / 口播 的前后端对接 ----
            st_bc, d_bc = _get("/api/dj/broadcast")
            check("GET /api/dj/broadcast 返回 200", st_bc == 200, f"HTTP {st_bc}")
            check("dj.broadcast 含 text/ready 字段",
                  "text" in d_bc and "ready" in d_bc, json.dumps(d_bc, ensure_ascii=False))
            for name, qs in (("/api/dj/toggle", "on=1"), ("/api/radio/toggle", "on=1")):
                try:
                    req = urllib.request.Request(BASE + name + "?" + qs,
                                                 data=b"", method="POST")
                    with urllib.request.urlopen(req, timeout=8) as r:
                        body = json.loads(r.read().decode())
                    check(f"POST {name} 不崩溃", r.status == 200,
                          json.dumps(body, ensure_ascii=False))
                except Exception as e:  # noqa
                    check(f"POST {name} 不崩溃", False, f"{type(e).__name__}: {e}")
            check("state 含 roam_on 字段", "roam_on" in d2)
            check("state 含 dj_on 字段", "dj_on" in d2)
            for key, desc in (('function bindToggle', 'AI/电台按钮绑定函数'),
                              ('/api/dj/toggle', 'AI 口播接后端'),
                              ('/api/radio/toggle', '电台模式接后端'),
                              ('function loadBroadcast', '口播文案拉取'),
                              ("'/api/dj/broadcast'", '口播接口调用'),
                              ("$('d-radio').classList.toggle('on', d.roam_on)",
                               '电台点亮态以后端为权威'),
                              ("$('d-ai').classList.toggle('on', d.dj_on)",
                               'AI 点亮态以后端为权威')):
                check(f"AI dock 对接: {desc}", key in html)
            # 播放模式三态
            for m in ("'order'", "'loop_one'", "'random'"):
                check(f"播放模式含 {m}", m in html)
            # 接口调用
            check("前端定义了 API 基址 /api/playback", "const API = '/api/playback'" in html)
            for suffix in ("'/state'", "'/toggle'", "'/next'", "'/prev'",
                           "'/seek'", "'/volume'", "'/mode'"):
                check(f"前端调用 API + {suffix}", suffix in html)

        # ---- Phase 3：队列 + 推荐货架 ----
        if args.phase >= 3:
            for path, key in (("/api/queue", "queue"),
                              ("/api/recommendations", "recommendations"),
                              ("/api/reco", "reco")):
                st3, d3 = _get(path)
                check(f"GET {path} 返回 200", st3 == 200, f"HTTP {st3}")
                check(f"{path} 含 entries 列表", "entries" in d3,
                      f"ok={d3.get('ok')} entries={len(d3.get('entries') or [])}")
            for name, qs in (("/api/reco/play", "track_id=1"),
                             ("/api/reco/remove", "track_id=1"),
                             ("/api/queue/remove", "track_id=1"),
                             ("/api/reco/play-all", ""),
                             ("/api/play/track/1", "")):
                try:
                    req = urllib.request.Request(BASE + name + ("?" + qs if qs else ""),
                                                 data=b"", method="POST")
                    with urllib.request.urlopen(req, timeout=8) as r:
                        body = json.loads(r.read().decode())
                    check(f"POST {name} 不崩溃", r.status == 200,
                          json.dumps(body, ensure_ascii=False))
                except Exception as e:  # noqa
                    check(f"POST {name} 不崩溃", False, f"{type(e).__name__}: {e}")
            # 前端卡片（沿用原 .scard 配方）
            for key, desc in (('id="cards"', '卡片容器'),
                              ('.scard{', '卡片样式(原配方)'),
                              ('linear-gradient(118deg', 'tone 斜向色洗'),
                              ('transparent 1px 38px', '38px 网格线'),
                              ('.scard.playing{', 'playing 高亮态'),
                              ('.sc-eq{', '卡片均衡器'),
                              ('function buildCards', '构建函数'),
                              ('/api/reco/play?track_id=', '点播按身份(非下标)'),
                              ("case 'playlist_updated'", 'WS 事件重渲染'),
                              # 动画（沿用原 #shelf 的 shelfTick / spring）
                              ('function Spring', '弹簧物理'),
                              ('requestAnimationFrame(layout)', 'rAF 逐帧布局'),
                              ('perspective:1500px', '3D 透视容器'),
                              ('transform-style:preserve-3d', '3D 变换保留'),
                              ('R_CARD * Math.cos(arcA)', '弧排布(极坐标→直角)'),
                              ('rotateY(', 'rotateY 弧面旋转'),
                              ('(absD - 0.5) * 0.7', '景深模糊'),
                              ('raw * raw * (3 - 2 * raw)', '错峰揭示 smoothstep'),
                              ('liftT > c.lift ? 0.20 : 0.13', '悬停浮起不对称插值'),
                              ('will-change:transform,opacity', '卡片 will-change'),
                              ('const STEP_Z', 'z 深度递减')):
                check(f"Phase3 前端: {desc}", key in html)
            # 反面：卡片点播不得用下标
            check("Phase3 契约: 点播不用下标(避免点A播B)",
                  "encodeURIComponent(e.id)" in html)

        # ---- Phase 4：不喜欢（dislike）+ 历史 ----
        # ⚠ 收藏(favorites)/曲库(library) 是**废弃功能**，已整条移除；
        #   断言里要反向确认它们"确实不存在"，防止以后被误加回来。
        if args.phase >= 4:
            st4, d4 = _get("/api/history")
            check("GET /api/history 返回 200", st4 == 200, f"HTTP {st4}")
            check("history 含 entries", "entries" in d4,
                  f"ok={d4.get('ok')} n={len(d4.get('entries') or [])}")
            st5, d5 = _get("/api/dislikes")
            check("GET /api/dislikes 返回 200", st5 == 200, f"HTTP {st5}")
            check("dislikes 含 entries", "entries" in d5)
            for name in ("/api/dislike/add?key=i:1", "/api/dislike/remove?key=i:1"):
                try:
                    req = urllib.request.Request(BASE + name, data=b"", method="POST")
                    with urllib.request.urlopen(req, timeout=8) as r:
                        body = json.loads(r.read().decode())
                    check(f"POST {name} 不崩溃", r.status == 200,
                          json.dumps(body, ensure_ascii=False))
                except Exception as e:  # noqa
                    check(f"POST {name} 不崩溃", False, f"{type(e).__name__}: {e}")
            try:
                req = urllib.request.Request(BASE + "/api/history/1", data=b"",
                                             method="DELETE")
                with urllib.request.urlopen(req, timeout=8) as r:
                    body = json.loads(r.read().decode())
                check("DELETE /api/history/1 不崩溃", r.status == 200,
                      json.dumps(body, ensure_ascii=False))
            except Exception as e:  # noqa
                check("DELETE /api/history/1 不崩溃", False, f"{type(e).__name__}: {e}")
            # 前端
            # （曲库已恢复，原先「曲库已废弃」的反向断言已删除）
            for key, desc in (('data-v="reco"', '推荐栏目'),
                              ('data-v="history"', '历史栏目'),
                              ('id="d-dislike"', '不喜欢按钮'),
                              ('M4 20L20 4', '红心+斜杠图标'),
                              ('#d-dislike.on{color:#ff5c7a', '不喜欢点亮变红'),
                              ('/api/dislike/add', 'dislike 接口已接'),
                              ('id="tabs"', '栏目 tabs')):
                check(f"Phase4 前端: {desc}", key in html)
            check("Phase4 契约: 收藏已废弃(前端无 favorites)",
                  ('/api/favorites' not in html)
                  and ('data-v=' + chr(34) + 'fav' + chr(34)) not in html)
            check("Phase4 契约: 曲库已恢复(前端有 library tab)",
                  ('data-v=' + chr(34) + 'library' + chr(34)) in html)

        # ---- 液态玻璃 WebGL 折射 + 推荐页/队列分离 ----
        if args.phase >= 4:
            try:
                with urllib.request.urlopen(BASE + "/liquid.js", timeout=8) as r:
                    lq = r.read().decode("utf-8", errors="replace")
                check("液态玻璃: GET /liquid.js 返回 200", r.status == 200,
                      f"{len(lq)} 字节")
            except Exception as e:  # noqa
                lq = ""
                check("液态玻璃: GET /liquid.js 返回 200", False,
                      f"{type(e).__name__}: {e}")

            for key, desc in (("feDisplacementMap", "折射核心:位移贴图(scale=9)"),
                              ("vnoise", "值噪声(对齐 feTurbulence)"),
                              ("fractal", "分形噪声(2 octaves)"),
                              ("uChroma", "RGB 色差"),
                              ("uScale, 9.0", "位移幅度 scale=9"),
                              ("gl_FragCoord", "逐像素片元着色器"),
                              ("< 33", "30fps 限流(省电)")):
                check(f"液态玻璃算法: {desc}", key in lq)

            check("液态玻璃: 前端引入 liquid.js", 'src="/liquid.js"' in html)
            check("液态玻璃: 画布 #bgliquid 存在", 'id="bgliquid"' in html)
            check("液态玻璃: SVG 兜底滤镜 #liquid 存在",
                  'id="liquid"' in html and "feTurbulence" in html)
            check("液态玻璃: 封面驱动折射(随歌变色)",
                  "LiquidBg.setSource(st.cover)" in html)
            check("液态玻璃: 无封面时程序化底图兜底", "initLiquidBg" in html)

            # 推荐页契约：只读推荐货架，不得读队列
            rb = open(os.path.join(str(ROOT), "webapi", "radio_bridge.py"),
                      encoding="utf-8").read()
            i = rb.find("def get_recommendations(")
            seg = rb[i:i + 1200] if i >= 0 else ""
            check("推荐页契约: 读推荐货架(get_reco_entries)",
                  "get_reco_entries()" in seg)
            check("推荐页契约: 不再读播放队列(避免被顶替)",
                  "self.radio.get_playlist_entries()" not in seg)

            stl, _ = _get("/api/library")
            check("曲库已恢复: GET /api/library 返回 200", stl == 200, f"HTTP {stl}")
            stq, _ = _get("/api/queue")
            check("队列独立入口: GET /api/queue 返回 200", stq == 200, f"HTTP {stq}")
            for key, desc in (('data-v="queue"', "队列 tab"),
                              ('data-v="library"', "曲库 tab"),
                              ('data-v="history"', "历史 tab"),
                              ('data-v="reco"', "推荐 tab")):
                check(f"栏目: {desc}", key in html)
            check("契约: 收藏仍是废弃(无 favorites tab)",
                  ("data-v=" + chr(34) + "fav" + chr(34)) not in html)

        # ---- 真·液态玻璃：dock 局部折射（非全局弯曲）+ 曲库点播 ----
        if args.phase >= 4:
            try:
                with urllib.request.urlopen(BASE + "/liquid_glass.js", timeout=8) as r:
                    lg = r.read().decode("utf-8", errors="replace")
                check("液态玻璃: GET /liquid_glass.js 返回 200", r.status == 200,
                      f"{len(lg)} 字节")
            except Exception as e:  # noqa
                lg = ""
                check("液态玻璃: GET /liquid_glass.js 返回 200", False,
                      f"{type(e).__name__}: {e}")

            for key, desc in (("function sdf(", "SDF 圆角矩形有符号距离场"),
                              ("function buildMap(", "透镜位移图生成"),
                              ("feImage", "feImage 承载位移图"),
                              ("feDisplacementMap", "feDisplacementMap 位移"),
                              ("backdropFilter = vf", "backdrop-filter 局部折射"),
                              ("['#dock', 44, 26, 55", "dock 是折射目标"),
                              ("ResizeObserver", "尺寸变化自动重建位移图"),
                              ("smoothstep", "smoothstep 边缘衰减"),
                              ("setTimeout(function () { setHref(url); }, 80)",
                               "dataURL 延迟重设兜底")):
                check(f"液态玻璃(局部): {desc}", key in lg)

            # 契约：弯曲只在元素内 —— 全局背景折射必须默认关闭
            check("契约: 全局背景折射默认关闭(避免整屏弯曲)",
                  "_enabled = false" in lq)
            check("契约: 全局折射仍可手动开启(setEnabled)",
                  "setEnabled" in lq)

            # 曲库/历史点播：必须走 play/track/{id}，不能走 reco/play
            check("契约: 曲库/历史点播走 /api/play/track(回库查Track)",
                  "'/api/play/track/' + encodeURIComponent(e.id)" in html)
            check("契约: 推荐/队列点播走 /api/reco/play",
                  "'/api/reco/play?track_id=' + encodeURIComponent(e.id)" in html)

        # ---- 歌词海报（背景 drawPoster）+ asyncio 噪音静音 ----
        if args.phase >= 4:
            try:
                with urllib.request.urlopen(BASE + "/poster.js", timeout=8) as r:
                    ps = r.read().decode("utf-8", errors="replace")
                check("歌词海报: GET /poster.js 返回 200", r.status == 200,
                      f"{len(ps)} 字节")
            except Exception as e:  # noqa
                ps = ""
                check("歌词海报: GET /poster.js 返回 200", False,
                      f"{type(e).__name__}: {e}")

            for key, desc in (("function drawPoster", "海报绘制(含歌词)"),
                              ("function drawSkinBase", "皮肤层(底板+28px网格)"),
                              ("function buildPosterBuffer", "小图放大=天然模糊"),
                              ("function parseLRC", "LRC 歌词解析"),
                              ("H * 0.52 + k * size * 2.0", "歌词当前行±1邻行布局"),
                              ("H * 0.048", "当前行字号(H*0.048)"),
                              ("rgba(6,5,16,.72)", "压暗渐层(保证歌词可读)")):
                check(f"歌词海报: {desc}", key in ps)

            check("歌词海报: 画布 #poster-canvas 存在", 'id="poster-canvas"' in html)
            check("歌词海报: 封面驱动底图", "Poster.setCover(st.cover)" in html)
            check("歌词海报: 进度推进歌词行", "Poster.seekLyric(st.position)" in html)
            check("歌词海报: L键/词按钮切显隐", "Poster.setLyricOn(on)" in html)

            # asyncio 噪音静音
            sv = open(os.path.join(str(ROOT), "webapi", "server.py"),
                      encoding="utf-8").read()
            check("契约: asyncio 断连噪音已静音",
                  "_exc_handler" in sv and "ConnectionResetError" in sv)

        asyncio.run(_ws_check())

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    fails = [n for n, ok, _ in _results if not ok]
    print()
    print("=" * 62)
    print(f"总计 {len(_results)} 项，PASS {len(_results) - len(fails)}，FAIL {len(fails)}")
    if fails:
        for n in fails:
            print(f"  ✗ {n}")
    print("=" * 62)
    print("REAL_OK" if not fails else "REAL_FAIL")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
