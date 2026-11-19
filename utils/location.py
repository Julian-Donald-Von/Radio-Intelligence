"""
地点与时段：双冗余定位（IP + 电脑位置信息），以电脑位置信息为准；
以及中文时段判断（morning/afternoon/evening/night）。

定位策略：
  1. 优先"电脑位置信息"：
     · 实时定位（WinRT Geolocator，需系统授予位置权限，含超时保护）；
     · 若无可联网/无权限，回退到本机系统区域设置（Windows 国家/地区，无需网络）。
  2. 冗余"IP 定位"（复用 utils.weather.locate_by_ip）。
  3. 二者合并：电脑位置信息优先；仅当电脑侧无法给出城市时，再以 IP 的城市补全。
  4. 均失败 → 配置中的默认城市（仅用于口播文案）。
"""
from __future__ import annotations

import logging
import threading
import queue
import json
import urllib.request

from config.settings import GeoConfig

logger = logging.getLogger("MusicAI.Location")

_geo_cache: dict = {}
_geo_lock = threading.Lock()


def _cached(key: str, fn):
    with _geo_lock:
        if key in _geo_cache:
            return _geo_cache[key]
    val = fn()
    with _geo_lock:
        _geo_cache[key] = val
    return val


# --------------------------------------------------------------------------
# 1) 电脑位置信息
# --------------------------------------------------------------------------
def _winrt_location() -> tuple[float, float] | None:
    """尝试用 WinRT Geolocator 取实时经纬度（需系统授予位置权限）。

    用独立线程 + 超时保护，避免在无权限/无传感器环境下阻塞。
    未安装 winrt 或失败均返回 None。
    """
    try:
        import winrt.windows.devices.geolocation as gl  # type: ignore
    except Exception:
        return None
    q: queue.Queue = queue.Queue()

    def run() -> None:
        try:
            locator = gl.Geolocator()
            pos = locator.get_geoposition_async().get()  # 阻塞直到完成
            coord = pos.coordinate
            q.put((float(coord.latitude), float(coord.longitude)))
        except Exception as e:  # noqa
            q.put(e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=4)
    if not t.is_alive():
        res = q.get()
        if isinstance(res, tuple):
            return res
    logger.warning("WinRT 实时定位不可用（无权限/无传感器/超时），改用系统区域。")
    return None


def _windows_region() -> str:
    """Windows 系统区域（国家/地区），无需网络。失败返回空串。"""
    try:
        import ctypes
        GEOCLASS_NATION = 16
        GEO_FRIENDLYNAME = 8
        kernel32 = ctypes.windll.kernel32
        geoid = kernel32.GetUserGeoID(GEOCLASS_NATION)
        if geoid <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(256)
        n = kernel32.GetGeoInfoW(geoid, GEO_FRIENDLYNAME, buf, 255, 0)
        if n and n > 1:
            return buf.value
    except Exception:  # noqa
        pass
    return ""


def _reverse_geocode(lat: float, lon: float) -> dict:
    """经纬度 -> 城市/国家（Nominatim，带超时，网络失败时仅回坐标）。"""
    try:
        url = (
            "https://nominatim.openstreetmap.org/reverse?format=jsonv2"
            f"&lat={lat}&lon={lon}&zoom=10&accept-language=zh-CN"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "MusicAI/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.load(r)
        addr = (d.get("address") or {})
        city = (
            addr.get("city") or addr.get("town") or addr.get("county")
            or addr.get("state") or ""
        )
        country = addr.get("country") or ""
        return {"city": city, "country": country, "lat": lat, "lon": lon}
    except Exception as e:  # noqa
        logger.warning("反向地理编码失败: %s", e)
        return {"city": "", "country": "", "lat": lat, "lon": lon}


def get_computer_location() -> dict:
    """电脑位置信息：实时定位优先，回退系统区域。返回含 city/country 的字典。"""
    coords = _winrt_location()
    if coords:
        lat, lon = coords
        rev = _reverse_geocode(lat, lon)
        if rev.get("city"):
            return {**rev, "source": "computer"}
        # 有坐标但反向编码失败：带坐标返回，下游用 IP 补城市
        return {"city": "", "country": "", "lat": lat, "lon": lon, "source": "computer"}
    region = _windows_region()
    if region:
        return {"city": "", "country": region, "source": "computer"}
    return {}


# --------------------------------------------------------------------------
# 2) IP 定位（冗余）
# --------------------------------------------------------------------------
def get_ip_location() -> dict:
    try:
        from utils.weather import locate_by_ip
        geo = locate_by_ip()
        if geo:
            return {
                "city": geo.get("city", "") or "",
                "country": geo.get("country", "") or "",
                "region": geo.get("region", "") or "",
                "lat": geo.get("lat"), "lon": geo.get("lon"),
                "source": "ip",
            }
    except Exception as e:  # noqa
        logger.warning("IP 定位失败: %s", e)
    return {}


# --------------------------------------------------------------------------
# 3) 合并：手动覆盖 > 电脑位置信息 > IP 定位 > 默认城市
# --------------------------------------------------------------------------
def _manual_city() -> str:
    """用户在设置里手动指定的城市（最高优先级）。读取失败则回退空串。"""
    try:
        from config.settings import user_config
        return (user_config.data.get("geo", {}).get("city") or "").strip()
    except Exception:  # noqa
        return ""


def get_location() -> dict:
    """双冗余定位，优先级：手动覆盖 > 电脑位置信息 > IP 定位 > 默认城市。

    返回 {city, country, source, lat, lon}。
    source 标记实际生效的来源：manual / computer / ip / fallback。
    """
    override = _manual_city()
    # 手动覆盖：直接返回，不再打不可靠的 IP / WinRT 网络请求
    if override:
        return {"city": override, "country": "", "source": "manual",
                "lat": None, "lon": None}
    comp = _cached("computer", get_computer_location)
    ip = _cached("ip", get_ip_location)

    city = comp.get("city") or ip.get("city") or GeoConfig.FALLBACK_CITY
    country = comp.get("country") or ip.get("country") or ""
    if comp.get("city"):
        source = "computer"
    elif ip.get("city"):
        source = "ip"
    else:
        source = "fallback"
    return {
        "city": city,
        "country": country,
        "source": source,
        "lat": comp.get("lat") or ip.get("lat"),
        "lon": comp.get("lon") or ip.get("lon"),
    }


def get_city() -> str:
    """当前城市（用于口播）。双冗余，电脑位置优先。"""
    return get_location().get("city") or GeoConfig.FALLBACK_CITY


# --------------------------------------------------------------------------
# 时段
# --------------------------------------------------------------------------
def time_slot(ts: float | None = None) -> str:
    """返回中文时段与 key。"""
    import time
    h = time.localtime(ts or time.time()).tm_hour
    if 5 <= h < 11:
        return "morning", "清晨"
    if 11 <= h < 14:
        return "noon", "正午"
    if 14 <= h < 18:
        return "afternoon", "午后"
    if 18 <= h < 22:
        return "evening", "傍晚"
    return "night", "深夜"


_WEEK_CN = ("一", "二", "三", "四", "五", "六", "日")


def now_date(ts: float | None = None) -> tuple:
    """★ 2026-09-21：**日期**（用户要求："上传给大模型的除了时间之外还需要有日期"）。

    为什么必须有：口播原先只有「时段」（清晨/傍晚…）而没有日期；而检索资料给出的
    恰恰是**发行年份**（《晴天》2003-07-31、《Lemon》2018-03-14）。没有"今天几号"
    这个锚点，模型就算拿到了年份也推不出「二十多年前的作品」这类
    **由「资料 + 日期」直接算得**的说法（它不是编造），于是资料里最有画面感的
    那条事实反而用不上 —— 这正是"资料给了却没被用"的一个具体成因。

    返回 `(iso, 中文日期, 星期, 季节)`：
        ("2026-09-21", "2026年9月21日", "星期一", "秋季")
    """
    import time as _t
    lt = _t.localtime(ts or _t.time())
    iso = _t.strftime("%Y-%m-%d", lt)
    cn = "%d年%d月%d日" % (lt.tm_year, lt.tm_mon, lt.tm_mday)
    wd = "星期" + _WEEK_CN[lt.tm_wday]
    m = lt.tm_mon
    season = ("春季" if 3 <= m <= 5 else
              "夏季" if 6 <= m <= 8 else
              "秋季" if 9 <= m <= 11 else "冬季")
    return iso, cn, wd, season
