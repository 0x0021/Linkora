"""
Markdown 卡片支持测试。

背景：天气工具已改为纯结构化数据返回（days/current/alerts），不再内置 markdown 模板，
由 LLM 根据用户问题自由组织自然语言回复。本文件验证：
- 天气工具 execute 返回纯数据（无 markdown/summary 模板字段）
- _build_markdown 纯函数仍可独立使用（保留供可选场景）
- 防灌水截断（_enforce_brevity）对结构化 markdown 卡片放宽上限，但不会无限放宽
- 自动回复时能从正文首行 markdown 标题提取卡片标题（extract_card_title）
"""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import LlmConfig
from src.llm.agent import LLMAgent
from src.tools.weather import _build_markdown, WeatherTool
from main import extract_card_title


# ---------- 1) 天气 markdown 卡片 ----------

def _sample_cards():
    return [
        {
            "label": "7/11 周五", "weather": "晴", "weather_code": 0,
            "temp_min": "24°C", "temp_max": "31°C", "precip_prob": "10%",
            "wind": "东南风3级", "wind_gust": "20 km/h", "uv_index": 6,
            "alerts": [], "precip_hourly": {8: 5, 18: 15}, "wet_windows": [],
        },
        {
            "label": "7/12 周六", "weather": "小雨", "weather_code": 61,
            "temp_min": "22°C", "temp_max": "28°C", "precip_prob": "70%",
            "wind": "北风4级", "uv_index": 3, "alerts": ["强降水"],
            "precip_hourly": {14: 70, 15: 65, 16: 55},
            "wet_windows": [{"start": 14, "end": 16, "max_prob": 70}],
        },
    ]


def test_build_markdown_structure():
    cards = _sample_cards()
    current = {"temperature": "28°C", "feels_like": "30°C",
               "weather": "晴", "humidity": "55%", "wind": "东南风3级"}
    md = _build_markdown("北京", "7/11 周五 至 7/12 周六", cards, current,
                         ["2026-07-11", "2026-07-12"])
    assert md.startswith("## 🌤 北京天气 · 7/11 周五 至 7/12 周六")
    # 每日一行 + emoji
    assert "**7/11 周五**" in md and "☀️" in md
    assert "**7/12 周六**" in md and "🌧️" in md
    # 极端值告警
    assert "⚠️ 强降水" in md
    # 高概率降水时段（数据驱动，不绑定通勤）
    assert "高概率降水时段" in md
    assert "14:00-16:00" in md and "最高 70%" in md
    assert "通勤" not in md and "早高峰" not in md
    # 来源标注
    assert "> 来源：open-meteo.com" in md


def test_weather_execute_returns_structured_data():
    """端到端：execute 返回纯结构化数据（无 markdown/summary 模板字段），由 LLM 自由组织语言。"""
    fake_geo = {
        "name": "北京", "admin1": "北京", "country": "中国",
        "latitude": 39.9, "longitude": 116.4,
    }
    # 构造 2 天预报，daily.time 含今天与明天
    from datetime import date, timedelta
    today = date.today()
    t1 = str(today)
    t2 = str(today + timedelta(days=1))

    def _fake_fetch(lat, lon, days, timeout):
        return {
            "current": {
                "temperature_2m": 28, "relative_humidity_2m": 55,
                "apparent_temperature": 30, "weather_code": 0,
                "wind_speed_10m": 12, "wind_direction_10m": 120, "precipitation": 0,
            },
            "daily": {
                "time": [t1, t2],
                "weather_code": [0, 61],
                "temperature_2m_max": [31, 28],
                "temperature_2m_min": [24, 22],
                "precipitation_probability_max": [10, 70],
                "precipitation_sum": [0, 12],
                "rain_sum": [0, 12],
                "wind_speed_10m_max": [12, 20],
                "wind_gusts_10m_max": [20, 35],
                "wind_direction_10m_dominant": [120, 30],
                "uv_index_max": [6, 3],
            },
            "hourly": {
                "time": [f"{t1}T08:00", f"{t1}T18:00"],
                "precipitation_probability": [5, 15],
                "temperature_2m": [25, 27],
            },
        }

    with mock.patch("src.tools.weather._geocode", return_value=fake_geo), \
         mock.patch("src.tools.weather._fetch_forecast", side_effect=_fake_fetch):
        tool = WeatherTool()
        out = tool.execute({"city": "北京", "query": "未来两天北京天气"})
    # 不再返回 markdown / summary 模板字段
    assert "markdown" not in out, "execute 不应返回 markdown 字段（已改为纯数据）"
    assert "summary" not in out, "execute 不应返回 summary 字段（已改为纯数据）"
    # 结构化数据字段齐全
    assert out.get("city") == "北京"
    assert isinstance(out.get("days"), list) and len(out["days"]) == 2
    day0 = out["days"][0]
    assert day0["weather"] == "晴"
    assert "31°C" in day0["temp_max"]
    assert "24°C" in day0["temp_min"]
    assert out.get("source") == "open-meteo.com"


# ---------- 2) 防灌水截断对 markdown 卡片放宽 ----------

def _make_agent(hard=150):
    adv = LlmConfig().advanced
    adv.hard_truncation_chars = hard
    return LLMAgent(config=LlmConfig(advanced=adv), client=None, tool_router=None)


def test_brevity_truncates_plain_long_reply():
    a = _make_agent(hard=150)
    long_plain = "这是一句很长很长的话。" * 40  # 远超 150
    out = a._enforce_brevity(long_plain)
    assert len(out) <= 150


def test_brevity_keeps_markdown_card():
    a = _make_agent(hard=150)
    # 结构化 markdown 卡片：含标题/加粗/多行，长度在 150~1300 之间
    lines = ["## 🌤 北京天气 · 7/11-7/13", ""]
    for i in range(8):
        lines.append(f"**7/{11+i} 周X** ☀️ 晴 24~31°C")
        lines.append("> 降水 10% · 东南风3级 · 紫外线6")
    card = "\n".join(lines)
    assert len(card) > 150
    out = a._enforce_brevity(card)
    # markdown 卡片不应被 150 上限砍断
    assert out == card


def test_brevity_still_caps_huge_markdown():
    a = _make_agent(hard=150)
    huge = "## 标题\n" + ("**加粗内容** 一二三四五六七八九十\n" * 200)  # 远超 1300
    out = a._enforce_brevity(huge)
    assert len(out) <= 1300


def test_brevity_inline_hashtag_not_structured():
    """回归 F11：行内 #123 / #fff 不应被误判为 markdown 标题而放宽截断（非飞书平台无 # 标题）。"""
    a = _make_agent(hard=150)
    # 单行长文本含 issue 引用 #123，无真正标题/加粗/多行 → 应被硬截断到 150
    reply = "这是一句很长很长用来验证截断是否生效的话。" * 20 + " 参见 issue #123"
    out = a._enforce_brevity(reply)
    assert len(out) <= 150


# ---------- 3) 卡片标题提取 ----------

def test_extract_card_title_from_heading():
    text = "## 北京天气 · 7/11-7/13\n\n**今天** ☀️ 晴 24~31°C\n降水 10%"
    title, body = extract_card_title(text, default_title="回复")
    assert title == "北京天气 · 7/11-7/13"
    assert body.startswith("**今天**")
    assert "##" not in body.split("\n")[0]


def test_extract_card_title_no_heading_returns_default():
    text = "今天北京晴，24到31度，出门不用带伞。"
    title, body = extract_card_title(text, default_title="张三")
    assert title == "张三"
    assert body == text


def test_extract_card_title_h1_and_h3():
    t1, _ = extract_card_title("# 大标题\n正文", "默认")
    assert t1 == "大标题"
    t3, _ = extract_card_title("### 小标题\n正文", "默认")
    assert t3 == "小标题"
