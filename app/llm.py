import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


async def generate_reply(message: str, history: list[dict]) -> str:
    """Send previous turns and the new message without modifying history."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Set GEMINI_API_KEY in your environment or local .env file.")
    async with genai.Client(
        api_key=api_key, http_options=types.HttpOptions(timeout=30000)
    ).aio as client:
        response = await client.models.generate_content(
            model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
            contents=history + [{"role": "user", "parts": [{"text": message}]}],
            config=types.GenerateContentConfig(max_output_tokens=1024),
        )
    if not response.text:
        raise RuntimeError("Gemini returned no text. Try another message.")
    return response.text
