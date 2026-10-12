"""First-token deadline and single retry for the Groq LLM stream."""
import asyncio
from types import SimpleNamespace

import pytest

from app import llm
from app.speech import SpeechError


def chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=None))])


class Stream:
    def __init__(self, texts, stall=False):
        self.texts, self.stall, self.closed = texts, stall, False

    def __aiter__(self):
        return self.items()

    async def items(self):
        if self.stall:
            await asyncio.sleep(3600)
        for text in self.texts:
            yield chunk(text)

    async def close(self):
        self.closed = True


class Client:
    def __init__(self, streams):
        self.streams, self.created = list(streams), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def with_options(self, **options):
        return self

    async def create(self, **request):
        stream = self.streams.pop(0)
        self.created.append(stream)
        return stream


async def collect(client, **options):
    messages = [{"role": "user", "content": "hi"}]
    return [delta async for delta in llm.stream_model(messages, client=client, **options)]


def test_stalled_first_token_is_retried_once():
    client = Client([Stream(["never"], stall=True), Stream(["Hello", " there"])])
    assert asyncio.run(collect(client, first_token_s=0.05)) == ["Hello", " there"]
    assert [stream.closed for stream in client.created] == [True, True]


def test_second_stall_fails_with_a_safe_error():
    client = Client([Stream([], stall=True), Stream([], stall=True)])
    with pytest.raises(SpeechError) as error:
        asyncio.run(collect(client, first_token_s=0.05))
    assert error.value.status_code == 504


def test_history_marks_cut_offs_and_drops_unanswered_tool_calls():
    history = [
        {"role": "user", "parts": [{"text": "hi"}], "status": "completed"},
        {"role": "model", "parts": [{"text": "Well, it..."}], "status": "interrupted"},
        {"role": "user", "parts": [{"text": "time?"}], "status": "completed"},
        {"role": "model", "parts": [{"text": "Let me check."}], "status": "completed",
         "tool_calls": [{"id": "a", "name": "get_time", "arguments": "{}"},
                        {"id": "b", "name": "get_weather", "arguments": "{}"}]},
        {"role": "tool", "parts": [{"text": "{}"}], "status": "completed", "tool_call_id": "a"},
    ]
    messages = llm.to_chat_messages(history)
    assert messages[0]["role"] == "system"
    assert "cut off" in messages[2]["content"]
    # Call "b" never got a result (interrupted mid-tool), so it is not sent back.
    assert [call["id"] for call in messages[4]["tool_calls"]] == ["a"]
    assert messages[5] == {"role": "tool", "tool_call_id": "a", "content": "{}"}


def tool_chunk(index, call_id=None, name=None, arguments=None):
    function = SimpleNamespace(name=name, arguments=arguments)
    call = SimpleNamespace(index=index, id=call_id, function=function)
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[call]))])


def test_streamed_tool_call_fragments_are_assembled():
    class ToolStream(Stream):
        async def items(self):
            yield tool_chunk(0, "id-1", "get_weather", '{"loca')
            yield tool_chunk(0, None, None, 'tion": "Mysuru"}')

    events = asyncio.run(collect(Client([ToolStream([])])))
    assert events == [[llm.ToolCallRequest("id-1", "get_weather", '{"location": "Mysuru"}')]]


def test_short_rate_limit_wait_is_retried(monkeypatch):
    import httpx
    from groq import RateLimitError

    response = httpx.Response(429, headers={"retry-after": "0"},
                              request=httpx.Request("POST", "https://api.groq.com"))
    limited = RateLimitError("limit", response=response, body=None)

    class LimitedOnce(Client):
        async def create(self, **request):
            if not self.created:
                self.created.append(None)
                raise limited
            return await super().create(**request)

    client = LimitedOnce([Stream(["Hi"])])
    assert asyncio.run(collect(client)) == ["Hi"]


def test_long_rate_limit_wait_fails_fast():
    import httpx
    from groq import RateLimitError

    response = httpx.Response(429, headers={"retry-after": "30"},
                              request=httpx.Request("POST", "https://api.groq.com"))

    class Limited(Client):
        async def create(self, **request):
            raise RateLimitError("limit", response=response, body=None)

    with pytest.raises(SpeechError) as error:
        asyncio.run(collect(Limited([])))
    assert error.value.status_code == 429


def turn(role, text, **extra):
    return {"role": role, "parts": [{"text": text}], "status": "completed", **extra}


def test_earlier_tool_exchanges_are_dropped_and_history_is_windowed(monkeypatch):
    monkeypatch.setattr(llm, "HISTORY_MESSAGES", 4)
    history = [turn("user", "u1"), turn("model", "a1"),
               turn("user", "weather?"),
               turn("model", "", tool_calls=[{"id": "old", "name": "get_weather", "arguments": "{}"}]),
               turn("tool", "{big json}", tool_call_id="old"),
               turn("model", "It is sunny."),
               turn("user", "time?"),
               turn("model", "", tool_calls=[{"id": "new", "name": "get_time", "arguments": "{}"}]),
               turn("tool", "{now}", tool_call_id="new")]
    messages = llm.to_chat_messages(history)
    assert [(m["role"], m.get("content")) for m in messages[1:]] == [
        ("user", "u1"), ("assistant", "a1"), ("user", "weather?"), ("assistant", "It is sunny."),
        ("user", "time?"), ("assistant", ""), ("tool", "{now}")]
    # Earlier filler text attached to a tool call is dropped with the call.
    history[3] = turn("model", "Let me check that.", tool_calls=history[3]["tool_calls"])
    assert "Let me check that." not in [m.get("content") for m in llm.to_chat_messages(history)]
    assert messages[-2]["tool_calls"][0]["id"] == "new"
    monkeypatch.setattr(llm, "HISTORY_MESSAGES", 2)
    assert [m.get("content") for m in llm.to_chat_messages(history)[1:3]] == ["weather?", "It is sunny."]


def test_system_prompt_carries_local_time_in_twelve_hour_form():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    prompt = llm.system_prompt(datetime(2026, 10, 12, 20, 9, tzinfo=ZoneInfo("Asia/Kolkata")))
    assert prompt.endswith("Monday, 12 October 2026, 8:09 PM (Asia/Kolkata).")
