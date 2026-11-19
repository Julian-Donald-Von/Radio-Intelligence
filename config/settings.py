"""
全局配置：集中管理路径、音量、AI、天气等参数。
所有路径默认落在 D:\\MusicAI 下，避免污染 C 盘。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

# ═══ HF 下载源统一走国内镜像（必须在任何 huggingface_hub / transformers
# import **之前**设置：huggingface_hub.constants.ENDPOINT 在 import 时固化，
# 之后 setdefault 无效 → 启动时 melo/CLAP 仍向 huggingface.co 发 HEAD，
# 离线环境 SSL 失败重试 5 次×N 模型 ≈ 60s，且最终导致 melo import 报错）。
# 本机实测：hf-mirror.com 200 / huggingface.co 000。
# 若用户显式设置 HF_ENDPOINT 则尊重（不覆盖）。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

# ═══ ★★ OMP/MKL **等待策略**：同样必须在任何 torch import **之前**设置
# （OpenMP 运行时在**第一个并行区**初始化时读取该变量，之后 setdefault 无效）。
#
# 背景（2026-09-11 实测，证据来自 `python -m training.cli deploy_bench`）：
#   CLAP 的**单窗**前向实测占用 **4.68 个 CPU 核**；而"纯 CUDA 运算"对照只有 **0.95 核**
#   （已用 sleep / cuda-matmul / cuda-conv1d / cpu-matmul 四组对照验证过这把尺子准）。
#   ⇒ 那多出来的 ~3.7 个核**不是算力，是空烧**：torch 的 CPU 侧算子（numpy↔tensor 转换、
#     若干 1-D 层、`.to(device)`）会进出 OpenMP 并行区，而 **Intel OMP 默认
#     `KMP_BLOCKTIME=200 ms` 是自旋等待**（spin-wait）—— GPU 在算，CPU 在空转占核。
#   ⇒ 这正是 R88/R89 那次 **PortAudio underflow** 的机理：整曲索引占满核 ⇒
#     `sounddevice` 的 **23 ms RT 回调**抢不到核 ⇒ 用户听感"每首歌开头一两分钟断续/重复"。
#   ⇒ `core.audio_features.cap_torch_threads_for_audio()` 只收窄了**线程数**，
#     **管不住等待策略** ⇒ 对这条路径无效。
#
# 修：`KMP_BLOCKTIME=0`（Intel OMP 的正牌旋钮）+ `OMP_WAIT_POLICY=PASSIVE`。
#   实测（**同进程、同素材、只改这两个 env**）：
#     并行度 **4.68 → 0.96 核**；单窗延迟 **24.21 → 21.62 ms**
#   ⇒ **CPU 占用降约 5 倍，延迟不升反降**（纯收益，不是"拿速度换核数"）。
# 若用户显式设置了这两个变量则尊重（不覆盖）。
os.environ.setdefault("KMP_BLOCKTIME", "0")
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")

# ═══ ★★ 并行线程数上限（OpenMP / OpenBLAS / MKL 池）：必须在 BLAS/numpy **首次
# 并行区**之前设置，否则 setdefault 无效。
#
# 为什么需要：上面只解决了「等待策略」（自旋→睡眠），但**没有限制线程数**。
# `cap_torch_threads_for_audio()` 只管 torch 的 intra-op 线程池，**管不到 numpy
# 的 OpenBLAS / Intel MKL / OpenMP 线程池**——而 librosa 解码、CLAP 的 numpy 预处理、
# 分位数标定、投影 dot 都走这些池。若不限制，它们会吃满全部物理核，与
# `sounddevice` 的 **23 ms RT 回调**抢核 ⇒ **output underflow**（听感=断流/重复）。
# 这正是「之前修好的卡顿又出现」的疑似元凶：等待策略修了，但线程数仍全开。
#
# 尺子与 `cap_torch_threads_for_audio` 一致（cpu//3，可被 MUSICAI_TORCH_THREADS 覆盖），
# 抢在 BLAS 初始化前写进环境，使其第一个并行区就生效。用户显式设了这些变量则尊重。
def _musicai_parallel_threads() -> int:
    cpu_n = os.cpu_count() or 4
    try:
        want = int(os.environ.get("MUSICAI_TORCH_THREADS", "0") or 0)
    except Exception:  # noqa 环境变量乱填 → 走默认
        want = 0
    if want <= 0:
        want = max(2, cpu_n // 3)
        want = max(1, min(cpu_n - 1, want))   # 小核机器兜底：至少给别处留 1 核
    return want
_mt = _musicai_parallel_threads()
for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, str(_mt))

from ai.prompts import (
    TASTE_OUTPUT_DEFAULT, TRANSITION_OUTPUT_DEFAULT, BROADCAST_OUTPUT_DEFAULT,
)
from utils.secure_storage import is_protected, protect, unprotect

# ---------- 根目录 ----------
# 项目根：D:\MusicAI
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 数据目录（数据库、缓存、TTS 文件）
DATA_DIR = PROJECT_ROOT / "data_runtime"
DB_PATH = DATA_DIR / "musicai.db"
TTS_CACHE_DIR = DATA_DIR / "tts_cache"   # 历史遗留缓存目录（仅启动清理用，新音频不再写入）
TTS_TMP_DIR = DATA_DIR / "tts_tmp"       # TTS 生成的临时音频：即用即扔，启动/退出/周期清理
COVER_CACHE_DIR = DATA_DIR / "covers"
LOG_DIR = DATA_DIR / "logs"

# ffmpeg 二进制（MeloTTS 音频后处理用）。优先使用项目内置的完整构建；
# 若缺失则运行时由 core/tts_melotts.py 退回到 imageio-ffmpeg 自带或系统 PATH 上的 ffmpeg。
FFMPEG_BIN = PROJECT_ROOT / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"

for _d in (DATA_DIR, TTS_CACHE_DIR, TTS_TMP_DIR, COVER_CACHE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------- 音乐库扫描根（用户自行修改） ----------
# 默认扫描 D 盘常见音乐目录；留空则仅使用已入库记录
MUSIC_LIBRARY_ROOTS = [
    r"D:\CloudMusic",
    r"D:\Music",
    r"D:\QQMusicCache",
]

# ---------- 播放/电台调度参数 ----------
class RadioConfig:
    # 推荐列表固定保留的歌曲数（含正在播放）
    PLAYLIST_SIZE = 5
    # 转场：当前歌曲还剩多少毫秒时，提前开始下一首的「转场口播」（音乐淡出）。
    # 用户要求：转场必须「在前一首播完 5 秒之前」开始、且必须播完才切下一首，
    # 故固定为 5000ms（前一首结束前 5 秒起播转场），并由 _begin_transition 的
    # wait_tts 保证「转场播完才 advance」，杜绝「转场没播完就播下一首」。
    TRANSITION_LEAD_MS = 5000          # 转场预告窗口：前一首结束前 5 秒起播转场
    # 音乐淡出到静音的时长（口播前）
    FADE_OUT_MS = 1500
    # 口播结束后，下一首淡入到目标音量的时长
    FADE_IN_MS = 1500
    # 纯口播窗口（静默模式下无 TTS，用该时长模拟"口播间隙"）
    BROADCAST_WINDOW_MS = 9000
    # 待播低于该数量时，自动补充到 PLAYLIST_SIZE
    REFILL_THRESHOLD = 1
    # R57-3 连跳风暴刹车：连续几首音源失效就暂停电台（而不是按队列长度放大）。
    # 音源登录态失效 / API 批量失败时，逐首 resolve_url 全落空 → 若阈值随队列长
    # （旧 max(len+3,6) ≈ 8）用户会看到"疯狂连跳 7~8 首才停"。3 首即停、广播错误。
    SKIP_STORM_LIMIT = 3
    # 开场导听看门狗：AI 口播（LLM+TTS）超过该毫秒数仍未就绪，则直接放歌，
    # 防止口播生成失败/超时时音乐永远不响。
    INTRO_MAX_WAIT_MS = 5000

    # 风格滑块四档：更熟悉（曲库内为主）↔ 更陌生（曲库外为主）
    FAMILIARITY_LABELS = [
        "更熟悉",
        "略熟悉",
        "略陌生",
        "更陌生",
    ]

    # 曲库播放模式
    PLAY_MODES = {
        "order": "列表播放",
        "random": "随机播放",
        "loop_one": "单曲循环",
    }


# ---------- R114：在线曲「音频比元数据短很多」的判据阈值（**单一来源**） ----------
# 现实：在线曲的直链是现场解析的，**同一首歌可能拿到降级/试听片段**（实测 20 s
# vs 完整 300 s），或下载被截断；而曲目元数据里的 duration 仍是完整曲的时长。
# 两个层次都要用同一个阈值，故放此处（`core/` 与 `ai/` 都已 import 本模块）：
#   · `data/sources/url_health.py` —— 取链时验货（比 API 自己声明的 time / size）
#   · `core/audio_engine.py:1316` —— 播放时短音频守卫（比文件真实时长），
#     让进度条/转场排程按**真实时长**走，而不是按 300 s 铺一条永远走不到的进度条
# 取 0.5 的依据：实测降级案例是 **0.067**（20 s / 300 s），离阈值极远；
# 同时故意留宽，容忍元数据本身的误差（实测存在整曲 240 s 被标 300 s 的情况）。
SHORT_AUDIO_RATIO = 0.5


# ---------- 播放引擎参数 ----------
class PlayerConfig:
    # 音乐通道初始音量(0.0~1.0)
    MUSIC_VOLUME = 0.85
    # Ducking 时音乐被压到的音量（AI导听口播期间）—— R49-26 用户原话"压低到 30%"
    DUCK_VOLUME = 0.30
    # 淡入淡出时长(毫秒)
    FADE_MS = 1200
    # 前奏导听触发：歌曲开始后的若干秒内进行 AI 口播
    INTRO_DJ_AFTER_MS = 300
    # 口播结束后音乐渐强恢复时长(毫秒)
    RECOVER_FADE_MS = 1500


# ---------- TTS 参数 ----------
class TTSConfig:
    VOICE = "zh-CN-XiaoxiaoNeural"
    RATE = "+0%"          # 语速
    VOLUME = "+0%"        # 音量
    # 口播最大长度，避免过长
    MAX_TEXT_LEN = 180


# ---------- AI 大脑参数 ----------
class AIConfig:
    # 兼容 OpenAI 协议的端点；留空则进入"离线演示模式"(内置模板口播)
    API_BASE = os.getenv("MUSICAI_API_BASE", "")
    API_KEY = os.getenv("MUSICAI_API_KEY", "")
    MODEL = os.getenv("MUSICAI_MODEL", "gpt-4o-mini")
    # 超时(秒)
    TIMEOUT = 30
    # 是否启用 LLM（False 时使用离线模板）
    ENABLED = bool(API_BASE and API_KEY)

    # 漫游推荐策略
    ROAM_STRATEGY = "continuous"   # continuous=连续相似 | leap=风格跳跃

    # 启动是否自动播放第一首（需文件存在）。默认 False，避免启动即崩/惊扰
    AUTO_PLAY = False

    # 转场风格决策：intro / outro / none
    TRANSITION_DEFAULT = "intro"


# ---------- 本地模型 / 音色目录（相对项目根，可移植） ----------
# 全部使用相对路径，确保整套系统可整体拷贝到其它机器。
MODELS_DIR = PROJECT_ROOT / "models"
CHATTTS_DIR = MODELS_DIR / "chattts-onnx"      # ChatTTS ONNX 权重
VOICES_DIR = PROJECT_ROOT / "voices"           # 用户导入音色目录
VOICES_CONFIG = VOICES_DIR / "config.json"     # 音色注册表
OFFLINE_KB_DIR = DATA_DIR / "kb"               # 离线向量知识库（sqlite-vec）

for _d in (MODELS_DIR, CHATTTS_DIR, VOICES_DIR, OFFLINE_KB_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ---------- TTS 多语言路由表 ----------
# 口播中不同语言的歌名/人名由 LLM 用 <lang>原文</lang> 标签包裹，
# 调度器据此把每个片段分给对应引擎/音色合成，再按原文顺序拼接。
# 仅安装用到的语言（按需扩展；de/ru/KR 等 MeloTTS 支持的语言可加进本表）。
# zh/en/es/fr 由本地 MeloTTS 引擎统一按语言加载模型合成（支持 GPU 加速）；
# ja 仍走本地 VOICEVOX（独立 App）。
TTS_ENGINE = {
    "zh": {"provider": "melotts",  "model": "ZH"},
    "en": {"provider": "melotts",  "model": "EN"},
    "ja": {"provider": "voicevox", "speaker": "四国めたん"},
    "es": {"provider": "melotts",  "model": "ES"},
    "fr": {"provider": "melotts",  "model": "FR"},
    # 韩语 / 德语：本机当前无法用 MeloTTS 提供真嗓（见下），按「接受回退」原则
    #   改由 Edge 云 TTS 真实本地嗓兜底（ko-KR / de-DE，见 core/tts_edge.EDGE_LANG_VOICES），
    #   发音正确、零 GPU 占用（不影响显存预算）。
    #   - 韩语：melo 有 MeloTTS-Korean（语言码 KR，_LANG_MAP 已就绪），但本机网络拉不到权重
    #     （HF 镜像返回 0 字节占位 / melo 内置 S3 链接 404 / huggingface.co 502），故走 Edge。
    #     待权重可达时把下面这行改回 {"provider": "melotts", "model": "KR"} 即自动升级为真 MeloTTS 韩语。
    #   - 德语：melo 官方 fork 的 LANG_TO_HF_REPO_ID 仅有 EN/FR/JP/ES/ZH/KR，根本无德语模型，
    #     只能走 Edge（de-DE-KatjaNeural）。
    "ko": {"provider": "edge",     "model": "ko-KR-SunHiNeural"},
    "de": {"provider": "edge",     "model": "de-DE-KatjaNeural"},
    # 按需扩展（俄语等 MeloTTS 也没有，统一走 Edge 兜底）：
    # "ru": {"provider": "edge",     "model": "ru-RU-SvetlanaNeural"},
}

# VOICEVOX 本地服务地址（独立 App，需另装并启动）；可用环境变量覆盖。
VOICEVOX_HOST = os.getenv("MUSICAI_VOICEVOX_HOST", "127.0.0.1:50021")

# 片段间拼接静音（毫秒），防止不同引擎/语种音色突兀相接。
TTS_SEGMENT_SILENCE_MS = 80
# 拼接输出统一采样率（VOICEVOX 原生 24000，MeloTTS 为 44100，统一重采样到该值）。
TTS_SEGMENT_SR = 24000


# ---------- 显存预算（RTX 4060 Laptop 8GB） ----------
class GPUConfig:
    # LLM 显存预算：qwen3:4b Q4_K_M ≈ 2.4GB，留余量给 KV cache
    LLM_BUDGET_MB = 5120
    # ★ 2026-11-17 task-1：MeloTTS 已退休（TTS 由独立 worker 的 IndexTTS 承担，
    #   不占本进程预算）。本进程现在只有 CLAP 音频语义编码在用，故改名。
    CLAP_BUDGET_MB = 1536
    TTS_BUDGET_MB = CLAP_BUDGET_MB      # 兼容旧脚本/旧配置名，新代码不要再用
    # 系统 / 音频缓冲强制保留（部署规范"严格限制"：LLM+TTS≤6.5GB + 本缓冲 = 8.0GB）
    SYSTEM_BUFFER_MB = 1536
    # LLM + TTS 同时加载硬上限（≤6.5GB），超出则触发降级
    COMBINED_CAP_MB = 6656
    # CUDA 设备号（多卡时指定，默认 0）
    DEVICE_INDEX = 0


# ---------- 天气 / 地点 ----------
class GeoConfig:
    # 和风天气等服务的 key；为空则使用内置静态演示数据
    WEATHER_API_KEY = os.getenv("MUSICAI_WEATHER_KEY", "")
    # 默认城市（用于口播）
    DEFAULT_CITY = "上海"
    # 通过 IP 定位失败时回退到该城市
    FALLBACK_CITY = "上海"


# ---------- 网易云音乐源（NeteaseCloudMusicApi） ----------
class NeteaseConfig:
    # 是否启用网易云作为曲库来源
    ENABLED = bool(os.getenv("MUSICAI_NCM_API", ""))
    # 本机运行的 NeteaseCloudMusicApi 服务地址
    API_BASE = os.getenv("MUSICAI_NCM_API", "http://127.0.0.1:3000")
    # 登录方式（二选一）：粘贴浏览器 cookie，或用手机号+密码
    COOKIE = os.getenv("MUSICAI_NCM_COOKIE", "")
    PHONE = os.getenv("MUSICAI_NCM_PHONE", "")
    PASSWORD = os.getenv("MUSICAI_NCM_PASSWORD", "")
    # 启动时自动加载的歌单 ID（留空则手动加载）
    DEFAULT_PLAYLIST_ID = os.getenv("MUSICAI_NCM_PLAYLIST", "")


# =========================================================================
# 用户可配置项（应用内"设置"页编辑，持久化到 D:\MusicAI\data_runtime\config.json）
# =========================================================================
USER_CONFIG_PATH = DATA_DIR / "config.json"

# 默认值：环境变量优先（方便老用户），否则用内置默认
def _env_or(default, env_name):
    v = os.getenv(env_name, "")
    return v if v else default


def _safe_int_env(env_name, default):
    """整数字段的环境变量覆盖：非法值回退默认，绝不因一个坏 env 让配置加载失败。"""
    v = os.getenv(env_name, "")
    if not v:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

DEFAULT_USER_CONFIG = {
    "ai": {
        "api_base": _env_or("", "MUSICAI_API_BASE"),
        "api_key": _env_or("", "MUSICAI_API_KEY"),
        "model": _env_or("gpt-4o-mini", "MUSICAI_MODEL"),
        # 后端选择：api=OpenAI 兼容远程（混元/Gemini/DeepSeek/GPT/Kimi 等）；
        # ollama=本机 Ollama 本地小模型（无需 API Key，数据不出本机）。
        # 选 ollama 时下面 ollama_* 生效，api_base/api_key 可留空。
        # ollama=本机 Ollama（旧路径，保留回退）；
        # llamacpp=★2026-11-16 直连 llama.cpp server（脱离 Ollama，省 ~1GB 常驻 +
        #   免 Ollama 代理层），走 /v1/chat/completions，支持参数层 reasoning_budget
        #   强制收尾（治"模型反刍把预算用光、正文为零"）。见 ai/model.py::_chat_llamacpp。
        #   进程生命周期由外部管理（bat/服务），本模块只探活；不可达时不自动接管。
        "backend": _env_or("api", "MUSICAI_AI_BACKEND"),
        # llamacpp 直连地址（llama.cpp server 默认端口 8080；实验用 2719）
        "llamacpp_host": _env_or("http://127.0.0.1:8080", "MUSICAI_LLAMACPP_HOST"),
        # 思考预算（token）：>0 时启用参数层受控思考（到点强制收尾进正文）；
        # 0 = 关闭思考；-1 = 原生无界思考（实测会反刍到用满预算、正文为空，不建议）。
        # 推荐 512（APMP 实测：思考 1890 字 + 正文 622 字 + stop 正常收尾）。
        "llamacpp_reasoning_budget": _safe_int_env("MUSICAI_LLAMACPP_REASONING_BUDGET", 512),
        # llama.cpp 上下文窗口：口播提示词实测 6168 token ⇒ 必须 ≥8192（同 ollama_num_ctx）
        "llamacpp_num_ctx": _safe_int_env("MUSICAI_LLAMACPP_NUM_CTX", 8192),
        # Ollama 本地服务地址（OLLAMA_HOST）
        "ollama_host": _env_or("http://127.0.0.1:11434", "MUSICAI_OLLAMA_HOST"),
        # 口播模型（qwen3.6:35b-a3b，纯文本、可关思考、6.6GB）。★ 2026-09-20 用户拍板：deep/fast 统一 9b（实测标签正确率 88%，远高于 4b）；qwen3.5:4b 已卸载。
        # 量化锁定：/api/tags 实测该 tag 即 Q4_K_M（quantization_level=Q4_K_M，~3.4GB）。
        # 用户要求 Q4_K_M，禁用 Q3/Q5 —— 换 tag 前须先核对量化等级。
        "ollama_deep": _env_or("qwen3.6:35b-a3b", "MUSICAI_OLLAMA_DEEP"),
        # 快速短口播模型（转场/快评）：用户指定与 deep 同款 qwen3.6:35b-a3b
        "ollama_fast": _env_or("qwen3.6:35b-a3b", "MUSICAI_OLLAMA_FAST"),
        # 是否启用 3B 快速模型：开启后转场等短口播走 3B，降低延迟与 7B 占用
        "ollama_fast_enabled": True,
        "temperature": 0.8,
        "skip_temperature": False,
        # 联网搜索：qwen/Ollama 本地模型走 DuckDuckGo 实时检索（零 Key）；
        # Kimi/Moonshot 走其内置 $web_search 工具。开启后 DJ 口播可引用歌手近况/
        # 新专辑等实时信息。慢网络下检索会自动 fast-fail（见 ai/search.py 超时保护），
        # 不影响口播生成的实时性。默认开启（用户要求 qwen 联网）。
        "web_search": True,
        # 思考模式：qwen3.5 等思考模型在生成口播前会先做一段内部思考（reasoning）。
        # 关闭（默认）：qwen3.5 实测 think=False 时完全不产出思考字段、正文干净，且从 88s 降到 ~1s，
        #   故默认关闭以保证 DJ 口播即时出稿（由电台「边播边想」预取吸收，不影响切歌）。
        # 开启：正文质量可能更高，但 Ollama 会多生成数千 token 思考，广播生成约 60~80s。
        # 云端 Kimi/Moonshot 的思考模式也走此开关。
        # JSON 结构化调用（品味/推荐/转场）始终关闭思考，避免与 response_format 冲突。
        "thinking": False,
        # 上下文窗口（口播连贯性 ↔ 极致速度的可调旋钮）：
        # 口播生成时仅保留「最近 N 首已播歌曲」的轻量连贯性上下文，绝不累积无限历史。
        # 0 = 纯实时、不保留任何历史上下文，追求极致速度（DJ 不记得刚播过什么）；
        # 默认 3（轻量连贯，便于口播呼应上一首、避免重复）。
        "context_window": 3,
        # 联网搜索节流（默认全开）：用户要求口播实时、「能联网则联网」，
        # 故默认 1（每首歌都真正联网搜索）。保留该旋钮仅作性能兜底——
        # 若某些慢网络/短歌曲场景下预取仍赶不上，可把它调大（N=每 N 首搜一次）
        # 或设 0（仅开场第一首搜）。0=仅首歌；1=每首；N>1=每 N 首。
        "web_search_interval": 1,
    },
    "tts": {
        # 引擎：indextts=IndexTTS 2.5 本地（默认，多语 ZH/EN/JA，GPU）|
        # edge=微软云 TTS（云端兜底，零配置）。
        # （旧引擎 chattts/melotts/voicevox 已于 2026-11-17 停用，模块文件仍保留。）
        "engine": "indextts",
        # 当 engine 为 chattts/melotts 时使用的音色 ID（对应 ./voices/config.json 中的 voice_id）；
        # 为空则使用引擎默认音色。
        "voice_id": "",
        "voice": "zh-CN-XiaoxiaoNeural",
        "rate": "+0%",
        "volume": "+0%",
        # ---- 多语言分片 TTS 的可调参数（设置页「语音 TTS」可改） ----
        # VOICEVOX 本地服务地址（headless 引擎 run.exe 默认监听端口）；环境变量可覆盖。
        "voicevox_host": _env_or("127.0.0.1:50021", "MUSICAI_VOICEVOX_HOST"),
        # VOICEVOX headless 引擎可执行文件（run.exe）路径；留空则自动探测常见安装位置。
        # 填了就用它：应用会在需要日文 TTS 时自动拉起该引擎（无需你手动开 VOICEVOX App），
        # 并在应用退出时自动关闭，做到「内嵌、不单独挂起」。环境变量 MUSICAI_VOICEVOX_EXE。
        "voicevox_exe": _env_or("", "MUSICAI_VOICEVOX_EXE"),
        # 是否用 GPU 跑 MeloTTS（本地已装 cu124 torch，默认开启加速）。
        # 关闭则走 CPU。MeloTTS 与 Ollama 共享 RTX 4060，按需切换、用完释放显存。
        # ★ R119-4z：**默认 False（走 CPU）**。35b-a3b 专用显存约 4.7GB（另约 16GB 落在主机内存）、整卡 8GB ⇒ 留给
        #   MeloTTS+CLAP 只剩 ~0.9GB，余量临界（真机闪退）。CPU 合成慢 10–22×，
        #   但口播离线预缓冲，用户无感；换来的是整卡不再被 TTS 挤爆。
        "melotts_gpu": _env_or(False, "MUSICAI_MELOTTS_GPU"),
        # MeloTTS 音色（说话人）：内置各语言默认说话人（ZH 仅 1 种；EN 含多种口音）。
        # 空 = 用引擎默认说话人；指定 key（如 "ZH" / "EN-Default"）切换。
        "melotts_speaker": _env_or("ZH", "MUSICAI_MELOTTS_SPEAKER"),
        # MeloTTS 自定义模型目录（拖拽/选择导入的「主播音色」模型权重所在文件夹）。
        # 留空则用内置模型；填入有效目录则优先加载该目录下的自定义音色（加载失败自动回退内置）。
        "melotts_model_dir": _env_or("", "MUSICAI_MELOTTS_MODEL_DIR"),
        # ---- MeloTTS 合成参数（设置页「语音 TTS · MeloTTS 声音调校」滑块可调） ----
        # 默认按「沉稳播报感」调校：放慢语速、低韵律起伏、低噪声、低波动。
        "melotts_speed": 0.88,          # 语速（越小越慢、越沉稳）
        "melotts_sdp_ratio": 0.12,      # 韵律随机度（越低越规整、播报感越强）
        "melotts_noise_scale": 0.32,    # 采样噪声（越低越干净稳定）
        "melotts_noise_scale_w": 0.45,  # 噪声带宽（越低节奏越规整）
        # ---- MeloTTS 音频后处理（ffmpeg 链，强化播报质感） ----
        # 启用后，每段 MeloTTS 合成输出会经 ffmpeg 做：低频/中频提升 + 高频微亮 +
        # 压缩器(播报感) + 轻微回声(空间感) + loudnorm 统一响度。
        "melotts_postprocess": True,        # ffmpeg 二进制路径：留空=自动探测（imageio-ffmpeg 自带 / 系统 PATH 上的 ffmpeg）。
        "melotts_ffmpeg_path": _env_or("", "MUSICAI_FFMPEG_PATH"),
        # ---- MeloTTS ffmpeg 后处理「参数」（设置页「后处理参数」滑块可调） ----
        # 与上面 melotts_postprocess 开关配合：开关开时，按这 7 个滑块拼出后处理链
        # （highshelf×3 + acompressor + aecho + loudnorm），强化「沉稳播报感」。
        # highshelf 三段增益（dB）：低频 120Hz / 中频 250Hz / 高频 3000Hz。
        "melotts_pp_hs1": 5.0,       # 低频增益（默认 +5dB，厚实沉稳）
        "melotts_pp_hs2": 3.0,       # 中频增益（默认 +3dB）
        "melotts_pp_hs3": 2.0,       # 高频微亮（默认 +2dB）
        # acompressor 压缩器：压低动态、强化播报质感。
        "melotts_pp_comp_th": -20.0, # 阈值 threshold（dB，默认 -20）
        "melotts_pp_comp_ratio": 4.0,  # 压缩比 ratio（默认 4:1）
        # aecho 回声（空间感，0=关闭）。
        "melotts_pp_echo": 0.5,      # 湿声比例（默认 0.5）
        # loudnorm 统一响度。
        "melotts_pp_loud_i": -16.0,  # 目标集成响度 I（LUFS，默认 -16）
        # 片段间拼接静音（毫秒），防止不同引擎/语种音色突兀相接。
        "segment_silence_ms": 80,
        # 拼接输出统一采样率（VOICEVOX 原生 24000，MeloTTS 为 44100；统一重采样到该值）。
        "segment_sr": 24000,
        # ---- IndexTTS 2.5（多语言 ZH/EN/JA；推理跑在 Python 3.11 常驻 worker）----
        # 主进程是 3.13，跨版本不能 in-process import ⇒ 走本地 HTTP worker
        # （tools/indextts/worker.py）。以下键留空即用约定路径。
        "indextts_port": _env_or(8799, "MUSICAI_INDEXTTS_PORT"),
        "indextts_venv": _env_or("", "MUSICAI_INDEXTTS_VENV"),          # 留空=.envs/indextts-v2
        "indextts_model_dir": _env_or("", "MUSICAI_INDEXTTS_MODEL_DIR"),  # 留空=models/IndexTTS-2.5
        "indextts_ref": "",        # ★ 三语统一音色：非空则中/英/日全用这一份参考音
        "indextts_ref_zh": "",     # 留空=data_runtime/f5_voicevox_verification/normal_zh.wav
        "indextts_ref_ja": "",
        "indextts_ref_en": "",
        "indextts_timeout": 300,   # 单次合成超时（秒）
        "indextts_autostart": True,
        # 后处理：与 MeloTTS 共用同一条 ffmpeg 链 + 同一组 7 个滑块（melotts_pp_*）
        "indextts_postprocess": True,
        # ★ 用完让位：合成结束静置 N 秒无新请求就卸载模型，把 5.4G 显存还给大模型。
        #   8GB 卡上两者同驻会互相换页（实测 LLM 与 TTS 都半速）。
        "indextts_unload_after_synth": True,
        "indextts_unload_idle_s": 8.0,
    },
    # ---------- GPU 显存额度划分（解放 qwen / 兼顾 melo） ----------
    # 本机 RTX 4060(8GB) 上，Ollama(qwen3.6:35b-a3b 常驻 ~5.5GB) 与 MeloTTS(~0.4GB) 共享显存。
    # 目标：给 qwen 留足常驻显存且不被 MeloTTS 挤占，同时给 MeloTTS 固定一片上限。
    "gpu": {
        # Ollama 显存上限（MiB）。start_app.py 拉起 ollama serve 前写入环境变量
        # OLLAMA_MAX_VRAM（字节），使 Ollama 永不占满整卡，给 MeloTTS 留位。
        # 0 表示不限制（沿用 Ollama 默认行为）。默认 5500MiB（8GB 卡留 ~2.5GB 余量）。
        "ollama_max_vram_mb": _env_or(5500, "MUSICAI_OLLAMA_MAX_VRAM_MB"),
        # 本进程可用的显存占整卡比例上限。main.py 启动早期对 torch 设置
        # set_per_process_memory_fraction。★ 2026-11-17 task-1：本进程已无 TTS
        #   （MeloTTS 退休；IndexTTS 跑在独立 worker，**不受**此上限约束），
        #   实际只有 CLAP 在用。键名保留 `melotts_gpu_fraction` 以兼容现有
        #   config.json；如设置新键 `process_gpu_fraction` 则优先。
        # 范围 0.05~0.95，默认 0.30（~2.4GB，仅作硬上限保护）。
        "melotts_gpu_fraction": _env_or(0.30, "MUSICAI_MELOTTS_GPU_FRACTION"),
        # 从本进程上限里划给 CLAP 音频语义编码的「子预算」（占整卡）。
        # 默认 0.05（~0.4GB，足够装下 CLAP 音频编码器），仅作语义记账；
        # 实际进程硬上限仍是 melotts_gpu_fraction/process_gpu_fraction。
        "clap_gpu_fraction": _env_or(0.05, "MUSICAI_CLAP_GPU_FRACTION"),
    },
    # 检索层：联网 / 离线 / 浏览器内核 运行时切换，禁止信息污染（每轮清理上一轮上下文）
    "search": {
        "mode": "browser",        # browser=本地「浏览器内核搜索服务」(tools/websearch_service，替代 DDG)
                                  # online=联网(DuckDuckGo) | offline=本地向量库(sqlite-vec)
        "online_provider": "duckduckgo",
        "max_results": 5,
        "offline_top_k": 5,
        # browser 模式专用：本地搜索服务地址与默认引擎（需先启动该服务）
        "browser_url": "http://127.0.0.1:8787",
        # ★ 2026-09-21：默认 `chain` = **服务端一跳到底的降级链**
        #   （`bing_rss` → `bing` → `360`，取第一个非空）。理由：单引擎会被整段
        #   限流（实测 Bing 对无头浏览器风控：所有查询 ~5.8s 返 0 条），
        #   而 `bing_rss` 实测连发 10 次**零限流**、每次 0.31~0.35s。
        #   也可显式填 `bing_rss` / `bing` / `360` 锁定单一引擎。
        "browser_engine": "chain",
        # 离线知识库构建来源开关（首次建库时读取）
        "offline_sources": ["id3", "artist_wiki", "song_story"],
    },
    "sources": {
        "netease": {
            "enabled": False,
            "backend": "musicbox",
            "musicbox_command": "musicbox",
            "api_base": _env_or("http://127.0.0.1:3000", "MUSICAI_NCM_API"),
            "cookie": _env_or("", "MUSICAI_NCM_COOKIE"),
            "phone": _env_or("", "MUSICAI_NCM_PHONE"),
            "password": _env_or("", "MUSICAI_NCM_PASSWORD"),
            "default_playlist_id": _env_or("", "MUSICAI_NCM_PLAYLIST"),
            "musicbox_uid": "",   # 网易云用户ID：音乐盒（导入"我喜欢的音乐"）
        },
        "qqmusic": {
            "enabled": False,
            # 本机运行的 qq-music-api 服务（默认 127.0.0.1:3200）；
            # 支持「扫码登录」：设置页点「扫码登录 QQ音乐」即可，
            # 登录后 Cookie 自动写入并 DPAPI 加密持久化。
            "api_base": _env_or("http://127.0.0.1:3200", "MUSICAI_QQ_API"),
            "cookie": "",       # 扫码登录后自动填写（DPAPI 加密）
            "uin": "",          # 扫码登录后记录 QQ 号（非机密）
            "default_playlist_id": _env_or("", "MUSICAI_QQ_PLAYLIST"),
            # 应用启动时自动后台拉起 qq-music-api（默认开），保证「扫码登录」按钮
            # 开箱即用、不再报 WinError 10061。设为 False 则需手动用 start-qq-api.bat
            # 启动，或设 MUSICAI_QQ_AUTOSTART=false 关闭自动拉起。
            "auto_start": _env_or(True, "MUSICAI_QQ_AUTOSTART"),
        },
        "spotify": {
            "enabled": False,
            "api_base": "",   # 可选：填一个 Spotify 兼容代理地址（可播放直链）
            "client_id": "",  # 可选：官方 Client Credentials（仅元数据）
            "client_secret": "",
            "cookie": "",
            "default_playlist_id": "",
        },
        "local": {
            "enabled": True,
            "roots": list(MUSIC_LIBRARY_ROOTS),
        },
    },
    "roam_strategy": "continuous",
    "auto_play": False,
    # 推荐落地模糊匹配：提高 AI 推荐歌名「实际播得出」的成功率。
    "match": {
        # 是否启用 rapidfuzz 模糊匹配（关闭则退回精确/包含匹配）。
        "fuzzy_enabled": _env_or(True, "MUSICAI_FUZZY_ENABLED"),
        # 模糊匹配阈值（0~100）。歌名模糊分 >= 阈值即视为命中；作者命中保持
        # 「准确或包含」（不模糊放宽），避免张冠李戴。默认 85。
        # 例：AI 推荐《Bohemian Rhapsody (Remastered)》，本地有《Bohemian Rhapsody》
        # 也能命中（括号/版本后缀被 token 化吸收）。
        "fuzzy_threshold": _env_or(85, "MUSICAI_FUZZY_THRESHOLD"),
    },
    # 推荐偏好（熟悉度级别）：0=更熟悉, 1=略熟悉, 2=略陌生, 3=更陌生。
    # 低档=严格贴合用户已有品味/曲库（听熟悉的），高档=大胆跳出舒适区探索新风格。
    # 该值直接作为关键变量注入推荐提示词，决定 AI 选歌的「熟悉↔探索」倾向，
    # 并持久化以保证重启后保留用户上次的偏好设置。
    "rec_pref": 0,
    # 手动城市覆盖：留空则自动定位（IP / 电脑位置，可能不准）；
    # 填了城市名则始终优先使用，解决 IP 地理库误判（如长春被定位成沈阳）。
    "geo": {"city": ""},
    # 可自定义的 AI 提示词（留空/内置默认则用 ai/prompts.py 内置默认）
    #   taste_output       : 口味分析·内容输出（可编辑，叠加在锁定的信息获取提示词上）
    #   broadcast(_output) : 口播内容·内容输出（可编辑，作为 DJ 人设补充）
    #   transition_output  : 转场·内容输出（可编辑，创作原则/风格参考）
    #   reco_explain       : 推荐理由系统指令（可编辑）
    # 注：口味分析/口播/转场 的"信息获取"提示词为系统固定（不可改），见 ai/prompts.py。
    "prompts": {
        "taste_output": TASTE_OUTPUT_DEFAULT,
        "broadcast": "",                 # 兼容旧键
        "broadcast_output": BROADCAST_OUTPUT_DEFAULT,
        # ★ 2026-11-17：默认**留空**。转场契约已经全在 TRANSITION_SYSTEM 里；
        #   这里再塞一份默认值 ⇒ 运行时两份逐字相同的提示词叠加（实测 1458 字），
        #   而转场本身只有 10~25 字，禁用词/字数规则还会各出现两次。
        #   用户想加个人风格时才在这里写。
        "transition_output": "",
        "reco_explain": "",
    },
    # ---------- UI 皮肤 / 控件透明度（插件化） ----------
    # 当前激活皮肤 key（对应 plugins/skins/ 下的插件，或内置 "default"）。
    # opacity 为各控件区域不透明度（0~1）：用户可在设置中调节，覆盖皮肤默认值，
    # 实现「控件透明度可设置」。effects / background_mode 为前端效果开关，由 UI 层读取。
    # main_ui：当前激活主界面插件 key——"classic" 为内置经典界面；
    #   其余为 plugins/ui/ 下的可插拔主界面。改这个值即可整体替换整套前端
    #   （符合项目「主界面可插件化」要求）。注：原 OpenGL 液态玻璃主界面（liquid_glass
    #   插件）已废弃，前端改为 HTML/CSS 方案，本键回退为 "classic"。
    "ui": {
        "skin": "default",
        "main_ui": "classic",
        "opacity": {
            "window": 1.0,
            "panel": 0.82,
            "card": 0.70,
            "text": 1.0,
        },
        "effects": {
            "water_bg": True,
            "particles": True,
        },
        "background_mode": "image",   # image | shader | solid（渲染模式总开关）
        "background_fit": "cover",    # cover | fit | stretch | parallax（媒体背景适配）
    },
}


def _deep_update(base: dict, over: dict) -> dict:
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


class UserConfig:
    """应用运行时配置：从 config.json 载入，可被"设置"页改写并保存。"""

    def __init__(self) -> None:
        self.data = self._load()
        if self._has_plaintext_secrets():
            # Migrate legacy plaintext config at startup on supported systems.
            try:
                self.save()
            except Exception:
                pass

    _SECRET_PATHS = (
        ("ai", "api_key"),
        ("sources", "netease", "cookie"),
        ("sources", "netease", "password"),
        ("sources", "qqmusic", "cookie"),
        ("sources", "spotify", "client_secret"),
        ("sources", "spotify", "cookie"),
    )

    def _has_plaintext_secrets(self) -> bool:
        for path in self._SECRET_PATHS:
            obj = self.data
            for key in path[:-1]:
                obj = obj.get(key, {})
            value = obj.get(path[-1], "")
            if value and not is_protected(value):
                return True
        return False

    def _decrypt_secrets(self, data: dict) -> dict:
        for path in self._SECRET_PATHS:
            obj = data
            for key in path[:-1]:
                obj = obj.get(key, {})
            if path[-1] in obj and obj[path[-1]]:
                try:
                    obj[path[-1]] = unprotect(obj[path[-1]])
                except Exception:
                    obj[path[-1]] = ""
        return data

    def _encrypted_copy(self) -> dict:
        out = copy.deepcopy(self.data)
        for path in self._SECRET_PATHS:
            obj = out
            for key in path[:-1]:
                obj = obj.get(key, {})
            value = obj.get(path[-1], "")
            if value and not is_protected(value):
                obj[path[-1]] = protect(value)
        return out

    def _load(self) -> dict:
        if USER_CONFIG_PATH.exists():
            try:
                with open(USER_CONFIG_PATH, encoding="utf-8") as f:
                    loaded = json.load(f)
                merged = copy.deepcopy(DEFAULT_USER_CONFIG)
                _deep_update(merged, loaded)
                # 兼容迁移：旧版 prompts.dj_system -> broadcast_output
                p = merged.setdefault("prompts", {})
                if not p.get("broadcast_output"):
                    p["broadcast_output"] = (
                        p.pop("dj_system", None)
                        or p.get("broadcast") or BROADCAST_OUTPUT_DEFAULT)
                if "taste_output" not in p:
                    p["taste_output"] = TASTE_OUTPUT_DEFAULT
                if "transition_output" not in p:
                    p["transition_output"] = ""
                # ★ 2026-11-17 去重迁移：旧版把默认值整份抄进了 config，
                #   与 TRANSITION_SYSTEM 逐字相同 ⇒ 运行时拼成两份。
                #   这里若发现"与内置默认一致"，直接清空（用户真改过的不动）。
                if (p.get("transition_output") or "").strip() == \
                        TRANSITION_OUTPUT_DEFAULT.strip():
                    p["transition_output"] = ""
                if "reco_explain" not in p:
                    p["reco_explain"] = ""
                # 内容安全迁移：移除口播默认示例里易触发内容审核的词（如"失恋"），
                # 仅替换已知的风险句子，保留用户其余自定义文字。
                _RISKY = '"他写这首歌的时候刚失恋，所以副歌才那么狠"'
                _SAFE = '"这首歌的副歌录了十几遍才过关，就为了那一下够狠"'
                bo = p.get("broadcast_output", "")
                if _RISKY in bo:
                    p["broadcast_output"] = bo.replace(_RISKY, _SAFE)
                return self._decrypt_secrets(merged)
            except Exception as e:  # noqa
                import logging
                logging.getLogger("MusicAI").warning("读取 config.json 失败，使用默认: %s", e)
        return copy.deepcopy(DEFAULT_USER_CONFIG)

    def save(self) -> None:
        USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = self._encrypted_copy()
        tmp = USER_CONFIG_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, USER_CONFIG_PATH)

    def reload(self) -> None:
        self.data = self._load()

    # ---- 便捷访问 ----
    @property
    def ai(self) -> dict:
        return self.data["ai"]

    @property
    def tts(self) -> dict:
        return self.data["tts"]

    @property
    def gpu(self) -> dict:
        return self.data.get("gpu", {})

    @property
    def match(self) -> dict:
        return self.data.get("match", {})

    @property
    def sources(self) -> dict:
        return self.data["sources"]

    @property
    def prompts(self) -> dict:
        return self.data.get("prompts", {})

    @property
    def ui(self) -> dict:
        return self.data.setdefault("ui", {})

    @property
    def ai_enabled(self) -> bool:
        a = self.data["ai"]
        # ollama 本地后端无需 api_base/api_key 即可视为"已启用"（可达性由 LLMClient 运行时校验）
        if (a.get("backend") or "api").lower() == "ollama":
            return True
        return bool(a.get("api_base") and a.get("api_key"))


# 全局单例：所有模块（brain/tts/sources）运行时读取它
user_config = UserConfig()
