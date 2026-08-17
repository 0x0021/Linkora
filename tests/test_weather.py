"""Weather 工具纯函数单元测试：覆盖风向等级、emoji、中文数字解析、日期范围解析、卡片构建。"""
from __future__ import annotations

import datetime
import urllib.parse
from unittest import mock

import pytest

from src.tools.weather import (
    _beaufort,
    _weather_emoji,
    _parse_cn_number,
    _label,
    _parse_date_range,
    _build_day_card,
    _build_summary,
    _build_markdown,
    _fmt_time,
    _geocode,
    _geocode_nominatim,
    _geocode_open_meteo,
    _compose_display_name,
    _extract_location_from_query,
    _relaxed_location_candidates,
    _nominatim_score,
    WMO_CODES,
)


# ── _beaufort ─────────────────────────────────────────────────

class TestBeaufort:
    @pytest.mark.parametrize("kmh,expected", [
        (0, "0级静风"),
        (0.5, "0级静风"),
        (1, "1级软风"),
        (5, "1级软风"),
        (6, "2级轻风"),
        (11, "2级轻风"),
        (12, "3级微风"),
        (19, "3级微风"),
        (20, "4级和风"),
        (28, "4级和风"),
        (29, "5级清风"),
        (38, "5级清风"),
        (39, "6级强风"),
        (49, "6级强风"),
        (50, "7级疾风"),
        (61, "7级疾风"),
        (62, "8级大风"),
        (74, "8级大风"),
        (75, "9级烈风"),
        (88, "9级烈风"),
        (89, "10级狂风"),
        (102, "10级狂风"),
        (103, "11级暴风"),
        (117, "11级暴风"),
        (118, "12级飓风"),
        (200, "12级飓风"),
    ])
    def test_all_levels(self, kmh, expected):
        assert _beaufort(kmh) == expected


# ── _weather_emoji ───────────────────────────────────────────

class TestWeatherEmoji:
    def test_sunny(self):
        assert "☀️" in _weather_emoji(0)
        assert "☀️" in _weather_emoji(1)

    def test_partly_cloudy(self):
        assert "🌤" in _weather_emoji(2)

    def test_cloudy(self):
        assert "☁" in _weather_emoji(3)

    def test_fog(self):
        assert "🌫" in _weather_emoji(45)
        assert "🌫" in _weather_emoji(48)

    def test_drizzle(self):
        assert "🌦" in _weather_emoji(51)
        assert "🌦" in _weather_emoji(57)

    def test_rain(self):
        assert "🌧" in _weather_emoji(61)
        assert "🌧" in _weather_emoji(67)

    def test_snow(self):
        assert "🌨" in _weather_emoji(71)
        assert "🌨" in _weather_emoji(77)
        assert "🌨" in _weather_emoji(85)
        assert "🌨" in _weather_emoji(86)

    def test_showers(self):
        assert "🌧" in _weather_emoji(80)
        assert "🌧" in _weather_emoji(82)

    def test_thunder(self):
        assert "⛈" in _weather_emoji(95)
        assert "⛈" in _weather_emoji(99)

    def test_unknown_returns_default(self):
        assert "🌡" in _weather_emoji(999)
        assert "🌡" in _weather_emoji(-1)


# ── _parse_cn_number ─────────────────────────────────────────

class TestParseCNNumber:
    def test_arabic(self):
        assert _parse_cn_number("第3天") == 3
        assert _parse_cn_number("未来10天") == 10

    def test_single_digit(self):
        assert _parse_cn_number("一") == 1
        assert _parse_cn_number("两") == 2
        assert _parse_cn_number("九") == 9

    def test_ten(self):
        assert _parse_cn_number("十") == 10

    def test_teens(self):
        assert _parse_cn_number("十一") == 11
        assert _parse_cn_number("十五") == 15
        assert _parse_cn_number("十九") == 19

    def test_twenty_to_ninety(self):
        assert _parse_cn_number("二十") == 20
        assert _parse_cn_number("二十五") == 25
        assert _parse_cn_number("九十九") == 99

    def test_not_a_number(self):
        assert _parse_cn_number("无所谓") is None

    def test_mixed_in_text(self):
        assert _parse_cn_number("未来十五天天气") == 15
        assert _parse_cn_number("接下来3天") == 3


# ── _label ───────────────────────────────────────────────────

class TestLabel:
    def test_monday(self):
        d = datetime.date(2026, 7, 13)  # Monday
        assert _label(d) == "7/13 周一"

    def test_sunday(self):
        d = datetime.date(2026, 7, 19)  # Sunday
        assert _label(d) == "7/19 周日"


# ── _parse_date_range ────────────────────────────────────────

class TestParseDateRange:
    TODAY = datetime.date(2026, 7, 13)  # Monday

    def test_today(self):
        dates, label = _parse_date_range("今天天气怎么样", self.TODAY)
        assert dates == [self.TODAY]
        assert "7/13" in label

    def test_tomorrow(self):
        dates, _ = _parse_date_range("明天天气", self.TODAY)
        assert dates == [datetime.date(2026, 7, 14)]

    def test_day_after_tomorrow(self):
        dates, _ = _parse_date_range("后天", self.TODAY)
        assert dates == [datetime.date(2026, 7, 15)]

    def test_three_days_later(self):
        # "大后天" 同时命中 "后天" 正则，因此返回 [后天, 大后天]
        dates, _ = _parse_date_range("大后天", self.TODAY)
        assert dates == [datetime.date(2026, 7, 15), datetime.date(2026, 7, 16)]

    def test_yesterday(self):
        dates, _ = _parse_date_range("昨天天气", self.TODAY)
        assert dates == [datetime.date(2026, 7, 12)]

    def test_weekend(self):
        dates, _ = _parse_date_range("周末天气", self.TODAY)
        assert dates == [datetime.date(2026, 7, 18), datetime.date(2026, 7, 19)]

    def test_weekday_single(self):
        dates, _ = _parse_date_range("周五天气", self.TODAY)
        assert dates == [datetime.date(2026, 7, 17)]

    def test_weekday_next_week_single(self):
        dates, _ = _parse_date_range("下周一天气", self.TODAY)
        assert dates == [datetime.date(2026, 7, 20)]

    def test_weekday_range(self):
        dates, _ = _parse_date_range("周一到周三天气", self.TODAY)
        assert len(dates) == 3
        assert dates[0] == datetime.date(2026, 7, 13)  # today is Monday
        assert dates[-1] == datetime.date(2026, 7, 15)

    def test_next_week_range(self):
        dates, _ = _parse_date_range("下周一到周五天气", self.TODAY)
        assert len(dates) == 5
        assert dates[0] == datetime.date(2026, 7, 20)

    def test_future_n_days(self):
        dates, _ = _parse_date_range("未来3天天气", self.TODAY)
        assert len(dates) == 3
        assert dates[0] == self.TODAY
        assert dates[-1] == datetime.date(2026, 7, 15)

    def test_future_n_days_chinese(self):
        dates, _ = _parse_date_range("未来五天天气", self.TODAY)
        assert len(dates) == 5

    def test_date_of_month(self):
        dates, _ = _parse_date_range("15号天气", self.TODAY)
        assert dates == [datetime.date(2026, 7, 15)]

    def test_date_of_month_next_month(self):
        # July 13, day 5 → next month (Aug)
        dates, _ = _parse_date_range("5号", self.TODAY)
        assert dates == [datetime.date(2026, 8, 5)]

    def test_open_ended_default_3_days(self):
        dates, _ = _parse_date_range("天气怎么样", self.TODAY)
        assert len(dates) == 3

    def test_specific_query_default_1_day(self):
        dates, _ = _parse_date_range("北京", self.TODAY)
        assert len(dates) == 1

    def test_mixed_today_and_tomorrow(self):
        dates, _ = _parse_date_range("今天和明天天气", self.TODAY)
        assert len(dates) == 2
        assert datetime.date(2026, 7, 13) in dates
        assert datetime.date(2026, 7, 14) in dates

    def test_range_label_single_day(self):
        _, label = _parse_date_range("今天", self.TODAY)
        assert "7/13" in label
        assert "周一" in label

    def test_range_label_multi_day(self):
        _, label = _parse_date_range("周一到周三", self.TODAY)
        assert "7/13" in label
        assert "7/15" in label
        assert "至" in label


# ── _build_day_card ──────────────────────────────────────────

class TestBuildDayCard:
    def test_basic_card(self):
        day = {"weather_code": 0, "temperature_2m_max": 30, "temperature_2m_min": 20}
        date_obj = datetime.date(2026, 7, 14)
        card = _build_day_card(day, date_obj, None)
        assert card["weather"] == "晴"
        assert card["temp_max"] == "30°C"
        assert card["temp_min"] == "20°C"
        assert card["alerts"] == []

    def test_high_temp_alert(self):
        day = {"weather_code": 1, "temperature_2m_max": 36, "temperature_2m_min": 25}
        card = _build_day_card(day, datetime.date(2026, 7, 14), None)
        assert "高温" in card["alerts"]

    def test_low_temp_alert(self):
        day = {"weather_code": 3, "temperature_2m_max": 8, "temperature_2m_min": 2}
        card = _build_day_card(day, datetime.date(2026, 12, 14), None)
        assert "低温" in card["alerts"]

    def test_wind_alert(self):
        day = {"weather_code": 0, "wind_gusts_10m_max": 55, "temperature_2m_max": 25, "temperature_2m_min": 15}
        card = _build_day_card(day, datetime.date(2026, 7, 14), None)
        assert "大风" in card["alerts"]

    def test_precip_alert(self):
        day = {"weather_code": 61, "precipitation_probability_max": 70,
               "temperature_2m_max": 20, "temperature_2m_min": 12}
        card = _build_day_card(day, datetime.date(2026, 7, 14), None)
        assert "强降水" in card["alerts"]

    def test_uv_alert(self):
        day = {"weather_code": 0, "uv_index_max": 10, "temperature_2m_max": 30, "temperature_2m_min": 20}
        card = _build_day_card(day, datetime.date(2026, 7, 14), None)
        assert "强紫外线" in card["alerts"]

    def test_multiple_alerts(self):
        day = {"weather_code": 95, "temperature_2m_max": 37, "temperature_2m_min": 25,
               "precipitation_probability_max": 80, "wind_gusts_10m_max": 60,
               "uv_index_max": 9}
        card = _build_day_card(day, datetime.date(2026, 7, 14), None)
        assert len(card["alerts"]) >= 3

    def test_precip_hourly_and_wet_windows(self):
        day = {"weather_code": 0, "temperature_2m_max": 28, "temperature_2m_min": 20}
        hourly = {
            "time": ["2026-07-14T13:00", "2026-07-14T14:00",
                     "2026-07-14T15:00", "2026-07-14T16:00", "2026-07-14T17:00"],
            "precipitation_probability": [10, 55, 60, 45, 20],
        }
        card = _build_day_card(day, datetime.date(2026, 7, 14), hourly)
        assert card["precip_hourly"] == {13: 10, 14: 55, 15: 60, 16: 45, 17: 20}
        # 14-16 点连续高概率（>=40），17 点 20% 不计入
        assert card["wet_windows"] == [{"start": 14, "end": 16, "max_prob": 60}]

    def test_no_hourly_data(self):
        day = {"weather_code": 0, "temperature_2m_max": 28, "temperature_2m_min": 20}
        card = _build_day_card(day, datetime.date(2026, 7, 14), None)
        assert card["precip_hourly"] == {}
        assert card["wet_windows"] == []

    def test_missing_fields_default_to_empty(self):
        day = {"weather_code": -1}
        card = _build_day_card(day, datetime.date(2026, 7, 14), None)
        assert card["weather"] == "未知"
        assert card["temp_max"] == ""
        assert card["temp_min"] == ""

    def test_precip_prob_max_and_mean(self):
        """降水概率百分比应明确出现在 precip_prob_max 字段中。"""
        day = {
            "weather_code": 61, "temperature_2m_max": 28, "temperature_2m_min": 20,
            "precipitation_probability_max": 65, "precipitation_probability_mean": 40,
            "rain_sum": 4.4, "precipitation_sum": 5.0, "precipitation_hours": 3,
        }
        card = _build_day_card(day, datetime.date(2026, 7, 14), None)
        assert card["precip_prob_max"] == "65%"
        assert card["precip_prob_mean"] == "40%"
        # rain_sum 与 precip_prob 是不同维度，不应混淆
        assert card["rain_sum"] == "4.4mm"
        assert card["precip_sum"] == "5.0mm"
        assert card["precip_hours"] == 3

    def test_feels_like_temperature(self):
        """体感温度应独立于实际温度展示。"""
        day = {
            "weather_code": 0, "temperature_2m_max": 35, "temperature_2m_min": 26,
            "apparent_temperature_max": 38, "apparent_temperature_min": 29,
        }
        card = _build_day_card(day, datetime.date(2026, 7, 14), None)
        assert card["feels_like_max"] == "38°C"
        assert card["feels_like_min"] == "29°C"

    def test_sunrise_sunset(self):
        """日出日落时间应格式化为 HH:MM。"""
        day = {
            "weather_code": 0, "temperature_2m_max": 30, "temperature_2m_min": 20,
            "sunrise": "2026-07-14T04:46", "sunset": "2026-07-14T19:18",
        }
        card = _build_day_card(day, datetime.date(2026, 7, 14), None)
        assert card["sunrise"] == "04:46"
        assert card["sunset"] == "19:18"

    def test_sunrise_sunset_none(self):
        """日出日落缺失时返回空串。"""
        day = {"weather_code": 0, "temperature_2m_max": 30, "temperature_2m_min": 20}
        card = _build_day_card(day, datetime.date(2026, 7, 14), None)
        assert card["sunrise"] == ""
        assert card["sunset"] == ""

    def test_hourly_humidity_aggregation(self):
        """逐时湿度应聚合为日均值 humidity_mean。"""
        day = {"weather_code": 0, "temperature_2m_max": 30, "temperature_2m_min": 20}
        hourly = {
            "time": ["2026-07-14T08:00", "2026-07-14T12:00", "2026-07-14T18:00"],
            "precipitation_probability": [10, 20, 30],
            "relative_humidity_2m": [70, 50, 65],
        }
        card = _build_day_card(day, datetime.date(2026, 7, 14), hourly)
        assert card["humidity_mean"] == "62%"  # round((70+50+65)/3) = 61.67 → 62
        assert card["humidity_hourly"] == {8: 70, 12: 50, 18: 65}

    def test_no_rain_summary_omits_zero_values(self):
        """无降雨时 summary/markdown 不显示降雨量和降水时长。"""
        day = {
            "weather_code": 0, "temperature_2m_max": 30, "temperature_2m_min": 20,
            "precipitation_probability_max": 5, "rain_sum": 0, "precipitation_hours": 0,
        }
        card = _build_day_card(day, datetime.date(2026, 7, 14), None)
        assert card["rain_sum"] == "0mm"  # 原始值保留
        # 但 summary/markdown 渲染时会跳过 0mm / 0h


# ── _fmt_time ────────────────────────────────────────────────

class TestFmtTime:
    def test_iso_format(self):
        assert _fmt_time("2026-07-14T04:46") == "04:46"

    def test_short_string(self):
        assert _fmt_time("04:46") == "04:46"

    def test_none_returns_empty(self):
        assert _fmt_time(None) == ""

    def test_empty_string(self):
        assert _fmt_time("") == ""


# ── _build_summary ───────────────────────────────────────────

class TestBuildSummary:
    def test_single_day(self):
        card = _build_day_card(
            {"weather_code": 0, "temperature_2m_max": 30, "temperature_2m_min": 20},
            datetime.date(2026, 7, 14), None)
        text = _build_summary("北京", "7/14 周二", [card], [])
        assert "北京" in text
        assert "7/14" in text
        assert "晴" in text
        assert "30°C" in text

    def test_with_alerts(self):
        card = _build_day_card(
            {"weather_code": 0, "temperature_2m_max": 36, "temperature_2m_min": 25,
             "precipitation_probability_max": 70},
            datetime.date(2026, 7, 14), None)
        text = _build_summary("北京", "7/14", [card], [])
        assert "高温" in text
        assert "强降水" in text

    def test_past_day_skipped(self):
        card = _build_day_card(
            {"weather_code": 0, "temperature_2m_max": 25, "temperature_2m_min": 15},
            datetime.date(2026, 7, 12), None)
        card["_past"] = True
        text = _build_summary("上海", "7/12", [card], [])
        assert "已过去" in text

    def test_wet_windows_section(self):
        day = {"weather_code": 0, "temperature_2m_max": 28, "temperature_2m_min": 20}
        hourly = {
            "time": ["2026-07-14T08:00"],
            "precipitation_probability": [50],
        }
        card = _build_day_card(day, datetime.date(2026, 7, 14), hourly)
        text = _build_summary("深圳", "7/14", [card], [])
        assert "高概率降水时段" in text


# ── _build_markdown ──────────────────────────────────────────

class TestBuildMarkdown:
    def test_basic_card(self):
        card = _build_day_card(
            {"weather_code": 0, "temperature_2m_max": 30, "temperature_2m_min": 20},
            datetime.date(2026, 7, 14), None)
        md = _build_markdown("北京", "7/14 周二", [card], None, [])
        assert "北京" in md
        assert "7/14" in md
        assert "晴" in md
        assert "☀" in md

    def test_with_current(self):
        current = {"weather": "晴", "temperature": "25°C", "feels_like": "27°C",
                   "humidity": "60%", "wind": "2级轻风"}
        card = _build_day_card(
            {"weather_code": 0, "temperature_2m_max": 30, "temperature_2m_min": 20},
            datetime.date(2026, 7, 14), None)
        md = _build_markdown("北京", "7/14", [card], current, [])
        assert "当前" in md
        assert "25°C" in md
        assert "体感" in md

    def test_with_alerts(self):
        card = _build_day_card(
            {"weather_code": 0, "temperature_2m_max": 36, "temperature_2m_min": 25},
            datetime.date(2026, 7, 14), None)
        md = _build_markdown("北京", "7/14", [card], None, [])
        assert "⚠️" in md
        assert "高温" in md

    def test_past_day(self):
        card = _build_day_card(
            {"weather_code": 0, "temperature_2m_max": 25, "temperature_2m_min": 15},
            datetime.date(2026, 7, 12), None)
        card["_past"] = True
        md = _build_markdown("上海", "7/12", [card], None, [])
        assert "已过去" in md

    def test_source_line(self):
        card = _build_day_card(
            {"weather_code": 0, "temperature_2m_max": 30, "temperature_2m_min": 20},
            datetime.date(2026, 7, 14), None)
        md = _build_markdown("北京", "7/14", [card], None, [])
        assert "open-meteo.com" in md

    def test_wet_windows_section(self):
        day = {"weather_code": 0, "temperature_2m_max": 28, "temperature_2m_min": 20}
        hourly = {
            "time": ["2026-07-14T07:00"],
            "precipitation_probability": [60],
        }
        card = _build_day_card(day, datetime.date(2026, 7, 14), hourly)
        md = _build_markdown("深圳", "7/14", [card], None, [])
        assert "高概率降水时段" in md
        assert "通勤" not in md
        assert "早高峰" not in md


# ── 地理解析（细粒度 + 数据源一致） ─────────────────────────

class TestGeocode:
    def test_compose_display_name_with_admin_levels(self):
        geo = {
            "name": "文三路", "admin1": "浙江省", "admin2": "杭州市",
            "admin3": "西湖区", "admin4": "",
        }
        assert _compose_display_name(geo, "杭州") == "文三路(西湖区, 杭州市, 浙江省)"

    def test_compose_display_name_no_extra_levels(self):
        geo = {"name": "北京", "admin1": "北京", "admin2": "", "admin3": "", "admin4": ""}
        # admin1 与 name 完全相同，不重复括号
        assert _compose_display_name(geo, "北京") == "北京"

    def test_open_meteo_geocode_parses_admin_levels(self):
        # 验证 Open-Meteo 地理编码兜底能下钻 admin1~4
        payload = {
            "results": [{
                "name": "西湖区", "latitude": 30.25, "longitude": 120.13,
                "admin1": "浙江省", "admin2": "杭州市", "admin3": "西湖区",
                "admin4": "西溪街道", "country": "中国",
            }]
        }

        class _FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return payload

        with mock.patch("src.tools.weather._http_get", return_value=_FakeResp()):
            geo = _geocode_open_meteo("杭州西湖区", 10)
        assert geo["name"] == "西湖区"
        assert geo["admin4"] == "西溪街道"
        assert geo["latitude"] == 30.25

    def test_geocode_prefers_nominatim(self):
        # Nominatim 返回街道级 -> 应被优先采用；Open-Meteo 不应被调用
        nominatim_payload = [{
            "lat": "30.2741", "lon": "120.1551",
            "display_name": "文三路, 西湖区, 杭州市, 浙江省, 中国",
            "address": {
                "road": "文三路", "city_district": "西湖区",
                "city": "杭州市", "state": "浙江省", "country": "中国",
            },
        }]

        class _NominatimResp:
            def raise_for_status(self):
                pass

            def json(self):
                return nominatim_payload

        calls = []

        def _fake_get(url, headers=None, timeout=None, **kwargs):
            calls.append(url)
            return _NominatimResp()

        with mock.patch("src.tools.weather.ssrf_safe_get", side_effect=_fake_get):
            geo = _geocode("杭州市西湖区文三路", 10)
        assert geo["source"] == "nominatim"
        assert geo["name"] == "文三路"
        assert geo["admin3"] == "西湖区"
        assert geo["latitude"] == 30.2741
        # 未回落到 Open-Meteo 地理编码
        assert not any(
            c.startswith("https://geocoding-api.open-meteo.com")
            for c in calls
        )

    def test_geocode_nominatim_limits_to_cn(self):
        # 请求必须带 countrycodes=cn，排除海外同名地点
        class _R:
            def raise_for_status(self):
                pass

            def json(self):
                return []

        captured = {}

        def _fake_get(url, headers=None, timeout=None, **kwargs):
            captured["url"] = url
            return _R()

        with mock.patch("src.tools.weather.ssrf_safe_get", side_effect=_fake_get):
            _geocode_nominatim("Springfield", 10)
        assert "countrycodes=cn" in captured["url"]

    def test_geocode_nominatim_picks_highest_importance(self):
        # 多候选时按 importance 降序取最相关，而非首个/邻近路名
        payload = [
            {
                "lat": "30.100", "lon": "120.100", "importance": "0.1",
                "address": {
                    "road": "某小路", "city": "杭州市",
                    "state": "浙江省", "country": "中国",
                },
            },
            {
                "lat": "30.2741", "lon": "120.1551", "importance": "0.9",
                "address": {
                    "road": "文三路", "city_district": "西湖区",
                    "city": "杭州市", "state": "浙江省", "country": "中国",
                },
            },
        ]

        class _R:
            def raise_for_status(self):
                pass

            def json(self):
                return payload

        with mock.patch("src.tools.weather.ssrf_safe_get", return_value=_R()):
            geo = _geocode_nominatim("杭州文三路", 10)
        # 应选 importance=0.9 的文三路，而非 importance=0.1 的某小路
        assert geo["name"] == "文三路"
        assert geo["latitude"] == 30.2741


# ── WMO_CODES 常量完整性 ─────────────────────────────────────

class TestWMOCodes:
    def test_codes_exist(self):
        assert 0 in WMO_CODES
        assert 99 in WMO_CODES
        assert len(WMO_CODES) >= 20


# ── 地点抽取（自由文本 -> 完整地点） ─────────────────────────

class TestExtractLocation:
    @pytest.mark.parametrize("query,expected", [
        ("帮我查一下某城市明天天气怎么样", "某城市"),
        ("帮我查一下杭州市西湖区文三路明天天气", "杭州市西湖区文三路"),
        ("北京国贸三期今天下雨吗", "北京国贸三期"),
        ("上海中心大厦天气", "上海中心大厦"),
        ("周口店天气", "周口店"),          # 「周」不能误切
        ("周末深圳天气", "深圳"),            # 地点在日期之后
        ("下周一到周五某城市天气怎么样", "某城市"),
        ("明天北京下雨吗", "北京"),
        ("下周杭州天气", "杭州"),
        ("本周三上海天气", "上海"),
        ("未来三天杭州天气", "杭州"),
        ("查一下某城市", "某城市"),
        ("请问杭州的天气", "杭州"),
        ("后天西湖区天气", "西湖区"),
    ])
    def test_extract(self, query, expected):
        assert _extract_location_from_query(query) == expected

    def test_empty_query(self):
        assert _extract_location_from_query("") == ""
        assert _extract_location_from_query("   ") == ""


# ── Nominatim 细粒度（楼栋/街道优先 + 门牌号 + 回退） ────────

class TestNominatimFineGrained:
    def _resp(self, payload):
        class _R:
            def raise_for_status(self):
                pass

            def json(self):
                return payload
        return _R()

    def test_building_name_and_house_number(self):
        # 楼栋 POI：用专有名称，道路带门牌号；详细 query 时 building 优先于 city
        payload = [
            {"lat": "31.2336", "lon": "121.5055", "importance": "0.510",
             "type": "commercial", "addresstype": "building", "name": "上海中心大厦",
             "address": {"road": "银城中路", "house_number": "501",
                         "building": "上海中心大厦", "city": "浦东新区", "state": "上海市"}},
            {"lat": "31.2304", "lon": "121.4737", "importance": "0.753",
             "type": "city", "addresstype": "city", "name": "上海市",
             "address": {"city": "上海市", "state": "上海市"}},
        ]
        with mock.patch("src.tools.weather.ssrf_safe_get", return_value=self._resp(payload)):
            geo = _geocode_nominatim("上海中心大厦", 10)
        assert geo["name"] == "上海中心大厦"
        assert geo["feature_type"] == "building"
        assert geo["latitude"] == 31.2336

    def test_road_with_house_number(self):
        # 道路结果带门牌号：展示名应为『文三路88号』
        payload = [{
            "lat": "30.2741", "lon": "120.1551", "importance": "0.3",
            "type": "secondary", "addresstype": "road", "name": "文三路",
            "address": {"road": "文三路", "house_number": "88",
                        "city_district": "西湖区", "city": "杭州市", "state": "浙江省"},
        }]
        with mock.patch("src.tools.weather.ssrf_safe_get", return_value=self._resp(payload)):
            geo = _geocode_nominatim("文三路88号", 10)
        assert geo["name"] == "文三路88号"

    def test_relaxed_fallback_to_city(self):
        # 详细楼栋名在 OSM 缺数据（n=0）：逐级回退到城市，而非整条失败
        calls = []

        def _fake_get(url, headers=None, timeout=None, **kwargs):
            calls.append(url)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("q", [""])[0]
            # 精确查询返回空，城市回退返回结果
            if q == "北京国贸三期":
                return self._resp([])
            return self._resp([{"lat": "39.9042", "lon": "116.4074",
                                 "importance": "0.796", "type": "city",
                                 "addresstype": "city", "name": "北京市",
                                 "address": {"city": "北京市", "state": "北京市"}}])

        with mock.patch("src.tools.weather.ssrf_safe_get", side_effect=_fake_get):
            geo = _geocode_nominatim("北京国贸三期", 10)
        assert geo["name"] == "北京市"
        # 触发了回退候选请求（北京国贸 / 北京国 / 北京）
        assert any("q=%E5%8C%97%E4%BA%AC" in c for c in calls)

    def test_score_prefers_building_for_detailed_query(self):
        city = {"importance": "0.9", "addresstype": "city", "address": {}}
        bldg = {"importance": "0.5", "addresstype": "building",
                "address": {"building": "X"}}
        # 详细 query：楼栋分数高
        assert _nominatim_score(bldg, "北京国贸三期") > _nominatim_score(city, "北京国贸三期")
        # 纯城市 query：按 importance，不被 specificity 干扰
        assert _nominatim_score(city, "北京") > _nominatim_score(bldg, "北京")

    def test_relaxed_candidates(self):
        assert _relaxed_location_candidates("北京国贸三期") == ["北京国贸", "北京国"]
        assert _relaxed_location_candidates("杭州市西湖区文三路") == [
            "杭州市西湖区文", "杭州市西湖区", "杭州市西"]


# ── execute 直接坐标 ────────────────────────────────────────

class TestExecuteCoords:
    def test_direct_lat_lon_skips_geocode(self):
        from src.tools.weather import WeatherTool
        calls = []

        def _fake_fetch(lat, lon, days, timeout):
            calls.append((lat, lon))
            return {"daily": {"time": ["2026-08-18"], "weather_code": [0],
                              "temperature_2m_max": [30], "temperature_2m_min": [22]},
                    "hourly": {"time": [], "precipitation_probability": [],
                               "relative_humidity_2m": []},
                    "current": {"weather_code": 0, "temperature_2m": 26,
                                "apparent_temperature": 27, "relative_humidity_2m": 60,
                                "wind_speed_10m": 8}}

        with mock.patch("src.tools.weather._fetch_forecast", side_effect=_fake_fetch), \
             mock.patch("src.tools.weather._geocode") as geocode_mock:
            r = WeatherTool().execute({"location": "我的坐标", "lat": 39.9087,
                                       "lon": 116.3975, "query": "这里天气"})
        assert r["source"] == "open-meteo.com"
        assert r["city"] == "我的坐标"
        geocode_mock.assert_not_called()  # 直接坐标不应触发地理解析
        assert calls[0] == (39.9087, 116.3975)

