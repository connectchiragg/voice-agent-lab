"""Tools the voice agent can call. All free and keyless (Open-Meteo, local clock).

Every tool returns a small JSON-serialisable dict. Failures come back as
{"error": ...} for the model to explain, never as exceptions that end a turn.
"""
import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

logger = logging.getLogger("uvicorn.error")
TOOL_TIMEOUT_S = 5.0
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes, as used by Open-Meteo.
WEATHER = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle",
    57: "freezing drizzle", 61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain",
    67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers", 85: "snow showers",
    86: "heavy snow showers", 95: "thunderstorm", 96: "thunderstorm with hail",
    99: "thunderstorm with hail",
}


# Place name -> geocoding result. Places rarely move; this saves a round trip.
GEOCODE_CACHE: dict[str, dict[str, Any] | None] = {}
GEOCODE_CACHE_SIZE = 256


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[..., Awaitable[dict[str, Any]]]
    # True for tools that change something outside the conversation (book, send, pay).
    # A speculative reply runs only tools without side effects before it is committed.
    side_effects: bool = False

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


async def fetch_json(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET JSON, retrying once on a rate limit or server error (Open-Meteo has blips)."""
    for attempt in (1, 2):
        response = await client.get(url, params=params)
        if response.status_code == 429 or response.status_code >= 500:
            logger.warning("%s returned %d (attempt %d)", response.url.host, response.status_code, attempt)
            if attempt == 1:
                await asyncio.sleep(0.3)
                continue
        response.raise_for_status()
        return response.json()
    raise AssertionError("unreachable")


def most_populous(places: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Geocoders rank by name match; a speaker almost always means the biggest place.
    ("Mysore" otherwise resolves to a toll gate in Bengaluru.)"""
    return max(places, key=lambda place: place.get("population") or 0, default=None)


async def get_weather(location: str) -> dict[str, Any]:
    key = location.strip().lower()
    async with httpx.AsyncClient(timeout=TOOL_TIMEOUT_S) as client:
        if key not in GEOCODE_CACHE:
            geo = await fetch_json(client, GEOCODE_URL, {"name": location, "count": 10})
            if len(GEOCODE_CACHE) >= GEOCODE_CACHE_SIZE:
                GEOCODE_CACHE.pop(next(iter(GEOCODE_CACHE)))  # Drop the oldest entry.
            GEOCODE_CACHE[key] = most_populous(geo.get("results") or [])
        place = GEOCODE_CACHE[key]
        if place is None:
            return {"error": f"No place named {location!r} was found. Try the nearest town "
                             "or the official name (for example Madikeri instead of Coorg)."}
        data = await fetch_json(client, FORECAST_URL, {
            "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto",
            "forecast_days": 3, "current": "temperature_2m,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
        })
    now, daily = data["current"], data["daily"]
    # Short phrases rather than nested JSON: every token is re-sent to the model.
    return {
        "place": ", ".join(part for part in (place.get("name"), place.get("admin1"),
                                             place.get("country")) if part),
        "now": f"{WEATHER.get(now['weather_code'], 'unknown')}, {round(now['temperature_2m'])} C, "
               f"wind {round(now['wind_speed_10m'])} km/h",
        "forecast": [f"{date}: {WEATHER.get(code, 'unknown')}, {round(low)} to {round(high)} C, "
                     f"{rain}% chance of rain"
                     for date, code, high, low, rain in zip(
                         daily["time"], daily["weather_code"], daily["temperature_2m_max"],
                         daily["temperature_2m_min"], daily["precipitation_probability_max"])],
    }


async def get_time(timezone: str = "Asia/Kolkata") -> dict[str, Any]:
    try:
        now = datetime.now(ZoneInfo(timezone))
    except (ZoneInfoNotFoundError, ValueError):
        return {"error": f"Unknown time zone {timezone!r}. Use an IANA name like Europe/London."}
    # 12-hour time: the model misreads "15:30" when turning it into speech.
    return {"timezone": timezone, "date": now.strftime("%A, %d %B %Y"),
            "time": f"{now.strftime('%I').lstrip('0')}:{now:%M %p}"}


TOOLS = {tool.name: tool for tool in (
    Tool("get_weather",
         "Current weather and a three-day forecast (today first) for a city or town.",
         {"type": "object", "properties": {"location": {
             "type": "string", "description": "City or town name, e.g. Mysuru or Madikeri"}},
          "required": ["location"]},
         get_weather),
    Tool("get_time",
         "Current date and time in another time zone. The user's own local time is "
         "already in your instructions.",
         {"type": "object", "properties": {"timezone": {
             "type": "string", "description": "IANA time zone, e.g. Europe/London"}},
          "required": ["timezone"]},
         get_time),
)}
SCHEMAS = [tool.schema() for tool in TOOLS.values()]


def has_side_effects(name: str) -> bool:
    """Unknown names count as safe: run_tool only reports them as errors."""
    tool = TOOLS.get(name)
    return tool is not None and tool.side_effects


async def run_tool(name: str, arguments: str) -> str:
    """Run one tool call from the model and return its JSON result text."""
    started = time.perf_counter()
    try:
        tool = TOOLS[name]
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
        result = await asyncio.wait_for(tool.run(**args), TOOL_TIMEOUT_S)
    except KeyError:
        result = {"error": f"There is no tool called {name!r}."}
    except (ValueError, TypeError) as error:
        result = {"error": f"Invalid arguments for {name}: {error}"}
    except httpx.HTTPStatusError as error:
        logger.warning("Tool %s failed: %s returned %d", name, error.request.url.host,
                       error.response.status_code)
        result = {"error": f"{name} is unavailable right now."}
    except (TimeoutError, httpx.HTTPError) as error:
        logger.warning("Tool %s failed: %s", name, type(error).__name__)
        result = {"error": f"{name} is unavailable right now."}
    logger.info("Tool %s took %.0f ms", name, (time.perf_counter() - started) * 1000)
    return json.dumps(result, ensure_ascii=False)
