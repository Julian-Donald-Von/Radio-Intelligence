/* newui/js/fine/settings-schema.js — 「SETTING_SCHEMA」自定义空间模块（源自 ui-settings.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'settings-schema',
  source: 'ui-settings.js',
  space: 'SETTING_SCHEMA',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* [头部公共段并入] */
  /* ★ 2026-11-18：TTS 统一到 Qwen3-TTS。9 个独立可选音色，每个都覆盖中/英/日
     全部语种（不按语言拆分）；选中后全程同一副嗓子。
     后 7 个为 11-18 试听后用户保留：2 个真人录音 + 5 个 VOICEVOX normal。 */
  const QWEN3_TTS_VOICES = [
    ['alba-mackenna','Alba Mackenna'],
    ['ono-anna','Ono Anna'],
    ['serena','Serena'],
    ['uncle-fu','Uncle Fu'],
    ['四国めたん','四国めたん'],
    ['青山龍星','青山龍星'],
    ['もち子さん','もち子さん'],
    ['春日部つむぎ','春日部つむぎ'],
    ['中部つるぎ','中部つるぎ'],
  ];
  const FAMILIARITY_LABELS = ['更熟悉','略熟悉','略陌生','更陌生'];

  /* ---- 设置大纲（老 UI L3888-3968） ----
     adv:1 的项收进组内「高级选项」折叠区（默认隐藏，点开才显示）；
     collapsed:1 的组默认整组折叠。折叠状态记 localStorage，下次打开保持。 */
  const SETTING_SCHEMA = [
    { group: 'AI 大模型', gid: 'ai', items: [
      { path: 'ai.backend', type: 'select', label: 'AI 后端', options: [['llamacpp','本机 llama.cpp（默认·localhost:8080）'],['api','远程 API（OpenAI 兼容）']] },
      { path: 'ai.api_base', type: 'password', label: 'API 地址', ph: 'https://…（多数需以 /v1 结尾）' },
      { path: 'ai.api_key', type: 'password', label: 'API Key' },
      { path: 'ai.model', type: 'text', label: '模型名', ph: 'gpt-4o-mini / deepseek-chat / moonshot-v1-8k' },
      { path: 'ai.ollama_host', type: 'text', label: 'Ollama 地址', ph: 'http://127.0.0.1:11434' },
      { path: 'ai.ollama_deep', type: 'text', label: '口播模型（深度）', ph: 'qwen3.5:4b（默认，实测最稳）/ granite4.2:3b（可思考，但见下）' },
      /* ★ R119-3k 实测备注（2026-09-19，同一首"椎名林檎"用例各 5 次）：
         · qwen3.5:4b ：均值 51 汉字（合约内）、**多语言 <ja> 标签 3/3 次都用对**、0.8~2.6s。
         · granite4.2:3b：均值 69 汉字（超合约）、**<ja> 标签 0/3 次**（日文歌名会被 MeloTTS
           用中文模型硬读成乱码）、中文偏生硬 ⇒ **暂不推荐做口播主力**。
         · granite 的思考**可用**（Ollama 不拆字段，推理内联在 content 里、以 </think> 收尾，
           已在后端自动切分），开思考后单条约 20~30s。 */
      { path: 'ai.thinking', type: 'toggle', label: '思考模式（慢但更高质量）' },
      { path: 'ai.web_search', type: 'toggle', label: '联网搜索（DJ 口播引用实时信息）' },
      /* R66 精简提示：只保留「肉眼看不见的信息」（登录态是否持久化、是否会自动拉起引擎）；
         单纯复述按钮作用的 hint 全删（测试 AI 连接 / 测试朗读 / 测试 TTS 连接 都自明）。
         ⚠ .fx-btn-status 同时是动作状态位（data-status），hint 为空只是初始留白，不影响回显。 */
      { type: 'button', action: 'test-ai', label: '测试 AI 连接' },
      { path: 'ai.ollama_fast', type: 'text', label: '快速模型（转场）', ph: 'qwen3.5:4b', adv: 1 },
      { path: 'ai.ollama_fast_enabled', type: 'toggle', label: '启用快速模型（降低延迟）', adv: 1 },
      { path: 'ai.temperature', type: 'range', label: '温度（创造性）', min: 0, max: 2, step: 0.1, adv: 1 },
      { path: 'ai.skip_temperature', type: 'toggle', label: '不传温度（兼容 o1/o3）', adv: 1 },
      { path: 'ai.web_search_interval', type: 'select', label: '联网搜索频率', options: [[0,'0 · 仅开场第一首'],[1,'1 · 每首'],[3,'3 · 每3首'],[5,'5 · 每5首']], adv: 1 },
      { path: 'ai.context_window', type: 'select', label: '上下文窗口', options: [[0,'0 · 纯实时'],[3,'3 · 轻量连贯'],[6,'6 · 更强连贯']], adv: 1 },
      { path: 'prompts.broadcast_output', type: 'textarea', label: '口播风格提示词', rows: 3, ph: '补充风格要求；留空用内置人设（无需粘贴示例）', adv: 1 },
      { path: 'prompts.taste_output', type: 'textarea', label: '口味分析提示词', rows: 2, ph: '额外强调的分析侧重', adv: 1 },
      { path: 'prompts.transition_output', type: 'textarea', label: '转场提示词', rows: 2, ph: '创作原则与风格参考', adv: 1 },
      { path: 'prompts.reco_explain', type: 'textarea', label: '推荐理由提示词', rows: 2, ph: '推荐解说系统指令', adv: 1 },
    ]},
    { group: '语音 TTS', gid: 'tts', items: [
      { path: 'tts.qwen3tts_voice', type: 'select', label: '音色', options: QWEN3_TTS_VOICES,
        hint: '选一个音色，中文/英文/日文全程同一个嗓子；切换即时生效。' },
      { type: 'button', action: 'test-tts-sample', label: '测试朗读（合成并试听）' },
      { path: 'tts.qwen3tts_postprocess', type: 'toggle', label: '播报后处理（ffmpeg 均衡/压缩/响度）' },
      { type: 'subhead', label: '后处理参数（开启后处理时生效）', adv: 1 },
      { path: 'tts.melotts_pp_hs1', type: 'range', label: '低频增益 120Hz（dB）', min: -12, max: 12, step: 0.5, adv: 1 },
      { path: 'tts.melotts_pp_hs2', type: 'range', label: '中频增益 250Hz（dB）', min: -12, max: 12, step: 0.5, adv: 1 },
      { path: 'tts.melotts_pp_hs3', type: 'range', label: '高频微亮 3000Hz（dB）', min: -12, max: 12, step: 0.5, adv: 1 },
      { path: 'tts.melotts_pp_comp_th', type: 'range', label: '压缩器阈值（dB）', min: -40, max: 0, step: 1, adv: 1 },
      { path: 'tts.melotts_pp_comp_ratio', type: 'range', label: '压缩比', min: 1, max: 10, step: 0.5, adv: 1 },
      { path: 'tts.melotts_pp_echo', type: 'range', label: '回声湿声比例', min: 0, max: 1, step: 0.05, adv: 1 },
      { path: 'tts.melotts_pp_loud_i', type: 'range', label: '目标响度 I（LUFS）', min: -24, max: -10, step: 1, adv: 1 },
    ]},
    { group: '音源', gid: 'sources', items: [
      { type: 'subhead', label: '网易云（MusicBox 扫码登录）' },
      { path: 'sources.netease.enabled', type: 'toggle', label: '启用网易云' },
      { type: 'button', action: 'qr-netease', label: '扫码登录网易云', hint: '登录态本地保存，重启复用' },
      { path: 'sources.netease.musicbox_uid', type: 'text', label: '音乐盒（用户ID）', ph: '用于导入「我喜欢的音乐」', adv: 1 },
      { path: 'sources.netease.cookie', type: 'password', label: 'Cookie', adv: 1 },
      { type: 'subhead', label: 'QQ音乐（qq-music-api 扫码登录）' },
      { path: 'sources.qqmusic.enabled', type: 'toggle', label: '启用 QQ音乐' },
      { type: 'button', action: 'qr-qq', label: '扫码登录 QQ音乐', hint: 'Cookie 加密保存，重启复用' },
      /* R67-1：删 sources.qqmusic.api_base —— 用户原话"目前就留下...QQ音乐api"。
         等等：QQ api_base 是用户必填的本地服务地址（默认 127.0.0.1:3200），属于用户保留的
         "QQ音乐api" 功能本身。**保留**。 */
      { path: 'sources.qqmusic.api_base', type: 'text', label: '服务地址', ph: 'http://127.0.0.1:3200', adv: 1 },
      { path: 'sources.qqmusic.cookie', type: 'password', label: 'Cookie', adv: 1 },
      { path: 'sources.qqmusic.default_playlist_id', type: 'text', label: '默认歌单ID', ph: '登录后自动填充', adv: 1 },
      /* R67-1：删 sources.qqmusic.auto_start toggle —— 用户原话"启动时自动拉起
         qq-music-api默认启用不用加开关"。启动时的拉起行为已并入 radio_bridge.attach()。 */
      { type: 'subhead', label: 'Spotify', adv: 1 },
      { path: 'sources.spotify.enabled', type: 'toggle', label: '启用 Spotify', adv: 1 },
      { path: 'sources.spotify.api_base', type: 'text', label: '服务地址（可选）', ph: '兼容代理地址', adv: 1 },
      { path: 'sources.spotify.client_id', type: 'text', label: 'Client ID（可选）', adv: 1 },
      { path: 'sources.spotify.client_secret', type: 'password', label: 'Client Secret（可选）', adv: 1 },
      { path: 'sources.spotify.cookie', type: 'password', label: 'Cookie', adv: 1 },
      { path: 'sources.spotify.default_playlist_id', type: 'text', label: '默认歌单ID', adv: 1 },
      { type: 'subhead', label: '本地' },
      { path: 'sources.local.enabled', type: 'toggle', label: '扫描本地目录' },
      { path: 'sources.local.roots', type: 'textarea', label: '本地目录', rows: 3, ph: '每行一个目录，如：\\nD:\\\\CloudMusic\\nD:\\\\Music' },
    ]},
    { group: '通用', gid: 'general', items: [
      /* R67-2：rec_pref 从 SETTING_SCHEMA 删除 —— 用户原话"主界面有相对应的滑块了，
         通用设置里面的推荐偏好功能可以去了"。user_config.rec_pref 字段保留，
         由主界面 ui-column.js 推荐风格滑块统一读写。 */
      { path: 'geo.city', type: 'text', label: '所在城市（手动覆盖）', ph: '长春（留空则自动定位）' },
      { path: 'roam_strategy', type: 'select', label: '漫游策略', options: [['continuous','连续相似'],['leap','风格跳跃']] },
      { path: 'auto_play', type: 'toggle', label: '启动自动播放' },
      { path: 'search.mode', type: 'select', label: '检索方式', options: [['browser','浏览器内核（本地服务）'],['online','联网 DuckDuckGo'],['offline','本地向量库']] },
      { path: 'search.browser_url', type: 'text', label: '搜索服务地址', ph: 'http://127.0.0.1:8787', adv: 1 },
      { path: 'search.browser_engine', type: 'select', label: '搜索服务引擎', options: [['bing','必应国内版'],['360','360 搜索']], adv: 1 },
      { path: 'search.max_results', type: 'range', label: '搜索结果条数', min: 1, max: 10, step: 1, adv: 1 },
    ]},
    { group: '曲库与匹配', gid: 'match', collapsed: 1, items: [
      { path: 'match.fuzzy_enabled', type: 'toggle', label: '推荐模糊匹配（rapidfuzz）' },
      { path: 'match.fuzzy_threshold', type: 'range', label: '匹配阈值', min: 50, max: 100, step: 1, adv: 1 },
    ]},
  ];

`;
    const __epi = `  for (const __n of ["QWEN3_TTS_VOICES", "FAMILIARITY_LABELS", "SETTING_SCHEMA"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
