"""First-token deadline and single retry for the Groq LLM stream."""
import asyncio
from types import SimpleNamespace

import pytest

from app import llm
from app.speech import SpeechError


def chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])


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
    return [delta async for delta in llm.stream_reply("hi", [], client=client, **options)]


def test_stalled_first_token_is_retried_once():
    client = Client([Stream(["never"], stall=True), Stream(["Hello", " there"])])
    assert asyncio.run(collect(client, first_token_s=0.05)) == ["Hello", " there"]
    assert [stream.closed for stream in client.created] == [True, True]


def test_second_stall_fails_with_a_safe_error():
    client = Client([Stream([], stall=True), Stream([], stall=True)])
    with pytest.raises(SpeechError) as error:
        asyncio.run(collect(client, first_token_s=0.05))
    assert error.value.status_code == 504


def test_history_marks_cut_off_replies():
    history = [{"role": "user", "parts": [{"text": "hi"}], "status": "completed"},
               {"role": "model", "parts": [{"text": "Well, it..."}], "status": "interrupted"}]
    messages = llm.to_chat_messages("go on", history)
    assert messages[2]["role"] == "assistant"
    assert "cut off" in messages[2]["content"]


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
