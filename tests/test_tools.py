"""Tool execution: results, safe errors, and the Open-Meteo weather lookup."""
import asyncio
import json

import httpx
import pytest

from app import tools


def run(name, arguments):
    return json.loads(asyncio.run(tools.run_tool(name, arguments)))


def test_get_time_and_bad_inputs_return_errors_not_exceptions():
    result = run("get_time", '{"timezone": "Asia/Kolkata"}')
    assert set(result) == {"timezone", "date", "time"}
    assert result["time"].endswith(("AM", "PM")) and not result["time"].startswith("0")
    assert "error" in run("get_time", '{"timezone": "Mars/Base"}')
    assert "error" in run("no_such_tool", "{}")
    assert "error" in run("get_time", "not json")
    assert "error" in run("get_time", '{"unexpected": 1}')


@pytest.fixture
def open_meteo(monkeypatch):
    def handler(request):
        if "geocoding" in request.url.host:
            if request.url.params["name"] == "Atlantis":
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"results": [
                {"name": "Mysore Road Tolgate", "admin1": "Karnataka", "country": "India",
                 "latitude": 12.9, "longitude": 77.5},
                {"name": "Mysuru", "admin1": "Karnataka", "country": "India",
                 "latitude": 12.3, "longitude": 76.6, "population": 920550}]})
        return httpx.Response(200, json={
            "current": {"temperature_2m": 24.5, "weather_code": 2, "wind_speed_10m": 8},
            "daily": {"time": ["2026-10-12", "2026-10-13"], "weather_code": [61, 0],
                      "temperature_2m_max": [29, 30], "temperature_2m_min": [19, 20],
                      "precipitation_probability_max": [70, 5]}})

    monkeypatch.setattr(tools, "GEOCODE_CACHE", {})
    calls = []
    real_handler = handler

    def handler(request):
        calls.append(request.url.host)
        return real_handler(request)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(tools.httpx, "AsyncClient",
                        lambda **options: real_client(transport=httpx.MockTransport(handler), **options))
    return calls


def test_weather_summarises_open_meteo(open_meteo):
    result = run("get_weather", '{"location": "Mysore"}')
    assert result["place"] == "Mysuru, Karnataka, India"
    assert result["now"] == "partly cloudy, 24 C, wind 8 km/h"
    assert result["forecast"][0] == "2026-10-12: light rain, 19 to 29 C, 70% chance of rain"


def test_unknown_place_suggests_another_name(open_meteo):
    assert "No place named" in run("get_weather", '{"location": "Atlantis"}')["error"]


def test_geocoding_is_cached(open_meteo):
    run("get_weather", '{"location": "Mysore"}')
    run("get_weather", '{"location": " mysore "}')
    assert sum("geocoding" in host for host in open_meteo) == 1
    assert sum("geocoding" not in host for host in open_meteo) == 2


def test_server_error_is_retried_once(monkeypatch):
    responses = iter([httpx.Response(503), httpx.Response(200, json={"results": []})])
    real_client = httpx.AsyncClient
    monkeypatch.setattr(tools, "GEOCODE_CACHE", {})
    monkeypatch.setattr(tools.httpx, "AsyncClient", lambda **options: real_client(
        transport=httpx.MockTransport(lambda request: next(responses)), **options))
    assert "No place named" in run("get_weather", '{"location": "Nowhere"}')["error"]


def test_speakable_text_normalises_typography():
    from app.tts import speakable
    assert speakable("**Twenty\u2011five** in New\u202fYork \u2014 it\u2019s fine") == \
        "Twenty-five in New York, it's fine"
