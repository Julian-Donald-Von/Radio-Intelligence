"""
天气获取：通过 Open-Meteo（免 API key）实时获取本机天气。

定位策略（无需任何 key）：
  1. 优先用本机公网 IP 经 ipwho.is / freeipapi.com 反查经纬度与城市；
  2. 传入 city 时，用 Open-Meteo 地理编码把城市名解析成经纬度；
  3. 均失败时回退到配置中的默认城市（仅用于口播文案）。

返回形如：
  {"text":"晴","temp":24,"city":"上海","wind":12,"humidity":60,
   "code":0,"lat":31.23,"lon":121.47}
"""
from __future__ import annotations

import logging
import threading

import requests

from config.settings import GeoConfig

logger = logging.getLogger("MusicAI.Weather")

# WMO weather code -> 中文天气描述
WMO = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "小毛雨", 53: "毛毛雨", 55: "中毛雨",
    56: "冻毛雨", 57: "冻毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "阵雨", 82: "强阵雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}


_geo_cache: dict = {}
_geo_lock = threading.Lock()


def locate_by_ip() -> dict:
    """用本机公网 IP 反查经纬度与城市。

    返回 {lat, lon, city, region, country}；失败返回空 dict。
    结果会缓存，避免重复请求。
    """
    with _geo_lock:
        if "geo" in _geo_cache:
            return _geo_cache["geo"]

    result: dict = {}
    providers = (
        ("https://ipwho.is/", "ipwho"),
        ("https://freeipapi.com/api/json", "freeipapi"),
    )
    for url, tag in providers:
        try:
            r = requests.get(url, timeout=4, headers={"User-Agent": "MusicAI/1.0"})
            d = r.json()
            if tag == "ipwho":
                if d.get("success") and d.get("latitude"):
                    result = {
                        "lat": float(d["latitude"]),
                        "lon": float(d["longitude"]),
                        "city": d.get("city") or "",
                        "region": d.get("region") or "",
                        "country": d.get("country") or "",
                    }
                    break
            else:
                if d.get("latitude"):
                    result = {
                        "lat": float(d["latitude"]),
                        "lon": float(d["longitude"]),
                        "city": d.get("cityName") or "",
                        "region": d.get("regionName") or "",
                        "country": d.get("countryName") or "",
                    }
                    break
        except Exception as e:  # noqa
            logger.warning("IP 定位失败 %s: %s", url, e)

    with _geo_lock:
        _geo_cache["geo"] = result
    return result


def _geocode(city: str) -> tuple[float, float] | None:
    """Open-Meteo 地理编码：城市名 -> (lat, lon)。"""
    try:
        r = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "zh"},
            timeout=5, headers={"User-Agent": "MusicAI/1.0"},
        )
        d = r.json()
        if d.get("results"):
            res = d["results"][0]
            return float(res["latitude"]), float(res["longitude"])
    except Exception as e:  # noqa
        logger.warning("地理编码失败: %s", e)
    return None


def _wmo(code) -> str:
    try:
        return WMO.get(int(code), "未知")
    except Exception:  # noqa
        return "未知"


def _fallback(city: str) -> dict:
    return {
        "text": "晴", "temp": 22, "city": city or GeoConfig.FALLBACK_CITY,
        "wind": 0, "humidity": 50, "code": 0, "lat": None, "lon": None,
    }


def get_weather(city: str | None = None) -> dict:
    """获取实时天气。优先本机 IP 定位；给定城市则先地理编码。

    city: 可选的城市名（如用户手动指定）。
    """
    lat = lon = None
    resolved_city = city or ""

    if city:
        coords = _geocode(city)
        if coords:
            lat, lon = coords
            resolved_city = city

    if lat is None:
        geo = locate_by_ip()
        if geo:
            lat, lon = geo["lat"], geo["lon"]
            resolved_city = resolved_city or geo.get("city") or GeoConfig.FALLBACK_CITY

    if lat is None:
        logger.warning("无法定位，使用演示天气")
        return _fallback(resolved_city)

    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m",
            },
            timeout=5, headers={"User-Agent": "MusicAI/1.0"},
        )
        cur = r.json().get("current", {})
        return {
            "text": _wmo(cur.get("weather_code")),
            "temp": round(float(cur.get("temperature_2m", 22))),
            "city": resolved_city or GeoConfig.FALLBACK_CITY,
            "wind": round(float(cur.get("wind_speed_10m", 0))),
            "humidity": round(float(cur.get("relative_humidity_2m", 50))),
            "code": int(cur.get("weather_code", 0)),
            "lat": lat, "lon": lon,
        }
    except Exception as e:  # noqa
        logger.warning("Open-Meteo 失败，使用演示天气: %s", e)
        return _fallback(resolved_city)
