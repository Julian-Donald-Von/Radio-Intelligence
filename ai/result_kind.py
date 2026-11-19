# -*- coding: utf-8 -*-
"""R119-16 · **L1 结果类型判定**（检索筛选闸的第一级，纯函数、无依赖）。

为什么单独一个模块：`ai/search.py`（筛选层）与 `ai/page_fetch.py`（正文回捞）**都要**用它 ——
放一起会循环导入，各写一份又会漂移（本项目那条"同一个概念写两处"的设计债，见
`docs/r119_search_gate_design.md` §2）。所以：**只有这里一份**。

判据只用三样东西：`域名 + 标题 + 摘要` —— 确定性、可复现、可"见红"，不调模型。

★ 最有价值的是 **`homonym`（同名异物）**：L2 的作品锚问"这条在不在讲这首歌"，
而它问"**这条在讲哪一首 / 哪一个东西**"。实测 `波西米亚人（民族）_百度百科`
就属于它（事实词仅 3 个，正文讲的是民族）—— 先隔出去，L2 才不会被同名词条糊住。
"""
from __future__ import annotations

import os
import re
import unicodedata

# ── 类型枚举 ────────────────────────────────────────────────────────────────
ENCYCLOPEDIA = "encyclopedia"
MUSIC_PLATFORM = "music_platform"
PRESS_REVIEW = "press_review"
LYRICS_PAGE = "lyrics_page"
VIDEO_MEDIA = "video_media"
DOWNLOAD_PORTAL = "download_portal"
DICTIONARY_TERM = "dictionary_term"
HOMONYM = "homonym"
ADMIN_NOTICE = "admin_notice"
FORUM_QA = "forum_qa"
CONTENT_FARM = "content_farm"
UNKNOWN = "unknown"

# ★ 可直接丢掉的类型（歌曲查询）。`admin_notice` 在环境查询里只降权，故不在此列。
DROP_KINDS = (VIDEO_MEDIA, DOWNLOAD_PORTAL, DICTIONARY_TERM, HOMONYM, CONTENT_FARM)
# ★ 值得"回捞正文"的类型：仅百科/词条（正文最厚、事实最密，且取自静态词条页）。
#   已剔除 `MUSIC_PLATFORM`（网易云/Apple Music 等返回 SPA 站点导航 chrome，非歌曲事实）
#   与 `PRESS_REVIEW`（乐评/专访站点结构杂，正文回捞收益低、噪声高）。R119-27 实机回归确认。
FETCH_WORTHY = (ENCYCLOPEDIA,)

_DOM = {
    ENCYCLOPEDIA: ("baike.baidu.com", "wikipedia.org", "wikiwand.com", "baike.sogou.com",
                   "baike.so.com", "sogou.com/baike"),
    MUSIC_PLATFORM: ("music.163.com", "y.qq.com", "c.y.qq.com", "music.douban.com",
                     "douban.com", "music.apple.com", "kkbox.com", "streetvoice.com",
                     "genius.com", "xiami.com", "5nd.com"),
    LYRICS_PAGE: ("mojim.com", "geci", "kuge", "lrc", "lyric", "gqs.io", "91q.com",
                  "ting30.com", "2ting.com"),
    VIDEO_MEDIA: ("v.qq.com", "iqiyi.com", "youku.com", "douyin.com", "mgtv.com",
                  "bilibili.com/video", "y.qq.com/n/ryqq/mv", "music.163.com/#/mv",
                  "youtube.com", "b23.tv", "haokan.baidu.com", "kuaishou.com"),
    DOWNLOAD_PORTAL: ("9ku.com", "kugou.com", "kuwo.cn", "gequbao", "zhangxinhua",
                      "51vv", "music.51vv", "cctv.com/music"),
    DICTIONARY_TERM: ("dictionary.", "dict.", "hanyu.baidu.com", "zdic.net",
                      "cidian", "iciba.com", "youdao.com", "cambridge.org"),
    FORUM_QA: ("zhidao.baidu.com", "zhihu.com", "tieba.baidu.com", "douban.com/group",
               "reddit.com", "quora.com"),
    ADMIN_NOTICE: (".gov.cn", "gov.com.cn"),
}

_TITLE_RULES = (
    (LYRICS_PAGE, ("歌词", "lyric", "lrc", "歌谱", "简谱")),
    (VIDEO_MEDIA, ("mv", "在线观看", "在线看", "完整版视频", "高清", "1080p", "超清",
                   "中文字幕", "花絮")),
    (DOWNLOAD_PORTAL, ("下载", "无损", "伴奏", "铃声", "免费听", "mp3")),
    (DICTIONARY_TERM, ("翻译", "释义", "读音", "英文单词", "汉语词语", "字义", "词典",
                       "词语", "词组", "的意思")),
    (ADMIN_NOTICE, ("通知", "公告", "权责", "印发", "批复", "招标", "采购", "政府", "人大")),
    (PRESS_REVIEW, ("乐评", "专访", "深度", "解读", "盘点", "为何", "背后", "故事")),
)

# ★ 括号释义里**明确不是歌曲**的那批词 ⇒ 同名异物
_GLOSS_OTHER = {"民族", "民系", "族群", "人种", "汉语词语", "词语", "词组", "英文单词",
                "单词", "字", "汉字", "地理", "地区", "城市", "国家", "省", "市",
                "人物", "演员", "歌手", "作家", "导演", "电影", "电视剧", "节目", "综艺",
                "品牌", "公司", "企业", "软件", "工具", "游戏", "平台", "网站", "app",
                "植物", "动物", "化学物质", "物理量", "数学概念", "计算机术语", "算法"}
# ★ 括号释义里**是歌曲**的那批（正面信号，避免把 `晴天（歌曲）` 误判成异物）
_GLOSS_SONG = ("歌曲", "单曲", "专辑", "音乐作品", "流行歌曲", "摇滚歌曲", "民谣",
               "主题曲", "插曲", "片尾曲", "片头曲", "日语歌曲", "粤语歌曲", "韩语歌曲")

_GLOSS_RE = re.compile(r"[（(]([^）)]{1,12})[）)]")
_NOISE_RE = re.compile(r"[\s\u3000·・,，。.、:：;；\-—_\|()（）\[\]【】\"'“”‘’!！?？]")
_YEAR_RE = re.compile(r"(?<![0-9])(?:19|20)[0-9]{2}(?![0-9])")
# 中文停用词（判"像不像自然文本"，借关键词堆砌检测那条思路）
_STOP = ("的", "了", "是", "在", "和", "与", "也", "就", "都", "而", "被", "把", "为",
         "于", "这", "那", "有", "以", "及其", "并且", "其中")
# 内容农场的罐头味：这些词在真百科里很少连着出现
_FARM_HINTS = ("小编", "今天小编", "一起来看看", "你知道吗", "震惊", "赶紧收藏",
               "点击查看", "本文", "综上所述", "希望对大家有帮助")


# 零宽字符（复制粘贴 / 网页常带，会悄悄破坏归一比对）
_ZW_RE = re.compile("[\u200b-\u200f\ufeff]")
# 日文汉字 → 简体（覆盖常见音乐人 / 歌名；旧版只有 20 字，归一时会漏）
_JP2SC = str.maketrans({
    "師": "师", "澤": "泽", "黒": "黑", "龍": "龙", "學": "学", "國": "国", "會": "会",
    "來": "来", "櫻": "樱", "樹": "树", "橋": "桥", "優": "优", "島": "岛", "葉": "叶",
    "戶": "户", "藝": "艺", "廣": "广", "灣": "湾", "東": "东", "馬": "马", "樂": "乐",
    "歷": "历", "經": "经", "產": "产", "電": "电", "視": "视", "麼": "么", "愛": "爱",
    "義": "义", "書": "书", "號": "号", "頭": "头", "監": "监", "劇": "剧", "總": "总",
    "動": "动", "畫": "画", "團": "团", "終": "终", "發": "发", "車": "车", "場": "场",
    "園": "园", "關": "关", "實": "实", "銀": "银", "體": "体", "養": "养", "遠": "远",
    "層": "层", "狀": "状", "說": "说", "話": "话", "語": "语", "讀": "读", "課": "课",
    "資": "资", "勢": "势", "與": "与", "豐": "丰", "寶": "宝", "專": "专", "執": "执",
    "報": "报", "導": "导", "尋": "寻", "將": "将", "時": "时", "間": "间", "親": "亲",
    "顏": "颜", "顯": "显", "飛": "飞", "飯": "饭", "館": "馆", "長": "长", "門": "门",
    "問": "问", "題": "题", "風": "风", "雲": "云", "處": "处", "備": "备", "條": "条",
    "歲": "岁", "戰": "战", "單": "单", "堅": "坚", "壇": "坛", "夢": "梦", "運": "运",
    "選": "选", "舉": "举", "點": "点", "熱": "热", "歡": "欢", "燈": "灯", "禮": "礼",
    "職": "职", "員": "员", "據": "据", "擊": "击", "斷": "断", "舊": "旧", "繼": "继",
    "續": "续", "羅": "罗", "譯": "译", "詞": "词", "調": "调", "謝": "谢", "聲": "声",
    "網": "网", "際": "际", "對": "对", "開": "开", "閉": "闭", "陰": "阴", "陽": "阳",
    "縣": "县", "鄉": "乡", "鎮": "镇", "廳": "厅", "聰": "聪", "環": "环", "圖": "图",
    "當": "当", "後": "后", "萬": "万", "贊": "赞", "參": "参", "觀": "观", "覺": "觉",
    "響": "响", "麗": "丽", "巖": "岩", "帶": "带", "協": "协", "興": "兴", "聯": "联",
    "認": "认", "輸": "输", "輕": "轻", "轉": "转",
})
# ★ 别名表（variant → canonical）：把**两边都归一成 canonical**，根上解决
#   「歌手对不上就丢」（如 `Queen ↔ 皇后乐队` / `买辣椒也用券 ↔ 冯沁苑`）。
#   用 `MUSICAI_GATE_ALIAS=0` 可整体关掉（回归对照用）。
_ALIASES = (
    ("皇后乐队", "queen"), ("皇后樂隊", "queen"),
    ("米津玄師", "米津玄师"),
    ("冯沁苑", "买辣椒也用券"),
    ("高橋優", "高桥优"), ("高桥优", "高桥优"),
    ("周杰倫", "周杰伦"), ("周杰伦", "周杰伦"),
    ("李榮浩", "李荣浩"),
    ("濱崎步", "滨崎步"), ("濱崎あゆみ", "滨崎步"), ("滨崎步", "滨崎步"),
    ("宇多田光", "宇多田光"),
    ("矢澤永吉", "矢泽永吉"), ("桑田佳祐", "桑田佳祐"),
    ("小野莉莎", "小野丽莎"), ("玉置浩二", "玉置浩二"),
    ("德永英明", "德永英明"), ("中森明菜", "中森明菜"),
    ("松任谷由實", "松任谷由实"), ("山下達郎", "山下达郎"),
    ("竹内瑪莉亞", "竹内玛莉亚"),
    ("新垣結衣", "新垣结衣"), ("綾瀨遙", "绫濑遥"), ("長澤雅美", "长泽雅美"),
    ("石原聰美", "石原里美"), ("宮崎駿", "宫崎骏"), ("久石讓", "久石让"),
    ("坂本龍一", "坂本龙一"), ("中島美嘉", "中岛美嘉"), ("椎名林檎", "椎名林檎"),
    ("恰克與飛鳥", "恰克與飛鳥"),
    ("披頭士", "the beatles"), ("披头士", "the beatles"),
    ("滾石樂隊", "the rolling stones"), ("滚石乐队", "the rolling stones"),
    ("老鷹樂隊", "eagles"), ("猫王", "elvis presley"),
    ("aiko", "aiko"), ("mr.children", "mr.children"),
)


def normalize_entity(s: str) -> str:
    """实体名归一（单一来源，search 与 result_kind 共用）：

    零宽剥离 → NFKC → 日文汉字折简体 → 别名表 → 去标点空白 + 小写。
    """
    s = _ZW_RE.sub("", str(s or ""))
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(_JP2SC)
    low = s.lower()
    if os.environ.get("MUSICAI_GATE_ALIAS", "1") != "0":
        for a, c in _ALIASES:
            if a in low:
                low = low.replace(a, c)
    return _NOISE_RE.sub("", low)


def norm(s: str) -> str:
    """实体名归一：去标点空白 + 转小写（与 `ai/search._norm_entity` 共用 `normalize_entity`）。"""
    return normalize_entity(s)


def stopword_ratio(text: str) -> float:
    """停用词占比。自然中文散文一般 ≥6%；罐头文字显著偏低。"""
    t = str(text or "")
    if len(t) < 20:
        return 1.0                       # 太短不判（避免误伤）
    hit = sum(t.count(w) for w in _STOP)
    return hit / float(len(t))


def looks_like_farm(text: str) -> bool:
    """罐头/关键词堆砌判据（**只在 unknown 分支用**，避免误伤已知类型）。"""
    t = str(text or "")
    if not t:
        return False
    if any(h in t for h in _FARM_HINTS):
        return True
    strokes = len(re.findall(r"[\u4e00-\u9fff]", t))
    if strokes >= 60 and stopword_ratio(t) < 0.03:
        return True
    # 同 4-gram 重复 ≥3 次（堆砌的典型形态）
    grams = [t[i:i + 4] for i in range(max(0, len(t) - 3))]
    if len(grams) >= 40:
        g0 = grams[0]
        if grams.count(g0) >= 3:
            return True
    return False


def _host(url: str) -> str:
    try:
        return (url or "").split("/")[2].lower()
    except Exception:                                                     # noqa
        return ""


_RULE_MAP = dict(_TITLE_RULES)
# ★ 标题里出现就是**硬负例**的四类（与域名无关）：标题写"完整版视频mv"就是视频页，
#   哪怕它挂在 `zhidao.baidu.com`（实测真机里就有 `周杰伦的《晴天》的歌词_百度知道`）。
_NEG_FIRST = (LYRICS_PAGE, VIDEO_MEDIA, DOWNLOAD_PORTAL, DICTIONARY_TERM)


def classify(title: str, url: str, snippet: str = "", *,
             entity_q: str = "", artist_q: str = "") -> str:
    """返回一个 kind（见模块头枚举）。

    `entity_q` / `artist_q`：查询侧的歌名/歌手（归一前）。**只在判 `homonym` 时用到** ——
    不传也能跑（退化为"只看括号释义"）。

    判序（**顺序本身就是判据**，别随意调）：
      ① 标题硬负例（歌词/视频/下载/词典）—— 与域名无关；
      ② 域名（条目 / 平台 / 歌词站 / 视频站 / 下载站 / 词典 / 问答 / 政府）；
      ③ 其余标题规则（行政公告 / 访谈乐评）；
      ④ **同名异物**；
      ⑤ 内容农场（**只在走到这里才判**，避免误伤已知类型）；
      ⑥ unknown。
    """
    t = str(title or "")
    tl = t.lower()
    blob = "%s %s" % (t, str(snippet or ""))
    host = _host(url)
    full = (url or "").lower()

    for kind in _NEG_FIRST:                       # ①
        if any(w in tl for w in _RULE_MAP[kind]):
            return kind

    for kind, doms in _DOM.items():               # ②
        if any(d in host or d in full for d in doms):
            if kind in (ENCYCLOPEDIA, MUSIC_PLATFORM, FORUM_QA):
                # ★ 先判"是不是另一件东西"，再认这个域名
                return HOMONYM if _is_homonym(t, entity_q, artist_q) else kind
            return kind

    for kind in (ADMIN_NOTICE, PRESS_REVIEW):     # ③
        if any(w in tl for w in _RULE_MAP[kind]):
            return HOMONYM if _is_homonym(t, entity_q, artist_q) else kind

    if _is_homonym(t, entity_q, artist_q):        # ④
        return HOMONYM
    if looks_like_farm(blob):                     # ⑤
        return CONTENT_FARM
    return UNKNOWN                                # ⑥


def _is_homonym(title: str, entity_q: str, artist_q: str) -> bool:
    """同名异物：**这条在讲另一个东西**。

    两条判据（任一）：
      ① 标题里有括号释义，且释义属于"明确不是歌曲"的那批（`X（民族）` `X（英文单词）`）；
      ② 标题里**既不出现**查询实体、**也不出现**歌手（归一后）——
         例：查询 `Bohemian Rhapsody`，标题 `波西米亚人（民族）_百度百科`。
    ⚠ 反例必须放行：`晴天（2003年周杰伦演唱的流行摇滚歌曲）_百度百科` 含 `晴天`，
      且释义里有"歌曲" ⇒ 绝不能判成异物（否则把最该留的那条丢了）。
    """
    if not title:
        return False
    m = _GLOSS_RE.search(title)
    if m:
        gloss = m.group(1)
        if any(w in gloss for w in _GLOSS_SONG):
            return False
        if gloss in _GLOSS_OTHER or any(w in gloss for w in _GLOSS_OTHER):
            return True
    nq, na = norm(entity_q), norm(artist_q)
    if not nq:
        return False
    nt = norm(title)
    if nq in nt:
        return False
    if na and na in nt:
        return False
    return True


def is_drop(kind: str, *, song: bool = True) -> bool:
    """该 kind 是否**直接丢弃**。`song=False`（环境类查询）时只丢视频/下载/词典/农场。"""
    if kind in (VIDEO_MEDIA, DOWNLOAD_PORTAL, DICTIONARY_TERM, CONTENT_FARM):
        return True
    return bool(song) and kind == HOMONYM


def score_bonus(kind: str) -> int:
    """类型分（L5 用）：条目 > 平台 > 访谈 > 问答。"""
    return {ENCYCLOPEDIA: 6, MUSIC_PLATFORM: 4, PRESS_REVIEW: 3,
            LYRICS_PAGE: 0, UNKNOWN: 0, ADMIN_NOTICE: -3,
            FORUM_QA: -2}.get(kind, 0)
