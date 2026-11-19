/* i18n.js — 新 UI 的中 / 英界面切换（2026-11-19）
 *
 * 设计取舍：
 *   · 只翻译“界面壳”：按钮、设置项、提示、效果名、toast、titlebar、弹层。
 *   · 歌曲标题/艺人/歌词/AI 生成的歌评与口播属于“内容”，不硬翻译，保留原文。
 *   · 不逐个改 runtime.js / 各 module 的源码：用 DOM 文本字典 + MutationObserver
 *     在渲染后翻译，切回中文时按记录恢复；运行时新插入的节点也会被接住。
 *   · 词典以外的长文本不会被误翻（这是刻意的，避免把歌名/乐评拆坏）。
 *
 * 词条可直接在 DICT 里补；支持属性 title / aria-label / placeholder / alt。
 */
(function () {
  'use strict';

  var KEY = 'r54.lang';
  var DICT = {
    /* ── 顶栏 / 主界面 ───────────────────────────────────────── */
    "Radio Intelligence · AI 智能电台": "Radio Intelligence · AI Radio",
    "RADIO · 漫游": "RADIO · ROAM",
    "星际漫游": "Cosmic Roam",
    "播放全部": "Play all",
    "全部": "All",
    "默认": "Default",
    "自动": "Auto",
    "歌词海报": "Lyrics poster",
    "EQ 开": "EQ on",
    "EQ 关": "EQ off",
    "播放列表": "Playlist",
    "播放历史": "Play history",
    "历史": "History",
    "曲库": "Library",
    "我的曲库": "My Library",
    "歌单中心": "Playlists",
    "LIBRARY · 曲库": "LIBRARY",
    "PLAYLISTS · 歌单": "PLAYLISTS",
    "队列为空": "Queue is empty",
    "曲库为空": "Library is empty",
    "暂无播放历史": "No play history",
    "从曲库删除": "Remove from library",
    "收藏已并入曲库页管理": "Favorites are managed in the Library tab",
    "低音增强": "Bass boost",
    "高音增强": "Treble boost",
    "人声": "Vocals",
    "播客": "Podcast",
    "流行": "Pop",
    "摇滚": "Rock",
    "嘻哈": "Hip-hop",
    "古典": "Classical",
    "响度 V": "Loudness V",
    "复位：全频段 0 dB，EQ 旁路零开销。": "Reset: all bands at 0 dB; EQ bypassed.",
    "提升 31–125 Hz，为薄扬声器 / 耳机补充低频分量。":
      "Boost 31–125 Hz for thin speakers / headphones.",
    "提升 4k–16k，增加空气感与亮度（老录音 / 闷耳机适用）。":
      "Boost 4k–16k for air and brightness (old recordings / dull headphones).",
    "削减 250 Hz 浑浊 + 提升 1k–4k 人声临场，让人声突出。":
      "Cut 250 Hz mud + lift 1k–4k presence for vocals.",
    "削减 31–62 Hz（等效高通）+ 提升 4k 清晰度，说话更清楚。":
      "Cut 31–62 Hz (high-pass) + lift 4k clarity for speech.",
    "微笑曲线：轻抬低频与高频、中频不动，广播向听感。":
      "Smile curve: gentle low/high lift, mids flat — broadcast feel.",
    "低频冲击 + 4k/8k 镲片亮度，适合电吉他素材。":
      "Low impact + 4k/8k cymbal brightness; suits electric guitar.",
    "重 sub-bass（31/62），500 Hz 清泥、8k 提镲。":
      "Heavy sub-bass (31/62), 500 Hz cleanup, 8k cymbal lift.",
    "温和抬升，尊重原混音，久听不累。":
      "Gentle lift that respects the original mix; easy on long listens.",
    "V 型等响曲线：小音量聆听时补足人耳不敏感频段（静态版）。":
      "V-shaped equal-loudness curve for low-volume listening (static).",
    "未在播放": "Not playing",
    "词": "LRC",
    "歌词": "Lyrics",
    "推荐风格": "Style",
    "略熟悉": "Somewhat familiar",
    "更熟悉": "More familiar",
    "略陌生": "Somewhat unfamiliar",
    "更陌生": "More unfamiliar",
    "均衡器": "Equalizer",
    "手动": "Manual",
    "预设": "Presets",
    "动态": "Dynamic",
    "全部归零": "Reset all",
    "补偿强度": "Compensation",
    "音量越低，自动提升低频 / 高频（等响度补偿，Fletcher–Munson）。":
      "At low volume, boosts lows/highs (Fletcher–Munson equal-loudness compensation).",
    "设置": "Settings",
    "换肤": "Appearance",
    "皮肤": "Skin",
    "高亮色（覆盖主题）": "Accent color (overrides theme)",
    "背景效果": "Background effect",
    "无效果": "None",
    "亚克力": "Acrylic",
    "音频律动": "Audio pulse",
    "叠层效果": "Overlay effect",
    "水面": "Water",
    "堆芯": "Reactor Core",
    "音域回响": "Sonic Echo",
    "音域回响 · 本体": "Sonic Echo · Core",
    "主题色": "Theme color",
    "蓝调时刻": "Blue Hour",
    "霓虹东京": "Neon Tokyo",
    "赛博森林": "Cyber Forest",
    "水墨": "Ink Wash",
    "冰川白昼": "Glacier Day",
    "酒红信号": "Wine Signal",
    "深空紫": "Deep Space",
    "青碧": "Teal",
    "落日": "Sunset",
    "皇家蓝": "Royal Blue",
    "苔原": "Tundra",
    "幽兰": "Orchid",
    "余烬": "Embers",
    "地形密度": "Terrain density",
    "跳动倍率": "Pulse gain",
    "流星": "Meteors",
    "情绪涟漪": "Mood ripples",
    "浮空块": "Floating blocks",
    "水面参数": "Water parameters",
    "波纹细度": "Ripple detail",
    "k⁻² 谱": "k⁻² spectrum",
    "k⁻² 物理谱": "k⁻² physical spectrum",
    "情绪强度": "Mood intensity",
    "画质档位": "Quality",
    "高": "High",
    "中": "Medium",
    "低": "Low",
    "换一批波形": "New wave set",
    "恢复默认": "Reset defaults",
    "高级（工程对照）": "Advanced (engineering)",
    "视觉调参（ui-water）": "Visual tuning (ui-water)",
    "对照开关": "A/B toggle",
    "物理增强（R90）": "Physics boost (R90)",
    "段切换脉冲": "Segment pulse",
    "事件模式": "Event mode",
    "情绪包络": "Mood envelope",
    "逐拍涟漪": "Beat ripples",
    "波银行": "Wave bank",
    "相位累加": "Phase accumulation",
    "旧版三波": "Legacy 3-wave",
    "整体亮度": "Overall brightness",
    "横向扰动（折射）★": "Lateral distortion (refraction) ★",
    "坡度亮线增益": "Slope highlight gain",
    "源衰减（0=关·默认）": "Source falloff (0=off)",
    "微涟漪平流（0=关·会平移）": "Micro-ripple drift (0=off)",
    "细波纹随激动变密": "Fine ripples follow intensity",
    "凝结阻尼（0=关）": "Condensation damping (0=off)",
    "亚网格耗散（消像素块）": "Sub-grid dissipation (de-block)",
    "换曲平静（0=不压）": "Track-change calm (0=off)",
    "凝结强度（安静时冻住）": "Condensation (freeze when quiet)",
    "自动调档（0=关·固定Q3）": "Auto quality (0=off · Q3 fixed)",
    "〔调试〕幅度 X 光": "[Debug] Amplitude X-ray",
    "水平动量系数": "Horizontal momentum",
    "〔实验〕等高线·强度": "[Exp] Contours · strength",
    "〔实验〕等高线·密度": "[Exp] Contours · density",
    "短波耗散（越小越细）": "Short-wave dissipation (finer)",
    "欢快雨（0=关）": "Happy rain (0=off)",
    "性能·分辨率上限": "Performance · resolution cap",
    "自定义壁纸": "Custom wallpaper",
    "歌曲封面": "Track cover",
    "图片": "Image",
    "链接": "Link",
    "视频": "Video",
    "壁纸引擎": "Wallpaper Engine",
    "选择文件": "Choose file",
    "导入": "Import",
    "扫码登录": "Scan to sign in",
    "等待扫码…": "Waiting for scan…",
    "请使用手机扫描二维码并在手机上确认登录":
      "Scan the QR code and confirm on your phone",
    "正在获取二维码…": "Fetching QR code…",
    "请先输入文件夹路径": "Enter a folder path first",
    "正在扫描…": "Scanning…",
    "扫描中…": "Scanning…",
    "扫描完成": "Scan complete",
    "⚠ 二维码已过期，请重新获取": "⚠ QR expired — refresh",
    "⚠ 二维码已过期": "⚠ QR expired",
    "⚠ 获取失败": "⚠ Failed to load",
    "⚠ 轮询失败": "⚠ Polling failed",
    "获取失败": "Failed to load",
    "轮询失败": "Polling failed",
    "请先选中一个歌单": "Select a playlist first",
    "正在播放歌单…": "Playing playlist…",
    "当前无曲目": "No current track",
    "播放失败": "Playback failed",
    "已移出「不喜欢的音乐」": "Removed from Disliked",
    "波形已重掷": "New waveform set",
    "水面参数已恢复默认": "Water parameters reset",
    "漫游 / 推荐下不可切换播放模式（只按顺序播放）":
      "Play mode can't be changed in Roam/Recommend (sequential only)",
    "语言 / Language": "Language",
    "切换界面文案，并让后端用所选语言生成新的 AI 口播/歌曲介绍（当前已生成的内容保留原语言）；歌曲信息与歌词保留原语言。":
      "Switches UI text and asks the backend to generate new AI DJ segments/song intros in this language. Already-generated copy keeps its language; track info and lyrics stay original.",

    /* ── 设置：AI ────────────────────────────────────────────── */
    "AI 大模型": "AI Model",
    "AI 后端": "AI backend",
    "本机 llama.cpp（默认·localhost:8080）": "Local llama.cpp (default · localhost:8080)",
    "远程 API（OpenAI 兼容）": "Remote API (OpenAI-compatible)",
    "本机 Ollama（本地模型）": "Local Ollama",
    "本机 llama.cpp": "Local llama.cpp",
    "Ollama（已退役）": "Ollama (retired)",
    "llama.cpp 地址": "llama.cpp URL",
    "API 地址": "API base URL",
    "https://…（多数需以 /v1 结尾）": "https://… (usually ends with /v1)",
    "模型名": "Model name",
    "Ollama 地址": "Ollama URL",
    "口播模型（深度）": "Broadcast model (deep)",
    "qwen3.5:4b（默认，实测最稳）/ granite4.2:3b（可思考，但见下）":
      "qwen3.5:4b (default, most stable) / granite4.2:3b (reasoning, see notes)",
    "思考模式（慢但更高质量）": "Thinking mode (slower, better)",
    "联网搜索（DJ 口播引用实时信息）": "Web search (DJ cites live info)",
    "测试 AI 连接": "Test AI connection",
    "快速模型（转场）": "Fast model (transitions)",
    "启用快速模型（降低延迟）": "Enable fast model (lower latency)",
    "温度（创造性）": "Temperature (creativity)",
    "不传温度（兼容 o1/o3）": "Omit temperature (o1/o3 compat)",
    "联网搜索频率": "Web search frequency",
    "0 · 仅开场第一首": "0 · First track only",
    "1 · 每首": "1 · Every track",
    "3 · 每3首": "3 · Every 3 tracks",
    "5 · 每5首": "5 · Every 5 tracks",
    "上下文窗口": "Context window",
    "0 · 纯实时": "0 · Live only",
    "3 · 轻量连贯": "3 · Light continuity",
    "6 · 更强连贯": "6 · Stronger continuity",
    "口播风格提示词": "Broadcast style prompt",
    "补充风格要求；留空用内置人设（无需粘贴示例）":
      "Extra style notes; blank uses the built-in persona (no examples needed)",
    "口味分析提示词": "Taste analysis prompt",
    "额外强调的分析侧重": "Extra analysis focus",
    "转场提示词": "Transition prompt",
    "创作原则与风格参考": "Creative principles and style reference",
    "推荐理由提示词": "Recommendation prompt",
    "推荐解说系统指令": "Recommendation explainer system prompt",

    /* ── 设置：TTS ───────────────────────────────────────────── */
    "语音 TTS": "Voice TTS",
    "音色": "Voice",
    "选一个音色，中文/英文/日文全程同一个嗓子；切换即时生效。":
      "Pick one voice for Chinese/English/Japanese; changes apply immediately.",
    "测试朗读（合成并试听）": "Test voice (synthesize & preview)",
    "播报后处理（ffmpeg 均衡/压缩/响度）": "Broadcast post-processing (ffmpeg EQ/comp/loudness)",
    "后处理参数（开启后处理时生效）": "Post-processing (active when enabled)",
    "低频增益 120Hz（dB）": "Low gain 120Hz (dB)",
    "中频增益 250Hz（dB）": "Mid gain 250Hz (dB)",
    "高频微亮 3000Hz（dB）": "High presence 3000Hz (dB)",
    "压缩器阈值（dB）": "Compressor threshold (dB)",
    "压缩比": "Compression ratio",
    "回声湿声比例": "Reverb wet mix",
    "目标响度 I（LUFS）": "Target loudness I (LUFS)",

    /* ── 设置：音源 ──────────────────────────────────────────── */
    "音源": "Sources",
    "登录态本地保存，重启复用": "Saved locally; reused on restart",
    "音乐盒（用户ID）": "MusicBox (user ID)",
    "用于导入「我喜欢的音乐」": "Used to import your liked tracks",
    "Cookie 加密保存，重启复用": "Encrypted; reused on restart",
    "服务地址": "Service URL",
    "默认歌单ID": "Default playlist ID",
    "登录后自动填充": "Filled automatically after login",
    "启用 Spotify": "Enable Spotify",
    "服务地址（可选）": "Service URL (optional)",
    "兼容代理地址": "Compatible proxy URL",
    "Client ID（可选）": "Client ID (optional)",
    "Client Secret（可选）": "Client Secret (optional)",
    "本地": "Local",
    "扫描本地目录": "Scan local folders",
    "本地目录": "Local folders",
    "每行一个目录，如：\\nD:\\\\CloudMusic\\nD:\\\\Music":
      "One folder per line, e.g.\\nD:\\\\CloudMusic\\nD:\\\\Music",

    /* ── 设置：通用 / 匹配 ───────────────────────────────────── */
    "通用": "General",
    "本地曲库": "Local Library",
    "导入文件夹": "Import folder",
    "重新扫描已配置曲库目录": "Rescan configured library folders",
    "导入的本地音乐会自动点亮红心。": "Imported local tracks are automatically liked.",
    "搜索歌曲 / 歌手": "Search tracks / artists",
    "输入要导入的音乐文件夹绝对路径，如 D:\\Music": "Folder path, e.g. D:\\Music",
    "不喜欢的音乐（AI 将避开）": "Disliked tracks (AI avoids)",
    "关于 Radio Intelligence": "About Radio Intelligence",
    "所在城市（手动覆盖）": "City (manual override)",
    "长春（留空则自动定位）": "Changchun (blank = auto locate)",
    "漫游策略": "Roam strategy",
    "连续相似": "Continuous similarity",
    "风格跳跃": "Style leap",
    "启动自动播放": "Autoplay on launch",
    "检索方式": "Search mode",
    "浏览器内核（本地服务）": "Browser engine (local service)",
    "联网 DuckDuckGo": "Online DuckDuckGo",
    "本地向量库": "Local vector store",
    "搜索服务地址": "Search service URL",
    "搜索服务引擎": "Search engine",
    "必应国内版": "Bing (China)",
    "360 搜索": "360 Search",
    "搜索结果条数": "Search results",
    "曲库与匹配": "Library & matching",
    "推荐模糊匹配（rapidfuzz）": "Fuzzy recommendation matching (rapidfuzz)",
    "匹配阈值": "Match threshold",

    /* ── 播放 / 卡片 / 歌单 ──────────────────────────────────── */
    "未知曲目": "Unknown track",
    "暂无内容": "No content",
    "还没有不喜欢的歌曲 —— AI 会避开它们。": "No disliked tracks yet — AI will avoid them.",
    "移除": "Remove",
    "下一首": "Next",
    "上一首": "Previous",
    "播放": "Play",
    "暂停": "Pause",
    "收藏": "Favorite",
    "喜欢（收藏到曲库）": "Like (add to library)",
    "不喜欢（AI 将避开）": "Dislike (AI avoids)",
    "播放模式": "Play mode",
    "顺序": "Sequential",
    "顺序播放": "Sequential",
    "随机": "Shuffle",
    "单曲循环": "Repeat one",
    "随机播放": "Shuffle",
    "列表循环": "Repeat all",
    "音量": "Volume",
    "静音": "Mute",
    "取消静音": "Unmute",
    "上一曲": "Previous track",
    "下一曲": "Next track",
    "播放/暂停": "Play/Pause",
    "播放模式：顺序": "Play mode: sequential",
    "播放该歌单全部曲目": "Play all tracks in this playlist",
    "全部音源": "All sources",
    "切换搜索源": "Switch search source",
    "删除": "Delete",
    "清空": "Clear",
    "清除": "Clear",
    "漫游": "Roam",
    "歌单": "Playlist",
    "静音切换": "Toggle mute",
    "电台模式": "Radio mode",
    "AI 伴听": "AI listening",
    "AI 思考中…": "AI thinking…",
    "均衡器（EQ）": "Equalizer (EQ)",
    "EQ 总开关": "EQ master",
    "界面高亮色": "UI accent color",
    "选择本地图片文件": "Choose a local image",
    "二维码": "QR code",
    "队列": "Queue",
    "正在播放": "Now playing",
    "已暂停": "Paused",
    "加载失败": "Failed to load",
    "加载歌单曲目…": "Loading playlist…",
    "该歌单暂无曲目": "No tracks in this playlist",
    "未选择歌单": "No playlist selected",
    "歌单曲目加载失败": "Failed to load playlist",
    "搜索歌曲、艺人、歌单": "Search tracks, artists, playlists",
    "未找到相关歌曲（可换关键词）": "No matches — try another keyword",
    "搜索中…": "Searching…",
    "推荐理由": "Why this track",
    "播放全部": "Play all",
    "待播": "Queued",

    /* ── 弹层 / 提示 / 标题栏 ────────────────────────────────── */
    "关闭": "Close",
    "最小化": "Minimize",
    "最大化": "Maximize",
    "拖动移动窗口 / 双击最大化": "Drag to move / double-click to maximize",
    "已设置 · 留空保持不变": "Set · leave blank to keep",
    "未设置": "Not set",
    "需连接后端后显示": "Connect backend to view",
    "连接后端后显示": "Shown after backend connects",
    "已扫码，请在手机上确认": "Scanned — confirm on phone",
    "二维码已过期，请重新获取": "QR expired — refresh",
    "✅ 登录成功，已启用音源并保存登录态": "✅ Signed in; source enabled and saved",
    "导入失败（后端未连接？）": "Import failed (backend offline?)",
    "导入失败：后端未连接": "Import failed: backend offline",
    "扫描失败：后端未连接": "Scan failed: backend offline",
    "请先粘贴 URL / 路径，或点「选择文件」": "Paste a URL/path or choose a file",
    "浏览器无法直接读本地路径，请点「选择文件」上传":
      "Browsers can't read local paths — choose a file",
    "粘贴图片 URL，或点「选择文件」": "Paste an image URL or choose a file",
    "粘贴视频直链 URL，或点「选择文件」": "Paste a video URL or choose a file",
    "粘贴壁纸直链 URL": "Paste wallpaper URL",
    "粘贴 Steam 创意工坊链接 / 壁纸 ID，或点「选择文件」上传 .zip 工程包":
      "Paste a Steam Workshop link/ID, or choose a .zip project",
    "网页无法直接读取本地工程文件夹，请用「选择文件」上传壁纸引擎导出的 .zip":
      "Browsers can't read local project folders — choose the exported .zip",
    "当前播放歌曲封面（无需导入）": "Current track cover (no import)",
    "背景随当前歌曲封面变化": "Background follows current cover",
    "Radio Intelligence 玻璃 logo（项目内置默认）": "Radio Intelligence glass logo (built-in)",
    "Radio Intelligence 玻璃 logo": "Radio Intelligence glass logo",
    "取消": "Cancel",
    "确定": "OK",
    "保存": "Save",
    "已保存": "Saved",
    "已复制": "Copied",
    "操作失败": "Action failed",
    "网络错误": "Network error",
    "请稍候…": "Please wait…",
    "正在连接…": "Connecting…",
    "已连接": "Connected",
    "已断开": "Disconnected",
    "重试": "Retry",
    "刷新": "Refresh",
    "测试": "Test",
    "启用": "Enable",
    "禁用": "Disable",
  };

  /* 带数字的界面串。只匹配明确的 UI 形态，避免误伤歌名。 */
  var PATTERNS = [
    [/^待播\s*(\d+)\s*首$/, function (m) { return m[1] + ' queued'; }],
    [/^(\d+)\s*首推荐$/, function (m) { return m[1] + ' recommendations'; }],
    [/^高级选项（(\d+)\s*项）$/, function (m) { return 'Advanced options (' + m[1] + ')'; }],
    [/^已导入\s*(\d+)\s*首$/, function (m) { return m[1] + ' imported'; }],
    [/^(\d+)\s*首$/, function (m) { return m[1] + ' tracks'; }],
    [/^(\d+)\s*首歌曲$/, function (m) { return m[1] + ' songs'; }],
    [/^(\d+)\s*个歌单$/, function (m) { return m[1] + ' playlists'; }],
    [/^共\s*(\d+)\s*首$/, function (m) { return m[1] + ' total'; }],
    [/^第\s*(\d+)\s*首$/, function (m) { return 'Track ' + m[1]; }],
    [/^音量\s*(\d+)$/, function (m) { return 'Volume ' + m[1]; }],
    [/^已切换到「(.+)」$/, function (m) { return 'Switched to "' + (tr(m[1]) || m[1]) + '"'; }],
    [/^扫描完成，新增\s*(\d+)\s*首$/, function (m) { return 'Scan complete · ' + m[1] + ' new tracks'; }],
    [/^每行一个目录，如：/, function () { return 'One folder per line, e.g. D:\\CloudMusic, D:\\Music'; }],
    [/^失败：(.+)$/, function (m) { return 'Failed: ' + (tr(m[1]) || m[1]); }],
    [/^请求失败：(.+)$/, function (m) { return 'Request failed: ' + (tr(m[1]) || m[1]); }],
    [/^播放失败：(.+)$/, function (m) { return 'Playback failed: ' + (tr(m[1]) || m[1]); }],
    [/^⚠\s*(.+)$/, function (m) { return '⚠ ' + (tr(m[1]) || m[1]); }],
  ];

  var ATTRS = ['title', 'aria-label', 'placeholder', 'alt'];
  var SKIP_TAGS = { SCRIPT: 1, STYLE: 1, CODE: 1, PRE: 1, CANVAS: 1 };
  var SKIP_SEL = [
    '[data-i18n-skip]',
    '.scard .sc-name', '.scard .sc-sub', '.scard .sc-chip',
    '.dn', '.da', '.ss-title', '.ss-artist', '.dl-name', '.dl-artist',
    '.song-title', '.song-artist', '.card-title', '.card-artist',
    '.lyrics', '.lyric-line', '#lyrics', '#lyric',
    '.poster-title', '.poster-artist',
    '.bc-text', '.broadcast-text', '.host-text', '.ai-text',
    '.ai-line', '.host-line',
  ].join(',');

  var lang = 'zh';
  var textRec = new Map();   // textNode -> {zh, en}
  var attrRec = new Map();   // element -> {attr: {zh, en}}
  var observer = null;
  var applying = false;

  function norm(s) {
    return String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  }

  function tr(s) {
    var k = norm(s);
    if (!k) return null;
    if (Object.prototype.hasOwnProperty.call(DICT, k)) return DICT[k];
    for (var i = 0; i < PATTERNS.length; i++) {
      var m = k.match(PATTERNS[i][0]);
      if (m) return PATTERNS[i][1](m);
    }
    return null;
  }

  function skipped(el) {
    if (!el || el.nodeType !== 1) return false;
    if (SKIP_TAGS[el.tagName]) return true;
    try { return !!el.closest(SKIP_SEL); } catch (_e) { return false; }
  }

  function translateText(node) {
    if (lang !== 'en' || !node || node.nodeType !== 3) return;
    var parent = node.parentElement;
    if (!parent || skipped(parent)) return;
    /* textarea 的正文是用户输入，不能翻；但它的 placeholder 需要翻（走 translateAttr）。 */
    if (parent.closest('textarea')) return;
    var rec = textRec.get(node);
    if (rec && node.nodeValue === rec.en) return;
    var raw = node.nodeValue;
    var trimmed = raw.trim();
    if (!trimmed) return;
    var en = tr(trimmed);
    if (!en || en === trimmed) return;
    var next = raw.replace(trimmed, en);
    textRec.set(node, { zh: raw, en: next });
    node.nodeValue = next;
  }

  function translateAttr(el) {
    if (lang !== 'en' || !el || el.nodeType !== 1 || skipped(el)) return;
    var rec = attrRec.get(el);
    if (rec) {
      var unchanged = true;
      for (var k in rec) {
        if (el.getAttribute(k) !== rec[k].en) { unchanged = false; break; }
      }
      if (unchanged) return;
    }
    var out = null;
    for (var i = 0; i < ATTRS.length; i++) {
      var a = ATTRS[i];
      if (!el.hasAttribute(a)) continue;
      var raw = el.getAttribute(a);
      var trimmed = raw.trim();
      if (!trimmed) continue;
      var en = tr(trimmed);
      if (!en || en === trimmed) continue;
      if (!out) out = {};
      out[a] = { zh: raw, en: raw.replace(trimmed, en) };
      el.setAttribute(a, out[a].en);
    }
    if (out) attrRec.set(el, out);
  }

  function walk(root) {
    if (!root) return;
    if (root.nodeType === 3) { translateText(root); return; }
    if (root.nodeType !== 1 && root.nodeType !== 9 && root.nodeType !== 11) return;
    var walker = document.createTreeWalker(
      root,
      NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT,
      {
        acceptNode: function (n) {
          if (n.nodeType === 1 && skipped(n)) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        },
      }
    );
    var n;
    while ((n = walker.nextNode())) {
      if (n.nodeType === 3) translateText(n);
      else translateAttr(n);
    }
  }

  function restoreAll() {
    textRec.forEach(function (rec, node) {
      if (node && node.nodeValue === rec.en) node.nodeValue = rec.zh;
    });
    textRec.clear();
    attrRec.forEach(function (rec, el) {
      if (!el) return;
      for (var a in rec) {
        if (el.getAttribute(a) === rec[a].en) el.setAttribute(a, rec[a].zh);
      }
    });
    attrRec.clear();
  }

  function prune() {
    textRec.forEach(function (_rec, node) {
      if (!node.isConnected) textRec.delete(node);
    });
    attrRec.forEach(function (_rec, el) {
      if (!el.isConnected) attrRec.delete(el);
    });
  }

  function syncToggle() {
    var seg = document.getElementById('lang-seg');
    if (seg) {
      seg.querySelectorAll('[data-lang]').forEach(function (b) {
        b.classList.toggle('active', b.getAttribute('data-lang') === lang);
      });
    }
  }

  var lastApiLang = null;
  var apiRetryTimer = 0;
  var apiRetryCount = 0;

  function scheduleApiRetry(code) {
    if (apiRetryTimer || apiRetryCount >= 15) return;
    apiRetryCount++;
    apiRetryTimer = setTimeout(function () {
      apiRetryTimer = 0;
      if (lang === code) syncBackendLanguage(code);
    }, 8000);
  }

  /* 把 UI 语言同步给后端口播：LLM prompt + TTS 语种 + 英文字数口径。
     启动时 radio 可能还没装配好（radio_ready=False）→ 8s 一次重试，最多约 2 分钟；
     旧后端没有该路由（404/405）时不重试，页面仍可切换 UI。 */
  function syncBackendLanguage(code) {
    code = code === 'en' ? 'en' : 'zh';
    if (lastApiLang === code) return;
    lastApiLang = code;
    try {
      fetch('/api/broadcast/language?lang=' + encodeURIComponent(code), {
        method: 'POST',
        keepalive: true,
      }).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }).then(function (d) {
        if (d && d.ok === true) {
          apiRetryCount = 0;
          return;
        }
        lastApiLang = null;
        if (d && /radio 未接入|not ready/i.test(String(d.error || ''))) {
          scheduleApiRetry(code);
        }
        try { console.warn('[i18n] broadcast language not applied:', d && d.error); } catch (_e) {}
      }).catch(function () {
        lastApiLang = null;
      });
    } catch (_e) {
      lastApiLang = null;
    }
  }

  function setLang(next) {
    next = next === 'en' ? 'en' : 'zh';
    if (next === lang) { syncToggle(); syncBackendLanguage(lang); return; }
    applying = true;
    lang = next;
    try { localStorage.setItem(KEY, lang); } catch (_e) {}
    document.documentElement.lang = lang === 'en' ? 'en' : 'zh-CN';
    document.documentElement.setAttribute('data-lang', lang);
    document.title = lang === 'en' ? 'Radio Intelligence · AI Radio' : 'Radio Intelligence · AI 智能电台';
    prune();
    if (lang === 'en') walk(document.body);
    else restoreAll();
    applying = false;
    syncToggle();
    apiRetryCount = 0;
    if (apiRetryTimer) { clearTimeout(apiRetryTimer); apiRetryTimer = 0; }
    syncBackendLanguage(lang);
    try {
      window.dispatchEvent(new CustomEvent('i18n:change', { detail: { lang: lang } }));
    } catch (_e) {}
  }

  function injectStyles() {
    if (document.getElementById('i18n-style')) return;
    var st = document.createElement('style');
    st.id = 'i18n-style';
    st.textContent = [
      'html[lang=en] #settings-panel .stab{font-size:12px;letter-spacing:.005em}',
      'html[lang=en] #settings-panel .fx-btn-status{word-break:normal;overflow-wrap:anywhere}',
      'html[lang=en] #settings-panel .fx-btn-row{flex-wrap:wrap}',
      'html[lang=en] #settings-panel .fx-btn-status{flex-basis:100%}',
      'html[lang=en] #settings-panel .set-label,html[lang=en] #theme-panel .set-label{letter-spacing:.005em}',
      'html[lang=en] .set-hint{letter-spacing:.005em}',
      '#lang-seg{gap:6px}',
      '#lang-seg .wp-type{min-width:74px}',
      '#settings-panel #lang-seg{display:inline-flex;gap:6px}',
      '#settings-panel #lang-seg .wp-type{padding:7px 13px;border-radius:10px;',
      'background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.14);',
      'color:rgba(255,255,255,.72);font:inherit;font-size:13px;cursor:pointer;',
      'transition:background .2s,color .2s,border-color .2s}',
      '#settings-panel #lang-seg .wp-type:hover{background:rgba(255,255,255,.12);color:#fff}',
      '#settings-panel #lang-seg .wp-type.active{background:color-mix(in srgb, var(--primary) 30%, transparent);',
      'border-color:var(--primary);color:#fff}',
    ].join('\n');
    document.head.appendChild(st);
  }

  function makeSettingsRow() {
    var general = document.querySelector('#settings-panel .stab-panel[data-tab="general"]');
    if (!general || document.getElementById('lang-seg')) return;
    var sec = document.createElement('section');
    sec.className = 'set-group';
    sec.setAttribute('data-gid', 'language');
    sec.innerHTML = [
      '<h4>语言 / Language</h4>',
      '<div class="set-row"><div class="wo-seg" id="lang-seg">',
      '<button class="wp-type active" type="button" data-lang="zh" data-i18n-skip>中文</button>',
      '<button class="wp-type" type="button" data-lang="en" data-i18n-skip>English</button>',
      '</div></div>',
      '<div class="set-hint" id="lang-hint">切换界面文案，并让后端用所选语言生成新的 AI 口播/歌曲介绍（当前已生成的内容保留原语言）；歌曲信息与歌词保留原语言。</div>',
    ].join('');
    general.insertBefore(sec, general.firstChild);
    sec.addEventListener('click', function (e) {
      var b = e.target.closest ? e.target.closest('[data-lang]') : null;
      if (b) setLang(b.getAttribute('data-lang'));
    });
  }

  function startObserver() {
    if (observer || !document.body) return;
    observer = new MutationObserver(function (muts) {
      if (lang !== 'en' || applying) return;
      applying = true;
      for (var i = 0; i < muts.length; i++) {
        var m = muts[i];
        if (m.type === 'childList') {
          for (var j = 0; j < m.addedNodes.length; j++) walk(m.addedNodes[j]);
        } else if (m.type === 'characterData') {
          translateText(m.target);
        } else if (m.type === 'attributes') {
          translateAttr(m.target);
        }
      }
      applying = false;
    });
    observer.observe(document.body, {
      subtree: true, childList: true, characterData: true,
      attributes: true, attributeFilter: ATTRS,
    });
  }

  function boot() {
    injectStyles();
    makeSettingsRow();
    try { lang = localStorage.getItem(KEY) === 'en' ? 'en' : 'zh'; } catch (_e) { lang = 'zh'; }
    document.documentElement.lang = lang === 'en' ? 'en' : 'zh-CN';
    document.documentElement.setAttribute('data-lang', lang);
    document.title = lang === 'en' ? 'Radio Intelligence · AI Radio' : 'Radio Intelligence · AI 智能电台';
    if (lang === 'en') walk(document.body);
    syncToggle();
    syncBackendLanguage(lang);
    startObserver();
    /* 设置卡是静态 DOM；若未来被重建，再补一次。 */
    setTimeout(function () { makeSettingsRow(); syncToggle(); }, 1200);
  }

  window.addEventListener('storage', function (e) {
    if (e.key === KEY) setLang(e.newValue === 'en' ? 'en' : 'zh');
  });

  window.__i18n = {
    get lang() { return lang; },
    setLang: setLang,
    syncBackendLanguage: syncBackendLanguage,
    t: tr,
    dict: DICT,
    translateAll: function () { walk(document.body); },
    add: function (map) { Object.assign(DICT, map); if (lang === 'en') walk(document.body); },
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
