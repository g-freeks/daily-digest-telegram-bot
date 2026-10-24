"""Loads config.json on every run, keeping the last good version on
parse/validation errors.

Per CLAUDE.md: "Validate on load: unknown source type, missing prompt, bad URL
-> reject, keep last good config, report the error in chat."
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from collectors import ALWAYS_INCLUDE_TAGS, SEVERITIES, validate_source

logger = logging.getLogger(__name__)


def _non_empty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_filters(filters: object) -> List[str]:
    if filters is None:
        return []
    if not isinstance(filters, dict):
        return ["topic.filters must be an object"]
    errors = []
    min_severity = filters.get("min_severity")
    if min_severity is not None and min_severity not in SEVERITIES:
        errors.append(f"topic.filters.min_severity must be one of {SEVERITIES}")
    always_include = filters.get("always_include", [])
    if not isinstance(always_include, list) or any(t not in ALWAYS_INCLUDE_TAGS for t in always_include):
        errors.append(f"topic.filters.always_include must be a list of {ALWAYS_INCLUDE_TAGS}")
    packages = filters.get("packages")
    if packages is not None:
        if not isinstance(packages, dict):
            errors.append("topic.filters.packages must be an object")
        else:
            watchlist = packages.get("watchlist", [])
            if not isinstance(watchlist, list) or not all(_non_empty_str(p) for p in watchlist):
                errors.append("topic.filters.packages.watchlist must be a list of package names")
            min_downloads = packages.get("min_weekly_downloads")
            if min_downloads is not None and not isinstance(min_downloads, int):
                errors.append("topic.filters.packages.min_weekly_downloads must be an int")
    return errors


def validate_config(config: dict) -> List[str]:
    """Returns a list of human-readable errors; empty means the config is usable."""
    if not isinstance(config, dict):
        return ["config must be a JSON object"]
    errors = []

    chat_ids = config.get("allowed_chat_ids")
    if not isinstance(chat_ids, list) or not all(isinstance(c, int) for c in chat_ids):
        errors.append("allowed_chat_ids must be a list of ints")

    schedule = config.get("schedule")
    if not isinstance(schedule, dict):
        errors.append("missing or invalid schedule object")
    else:
        try:
            datetime.strptime(schedule.get("time", ""), "%H:%M")
        except (TypeError, ValueError):
            errors.append("schedule.time must be HH:MM")
        try:
            ZoneInfo(schedule.get("timezone", ""))
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            errors.append(f"schedule.timezone is not a known timezone: {schedule.get('timezone')!r}")

    topic = config.get("topic")
    if not isinstance(topic, dict):
        errors.append("missing or invalid topic object")
    else:
        for field in ("name", "prompt", "empty_message"):
            if not _non_empty_str(topic.get(field)):
                errors.append(f"topic.{field} must be a non-empty string")
        sources = topic.get("sources")
        if not isinstance(sources, list) or not sources:
            errors.append("topic.sources must be a non-empty list")
        else:
            for i, source in enumerate(sources):
                errors.extend(f"topic.sources[{i}]: {e}" for e in validate_source(source))
        errors.extend(_validate_filters(topic.get("filters")))

    weather = config.get("weather")
    if not isinstance(weather, dict):
        errors.append("missing or invalid weather object")
    else:
        if not _non_empty_str(weather.get("prompt")):
            errors.append("weather.prompt must be a non-empty string")
        location = weather.get("location")
        if not isinstance(location, dict):
            errors.append("weather.location must be an object")
        else:
            if not _non_empty_str(location.get("name")):
                errors.append("weather.location.name must be a non-empty string")
            lat, lon = location.get("lat"), location.get("lon")
            if not isinstance(lat, (int, float)) or not -90 <= lat <= 90:
                errors.append("weather.location.lat must be a number in [-90, 90]")
            if not isinstance(lon, (int, float)) or not -180 <= lon <= 180:
                errors.append("weather.location.lon must be a number in [-180, 180]")

    telegram = config.get("telegram", {})
    if not isinstance(telegram, dict) or not isinstance(telegram.get("receive_updates", True), bool):
        errors.append("telegram.receive_updates must be true or false")

    runtime = config.get("runtime")
    if not isinstance(runtime, dict):
        errors.append("missing or invalid runtime object")
    else:
        for field in ("timeout_seconds", "max_turns"):
            if not isinstance(runtime.get(field), int) or runtime[field] <= 0:
                errors.append(f"runtime.{field} must be a positive int")
        if not isinstance(runtime.get("weather_max_turns", 3), int):
            errors.append("runtime.weather_max_turns must be an int")

    return errors


def load_raw(path: Path) -> dict:
    """Reads config.json without caching or validation; raises on bad JSON."""
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


class ConfigStore:
    def __init__(self, path: Path):
        self._path = path
        self._last_good: Optional[dict] = None

    def load(self) -> Tuple[dict, Optional[str]]:
        """Returns (config, error). `error` is None on success. On a parse or
        validation error, returns the last good config (or {} if there isn't
        one yet) alongside a human-readable error message to report in chat."""
        try:
            config = load_raw(self._path)
        except (OSError, json.JSONDecodeError) as exc:
            error = f"{self._path} is invalid: {exc}"
            if self._last_good is None:
                logger.error(error)
                return {}, error
            logger.warning("%s — keeping last good config", error)
            return self._last_good, error

        errors = validate_config(config)
        if errors:
            error = f"{self._path} failed validation: " + "; ".join(errors)
            if self._last_good is None:
                logger.error(error)
                return {}, error
            logger.warning("%s — keeping last good config", error)
            return self._last_good, error

        self._last_good = config
        return config, None
