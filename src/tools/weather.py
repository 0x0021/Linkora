from __future__ import annotations

import datetime
import logging
import threading
import re
import time
import urllib.parse

from src.tools.base import BaseTool
from src.utils.net import ssrf_safe_get

logger = logging.getLogger(__name__)


def _http_get(url: str, timeout: int, retries: int = 3):
    """带重试的 GET，缓解沙箱/代理偶发的 TLS 断连（SSL EOF）。

    出站统一经 ssrf_safe_get（src.utils.net）：先校验 URL 公网可达并钉死 IP，
    杜绝 SSRF DNS 重绑定。
    """
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            return ssrf_safe_get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
        except ValueError:
            # SSRF 拦截/DNS 失败不应重试
            raise
        except Exception as e:  # 网络/TLS 瞬时错误，重试
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.5 + attempt * 0.5)
    assert last_err is not None
    raise last_err

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# Nominatim(OpenStreetMap) 专用请求头：其使用政策要求可识别的 User-Agent，
# 通用浏览器 UA 会被拒。低频个人调用足够；不做并发轰炸。
_NOMINATIM_HEADERS = {
    "User-Agent": "linkora-weather/0.1 (local dev; +https://openstreetmap.org)",
    "Accept": "application/json",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# Open-Meteo 使用的 WMO 天气代码 -> 中文描述
WMO_CODES = {
    0: "晴",
    1: "大致晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛雨",
    53: "毛毛雨",
    55: "大毛雨",
    56: "冻毛雨",
    57: "强冻毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "中阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴小冰雹",
    99: "雷阵雨伴大冰雹",
}

# WMO 代码 -> emoji（用于 markdown 卡片，纯文本装饰，不影响事实）
def _weather_emoji(code: int) -> str:
    if code in (0, 1):
        return "☀️"
    if code == 2:
        return "🌤️"
    if code == 3:
        return "☁️"
    if code in (45, 48):
        return "🌫️"
    if 51 <= code <= 57:
        return "🌦️"
    if 61 <= code <= 67:
        return "🌧️"
    if 71 <= code <= 77:
        return "🌨️"
    if 80 <= code <= 82:
        return "🌧️"
    if code in (85, 86):
        return "🌨️"
    if 95 <= code <= 99:
        return "⛈️"
    return "🌡️"

_CN_WEEKDAY = ["一", "二", "三", "四", "五", "六", "日", "天"]
_WEEKDAY_CN = ["一", "二", "三", "四", "五", "六", "日"]
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10, "两": 2}


def _beaufort(kmh: float) -> str:
    """把风速(km/h)转换为风力等级描述。"""
    if kmh < 1:
        return "0级静风"
    if kmh < 6:
        return "1级软风"
    if kmh < 12:
        return "2级轻风"
    if kmh < 20:
        return "3级微风"
    if kmh < 29:
        return "4级和风"
    if kmh < 39:
        return "5级清风"
    if kmh < 50:
        return "6级强风"
    if kmh < 62:
        return "7级疾风"
    if kmh < 75:
        return "8级大风"
    if kmh < 89:
        return "9级烈风"
    if kmh < 103:
        return "10级狂风"
    if kmh < 118:
        return "11级暴风"
    return "12级飓风"


# 判定为「高概率降水时段」的阈值（通用信号，非通勤专属）
_WET_THRESHOLD = 40


def _derive_wet_windows(hour_probs):
    """由逐小时降水概率推导连续的高概率时段（数据驱动、与场景无关）。

    hour_probs: list of (hour:int, prob:int)。返回 [{start, end, max_prob}]，
    每段为概率 >= _WET_THRESHOLD 的连续小时。工具只给信号，是否「通勤/出游要带伞」
    由 LLM 结合用户问题自行判断。
    """
    windows = []
    run = []
    for hour, prob in sorted(hour_probs, key=lambda x: x[0]):
        if prob < _WET_THRESHOLD:
            if run:
                windows.append(_close_wet_window(run))
                run = []
            continue
        if run and hour == run[-1][0] + 1:
            run.append((hour, prob))
        elif run:
            windows.append(_close_wet_window(run))
            run = [(hour, prob)]
        else:
            run = [(hour, prob)]
    if run:
        windows.append(_close_wet_window(run))
    return windows


def _close_wet_window(run):
    hours = [h for h, _ in run]
    probs = [p for _, p in run]
    return {"start": min(hours), "end": max(hours), "max_prob": max(probs)}


def _geocode_nominatim(city: str, timeout: int) -> dict | None:
    """用 OpenStreetMap Nominatim 做细粒度地理解析（可到街道/社区）。

    OSM 与 Open-Meteo 预报同源，保证解析出的经纬度与预报数据网格一致。
    限定中国范围（countrycodes=cn）排除海外同名地点；多候选时按 importance
    降序取最相关结果，规避模糊 query 命中邻近路名而非用户意图地点。
    返回最具体的经纬度 + 层级化地名（road/suburb/district/city/state/country）。
    """
    try:
        params = urllib.parse.urlencode({
            "q": city,
            "format": "jsonv2",
            "addressdetails": "1",
            "limit": "5",
            "accept-language": "zh-CN,zh",
            "countrycodes": "cn",
        })
        url = "https://nominatim.openstreetmap.org/search?" + params
        # Nominatim 对 UA 敏感，用专用 header 且单次请求（不重试轰炸）
        # 出站经 ssrf_safe_get：校验公网可达并钉死 IP（SSRF 防护下沉自 src.utils.net）
        resp = ssrf_safe_get(url, headers=_NOMINATIM_HEADERS, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        data = resp.json() or []
        if not data:
            return None
        # 显式按 importance 降序取最相关结果，避免模糊 query 命中邻近路名
        # 而非用户意图地点（海外同名地点已被 countrycodes=cn 排除）
        data.sort(key=lambda d: float(d.get("importance") or 0.0), reverse=True)
        top = data[0]
        lat = top.get("lat")
        lon = top.get("lon")
        if not lat or not lon:
            return None
        addr = top.get("address") or {}
        # 由细到粗挑选地名组件，取第一个非空作为 name
        name_parts = [
            addr.get("road"),
            addr.get("neighbourhood") or addr.get("suburb"),
            addr.get("city_district") or addr.get("district"),
            addr.get("city") or addr.get("town") or addr.get("village"),
            addr.get("county"),
            addr.get("state"),
        ]
        name = next((p for p in name_parts if p), top.get("name", city))
        return {
            "name": name,
            "admin1": addr.get("state", ""),
            "admin2": addr.get("city") or addr.get("town") or addr.get("village", ""),
            "admin3": addr.get("county")
                      or addr.get("city_district") or addr.get("district", ""),
            "country": addr.get("country", ""),
            "latitude": float(lat),
            "longitude": float(lon),
            "source": "nominatim",
        }
    except Exception as e:
        logger.warning("Nominatim 地理解析失败（城市=%s）: %s", city, e)
        return None


def _geocode_open_meteo(city: str, timeout: int) -> dict | None:
    """Open-Meteo 地理编码兜底（基于 OpenStreetMap，与预报数据源一致）。

    返回 name + admin1~admin4 层级（区/县/市级粒度），作为 Nominatim 不可用时
    的降级方案，避免回落到与预报网格不一致的 wttr.in 坐标。
    """
    try:
        url = (
            "https://geocoding-api.open-meteo.com/v1/search?count=1"
            f"&language=zh&name={urllib.parse.quote(city)}"
        )
        resp = _http_get(url, timeout)
        resp.raise_for_status()
        results = (resp.json() or {}).get("results") or []
        if results:
            r = results[0]
            return {
                "name": r.get("name", city),
                "admin1": r.get("admin1", ""),
                "admin2": r.get("admin2", ""),
                "admin3": r.get("admin3", ""),
                "admin4": r.get("admin4", ""),
                "country": r.get("country", ""),
                "latitude": r["latitude"],
                "longitude": r["longitude"],
                "source": "open-meteo",
            }
    except Exception as e:
        logger.warning("Open-Meteo 地理编码兜底失败（城市=%s）: %s", city, e)
    return None


# 地理编码缓存（只缓存成功结果 + 带 TTL）。
# 旧实现用 functools.lru_cache 会把瞬时失败(限流/抖动)返回的 None 永久缓存，
# 导致该城市此后永远走 wttr.in 兜底、再不重试正常路径。改为「仅成功入缓存」。
_GEO_CACHE: dict[str, tuple[float, dict]] = {}
_GEO_TTL_SECONDS = 7 * 24 * 3600  # 7 天
_GEO_LOCK = threading.Lock()


def _geocode(city: str, timeout: int) -> dict | None:
    """把地名解析为经纬度，尽量细（街道/社区优先），并与预报数据源一致。

    顺序：Nominatim(OSM, 最细) -> Open-Meteo 地理编码(OSM, 区/县级) -> None。
    不再用 wttr.in 做地理解析（其坐标与 Open-Meteo 网格可能不一致，且粒度粗）。
    成功结果按城市缓存 7 天（带 TTL），减少对第三方地理服务的请求与限流风险；
    失败结果不缓存，下次调用仍会正常重试。
    """
    now = time.time()
    with _GEO_LOCK:
        hit = _GEO_CACHE.get(city)
        if hit and now - hit[0] < _GEO_TTL_SECONDS:
            return hit[1]

    geo = _geocode_nominatim(city, timeout)
    if geo:
        _geo_store(city, geo, now)
        return geo
    geo = _geocode_open_meteo(city, timeout)
    if geo:
        _geo_store(city, geo, now)
        return geo
    return None


def _geo_store(city: str, geo: dict, now: float) -> None:
    with _GEO_LOCK:
        if len(_GEO_CACHE) > 256:
            _GEO_CACHE.clear()
        _GEO_CACHE[city] = (now, geo)


def _compose_display_name(geo: dict, fallback: str) -> str:
    """由层级地名拼出尽量细的展示名（如「文三路(西湖区, 杭州市, 浙江省)」）。

    由细到粗收集 admin4~admin1，剔除与 name 重复、相互重复，或中文行政后缀
    包含关系（如「北京」⊂「北京市」、「朝阳」⊂「朝阳区」）造成的冗余项。
    """
    name = geo.get("name") or fallback
    levels = []
    for key in ("admin4", "admin3", "admin2", "admin1"):
        v = geo.get(key)
        if not v or v == name or name in v or v in name or v in levels:
            continue
        levels.append(v)
    if not levels:
        return name
    return f"{name}({', '.join(levels)})"


def _fetch_forecast(lat: float, lon: float, days: int, timeout: int) -> dict | None:
    """拉取 Open-Meteo 多日预报（含当前、每日、逐小时降水概率与湿度）。"""
    days = max(1, min(days, 16))
    daily = (
        "weather_code,temperature_2m_max,temperature_2m_min,"
        "apparent_temperature_max,apparent_temperature_min,"
        "precipitation_probability_max,precipitation_probability_mean,"
        "precipitation_sum,rain_sum,precipitation_hours,"
        "wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant,"
        "uv_index_max,sunrise,sunset"
    )
    current = (
        "temperature_2m,relative_humidity_2m,apparent_temperature,"
        "weather_code,wind_speed_10m,wind_direction_10m,precipitation"
    )
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": "Asia/Shanghai",
        "forecast_days": days,
        "current": current,
        "daily": daily,
        "hourly": "precipitation_probability,temperature_2m,relative_humidity_2m",
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    try:
        resp = _http_get(url, timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("Open-Meteo 预报拉取失败: %s", e)
        return None


def _parse_cn_number(text: str) -> int | None:
    """解析阿拉伯数字或中文数字（支持 一到十 / 两）。"""
    m = re.search(r"(\d+)", text)
    if m:
        return int(m.group(1))
    # 中文数字：十一~十九 / 十 / 一~九
    if "十" in text:
        if text.strip() == "十":
            return 10
        m2 = re.search(r"([一二三四五六七八九])?十([一二三四五六七八九])?", text)
        if m2:
            tens = 1 if m2.group(1) is None else _CN_NUM[m2.group(1)]
            ones = 0 if m2.group(2) is None else _CN_NUM[m2.group(2)]
            return tens * 10 + ones
    for ch, val in _CN_NUM.items():
        if ch in text:
            return val
    return None


def _parse_date_range(query: str, today: datetime.date) -> tuple[list[datetime.date], str]:
    """解析中文天气问句中的日期范围，返回 (目标日期列表, 范围描述)。

    支持：今天/明天/后天/大后天、周X、下周X、本周X、周X到周Y、下周X到Y、
    未来N天/今后N天/N天、周末、X号/X日。无日期信息时按问题开放度默认 1~3 天。
    """
    q = query or ""
    target_week_offset = None  # None=自动, 0=本周, 1=下周
    if re.search(r"下(个)?(星期|周|礼拜)", q):
        target_week_offset = 1
    elif re.search(r"(本|这)(个)?(星期|周|礼拜)", q):
        target_week_offset = 0

    this_monday = today - datetime.timedelta(days=today.weekday())

    # 1) 相对日：今天/明天/后天/大后天/昨天/前天
    rel_days = []
    if "大后天" in q:
        rel_days.append(today + datetime.timedelta(days=3))
    if "后天" in q:
        rel_days.append(today + datetime.timedelta(days=2))
    if "明天" in q:
        rel_days.append(today + datetime.timedelta(days=1))
    if "今天" in q:
        rel_days.append(today)
    if "前天" in q:
        rel_days.append(today - datetime.timedelta(days=2))
    if "昨天" in q:
        rel_days.append(today - datetime.timedelta(days=1))

    # 2) 周末
    weekend = []
    if "周末" in q:
        # 周日(weekday=6)时当前周末仍在进行，周六应回退到昨天
        if today.weekday() == 6:
            sat = today - datetime.timedelta(days=1)
        else:
            sat = today + datetime.timedelta(days=(5 - today.weekday()) % 7)
        weekend = [sat, sat + datetime.timedelta(days=1)]

    # 3) X号 / X日
    date_of_month = []
    for m in re.finditer(r"(\d{1,2})\s*[号日]", q):
        day = int(m.group(1))
        if 1 <= day <= 31:
            y, mo = today.year, today.month
            if day < today.day:
                mo += 1
                if mo > 12:
                    mo = 1
                    y += 1
            try:
                date_of_month.append(datetime.date(y, mo, day))
            except ValueError:
                pass

    # 4) 未来/今后/接下来 N 天
    future_n = None
    fm = re.search(r"(未来|今后|接下来|近)\s*(\d+|[一二两三四五六七八九十]+)\s*天", q)
    if fm:
        n = _parse_cn_number(fm.group(2))
        if n:
            future_n = max(1, min(n, 16))

    # 5) 星期范围（周X 到 周Y）或单个星期
    weekday_mentions = []
    for m in re.finditer(r"(周|星期|礼拜)\s*([一二三四五六日天])", q):
        wd = _CN_WEEKDAY.index(m.group(2))  # 0=周一
        weekday_mentions.append(wd)
    week_dates = []
    if weekday_mentions:
        # 是否成对出现「X到Y」
        range_m = re.search(
            r"(周|星期|礼拜)\s*([一二三四五六日天])\s*(到|至|[-—~])\s*(周|星期|礼拜)?\s*([一二三四五六日天])",
            q,
        )
        if range_m:
            start_wd = _CN_WEEKDAY.index(range_m.group(2))
            end_wd = _CN_WEEKDAY.index(range_m.group(5))
            if target_week_offset is None:
                target_week_offset = 1 if end_wd < today.weekday() else 0
            base = this_monday + datetime.timedelta(weeks=target_week_offset or 0)
            # 若起点已早于今天，顺延一周
            if (base + datetime.timedelta(days=start_wd)) < today:
                base += datetime.timedelta(weeks=1)
            step = 1 if end_wd >= start_wd else -1
            wd = start_wd
            while True:
                week_dates.append(base + datetime.timedelta(days=wd))
                if wd == end_wd:
                    break
                wd += step
        else:
            base = this_monday + datetime.timedelta(weeks=target_week_offset or 0)
            for wd in weekday_mentions:
                d = base + datetime.timedelta(days=wd)
                if target_week_offset is None and d < today:
                    d += datetime.timedelta(weeks=1)
                week_dates.append(d)

    # 汇总所有解析到的日期
    all_dates = []
    all_dates.extend(rel_days)
    all_dates.extend(weekend)
    all_dates.extend(date_of_month)
    all_dates.extend(week_dates)
    # 去重保序
    seen = set()
    all_dates = [d for d in all_dates if not (d in seen or seen.add(d))]

    if future_n is not None:
        all_dates = [today + datetime.timedelta(days=i) for i in range(future_n)]
    elif not all_dates:
        # 无日期信息：开放问题默认 3 天，否则仅今天
        open_ended = any(k in q for k in ("怎么样", "如何", "怎样", "天气", "气温", "温度", "穿", "带伞", "下雨", "情况"))
        n = 3 if open_ended else 1
        all_dates = [today + datetime.timedelta(days=i) for i in range(n)]

    all_dates.sort()
    # 范围描述
    if len(all_dates) == 1:
        d = all_dates[0]
        label = f"{d.month}/{d.day} 周{_WEEKDAY_CN[d.weekday()]}"
    else:
        a, b = all_dates[0], all_dates[-1]
        label = f"{a.month}/{a.day} 周{_WEEKDAY_CN[a.weekday()]} 至 {b.month}/{b.day} 周{_WEEKDAY_CN[b.weekday()]}"
    return all_dates, label


def _label(d: datetime.date) -> str:
    return f"{d.month}/{d.day} 周{_WEEKDAY_CN[d.weekday()]}"


def _build_day_card(day: dict, date_obj: datetime.date, hourly: dict | None) -> dict:
    """由 Open-Meteo 单日数据构造结构化卡片（纯数据 + 通用极端值信号）。

    不在这里写死任何"带伞/穿衣/通勤"类结论——那是 LLM 的职责。
    工具只负责把数据挖全：逐小时降水概率(precip_hourly) 与数据驱动推导的
    高概率降水时段(wet_windows) 都作为原始信号给出，极端数值打上通用阈值信号
    （如高温/大风）。具体建议由 LLM 结合用户实际问题解读，工具不预设场景。
    """
    code = day.get("weather_code", -1)
    desc = WMO_CODES.get(code, "未知")
    tmax = day.get("temperature_2m_max")
    tmin = day.get("temperature_2m_min")
    precip_prob_max = day.get("precipitation_probability_max")
    precip_prob_mean = day.get("precipitation_probability_mean")
    wind = day.get("wind_speed_10m_max")
    gust = day.get("wind_gusts_10m_max")
    uv = day.get("uv_index_max")
    rain_sum = day.get("rain_sum")
    precip_sum = day.get("precipitation_sum")
    precip_hours = day.get("precipitation_hours")
    # 体感温度（新增）
    feels_max = day.get("apparent_temperature_max")
    feels_min = day.get("apparent_temperature_min")
    # 日出日落（新增）
    sunrise = day.get("sunrise")
    sunset = day.get("sunset")

    # 逐小时降水概率与湿度（原始数据，按小时归集，供 LLM 自由解读任何场景）
    precip_hourly = {}
    humidity_hourly = {}
    wet_windows = []
    day_probs = []  # 逐小时降水概率（仅 hourly 存在时填充；提前初始化防 hourly=None 时 UnboundLocalError）
    if hourly:
        times = hourly.get("time") or []
        probs = hourly.get("precipitation_probability") or []
        hums = hourly.get("relative_humidity_2m") or []
        day_prefix = str(date_obj)
        for i, t in enumerate(times):
            if not t.startswith(day_prefix):
                continue
            hour = int(t[11:13])
            if i < len(probs) and probs[i] is not None:
                precip_hourly[hour] = probs[i]
                day_probs.append((hour, probs[i]))
            if i < len(hums) and hums[i] is not None:
                humidity_hourly[hour] = hums[i]
        # 数据驱动推导「高概率降水时段」（与具体场景无关，LLM 自行决定如何解读）
        wet_windows = _derive_wet_windows(day_probs)

    # 降水概率：优先用 Open-Meteo 的 daily 聚合值；若该日 daily 字段为空
    #（API 偶发返回 null，尤其降水量为 0 或预报不确定时），从逐小时概率
    # 回退推导 max/mean，避免字段为空导致下游拿不到数值。
    if precip_prob_max is None and day_probs:
        precip_prob_max = max(p for _, p in day_probs)
    if precip_prob_mean is None and day_probs:
        precip_prob_mean = round(sum(p for _, p in day_probs) / len(day_probs))

    # 从逐小时湿度推导日均值（Open-Meteo 不提供 daily 湿度聚合）
    humidity_mean = None
    if humidity_hourly:
        vals = list(humidity_hourly.values())
        humidity_mean = round(sum(vals) / len(vals))
    elif hourly:
        # 兜底：当日逐小时湿度缺失时，用全量逐小时湿度整体均值
        all_hums = [v for v in (hourly.get("relative_humidity_2m", []) or []) if v is not None]
        if all_hums:
            humidity_mean = round(sum(all_hums) / len(all_hums))

    # 通用极端值信号：按数值阈值打标，与具体天气类型无关，可无限扩展
    alerts = []
    if tmax is not None and tmax >= 35:
        alerts.append("高温")
    if tmin is not None and tmin <= 4:
        alerts.append("低温")
    if (gust is not None and gust >= 50) or (wind is not None and wind >= 39):
        alerts.append("大风")
    if precip_prob_max is not None and precip_prob_max >= 60:
        alerts.append("强降水")
    if uv is not None and uv >= 8:
        alerts.append("强紫外线")

    card = {
        "date": str(date_obj),
        "label": _label(date_obj),
        "weather": desc,
        "weather_code": code,
        "temp_max": f"{tmax}°C" if tmax is not None else "",
        "temp_min": f"{tmin}°C" if tmin is not None else "",
        # 降水信息（分层展示，避免 LLM 混淆「概率」与「量」）
        "precip_prob_max": f"{precip_prob_max}%" if precip_prob_max is not None else "",
        "precip_prob_mean": f"{precip_prob_mean}%" if precip_prob_mean is not None else "",
        "rain_sum": f"{rain_sum}mm" if rain_sum is not None else "",
        "precip_sum": f"{precip_sum}mm" if precip_sum is not None else "",
        "precip_hours": int(precip_hours) if precip_hours is not None else None,
        # 风力
        "wind": _beaufort(wind) if wind is not None else "",
        "wind_gust": f"{gust} km/h" if gust is not None else "",
        # 紫外线
        "uv_index": uv if uv is not None else "",
        # 体感温度（新增）
        "feels_like_max": f"{feels_max}°C" if feels_max is not None else "",
        "feels_like_min": f"{feels_min}°C" if feels_min is not None else "",
        # 湿度（从 hourly 聚合）
        "humidity_mean": f"{humidity_mean}%" if humidity_mean is not None else "",
        # 日出日落（新增）
        "sunrise": _fmt_time(sunrise),
        "sunset": _fmt_time(sunset),
        # 告警
        "alerts": alerts,
        # 逐时数据
        "precip_hourly": precip_hourly,
        "humidity_hourly": humidity_hourly,
        "wet_windows": wet_windows,
    }
    return card


def _fmt_time(t: str | None) -> str:
    """格式化 ISO 时间为 HH:MM（处理 Open-Meteo 返回的 2026-08-03T04:46 格式）。"""
    if not t:
        return ""
    # Open-Meteo 返回 "2026-08-03T04:46"
    if "T" in t:
        return t.split("T")[1]
    return t[:5] if len(t) >= 5 else t


def _build_summary(city: str, range_label: str, cards: list[dict],
                   resolved: list[str]) -> str:
    """生成事实性摘要（不替 LLM 下结论）。

    每行为该日事实数据 + 通用极端值信号；高概率降水时段(wet_windows) 作为
    结构化信号单列一段，由 LLM 结合用户问题自行解读，不绑定通勤等特定场景。
    """
    lines = [f"【{city} {range_label} 天气预报】", ""]
    past = []
    for c in cards:
        if c.get("_past"):
            past.append(c["label"])
            continue
        line = (
            f"{c['label']}：{c['weather']}，"
            f"{c['temp_min']}~{c['temp_max']}"
        )
        # 体感温度（如有）
        if c.get("feels_like_min") and c.get("feels_like_max"):
            line += f"，体感 {c['feels_like_min']}~{c['feels_like_max']}"
        # 降水概率（核心字段，必须明确标注为百分比）
        if c.get("precip_prob_max"):
            line += f"，降水概率 {c['precip_prob_max']}"
        # 降雨量（与概率分开，避免混淆）
        if c.get("rain_sum") and c["rain_sum"] != "0mm":
            line += f"，降雨量 {c['rain_sum']}"
        if c.get("precip_hours") is not None and c["precip_hours"] > 0:
            line += f"，降水时长约 {c['precip_hours']}小时"
        # 湿度
        if c.get("humidity_mean"):
            line += f"，湿度 {c['humidity_mean']}"
        # 风力
        if c.get("wind"):
            line += f"，{c['wind']}"
        if c.get("uv_index") not in (None, "", "0"):
            line += f"，紫外线 {c['uv_index']}"
        alerts = c.get("alerts") or []
        if alerts:
            line += " [" + "][".join(alerts) + "]"
        lines.append(line)

    if past:
        lines.append("")
        lines.append(f"（{ '、'.join(past) } 已过去，无预报数据）")

    # 高概率降水时段（数据驱动，供 LLM 结合用户问题自由解读，不在此下结论）
    wet_days = [c for c in cards if c.get("wet_windows")]
    if wet_days:
        lines.append("")
        lines.append("高概率降水时段：")
        for c in wet_days:
            for w in c["wet_windows"]:
                lines.append(
                    f"  {c['label']}：{w['start']:02d}:00-{w['end']:02d}:00 "
                    f"（最高 {w['max_prob']}%）"
                )

    return "\n".join(lines)


def _build_markdown(city: str, range_label: str, cards: list[dict],
                    current: dict | None, resolved: list[str]) -> str:
    """生成可直接作为回复的 markdown 卡片（渲染为 markdown 消息卡片）。

    与 summary 的区别：summary 是纯文本事实串，markdown 是排版好的卡片正文，
    含标题层级、**加粗**、emoji、列表式明细与极端值告警，更利于在聊天里阅读。
    工具不替 LLM 下结论，告警仅列通用信号（高温/大风等），建议由 LLM 结合问题补述。
    """
    lines = [f"## 🌤 {city}天气 · {range_label}", ""]
    if current:
        lines.append(
            f"**当前** {current.get('weather', '')} "
            f"{current.get('temperature', '')}（体感 {current.get('feels_like', '')}），"
            f"湿度 {current.get('humidity', '')}，{current.get('wind', '')}"
        )
        lines.append("")

    past = [c["label"] for c in cards if c.get("_past")]
    for c in cards:
        if c.get("_past"):
            continue
        icon = _weather_emoji(c.get("weather_code", -1))
        line = f"**{c['label']}** {icon} {c['weather']} {c.get('temp_min', '')}~{c.get('temp_max', '')}"
        lines.append(line)
        detail = []
        # 体感温度
        if c.get("feels_like_min") and c.get("feels_like_max"):
            detail.append(f"体感 {c['feels_like_min']}~{c['feels_like_max']}")
        # 降水概率（核心字段，明确标注）
        if c.get("precip_prob_max"):
            detail.append(f"降水概率 {c['precip_prob_max']}")
        # 降雨量（与概率分开行）
        if c.get("rain_sum") and c["rain_sum"] != "0mm":
            detail.append(f"降雨量 {c['rain_sum']}")
        if c.get("precip_hours") is not None and c["precip_hours"] > 0:
            detail.append(f"降水约 {c['precip_hours']}h")
        # 湿度
        if c.get("humidity_mean"):
            detail.append(f"湿度 {c['humidity_mean']}")
        # 风力
        if c.get("wind"):
            detail.append(c["wind"])
        uv = c.get("uv_index")
        if uv not in (None, "", "0"):
            detail.append(f"紫外线 {uv}")
        # 日出日落
        if c.get("sunrise") and c.get("sunset"):
            detail.append(f"{c['sunrise']}日出 / {c['sunset']}日落")
        if detail:
            lines.append("> " + " · ".join(detail))
        alerts = c.get("alerts") or []
        if alerts:
            lines.append(f"> ⚠️ {' / '.join(alerts)}")

    # 高概率降水时段（数据驱动，供 LLM 自行解读，不绑定通勤等特定场景）
    wet_days = [c for c in cards if c.get("wet_windows")]
    if wet_days:
        lines.append("")
        lines.append("**高概率降水时段**")
        for c in wet_days:
            for w in c["wet_windows"]:
                lines.append(
                    f"> {c['label']}：{w['start']:02d}:00-{w['end']:02d}:00（最高 {w['max_prob']}%）"
                )

    if past:
        lines.append("")
        lines.append(f"_（{ '、'.join(past) } 已过去，无预报数据）_")

    lines.append("")
    lines.append("> 来源：open-meteo.com")
    return "\n".join(lines)


class WeatherTool(BaseTool):
    name = "get_weather"
    display_name = "查询天气"
    short_description = "查询天气与多日预报（结构化数据，含温度/降水概率(%)/降雨量(mm)/体感温度/湿度/风力/紫外线/逐小时降水概率/日出日落；地理定位可细至街道/社区，与预报数据源一致）"
    description = (
        "查询指定城市的天气与多日预报。基于 Open-Meteo 提供未来最多 16 天的预报，"
        "返回结构化数据：每日天气、最高/最低温、体感温度(feels_like_max/min)、"
        "降水概率(precip_prob_max, 百分比数值)、平均降水概率(precip_prob_mean)、"
        "降雨量(rain_sum, 毫米)、降水量(precip_sum)、降水时长(precip_hours, 小时)、"
        "风力、阵风、紫外线指数、湿度(humidity_mean, 从逐时数据聚合)、日出日落时间，"
        "以及逐小时降水概率(precip_hourly) 与逐小时湿度(humidity_hourly)，"
        "并打上通用极端值信号（高温/低温/大风/强降水/强紫外线）。"
        "重要：precip_prob_max 是降水概率百分比（如 \"65%\"），rain_sum 是累计降雨量毫米数（如 \"4.4mm\"），"
        "两者是不同维度，回复时请分别报告，不要把降雨量当作概率来报。"
        "地理定位尽量细（街道/社区优先，区/县/市兜底），且解析数据源与预报同源"
        "（OpenStreetMap -> Open-Meteo），保证返回的地名与经纬度一致，不会张冠李戴。"
        "覆盖'今天/明天/周X/下周X到Y/未来N天/周末/X号'等中文日期表达。"
        "当用户询问天气、气温、下雨、刮风、带伞、通勤、某天天气、冷不冷、热不热、台风、紫外线、出游、户外运动时使用。"
        "注意：本工具只提供数据与极端值信号，不预设任何场景（例如不再固定提醒通勤），"
        "请由你根据用户的实际问法决定该强调什么。"
        "请读取返回的结构化字段（days/current/alerts/precip_hourly/humidity_hourly/wet_windows），结合用户问题"
        "（如'带伞''穿衣''防晒''通勤''出游''户外运动''婚礼外拍'等）自行判断并组织自然语言回复——"
        "工具不替你下结论，按问法筛选关键信息，用通顺的口语化方式回答即可。"
        "建议把用户的原始天气问题整体作为 query 传入，以便精准判断日期范围与需求。"
    )
    # 场景关键词统一维护在 IntentRegistry 的 domain.weather（单一真源），
    # 此处仅声明服务类别，路由时自动解析。
    intent_categories = ["domain.weather"]
    parameters = {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "城市名称，例如'北京'、'上海'、'杭州'、'廊坊'。从用户消息中自动提取，无需询问用户",
            },
            "query": {
                "type": "string",
                "description": (
                    "用户的原始天气问题（可选但强烈建议传入），用于判断日期范围与具体需求，"
                    "例如'下周一到周五廊坊天气怎么样，上下班需不需要带伞'、'明天北京下雨吗'、'周末深圳天气'。"
                    "工具会自动解析'今天/明天/周X/下周X到Y/未来N天/周末/X号'等中文日期表达。"
                ),
            },
        },
        "required": [],
    }

    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    def execute(self, args: dict) -> str | dict:
        city = (args.get("city") or "").strip()
        query = (args.get("query") or "").strip()
        # 若未给 city 但 query 里含城市名，尝试从 query 提取（简单兜底）
        if not city and query:
            m = re.search(r"([\u4e00-\u9fa5]{2,5}?)(的|市|天气|气温|温度|下雨|降雨|降水|怎么样|如何|怎样)", query)
            if m:
                city = m.group(1)
        if not city:
            city = "北京"

        today = datetime.date.today()
        dates, range_label = _parse_date_range(query, today)
        # 需要的预报天数（覆盖到最远目标日）
        max_days = (dates[-1] - today).days + 1
        max_days = max(1, min(max_days, 16))

        geo = _geocode(city, self.timeout)
        if not geo:
            return self._fallback(city, query)
        fc = _fetch_forecast(geo["latitude"], geo["longitude"], max_days, self.timeout)
        if not fc:
            return self._fallback(city, query)

        daily = fc.get("daily") or {}
        d_times = daily.get("time", [])
        hourly = fc.get("hourly")
        current = fc.get("current")

        # 建立 date -> day 索引
        day_by_date = {}
        for i, t in enumerate(d_times):
            day_by_date[t] = i

        cards = []
        resolved = []
        for d in dates:
            ds = str(d)
            if d < today:
                cards.append({"label": _label(d), "_past": True, "date": ds})
                continue
            if ds in day_by_date:
                idx = day_by_date[ds]
                day = {k: (daily[k][idx] if idx < len(daily[k]) else None)
                       for k in daily if k != "time"}
                card = _build_day_card(day, d, hourly)
                cards.append(card)
                resolved.append(ds)
            else:
                cards.append({"label": _label(d), "_past": True, "date": ds})

        display_city = _compose_display_name(geo, city)

        # 当前实况（若今天在范围内）
        current_block = None
        if current:
            c_code = current.get("weather_code", -1)
            current_block = {
                "temperature": f"{current.get('temperature_2m')}°C",
                "feels_like": f"{current.get('apparent_temperature')}°C",
                "weather": WMO_CODES.get(c_code, "未知"),
                "humidity": f"{current.get('relative_humidity_2m')}%",
                "wind": _beaufort(current.get("wind_speed_10m") or 0),
            }

        return {
            "city": display_city,
            "range": range_label,
            "resolved_dates": resolved,
            "current": current_block,
            "days": [c for c in cards if not c.get("_past")],
            "source": "open-meteo.com",
        }

    def _fallback(self, city: str, query: str) -> dict:
        """Open-Meteo 不可用时的兜底：wttr.in 当前+今日，再不行用搜索。"""
        logger.warning("天气工具回退到 wttr.in（城市=%s）", city)
        try:
            url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1&lang=zh"
            # 出站经 ssrf_safe_get：校验公网可达并钉死 IP（SSRF 防护下沉自 src.utils.net）
            resp = ssrf_safe_get(url, headers=_HEADERS, timeout=self.timeout, allow_redirects=True)
            resp.raise_for_status()
            wd = resp.json()
            current = wd.get("current_condition", [{}])[0]
            desc = (current.get("lang_zh", [{}])[0].get("value", "")
                    if current.get("lang_zh")
                    else current.get("weatherDesc", [{}])[0].get("value", ""))
            today_f = wd.get("weather", [{}])[0] if wd.get("weather") else {}
            return {
                "city": city,
                "current": {
                    "temperature": f"{current.get('temp_C', '')}°C",
                    "weather": desc,
                    "humidity": f"{current.get('humidity', '')}%",
                    "wind": f"{current.get('windspeedKmph', '')} km/h",
                    "feels_like": f"{current.get('FeelsLikeC', '')}°C",
                },
                "today": {
                    "max_temp": f"{today_f.get('maxtempC', '')}°C",
                    "min_temp": f"{today_f.get('mintempC', '')}°C",
                },
                "summary": (
                    f"【{city} 当前天气】{desc}，{current.get('temp_C', '')}°C"
                    f"（体感 {current.get('FeelsLikeC', '')}°C），湿度 {current.get('humidity', '')}%，"
                    f"风速 {current.get('windspeedKmph', '')} km/h。\n"
                    f"（注：详细预报服务暂不可用，仅提供当前实况，多日预报请稍后重试）"
                ),
                "source": "wttr.in",
            }
        except Exception as e:
            logger.error("wttr.in 兜底也失败（城市=%s）: %s", city, e)
            return {"error": f"无法获取 {city} 的天气信息（天气服务暂不可用）"}
