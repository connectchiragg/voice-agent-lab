"""Hedged Groq LLM stream: a fallback model after a short first-token deadline, never a retry."""
import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest
from groq import RateLimitError

from app import groq_api, llm
from app.groq_api import ProviderError
from app.llm import ModelUsed


def chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=None))])


class Stream:
    def __init__(self, texts, delay=0.0):
        self.texts, self.delay, self.closed = texts, delay, False

    def __aiter__(self):
        return self.items()

    async def items(self):
        await asyncio.sleep(self.delay)
        for text in self.texts:
            yield chunk(text)

    async def close(self):
        self.closed = True


class Client:
    """Fake Groq client: each model name has its own queue of streams (or errors)."""

    def __init__(self, **streams):
        self.streams, self.created, self.models = streams, [], []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def with_options(self, **options):
        return self

    async def create(self, **request):
        self.models.append(request["model"])
        stream = self.streams[request["model"]].pop(0)
        if isinstance(stream, Exception):
            raise stream
        self.created.append(stream)
        return stream


def rate_limited():
    response = httpx.Response(429, headers={"retry-after": "0"},
                              request=httpx.Request("POST", "https://api.groq.com"))
    return RateLimitError("limit", response=response, body=None)


@pytest.fixture(autouse=True)
def models(monkeypatch):
    monkeypatch.setenv("GROQ_LLM_MODEL", "big")
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "small")
    monkeypatch.setenv("FIRST_TOKEN_DEADLINE_MS", "50")
    monkeypatch.setattr(llm, "hedge_stats", llm.HedgeStats())
    monkeypatch.setattr(groq_api, "last_rate_limited", None)
    monkeypatch.setattr(groq_api, "recent_requests", __import__("collections").deque())
    monkeypatch.setattr(llm, "recent_first_tokens_ms", __import__("collections").deque(maxlen=20))


async def collect(client, **options):
    messages = [{"role": "user", "content": "hi"}]
    return [delta async for delta in llm.stream_model(messages, client=client, **options)]


def test_prompt_primary_is_not_hedged():
    client = Client(big=[Stream(["Hello", " there"])])
    assert asyncio.run(collect(client)) == [ModelUsed("big"), "Hello", " there"]
    assert client.models == ["big"]
    assert llm.hedge_stats == llm.HedgeStats(requests=1, hedged=0, fallback_won=0)


def test_stalled_primary_is_hedged_and_the_first_to_start_wins():
    client = Client(big=[Stream(["late"], delay=3600)], small=[Stream(["Hello"])])
    assert asyncio.run(collect(client)) == [ModelUsed("small"), "Hello"]
    assert client.models == ["big", "small"]  # One fallback request, no retry of "big".
    assert [stream.closed for stream in client.created] == [True, True]  # Loser closed at once.
    assert llm.hedge_stats == llm.HedgeStats(requests=1, hedged=1, fallback_won=1)


def test_slow_primary_still_wins_if_it_starts_before_the_fallback():
    client = Client(big=[Stream(["Hi"], delay=0.1)], small=[Stream(["no"], delay=3600)])
    assert asyncio.run(collect(client)) == [ModelUsed("big"), "Hi"]
    assert client.models == ["big", "small"]
    assert client.created[1].closed  # The fallback was abandoned.
    assert llm.hedge_stats.hedged == 1 and llm.hedge_stats.fallback_won == 0


def test_tool_call_fragment_counts_as_the_first_token():
    class ToolStream(Stream):
        async def items(self):
            yield tool_chunk(0, "id-1", "get_time", '{"pla')
            await asyncio.sleep(0.2)  # Past the deadline, but the model has started.
            yield tool_chunk(0, None, None, 'ce": "London"}')

    client = Client(big=[ToolStream([])])
    events = asyncio.run(collect(client))
    assert events == [ModelUsed("big"), [llm.ToolCallRequest("id-1", "get_time", '{"place": "London"}')]]
    assert client.models == ["big"]


def test_rate_limited_primary_falls_back_without_waiting(monkeypatch):
    monkeypatch.setenv("FIRST_TOKEN_DEADLINE_MS", "10000")
    client = Client(big=[rate_limited()], small=[Stream(["Hi"])])
    started = time.perf_counter()
    assert asyncio.run(collect(client)) == [ModelUsed("small"), "Hi"]
    assert time.perf_counter() - started < 1
    assert groq_api.near_rate_limit()  # Optional speculative calls back off.


def test_without_a_fallback_a_rate_limit_fails_fast(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "")
    client = Client(big=[rate_limited()])
    with pytest.raises(ProviderError) as error:
        asyncio.run(collect(client))
    assert error.value.status_code == 429
    assert client.models == ["big"]


def test_without_a_fallback_a_stall_gives_up_with_a_safe_error(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "")
    monkeypatch.setattr(llm, "GIVE_UP_S", 0.1)
    client = Client(big=[Stream([], delay=3600)])
    with pytest.raises(ProviderError) as error:
        asyncio.run(collect(client))
    assert error.value.status_code == 504
    assert client.models == ["big"] and client.created[0].closed


def test_both_stalled_give_up_and_close_both(monkeypatch):
    monkeypatch.setattr(llm, "GIVE_UP_S", 0.2)
    client = Client(big=[Stream([], delay=3600)], small=[Stream([], delay=3600)])
    with pytest.raises(ProviderError) as error:
        asyncio.run(collect(client))
    assert error.value.status_code == 504
    assert [stream.closed for stream in client.created] == [True, True]


def test_failed_fallback_leaves_the_primary_racing():
    client = Client(big=[Stream(["Hi"], delay=0.15)], small=[rate_limited()])
    assert asyncio.run(collect(client)) == [ModelUsed("big"), "Hi"]
    assert groq_api.last_rate_limited is None  # Only the primary's bucket guards speculation.


def test_cancelling_the_caller_closes_both_streams():
    client = Client(big=[Stream([], delay=3600)], small=[Stream([], delay=3600)])

    async def scenario():
        task = asyncio.create_task(collect(client))
        await asyncio.sleep(0.1)  # Past the deadline: both requests are open.
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert [stream.closed for stream in client.created] == [True, True]


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
    assert messages[4]["role"] == "system"  # The local time follows the latest user message.
    assert [call["id"] for call in messages[5]["tool_calls"]] == ["a"]
    assert messages[6] == {"role": "tool", "tool_call_id": "a", "content": "{}"}


def tool_chunk(index, call_id=None, name=None, arguments=None):
    function = SimpleNamespace(name=name, arguments=arguments)
    call = SimpleNamespace(index=index, id=call_id, function=function)
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[call]))])


def test_streamed_tool_call_fragments_are_assembled():
    class ToolStream(Stream):
        async def items(self):
            yield tool_chunk(0, "id-1", "get_weather", '{"loca')
            yield tool_chunk(0, None, None, 'tion": "Mysuru"}')

    events = asyncio.run(collect(Client(big=[ToolStream([])])))
    assert events == [ModelUsed("big"), [llm.ToolCallRequest("id-1", "get_weather", '{"location": "Mysuru"}')]]


def turn(role, text, **extra):
    return {"role": role, "parts": [{"text": text}], "status": "completed", **extra}


def test_earlier_tool_exchanges_are_dropped_and_history_is_append_only(monkeypatch):
    history = [turn("user", "u1"), turn("model", "a1"),
               turn("user", "weather?"),
               turn("model", "", tool_calls=[{"id": "old", "name": "get_weather", "arguments": "{}"}]),
               turn("tool", "{big json}", tool_call_id="old"),
               turn("model", "It is sunny."),
               turn("user", "time?"),
               turn("model", "", tool_calls=[{"id": "new", "name": "get_time", "arguments": "{}"}]),
               turn("tool", "{now}", tool_call_id="new")]
    messages = llm.to_chat_messages(history)
    assert [(m["role"], m.get("content")) for m in messages[1:] if m["role"] != "system"] == [
        ("user", "u1"), ("assistant", "a1"), ("user", "weather?"), ("assistant", "It is sunny."),
        ("user", "time?"), ("assistant", ""), ("tool", "{now}")]
    # Earlier filler text attached to a tool call is dropped with the call.
    history[3] = turn("model", "Let me check that.", tool_calls=history[3]["tool_calls"])
    assert "Let me check that." not in [m.get("content") for m in llm.to_chat_messages(history)]
    assert messages[-2]["tool_calls"][0]["id"] == "new"


def test_a_very_long_call_drops_its_oldest_messages_in_chunks(monkeypatch):
    monkeypatch.setattr(llm, "MAX_CALL_MESSAGES", 6)
    monkeypatch.setattr(llm, "TRIM_CHUNK", 4)
    history = [turn("user" if n % 2 == 0 else "model", f"m{n}") for n in range(8)] + [turn("user", "now")]
    contents = [m["content"] for m in llm.to_chat_messages(history)[1:] if m["role"] != "system"]
    assert contents == ["m4", "m5", "m6", "m7", "now"]  # 8 > 6: one chunk of 4 dropped.
    history = history[:-1] + [turn("model", "m8"), turn("user", "next")]
    contents = [m["content"] for m in llm.to_chat_messages(history)[1:] if m["role"] != "system"]
    assert contents[:2] == ["m4", "m5"]  # Same start: the cached prefix still holds.


def test_the_prefix_is_byte_stable_across_turns_and_the_time_comes_last():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    zone = ZoneInfo("Asia/Kolkata")
    first = [turn("user", "hi")]
    second = first + [turn("model", "Hello."), turn("user", "what's on today?")]
    one = llm.to_chat_messages(first, "Owner context.", datetime(2026, 10, 16, 9, 0, tzinfo=zone))
    two = llm.to_chat_messages(second, "Owner context.", datetime(2026, 10, 16, 9, 7, tzinfo=zone))
    # Everything up to and including the previous user message is identical.
    assert one[:2] == two[:2]
    assert "local time" not in one[0]["content"].split("last system message")[1]
    assert two[-1] == {"role": "system", "content": "The user's local time is Friday, 16 October "
                                                    "2026, 9:07 AM (Asia/Kolkata)."}
    assert two[-2] == {"role": "user", "content": "what's on today?"}


def test_tools_are_identical_across_turns_and_for_speculation():
    from app import tools
    assert tools.schemas_for(None) is tools.schemas_for(None)
    assert tools.schemas_for("tel:+15550100099") == tools.SCHEMAS


def test_cached_tokens_are_read_from_groq_usage():
    usage = SimpleNamespace(prompt_tokens=2000, prompt_tokens_details=SimpleNamespace(cached_tokens=1536))
    assert llm.cached_tokens(usage) == 1536
    assert llm.cached_tokens(SimpleNamespace(prompt_tokens=10)) == 0
    assert llm.cached_tokens(SimpleNamespace(prompt_tokens_details={"cached_tokens": 7})) == 7


def test_time_note_in_twelve_hour_form():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    note = llm.time_note(datetime(2026, 10, 12, 20, 9, tzinfo=ZoneInfo("Asia/Kolkata")))
    assert note.endswith("Monday, 12 October 2026, 8:09 PM (Asia/Kolkata).")

def test_a_fetched_brief_stays_in_the_conversation_append_only():
    history = [turn("user", "brief me"),
               turn("model", "", tool_calls=[{"id": "b1", "name": "get_briefing", "arguments": "{}"}]),
               turn("tool", '{"ready": ["brief"]}', tool_call_id="b1"),
               turn("model", "Here is your brief."),
               turn("user", "what was the pay range?")]
    first = llm.to_chat_messages(history[:3])
    later = llm.to_chat_messages(history)
    # The brief's call and result are still there, at the same position.
    assert later[:3] == first[:2] + [later[2]] and later[2]["role"] == "assistant"
    assert later[3] == {"role": "tool", "tool_call_id": "b1", "content": '{"ready": ["brief"]}'}
    assert first[1:2] == later[1:2]
    # Other tools' results still drop after their turn.
    history[1]["tool_calls"][0]["name"] = "get_weather"
    assert all(m["role"] != "tool" for m in llm.to_chat_messages(history))


def test_the_hedge_deadline_follows_the_primarys_recent_first_tokens(monkeypatch):
    monkeypatch.setenv("FIRST_TOKEN_DEADLINE_MS", "900")
    assert llm.first_token_deadline_s() == 0.9  # Too few samples: the floor.
    llm.recent_first_tokens_ms.extend([600, 650, 700, 750, 800, 900, 930, 1000])
    assert llm.first_token_deadline_s() == pytest.approx((900 + llm.HEDGE_MARGIN_MS) / 1000)  # p75 of eight.
    llm.recent_first_tokens_ms.extend([5000] * 20)
    assert llm.first_token_deadline_s() == llm.HEDGE_MAX_MS / 1000


def test_a_winning_primary_teaches_the_deadline():
    asyncio.run(collect(Client(big=[Stream(["Hello"])])))
    assert len(llm.recent_first_tokens_ms) == 1


def test_no_slow_hedge_near_the_rate_limit(monkeypatch):
    monkeypatch.setattr(groq_api, "last_rate_limited", time.monotonic())
    client = Client(big=[Stream(["Hi"], delay=0.2)], small=[Stream(["no"])])
    assert asyncio.run(collect(client)) == [ModelUsed("big"), "Hi"]
    assert client.models == ["big"]  # Waited for the primary: the 20b's quota is kept.
