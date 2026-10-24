"""Weekend weather report (Sat–Sun): Python fetches the forecast, Claude (no
tools) writes the summary per `weather.prompt`.

Primary source: SMHI open data, category snow1g v1 (pmp3g v2 was retired and
now 404s). Fallback: MET Norway locationforecast 2.0 (needs a descriptive
User-Agent, which collectors.http_client sends).
"""
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

import httpx

import render_config
import runner
from collectors import http_client
from formatting import Outcome
from state import State

logger = logging.getLogger(__name__)

SMHI_URL = ("https://opendata-download-metfcst.smhi.se/api/category/snow1g/version/1"
            "/geotype/point/lon/{lon}/lat/{lat}/data.json")
MET_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
SMHI_MISSING = 9999

# SMHI weather symbol codes (Wsymb2), 1–27.
SMHI_SYMBOLS = {
    1: "clear sky", 2: "nearly clear sky", 3: "variable cloudiness", 4: "halfclear sky",
    5: "cloudy sky", 6: "overcast", 7: "fog", 8: "light rain showers", 9: "moderate rain showers",
    10: "heavy rain showers", 11: "thunderstorm", 12: "light sleet showers",
    13: "moderate sleet showers", 14: "heavy sleet showers", 15: "light snow showers",
    16: "moderate snow showers", 17: "heavy snow showers", 18: "light rain", 19: "moderate rain",
    20: "heavy rain", 21: "thunder", 22: "light sleet", 23: "moderate sleet", 24: "heavy sleet",
    25: "light snowfall", 26: "moderate snowfall", 27: "heavy snowfall",
}


class WeatherError(Exception):
    pass


def _value(data: dict, key: str):
    value = data.get(key)
    return None if value is None or value == SMHI_MISSING else value


def fetch_smhi(lat: float, lon: float, tz) -> List[dict]:
    # SMHI rejects more than 6 decimals.
    url = SMHI_URL.format(lat=round(lat, 6), lon=round(lon, 6))
    with http_client() as client:
        response = client.get(url)
    if response.status_code != 200:
        raise WeatherError(f"SMHI returned HTTP {response.status_code}")
    rows = []
    for step in response.json()["timeSeries"]:
        data = step["data"]
        symbol = _value(data, "symbol_code")
        rows.append({
            "time": datetime.fromisoformat(step["time"].replace("Z", "+00:00")).astimezone(tz),
            "temp_c": _value(data, "air_temperature"),
            "precip_mm": _value(data, "precipitation_amount_mean"),
            "precip_probability_pct": _value(data, "probability_of_precipitation"),
            "wind_ms": _value(data, "wind_speed"),
            "gust_ms": _value(data, "wind_speed_of_gust"),
            "wind_from_deg": _value(data, "wind_from_direction"),
            "thunder_probability_pct": _value(data, "thunderstorm_probability"),
            "sky": SMHI_SYMBOLS.get(symbol, symbol),
        })
    return rows


def fetch_met(lat: float, lon: float, tz) -> List[dict]:
    with http_client() as client:
        response = client.get(MET_URL, params={"lat": round(lat, 4), "lon": round(lon, 4)})
    if response.status_code != 200:
        raise WeatherError(f"MET Norway returned HTTP {response.status_code}")
    rows = []
    for step in response.json()["properties"]["timeseries"]:
        details = step["data"]["instant"]["details"]
        period = step["data"].get("next_1_hours") or step["data"].get("next_6_hours") or {}
        rows.append({
            "time": datetime.fromisoformat(step["time"].replace("Z", "+00:00")).astimezone(tz),
            "temp_c": details.get("air_temperature"),
            "precip_mm": (period.get("details") or {}).get("precipitation_amount"),
            "wind_ms": details.get("wind_speed"),
            "wind_from_deg": details.get("wind_from_direction"),
            "sky": (period.get("summary") or {}).get("symbol_code", "").replace("_", " ") or None,
        })
    return rows


def fetch_forecast(location: dict, tz):
    """Returns (source_name, rows). Raises WeatherError if both sources fail."""
    errors = []
    for name, fetch in (("SMHI", fetch_smhi), ("MET Norway", fetch_met)):
        try:
            rows = fetch(location["lat"], location["lon"], tz)
            if rows:
                return name, rows
            errors.append(f"{name}: empty forecast")
        except (WeatherError, httpx.HTTPError, ValueError, KeyError) as exc:
            logger.exception("%s forecast fetch failed", name)
            errors.append(f"{name}: {exc}")
    raise WeatherError("; ".join(errors))


def _day_summary(rows: List[dict]) -> dict:
    def values(key):
        return [r[key] for r in rows if r.get(key) is not None]

    temps, gusts, winds, precip = values("temp_c"), values("gust_ms"), values("wind_ms"), values("precip_mm")
    return {
        "temp_min_c": min(temps) if temps else None,
        "temp_max_c": max(temps) if temps else None,
        "precip_total_mm": round(sum(precip), 1) if precip else None,
        "hours_with_precip": [r["time"].strftime("%H:%M") for r in rows if (r.get("precip_mm") or 0) >= 0.1],
        "wind_max_ms": max(winds) if winds else None,
        "gust_max_ms": max(gusts) if gusts else None,
    }


def build_forecast_payload(source: str, rows: List[dict], now: datetime, location: dict) -> dict:
    today = now.date()
    by_day = defaultdict(list)
    for row in rows:
        if row["time"].date() == today and row["time"] < now - timedelta(hours=1):
            continue
        by_day[row["time"].date()].append(row)

    def day(date):
        day_rows = by_day.get(date, [])
        return {
            "date": date.isoformat(),
            "weekday": date.strftime("%A"),
            "summary": _day_summary(day_rows),
            "steps": [{**r, "time": r["time"].strftime("%H:%M")} for r in day_rows],
        }

    return {
        "source": source,
        "location": location["name"],
        "generated_local": now.strftime("%Y-%m-%d %H:%M %Z"),
        "units": {"temp": "°C", "precip": "mm per step (sum since previous step)", "wind": "m/s"},
        "today": day(today),
        "tomorrow": day(today + timedelta(days=1)),
    }


def run_weather(config: dict, state: State, now: datetime, *, retry_delay_seconds: Optional[float] = 300) -> Outcome:
    weather = config["weather"]
    runtime = config["runtime"]
    tz = ZoneInfo(config["schedule"]["timezone"])
    now = now.astimezone(tz)

    try:
        source, rows = fetch_forecast(weather["location"], tz)
    except WeatherError as exc:
        return Outcome(f"Weather report failed, couldn't fetch the forecast: {exc}", failed=True)

    payload = build_forecast_payload(source, rows, now, weather["location"])
    prompt = ("Write the weather report from this forecast data:\n\n<forecast>\n"
              + json.dumps(payload, indent=1, ensure_ascii=False) + "\n</forecast>")

    for attempt in (1, 2):
        try:
            result = runner.run_claude(
                prompt,
                render_config.render_weather_system_prompt(weather),
                state.work_dir,
                allowed_tools="",
                max_turns=runtime.get("weather_max_turns", 3),
                timeout_seconds=runtime["timeout_seconds"],
            )
            break
        except runner.RunnerError as exc:
            logger.exception("weather attempt %d failed", attempt)
            if attempt == 2 or retry_delay_seconds is None:
                return Outcome(f"Weather report failed: {str(exc)[:500]}", failed=True)
            time.sleep(retry_delay_seconds)

    state.log_cost("weather", result)
    logger.info("weather cost $%s (%s)", result.get("total_cost_usd"), source)
    text = (result.get("result") or "").strip()
    archive_path = state.archive_report(f"{now:%Y-%m-%d}-weather", text)
    return Outcome(text, archive_path)
