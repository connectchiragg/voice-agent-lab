import os
from collections.abc import AsyncIterator
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


async def stream_reply(message: str, history: list[dict]) -> AsyncIterator[str]:
    """Yield text chunks, retaining interruption context from earlier turns."""
    api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        raise RuntimeError("Set GEMINI_API_KEY in your environment or local .env file.")
    contents = []
    for turn in history:
        text = turn["parts"][0]["text"]
        if turn.get("status") == "interrupted":
            text += "\n[This assistant response was interrupted before completion.]"
        contents.append({"role": turn["role"], "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": message}]})
    async with genai.Client(
        api_key=api_key, http_options=types.HttpOptions(timeout=30000)
    ).aio as client:
        stream = await client.models.generate_content_stream(
            model=os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite"),
            contents=contents,
            config=types.GenerateContentConfig(
                max_output_tokens=1024,
                system_instruction="You are a voice assistant. Reply in English, conversationally, "
                "in at most 200 characters. Avoid Markdown. Ask to continue if more detail is needed.",
            ),
        )
        has_text = False
        finish_reason = None
        async for chunk in stream:
            if chunk.candidates and chunk.candidates[0].finish_reason:
                finish_reason = chunk.candidates[0].finish_reason
            if chunk.text:
                has_text = True
                yield chunk.text
        if not has_text or finish_reason != types.FinishReason.STOP:
            raise RuntimeError("Gemini did not finish the response.")
